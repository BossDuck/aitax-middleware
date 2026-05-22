"""
Despacha eventos de webhook al método correcto del cliente Syntage.

Diseño:
- Solo procesamos eventos `extraction.created` y `extraction.updated`.
- Hacemos GET a /extractions/{id} para obtener detalles completos.
- Filtramos por extractor y por status terminal, con dos rutas:

    Ruta A — invoice:
        extractor == "invoice" AND status == "finished"
        → ExtractionResult(action=ACTION_NOTIFY_COMPLETED)

    Ruta B — documentos fiscales:
        extractor IN {"tax_compliance", "tax_status"}
        AND status IN {"finished", "error", "stopped"}
        → ExtractionResult(action=ACTION_NOTIFY_STATUS_UPDATE)

- Si no cumple filtros → ExtractionNotRelevantError (evento → SKIPPED).
- Eventos de otro tipo (invoice.*, payment.*, etc.) → UnsupportedEventTypeError.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from app.syntage.client import SyntageClient


logger = logging.getLogger(__name__)


# ─── Configuración de eventos soportados ─────────────────────────────

SUPPORTED_EVENTS = frozenset({
    "extraction.created",
    "extraction.updated",
})

# ─── Ruta A: facturas ─────────────────────────────────────────────────

INVOICE_EXTRACTOR = "invoice"
INVOICE_RELEVANT_STATUS = "finished"

# ─── Ruta B: documentos fiscales ─────────────────────────────────────

TAX_DOC_EXTRACTORS = frozenset({"tax_compliance", "tax_status"})
# Solo notificamos estados terminales; running/pending son ruido.
TAX_DOC_RELEVANT_STATUSES = frozenset({"finished", "error", "stopped"})

# ─── Acciones que la tarea debe ejecutar ─────────────────────────────

ACTION_NOTIFY_COMPLETED = "notify_completed"        # → POST extraction-completed
ACTION_NOTIFY_STATUS_UPDATE = "notify_status_update"  # → POST extraction-status-update


# ─── Resultado del router ─────────────────────────────────────────────

@dataclass
class ExtractionResult:
    """
    Resultado de fetch_extraction_for_event.

    extraction: dict completo devuelto por Syntage (ya validado).
    action:     constante ACTION_* que indica qué endpoint de AITAX llamar.
    """
    extraction: dict
    action: str


# ─── Excepciones ──────────────────────────────────────────────────────

class UnsupportedEventTypeError(ValueError):
    """El tipo de evento no es uno que procesemos (ej. credential.updated)."""
    pass


class InvalidEventPayloadError(ValueError):
    """El payload no permite extraer el ID del recurso."""
    pass


class ExtractionNotRelevantError(Exception):
    """
    La extracción existe pero no nos interesa (extractor desconocido,
    status no terminal, etc.). NO es un error técnico, es filtrado normal.
    El worker la marcará como SKIPPED.
    """
    pass


class MissingTaxpayerError(Exception):
    """La extracción no trae taxpayer.id (RFC). Caso muy raro pero defensivo."""
    pass


# ─── Lógica del router ────────────────────────────────────────────────

def extract_resource_id(payload: dict, event_type: str) -> UUID:
    """
    Extrae el UUID del recurso afectado desde el campo `resource` del payload.

    Para eventos extraction.*, resource viene como "/extractions/<uuid>".
    """
    resource_iri = payload.get("resource")

    if not resource_iri or not isinstance(resource_iri, str):
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' no trae campo 'resource' válido. "
            f"Payload keys: {list(payload.keys())}"
        )

    last_segment = resource_iri.rstrip("/").rsplit("/", 1)[-1]

    if not last_segment:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'resource' vacío tras parsear: "
            f"'{resource_iri}'"
        )

    try:
        return UUID(last_segment)
    except ValueError as exc:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'resource' que no termina en UUID "
            f"válido: '{resource_iri}'"
        ) from exc


def fetch_extraction_for_event(
    client: SyntageClient,
    event_type: str,
    payload: dict,
) -> ExtractionResult:
    """
    Punto de entrada principal: dado un evento, llama a Syntage,
    aplica filtros y devuelve un ExtractionResult si nos interesa procesarla.

    Args:
        client: instancia de SyntageClient.
        event_type: tipo de evento (ej. "extraction.updated").
        payload: dict del payload del webhook.

    Returns:
        ExtractionResult con la extracción validada y la acción a ejecutar.

    Raises:
        UnsupportedEventTypeError: el evento no es extraction.*.
        InvalidEventPayloadError: no se puede extraer extraction_id del payload.
        ExtractionNotRelevantError: la extracción existe pero no pasa filtros.
        MissingTaxpayerError: la extracción no trae taxpayer.id.
        SyntageRetryableError / SyntageDefinitiveError: errores HTTP.
    """
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEventTypeError(
            f"Tipo de evento no soportado: '{event_type}'. "
            f"Soportados: {sorted(SUPPORTED_EVENTS)}"
        )

    extraction_id = extract_resource_id(payload, event_type)

    logger.info(
        "Consultando extracción %s (evento '%s')",
        extraction_id,
        event_type,
    )

    extraction = client.fetch_extraction(extraction_id)

    status = extraction.get("status")
    extractor = extraction.get("extractor")

    # ─── Validación: taxpayer.id (RFC) presente ───────────────────────
    taxpayer = extraction.get("taxpayer") or {}
    rfc = taxpayer.get("id")
    if not rfc:
        raise MissingTaxpayerError(
            f"Extracción {extraction_id} no trae taxpayer.id (RFC). "
            f"taxpayer={taxpayer}"
        )

    # ─── Ruta A: facturas ─────────────────────────────────────────────
    if extractor == INVOICE_EXTRACTOR:
        if status != INVOICE_RELEVANT_STATUS:
            raise ExtractionNotRelevantError(
                f"Extracción {extraction_id} (invoice) tiene status='{status}', "
                f"esperábamos '{INVOICE_RELEVANT_STATUS}'. Saltando."
            )
        logger.info(
            "Extracción %s lista para sync-completed: RFC=%s, finishedAt=%s, "
            "createdDataPoints=%s, updatedDataPoints=%s",
            extraction_id,
            rfc,
            extraction.get("finishedAt"),
            extraction.get("createdDataPoints"),
            extraction.get("updatedDataPoints"),
        )
        return ExtractionResult(extraction=extraction, action=ACTION_NOTIFY_COMPLETED)

    # ─── Ruta B: documentos fiscales ─────────────────────────────────
    if extractor in TAX_DOC_EXTRACTORS:
        if status not in TAX_DOC_RELEVANT_STATUSES:
            raise ExtractionNotRelevantError(
                f"Extracción {extraction_id} ({extractor}) tiene status='{status}', "
                f"no es un estado terminal {sorted(TAX_DOC_RELEVANT_STATUSES)}. Saltando."
            )
        logger.info(
            "Extracción %s lista para status-update: extractor=%s, status=%s, RFC=%s",
            extraction_id,
            extractor,
            status,
            rfc,
        )
        return ExtractionResult(extraction=extraction, action=ACTION_NOTIFY_STATUS_UPDATE)

    # ─── Extractor desconocido ────────────────────────────────────────
    raise ExtractionNotRelevantError(
        f"Extracción {extraction_id} tiene extractor='{extractor}', "
        f"no está en los extractores soportados. Saltando."
    )


def get_taxpayer_rfc(extraction: dict) -> str:
    """
    Helper para obtener el RFC desde una extracción ya validada.
    Asume que ya pasó por fetch_extraction_for_event (taxpayer.id garantizado).
    """
    return extraction["taxpayer"]["id"]
