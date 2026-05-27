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
    ACTION_NOTIFY_COMPLETED,
    ACTION_NOTIFY_STATUS_UPDATE,
    ExtractionNotRelevantError,
    InvalidEventPayloadError,
    MissingTaxpayerError,
    UnsupportedEventTypeError,
    fetch_extraction_for_event,
    get_taxpayer_rfc,
)

from redis import Redis
from redis.exceptions import LockNotOwnedError
from sqlalchemy import select

from app.aitax.client import (
    AitaxClient,
    AitaxDefinitiveError,
    AitaxRetryableError,
)
from app.aitax_models import Company
from app.config import settings
from app.sync.sync import sync_all


logger = logging.getLogger(__name__)

# Cliente Redis compartido por todos los tasks del worker.
# Redis es thread-safe — no hace falta instanciar uno por task.
_redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)

# Tiempo máximo que puede durar un sync antes de que el lock expire automáticamente.
# 8 horas — holgura suficiente para las empresas más grandes.
_SYNC_LOCK_TIMEOUT = 28800


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
    autoretry_for=(SyntageRetryableError, AitaxRetryableError, ConnectionError),
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
                result = fetch_extraction_for_event(
                    client=client,
                    event_type=event.event_type,
                    payload=event.payload,
                )
            extraction = result.extraction
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
        rfc = get_taxpayer_rfc(extraction)
        extraction_id = extraction.get("id")
        extractor = extraction.get("extractor")
        extraction_status = extraction.get("status")

        logger.info(
            "Extracción lista para notificar: event_id=%s, rfc=%s, "
            "extraction_id=%s, extractor=%s, status=%s, action=%s",
            event_uuid,
            rfc,
            extraction_id,
            extractor,
            extraction_status,
            result.action,
        )

        # ─── Idempotencia por extraction_id ───────────────────────────
        if extraction_id:
            already_processed = (
                db.query(SyntageWebhookEvent)
                .filter(
                    SyntageWebhookEvent.id != event.id,
                    SyntageWebhookEvent.status == EventStatus.PROCESSED.value,
                    # En el payload de Syntage, el extraction_id vive en:
                    # payload -> data -> object -> id
                    SyntageWebhookEvent.payload["data"]["object"]["id"].as_string()
                        == str(extraction_id),
                )
                .first()
            )

            if already_processed:
                logger.info(
                    "Extracción %s ya fue procesada por evento %s. "
                    "Saltando este (event_id=%s) para evitar sync duplicado.",
                    extraction_id, already_processed.id, event_uuid,
                )
                event.status = EventStatus.SKIPPED.value
                event.last_error = (
                    f"Extracción {extraction_id} ya procesada "
                    f"por evento {already_processed.id}"
                )
                event.processed_at = datetime.now(timezone.utc)
                db.commit()
                return {
                    "status": "skipped",
                    "event_id": event_internal_id,
                    "reason": "extraction_already_processed",
                    "previous_event_id": str(already_processed.id),
                }

        # ─── Sincronización / notificación ───────────────────────────
        if result.action == ACTION_NOTIFY_COMPLETED:
            # Sync directo: el microservicio escribe en la DB de AITAX sin
            # pasar por el endpoint interno de Django.
            company = db.execute(
                select(Company).where(Company.rfc == rfc)
            ).scalar_one_or_none()

            if company is None:
                logger.error(
                    "Empresa RFC=%s no encontrada en DB. Marcando evento como fallido.",
                    rfc,
                )
                _mark_failed(db, event, f"Company RFC={rfc} no existe en la DB de AITAX")
                return {
                    "status": "failed",
                    "event_id": event_internal_id,
                    "reason": f"company_not_found: {rfc}",
                }

            # ─── Lock por RFC ──────────────────────────────────────────────
            # Evita que dos workers corran sync_all en paralelo para la misma
            # empresa. Si el lock ya lo tiene otro worker, este evento se
            # salta — el sync ya está en curso.
            _lock = _redis.lock(
                f"aitax:sync:{rfc}",
                timeout=_SYNC_LOCK_TIMEOUT,
                blocking_timeout=0,
            )
            if not _lock.acquire(blocking=False):
                logger.info(
                    "Sync de RFC=%s ya en curso en otro worker. "
                    "Marcando SKIPPED (event_id=%s).",
                    rfc, event_uuid,
                )
                event.status = EventStatus.SKIPPED.value
                event.last_error = f"Sync de RFC={rfc} ya en curso"
                event.processed_at = datetime.now(timezone.utc)
                db.commit()
                return {
                    "status": "skipped",
                    "event_id": event_internal_id,
                    "reason": "sync_already_running",
                    "rfc": rfc,
                }

            try:
                # Notifica a Django que el sync está por empezar → SYNCING
                try:
                    with AitaxClient() as aitax:
                        aitax.notify_sync_started(rfc=rfc, extraction_id=extraction_id)
                except (AitaxDefinitiveError, AitaxRetryableError) as exc:
                    logger.warning(
                        "No se pudo notificar sync-started a AITAX (RFC=%s): %s. Continuando sync.",
                        rfc, exc,
                    )

                logger.info(
                    "Iniciando sync_all para RFC=%s (company_id=%s, extraction_id=%s)",
                    rfc, company.id, extraction_id,
                )

                total_invoices = 0
                total_concepts = 0
                total_payments = 0

                def on_invoices_progress(count):
                    nonlocal total_invoices
                    total_invoices += count
                    try:
                        with AitaxClient() as aitax:
                            aitax.notify_sync_progress(
                                rfc=rfc,
                                extraction_id=str(extraction_id),
                                invoices=total_invoices,
                                concepts=total_concepts,
                                payments=total_payments,
                            )
                    except Exception as exc:
                        logger.warning("sync_progress notify falló (invoices): %s", exc)

                def on_concepts_progress(count):
                    nonlocal total_concepts
                    total_concepts += count
                    try:
                        with AitaxClient() as aitax:
                            aitax.notify_sync_progress(
                                rfc=rfc,
                                extraction_id=str(extraction_id),
                                invoices=total_invoices,
                                concepts=total_concepts,
                                payments=total_payments,
                            )
                    except Exception as exc:
                        logger.warning("sync_progress notify falló (concepts): %s", exc)

                def on_payments_progress(count):
                    nonlocal total_payments
                    total_payments += count
                    try:
                        with AitaxClient() as aitax:
                            aitax.notify_sync_progress(
                                rfc=rfc,
                                extraction_id=str(extraction_id),
                                invoices=total_invoices,
                                concepts=total_concepts,
                                payments=total_payments,
                            )
                    except Exception as exc:
                        logger.warning("sync_progress notify falló (payments): %s", exc)

                aitax_result = sync_all(
                    company,
                    invoices_callback=on_invoices_progress,
                    concepts_callback=on_concepts_progress,
                    payments_callback=on_payments_progress,
                )
                logger.info("sync_all completado para RFC=%s: %s", rfc, aitax_result)

                # Notifica a Django para que actualice ExtractionSyncStatus y mande el email.
                try:
                    with AitaxClient() as aitax:
                        aitax.notify_extraction_completed(rfc=rfc, extraction_id=extraction_id)
                    logger.info("AITAX notificado de sync completado para RFC=%s", rfc)
                except AitaxDefinitiveError as exc:
                    logger.warning(
                        "No se pudo notificar a AITAX tras sync (RFC=%s, status=%s): %s. "
                        "El sync ya se hizo — se continúa como procesado.",
                        rfc, exc.status_code, exc,
                    )
                except AitaxRetryableError as exc:
                    logger.warning(
                        "Error transitorio al notificar a AITAX tras sync (RFC=%s): %s. "
                        "El sync ya se hizo — se continúa como procesado.",
                        rfc, exc,
                    )

            finally:
                try:
                    _lock.release()
                except LockNotOwnedError:
                    # El lock expiró durante el sync (empresa que tardó más de 8h).
                    logger.warning("Lock de sync para RFC=%s ya había expirado.", rfc)
                except Exception as exc:
                    logger.warning("Error liberando lock de sync para RFC=%s: %s", rfc, exc)

        elif result.action == ACTION_NOTIFY_STATUS_UPDATE:
            try:
                with AitaxClient() as aitax:
                    aitax_result = aitax.notify_extraction_status_update(
                        extraction_id=extraction_id,
                        extractor=extractor,
                        status=extraction_status,
                        rfc=rfc,
                    )
            except AitaxDefinitiveError as exc:
                logger.error(
                    "AITAX devolvió error definitivo en evento %s (status=%s): %s",
                    event_uuid, exc.status_code, exc,
                )
                _mark_failed(db, event, f"AITAX {exc.status_code}: {exc}")
                return {"status": "failed", "event_id": event_internal_id, "reason": str(exc)}
            except AitaxRetryableError:
                raise

        else:
            raise ValueError(f"Acción desconocida: {result.action!r}")

        logger.info(
            "Procesamiento exitoso para RFC=%s (action=%s)",
            rfc,
            result.action,
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
            "extraction_id": extraction_id,
            "extractor": extractor,
            "extraction_status": extraction_status,
            "rfc": rfc,
            "action": result.action,
            "aitax_result": aitax_result,
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