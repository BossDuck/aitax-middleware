"""
Tests del event_router: extracción de resource_id y despacho.
"""

from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.syntage.event_router import (
    extract_resource_id,
    fetch_resource_for_event,
    InvalidEventPayloadError,
    UnsupportedEventTypeError,
    SUPPORTED_EVENTS,
)


# ─── extract_resource_id ──────────────────────────────────────────────

def test_extract_resource_id_from_invoices_iri():
    payload = {
        "id": "evento-uuid",
        "type": "invoice.created",
        "source": "/invoices/91106968-1abd-4d64-85c1-4e73d96fb997",
    }
    result = extract_resource_id(payload, "invoice.created")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_from_line_items_iri():
    payload = {
        "type": "invoice_line_item.created",
        "source": "/line-items/91106968-1abd-4d64-85c1-4e73d96fb997",
    }
    result = extract_resource_id(payload, "invoice_line_item.created")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_from_payments_iri():
    """Los pagos tienen IRI más anidado: /invoices/payments/{uuid}"""
    payload = {
        "type": "invoice_payment.created",
        "source": "/invoices/payments/91106968-1abd-4d64-85c1-4e73d96fb997",
    }
    result = extract_resource_id(payload, "invoice_payment.created")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_handles_trailing_slash():
    payload = {"source": "/invoices/91106968-1abd-4d64-85c1-4e73d96fb997/"}
    result = extract_resource_id(payload, "invoice.created")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_handles_full_url():
    """Si source viene como URL completa, también funciona."""
    payload = {
        "source": "https://api.syntage.com/invoices/91106968-1abd-4d64-85c1-4e73d96fb997"
    }
    result = extract_resource_id(payload, "invoice.created")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_missing_source_raises():
    payload = {"id": "evt", "type": "invoice.created"}
    with pytest.raises(InvalidEventPayloadError, match="source"):
        extract_resource_id(payload, "invoice.created")


def test_null_source_raises():
    payload = {"source": None, "type": "invoice.created"}
    with pytest.raises(InvalidEventPayloadError, match="source"):
        extract_resource_id(payload, "invoice.created")


def test_empty_source_raises():
    payload = {"source": "", "type": "invoice.created"}
    with pytest.raises(InvalidEventPayloadError):
        extract_resource_id(payload, "invoice.created")


def test_source_not_ending_in_uuid_raises():
    payload = {"source": "/invoices/not-a-uuid"}
    with pytest.raises(InvalidEventPayloadError, match="UUID"):
        extract_resource_id(payload, "invoice.created")


def test_source_as_non_string_raises():
    payload = {"source": 12345}  # int en lugar de string
    with pytest.raises(InvalidEventPayloadError):
        extract_resource_id(payload, "invoice.created")


# ─── fetch_resource_for_event ─────────────────────────────────────────

def test_invoice_event_calls_fetch_invoice():
    client = MagicMock()
    client.fetch_invoice.return_value = {"id": "abc", "issuedAt": "2025-01-15"}

    invoice_uuid = uuid4()
    payload = {
        "type": "invoice.created",
        "source": f"/invoices/{invoice_uuid}",
    }

    result = fetch_resource_for_event(client, "invoice.created", payload)

    client.fetch_invoice.assert_called_once_with(invoice_uuid)
    client.fetch_line_item.assert_not_called()
    client.fetch_payment.assert_not_called()
    assert result == {"id": "abc", "issuedAt": "2025-01-15"}


def test_invoice_updated_calls_fetch_invoice():
    client = MagicMock()
    invoice_uuid = uuid4()
    payload = {
        "type": "invoice.updated",
        "source": f"/invoices/{invoice_uuid}",
    }

    fetch_resource_for_event(client, "invoice.updated", payload)

    client.fetch_invoice.assert_called_once_with(invoice_uuid)


def test_line_item_event_calls_fetch_line_item():
    client = MagicMock()
    line_item_uuid = uuid4()
    payload = {
        "type": "invoice_line_item.created",
        "source": f"/line-items/{line_item_uuid}",
    }

    fetch_resource_for_event(client, "invoice_line_item.created", payload)

    client.fetch_line_item.assert_called_once_with(line_item_uuid)
    client.fetch_invoice.assert_not_called()


def test_payment_event_calls_fetch_payment():
    client = MagicMock()
    payment_uuid = uuid4()
    payload = {
        "type": "invoice_payment.created",
        "source": f"/invoices/payments/{payment_uuid}",
    }

    fetch_resource_for_event(client, "invoice_payment.created", payload)

    client.fetch_payment.assert_called_once_with(payment_uuid)


def test_unsupported_event_type_raises():
    client = MagicMock()
    payload = {
        "type": "credential.updated",
        "source": "/credentials/abc",
    }

    with pytest.raises(UnsupportedEventTypeError, match="credential.updated"):
        fetch_resource_for_event(client, "credential.updated", payload)

    client.fetch_invoice.assert_not_called()
    client.fetch_line_item.assert_not_called()
    client.fetch_payment.assert_not_called()


def test_invalid_payload_propagates_error():
    """Si no se puede extraer resource_id, no se llama al cliente."""
    client = MagicMock()
    payload = {"type": "invoice.created"}  # sin source

    with pytest.raises(InvalidEventPayloadError):
        fetch_resource_for_event(client, "invoice.created", payload)

    client.fetch_invoice.assert_not_called()


# ─── Sanity check: los 6 eventos suscritos están soportados ───────────

@pytest.mark.parametrize(
    "event_type",
    [
        "invoice.created",
        "invoice.updated",
        "invoice_line_item.created",
        "invoice_line_item.updated",
        "invoice_payment.created",
        "invoice_payment.updated",
    ],
)
def test_all_subscribed_events_are_supported(event_type):
    assert event_type in SUPPORTED_EVENTS