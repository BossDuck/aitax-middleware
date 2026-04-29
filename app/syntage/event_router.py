"""
Despacha eventos de webhook al método correcto del cliente Syntage.

Diseño:
- Solo procesamos eventos `extraction.created` y `extraction.updated`.
- Hacemos GET a /extractions/{id} para obtener detalles completos.
- Filtramos:
    - status == "finished" (solo extracciones que terminaron OK)
    - extractor == "invoice" (solo extracciones de facturas)
- Si no cumple filtros → ExtractionNotRelevantError (evento → SKIPPED).
- Si pasa filtros → devolvemos la extracción para que el worker la mande a AITAX.

Eventos que NOS interesan:
    extraction.created (raro, pero por si Syntage los manda al iniciar)
    extraction.updated (la mayoría — incluye transiciones de estado)

Eventos que NO procesamos (ej. invoice.*, payment.*) → UnsupportedEventTypeError.
"""

import logging
from uuid import UUID

from app.syntage.client import SyntageClient


logger = logging.getLogger(__name__)


# ─── Configuración de filtros ─────────────────────────────────────────

SUPPORTED_EVENTS = frozenset({
    "extraction.created",
    "extraction.updated",
})

# Solo procesamos extracciones que YA TERMINARON correctamente.
RELEVANT_STATUS = "finished"

# Solo procesamos extracciones de facturas (no tax_returns, retentions, etc.).
RELEVANT_EXTRACTOR = "invoice"


# ─── Excepciones ──────────────────────────────────────────────────────

class UnsupportedEventTypeError(ValueError):
    """El tipo de evento no es uno que procesemos (ej. credential.updated)."""
    pass


class InvalidEventPayloadError(ValueError):
    """El payload no permite extraer el ID del recurso."""
    pass


class ExtractionNotRelevantError(Exception):
    """
    La extracción existe pero no nos interesa (ej. status=running,
    extractor=tax_return). NO es un error técnico, es filtrado normal.
    El worker la marcará como SKIPPED.
    """
    pass


class MissingTaxpayerError(Exception):
    """La extracción no trae taxpayer.id (RFC). Caso muy raro pero defensivo."""
    pass


# ─── Lógica del router ────────────────────────────────────────────────

def extract_resource_id(payload: dict, event_type: str) -> UUID:
    """
    Extrae el UUID del recurso afectado desde el campo `source` del payload.

    Para eventos extraction.*, source viene como "/extractions/<uuid>".
    """
    source = payload.get("source")

    if not source or not isinstance(source, str):
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' no trae campo 'source' válido. "
            f"Payload keys: {list(payload.keys())}"
        )

    last_segment = source.rstrip("/").rsplit("/", 1)[-1]

    if not last_segment:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'source' vacío tras parsear: '{source}'"
        )

    try:
        return UUID(last_segment)
    except ValueError as exc:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'source' que no termina en UUID válido: "
            f"'{source}'"
        ) from exc


def fetch_extraction_for_event(
    client: SyntageClient,
    event_type: str,
    payload: dict,
) -> dict:
    """
    Punto de entrada principal: dado un evento, llama a Syntage,
    aplica filtros y devuelve la extracción si nos interesa procesarla.

    Args:
        client: instancia de SyntageClient.
        event_type: tipo de evento (ej. "extraction.updated").
        payload: dict del payload del webhook.

    Returns:
        dict con la extracción de Syntage. Garantizado:
        - status == "finished"
        - extractor == "invoice"
        - taxpayer.id (RFC) presente

    Raises:
        UnsupportedEventTypeError: el evento no es extraction.*.
        InvalidEventPayloadError: no se puede extraer extraction_id del payload.
        ExtractionNotRelevantError: la extracción existe pero no pasa filtros
                                    (status != finished o extractor != invoice).
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

    # ─── Filtro 1: status ─────────────────────────────────────────────
    status = extraction.get("status")
    if status != RELEVANT_STATUS:
        raise ExtractionNotRelevantError(
            f"Extracción {extraction_id} tiene status='{status}', "
            f"esperábamos '{RELEVANT_STATUS}'. Saltando."
        )

    # ─── Filtro 2: extractor ──────────────────────────────────────────
    extractor = extraction.get("extractor")
    if extractor != RELEVANT_EXTRACTOR:
        raise ExtractionNotRelevantError(
            f"Extracción {extraction_id} tiene extractor='{extractor}', "
            f"esperábamos '{RELEVANT_EXTRACTOR}'. Saltando."
        )

    # ─── Validación: taxpayer.id (RFC) presente ───────────────────────
    taxpayer = extraction.get("taxpayer") or {}
    rfc = taxpayer.get("id")
    if not rfc:
        raise MissingTaxpayerError(
            f"Extracción {extraction_id} no trae taxpayer.id (RFC). "
            f"taxpayer={taxpayer}"
        )

    logger.info(
        "Extracción %s lista para sync: RFC=%s, finishedAt=%s, "
        "createdDataPoints=%s, updatedDataPoints=%s",
        extraction_id,
        rfc,
        extraction.get("finishedAt"),
        extraction.get("createdDataPoints"),
        extraction.get("updatedDataPoints"),
    )

    return extraction


def get_taxpayer_rfc(extraction: dict) -> str:
    """
    Helper para obtener el RFC desde una extracción ya validada.
    Asume que ya pasó por fetch_extraction_for_event (taxpayer.id garantizado).
    """
    return extraction["taxpayer"]["id"]