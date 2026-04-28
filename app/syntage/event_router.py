"""
Despacha eventos de webhook al método correcto del cliente Syntage.

Responsabilidades:
- Extraer el UUID del recurso desde el campo `source` del payload.
- Mapear el tipo de evento al método correcto del cliente.
- Devolver el dict con los datos del recurso, listo para enviar a AITAX.

Tipos de eventos soportados (los 6 a los que estamos suscritos):
    invoice.created, invoice.updated
    invoice_line_item.created, invoice_line_item.updated
    invoice_payment.created, invoice_payment.updated
"""

import logging
from uuid import UUID

from app.syntage.client import SyntageClient


logger = logging.getLogger(__name__)


# ─── Categorías de recursos ───────────────────────────────────────────

INVOICE_EVENTS = frozenset({"invoice.created", "invoice.updated"})
LINE_ITEM_EVENTS = frozenset(
    {"invoice_line_item.created", "invoice_line_item.updated"}
)
PAYMENT_EVENTS = frozenset(
    {"invoice_payment.created", "invoice_payment.updated"}
)

SUPPORTED_EVENTS = INVOICE_EVENTS | LINE_ITEM_EVENTS | PAYMENT_EVENTS


# ─── Excepciones ──────────────────────────────────────────────────────

class UnsupportedEventTypeError(ValueError):
    """El tipo de evento no está en la lista de eventos soportados."""


class InvalidEventPayloadError(ValueError):
    """El payload no permite extraer el ID del recurso."""


# ─── Lógica del router ────────────────────────────────────────────────

def extract_resource_id(payload: dict, event_type: str) -> UUID:
    """
    Extrae el UUID del recurso afectado desde el campo `source` del payload.

    Syntage envía `source` con formato IRI: "/invoices/<uuid>",
    "/line-items/<uuid>", "/invoices/payments/<uuid>".

    Tomamos el último segmento del path y lo validamos como UUID.

    Args:
        payload: dict con el payload del webhook (ya parseado).
        event_type: tipo de evento, usado solo para mensajes de error.

    Raises:
        InvalidEventPayloadError: si `source` falta, está vacío o no
        termina en un UUID válido.
    """
    source = payload.get("source")

    if not source or not isinstance(source, str):
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' no trae campo 'source' válido. "
            f"Payload: {payload}"
        )

    # Quita slashes finales y toma el último segmento.
    last_segment = source.rstrip("/").rsplit("/", 1)[-1]

    if not last_segment:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'source' vacío tras parsear: "
            f"'{source}'"
        )

    try:
        return UUID(last_segment)
    except ValueError as exc:
        raise InvalidEventPayloadError(
            f"Webhook '{event_type}' tiene 'source' que no termina en UUID "
            f"válido: '{source}'"
        ) from exc


def fetch_resource_for_event(
    client: SyntageClient,
    event_type: str,
    payload: dict,
) -> dict:
    """
    Punto de entrada principal: dado un evento, llama al endpoint
    correcto de Syntage y devuelve los datos del recurso.

    Args:
        client: instancia de SyntageClient.
        event_type: tipo de evento (ej. "invoice.created").
        payload: dict del payload del webhook.

    Returns:
        dict con los datos del recurso, tal como los devuelve Syntage.
        La estructura es la misma que esperan sync_invoices/concepts/payments
        en AITAX.

    Raises:
        UnsupportedEventTypeError: si el tipo no está soportado.
        InvalidEventPayloadError: si no se puede extraer el resource_id.
        SyntageRetryableError / SyntageDefinitiveError: errores HTTP.
    """
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEventTypeError(
            f"Tipo de evento no soportado: '{event_type}'. "
            f"Soportados: {sorted(SUPPORTED_EVENTS)}"
        )

    resource_id = extract_resource_id(payload, event_type)

    logger.info(
        "Despachando evento '%s' para recurso %s",
        event_type,
        resource_id,
    )

    if event_type in INVOICE_EVENTS:
        return client.fetch_invoice(resource_id)
    if event_type in LINE_ITEM_EVENTS:
        return client.fetch_line_item(resource_id)
    if event_type in PAYMENT_EVENTS:
        return client.fetch_payment(resource_id)

    # Defensivo: jamás debería llegar aquí porque ya validamos arriba.
    raise UnsupportedEventTypeError(
        f"Evento llegó al final del despacho sin match: '{event_type}'"
    )