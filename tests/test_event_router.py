from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.syntage.event_router import (
    SUPPORTED_EVENTS,
    ExtractionNotRelevantError,
    InvalidEventPayloadError,
    MissingTaxpayerError,
    UnsupportedEventTypeError,
    extract_resource_id,
    fetch_extraction_for_event,
    get_taxpayer_rfc,
)


# ─── Helper ───────────────────────────────────────────────────────────

def _make_extraction(
    extraction_id: UUID,
    status: str = "finished",
    extractor: str = "invoice",
    rfc: str = "PEIC211118IS0",
) -> dict:
    """Construye una respuesta de Syntage realista."""
    return {
        "@context": "/contexts/Extraction",
        "@id": f"/extractions/{extraction_id}",
        "@type": "Extraction",
        "id": str(extraction_id),
        "status": status,
        "extractor": extractor,
        "taxpayer": {
            "@id": f"/taxpayers/{rfc}",
            "@type": "Taxpayer",
            "id": rfc,
            "personType": "physical",
            "name": "Pedro Infante",
        },
        "startedAt": "2025-01-15T10:00:00Z",
        "finishedAt": "2025-01-15T10:05:00Z",
        "errorCode": None,
        "createdDataPoints": 100,
        "updatedDataPoints": 5,
    }


# ─── extract_resource_id ──────────────────────────────────────────────

def test_extract_resource_id_from_extractions_iri():
    payload = {
        "type": "extraction.updated",
        "source": "/extractions/91106968-1abd-4d64-85c1-4e73d96fb997",
    }
    result = extract_resource_id(payload, "extraction.updated")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_handles_full_url():
    payload = {
        "source": "https://api.syntage.com/extractions/91106968-1abd-4d64-85c1-4e73d96fb997"
    }
    result = extract_resource_id(payload, "extraction.updated")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_missing_source_raises():
    payload = {"id": "evt", "type": "extraction.updated"}
    with pytest.raises(InvalidEventPayloadError, match="source"):
        extract_resource_id(payload, "extraction.updated")


def test_null_source_raises():
    payload = {"source": None, "type": "extraction.updated"}
    with pytest.raises(InvalidEventPayloadError):
        extract_resource_id(payload, "extraction.updated")


def test_source_not_ending_in_uuid_raises():
    payload = {"source": "/extractions/not-a-uuid"}
    with pytest.raises(InvalidEventPayloadError, match="UUID"):
        extract_resource_id(payload, "extraction.updated")


# ─── fetch_extraction_for_event: happy path ───────────────────────────

def test_fetch_extraction_for_event_finished_invoice_passes():
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, status="finished", extractor="invoice"
    )

    payload = {
        "type": "extraction.updated",
        "source": f"/extractions/{extraction_id}",
    }

    result = fetch_extraction_for_event(client, "extraction.updated", payload)

    client.fetch_extraction.assert_called_once_with(extraction_id)
    assert result["status"] == "finished"
    assert result["extractor"] == "invoice"
    assert result["taxpayer"]["id"] == "PEIC211118IS0"


def test_fetch_extraction_for_event_works_for_extraction_created():
    """También aceptamos extraction.created si trae status=finished."""
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(extraction_id)

    payload = {
        "type": "extraction.created",
        "source": f"/extractions/{extraction_id}",
    }

    result = fetch_extraction_for_event(client, "extraction.created", payload)
    assert result is not None


# ─── Filtros: status ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "non_finished_status",
    ["pending", "running", "failed", "stopping", "stopped"],
)
def test_non_finished_status_raises_not_relevant(non_finished_status):
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, status=non_finished_status
    )

    payload = {
        "type": "extraction.updated",
        "source": f"/extractions/{extraction_id}",
    }

    with pytest.raises(ExtractionNotRelevantError, match=non_finished_status):
        fetch_extraction_for_event(client, "extraction.updated", payload)


# ─── Filtros: extractor ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "non_invoice_extractor",
    [
        "monthly_tax_return",
        "annual_tax_return",
        "tax_status",
        "tax_retention",
        "rpc",
    ],
)
def test_non_invoice_extractor_raises_not_relevant(non_invoice_extractor):
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, extractor=non_invoice_extractor
    )

    payload = {
        "type": "extraction.updated",
        "source": f"/extractions/{extraction_id}",
    }

    with pytest.raises(ExtractionNotRelevantError, match=non_invoice_extractor):
        fetch_extraction_for_event(client, "extraction.updated", payload)


# ─── Validación: taxpayer ─────────────────────────────────────────────

def test_missing_taxpayer_raises():
    extraction_id = uuid4()
    extraction = _make_extraction(extraction_id)
    del extraction["taxpayer"]

    client = MagicMock()
    client.fetch_extraction.return_value = extraction

    payload = {
        "type": "extraction.updated",
        "source": f"/extractions/{extraction_id}",
    }

    with pytest.raises(MissingTaxpayerError):
        fetch_extraction_for_event(client, "extraction.updated", payload)


def test_taxpayer_without_id_raises():
    extraction_id = uuid4()
    extraction = _make_extraction(extraction_id)
    extraction["taxpayer"] = {"name": "Sin RFC"}  # Sin id

    client = MagicMock()
    client.fetch_extraction.return_value = extraction

    payload = {
        "type": "extraction.updated",
        "source": f"/extractions/{extraction_id}",
    }

    with pytest.raises(MissingTaxpayerError):
        fetch_extraction_for_event(client, "extraction.updated", payload)


# ─── Eventos no soportados ────────────────────────────────────────────

@pytest.mark.parametrize(
    "unsupported_event",
    [
        "credential.updated",
        "invoice.created",
        "invoice_payment.updated",
        "tax_return.created",
    ],
)
def test_unsupported_event_type_raises(unsupported_event):
    client = MagicMock()
    payload = {"type": unsupported_event, "source": "/whatever/abc"}

    with pytest.raises(UnsupportedEventTypeError, match=unsupported_event):
        fetch_extraction_for_event(client, unsupported_event, payload)

    client.fetch_extraction.assert_not_called()


def test_invalid_payload_propagates_error():
    client = MagicMock()
    payload = {"type": "extraction.updated"}  # sin source

    with pytest.raises(InvalidEventPayloadError):
        fetch_extraction_for_event(client, "extraction.updated", payload)

    client.fetch_extraction.assert_not_called()


# ─── Helpers ──────────────────────────────────────────────────────────

def test_get_taxpayer_rfc_returns_id():
    extraction = _make_extraction(uuid4(), rfc="GEM001219KI7")
    assert get_taxpayer_rfc(extraction) == "GEM001219KI7"


# ─── Sanity check: eventos suscritos ──────────────────────────────────

def test_extraction_events_are_supported():
    assert "extraction.created" in SUPPORTED_EVENTS
    assert "extraction.updated" in SUPPORTED_EVENTS


def test_invoice_events_are_NOT_supported():
    """Confirmamos que los eventos viejos del plan original ya no se procesan."""
    assert "invoice.created" not in SUPPORTED_EVENTS
    assert "invoice_line_item.updated" not in SUPPORTED_EVENTS
    assert "invoice_payment.created" not in SUPPORTED_EVENTS