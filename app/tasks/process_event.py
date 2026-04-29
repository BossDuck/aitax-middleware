import logging
from datetime import datetime, timezone
from uuid import UUID

from celery import Task
from celery.exceptions import Ignore, MaxRetriesExceededError

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import EventStatus, SyntageWebhookEvent
from app.syntage.client import (
    SyntageClient,
    SyntageDefinitiveError,
    SyntageRetryableError,
)
from app.syntage.event_router import (
    ExtractionNotRelevantError,
    InvalidEventPayloadError,
    MissingTaxpayerError,
    UnsupportedEventTypeError,
    fetch_extraction_for_event,
)


logger = logging.getLogger(__name__)


# ─── Configuración de reintentos ──────────────────────────────────────
# Hasta 3 reintentos del worker (independientes de los retries de tenacity
# dentro del cliente Syntage). El backoff arranca en 60s y crece exponencial.
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 60  # 1 min, 2 min, 4 min entre reintentos


@celery_app.task(
    bind=True,                           # Da acceso a `self` (la Task).
    name="app.tasks.process_webhook_event",
    max_retries=MAX_RETRIES,
    acks_late=True,                      # Solo ack tras éxito o failure final.
    autoretry_for=(SyntageRetryableError, ConnectionError),
    retry_backoff=RETRY_BACKOFF_BASE,    # Backoff exponencial: 60s, 120s, 240s...
    retry_backoff_max=600,               # Tope de 10 min entre reintentos.
    retry_jitter=True,                   # Aleatoriedad para evitar "thundering herd".
)
def process_webhook_event(self: Task, event_internal_id: str) -> dict:
    """
    Procesa un webhook de Syntage previamente persistido en DB.

    Args:
        event_internal_id: UUID (como string) de SyntageWebhookEvent.id.
                           NO el syntage_event_id, sino nuestro id interno.

    Returns:
        Dict con resumen del procesamiento (útil para logs y debug).
    """
    event_uuid = UUID(event_internal_id)
    logger.info("Procesando evento %s (intento %s)", event_uuid, self.request.retries + 1)

    # Cada tarea abre su propia sesión de DB.
    # No reutilizamos sesiones porque el worker es multiproceso.
    db = SessionLocal()

    try:
        event = db.get(SyntageWebhookEvent, event_uuid)
        if event is None:
            logger.error("Evento no encontrado en DB: %s", event_uuid)
            return {"status": "not_found", "event_id": event_internal_id}

        # ─── Si ya fue procesado antes, no reprocesar ──────────────────
        # Esto puede pasar si Celery lo encola dos veces (ej. retry duplicado).
        if event.status == EventStatus.PROCESSED.value:
            logger.info("Evento %s ya estaba procesado, saltando", event_uuid)
            return {"status": "already_processed", "event_id": event_internal_id}

        # ─── Marcar como `processing` e incrementar attempts ───────────
        event.status = EventStatus.PROCESSING.value
        event.attempts = event.attempts + 1
        event.last_error = None
        db.commit()

        # ─── Llamar a Syntage ──────────────────────────────────────────
        try:
            with SyntageClient() as client:
                extraction = fetch_extraction_for_event(
                    client=client,
                    event_type=event.event_type,
                    payload=event.payload,
                )
        except UnsupportedEventTypeError as exc:
            # Evento que no nos corresponde procesar.
            logger.info("Evento no soportado %s: %s", event_uuid, exc)
            event.status = EventStatus.SKIPPED.value
            event.last_error = str(exc)
            event.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"status": "skipped", "event_id": event_internal_id, "reason": str(exc)}

        except ExtractionNotRelevantError as exc:
            # Extracción que no pasó filtros (status != finished o extractor != invoice).
            # Es un caso esperado, no un error.
            logger.info("Extracción no relevante %s: %s", event_uuid, exc)
            event.status = EventStatus.SKIPPED.value
            event.last_error = str(exc)
            event.processed_at = datetime.now(timezone.utc)
            db.commit()
            return {"status": "skipped", "event_id": event_internal_id, "reason": str(exc)}

        except InvalidEventPayloadError as exc:
            logger.error("Payload inválido en evento %s: %s", event_uuid, exc)
            _mark_failed(db, event, str(exc))
            return {"status": "failed", "event_id": event_internal_id, "reason": str(exc)}

        except MissingTaxpayerError as exc:
            logger.error("Extracción sin taxpayer %s: %s", event_uuid, exc)
            _mark_failed(db, event, str(exc))
            return {"status": "failed", "event_id": event_internal_id, "reason": str(exc)}

        except SyntageDefinitiveError as exc:
            logger.error(
                "Syntage devolvió error definitivo en evento %s (status=%s): %s",
                event_uuid, exc.status_code, exc,
            )
            _mark_failed(db, event, f"Syntage {exc.status_code}: {exc}")
            return {"status": "failed", "event_id": event_internal_id, "reason": str(exc)}

        except SyntageRetryableError:
            raise

        # ─── Aquí ya tenemos `extraction` validada ─────────────────────
        from app.syntage.event_router import get_taxpayer_rfc
        rfc = get_taxpayer_rfc(extraction)
        logger.info(
            "Extracción lista para sync: event_id=%s, rfc=%s, extraction_id=%s, "
            "createdDataPoints=%s, updatedDataPoints=%s",
            event_uuid,
            rfc,
            extraction.get("id"),
            extraction.get("createdDataPoints"),
            extraction.get("updatedDataPoints"),
        )

        # ─── Marcar como procesado ─────────────────────────────────────
        event.status = EventStatus.PROCESSED.value
        event.processed_at = datetime.now(timezone.utc)
        event.last_error = None
        db.commit()

        return {
            "status": "processed",
            "event_id": event_internal_id,
            "event_type": event.event_type,
            "extraction_id": extraction.get("id"),
            "rfc": rfc,
        }

    except MaxRetriesExceededError:
        # Celery agotó los reintentos. Marcamos failed permanente.
        # IMPORTANTE: re-leemos el evento porque la transacción anterior puede
        # haber sido invalidada.
        db.rollback()
        event = db.get(SyntageWebhookEvent, event_uuid)
        if event:
            _mark_failed(db, event, "Max retries exceeded")
        raise

    except SyntageRetryableError as exc:
        # autoretry_for lo va a capturar, pero antes registramos el error en DB.
        db.rollback()
        event = db.get(SyntageWebhookEvent, event_uuid)
        if event:
            event.status = EventStatus.PENDING.value  # Volverá a pending mientras reintenta.
            event.last_error = str(exc)
            db.commit()
        raise

    except Exception as exc:
        # Cualquier otra excepción inesperada: log + reintento + DB.
        logger.exception("Error procesando evento %s: %s", event_uuid, exc)
        db.rollback()
        event = db.get(SyntageWebhookEvent, event_uuid)
        if event:
            event.last_error = str(exc)
            event.status = EventStatus.PENDING.value
            db.commit()
        # Reintentar manualmente con backoff.
        try:
            raise self.retry(exc=exc, countdown=RETRY_BACKOFF_BASE * (2 ** self.request.retries))
        except MaxRetriesExceededError:
            event = db.get(SyntageWebhookEvent, event_uuid)
            if event:
                _mark_failed(db, event, f"Max retries exceeded: {exc}")
            raise

    finally:
        db.close()


def _mark_failed(db, event: SyntageWebhookEvent, error_message: str) -> None:
    """Helper para marcar un evento como `failed`."""
    event.status = EventStatus.FAILED.value
    event.last_error = error_message[:1000]  # Truncamos por si el error es enorme.
    event.processed_at = datetime.now(timezone.utc)
    db.commit()