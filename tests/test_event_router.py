from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.syntage.event_router import (
    ACTION_NOTIFY_COMPLETED,
    ACTION_NOTIFY_STATUS_UPDATE,
    SUPPORTED_EVENTS,
    TAX_DOC_EXTRACTORS,
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
        "resource": "/extractions/91106968-1abd-4d64-85c1-4e73d96fb997",
    }
    result = extract_resource_id(payload, "extraction.updated")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_extract_resource_id_handles_full_url():
    payload = {
        "resource": "https://api.syntage.com/extractions/91106968-1abd-4d64-85c1-4e73d96fb997"
    }
    result = extract_resource_id(payload, "extraction.updated")
    assert result == UUID("91106968-1abd-4d64-85c1-4e73d96fb997")


def test_missing_resource_raises():
    payload = {"id": "evt", "type": "extraction.updated"}
    with pytest.raises(InvalidEventPayloadError, match="resource"):
        extract_resource_id(payload, "extraction.updated")


def test_null_resource_raises():
    payload = {"resource": None, "type": "extraction.updated"}
    with pytest.raises(InvalidEventPayloadError):
        extract_resource_id(payload, "extraction.updated")


def test_resource_not_ending_in_uuid_raises():
    payload = {"resource": "/extractions/not-a-uuid"}
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
        "resource": f"/extractions/{extraction_id}",
    }

    result = fetch_extraction_for_event(client, "extraction.updated", payload)

    client.fetch_extraction.assert_called_once_with(extraction_id)
    assert result.action == ACTION_NOTIFY_COMPLETED
    assert result.extraction["status"] == "finished"
    assert result.extraction["extractor"] == "invoice"
    assert result.extraction["taxpayer"]["id"] == "PEIC211118IS0"


def test_fetch_extraction_for_event_works_for_extraction_created():
    """También aceptamos extraction.created si trae status=finished."""
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(extraction_id)

    payload = {
        "type": "extraction.created",
        "resource": f"/extractions/{extraction_id}",
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
        "resource": f"/extractions/{extraction_id}",
    }

    with pytest.raises(ExtractionNotRelevantError, match=non_finished_status):
        fetch_extraction_for_event(client, "extraction.updated", payload)


# ─── Filtros: extractor ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "unsupported_extractor",
    [
        "monthly_tax_return",
        "annual_tax_return",
        "tax_retention",
        "rpc",
    ],
)
def test_unsupported_extractor_raises_not_relevant(unsupported_extractor):
    """Extractores que no son invoice ni tax_doc → SKIPPED."""
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, extractor=unsupported_extractor
    )

    payload = {
        "type": "extraction.updated",
        "resource": f"/extractions/{extraction_id}",
    }

    with pytest.raises(ExtractionNotRelevantError):
        fetch_extraction_for_event(client, "extraction.updated", payload)


# ─── Ruta B: tax_compliance / tax_status ─────────────────────────────

@pytest.mark.parametrize("extractor", ["tax_compliance", "tax_status"])
@pytest.mark.parametrize("status", ["finished", "error", "stopped"])
def test_tax_doc_terminal_status_returns_status_update_action(extractor, status):
    """tax_compliance/tax_status en estado terminal → ACTION_NOTIFY_STATUS_UPDATE."""
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, status=status, extractor=extractor
    )

    payload = {
        "type": "extraction.updated",
        "resource": f"/extractions/{extraction_id}",
    }

    result = fetch_extraction_for_event(client, "extraction.updated", payload)

    assert result.action == ACTION_NOTIFY_STATUS_UPDATE
    assert result.extraction["extractor"] == extractor
    assert result.extraction["status"] == status


@pytest.mark.parametrize("extractor", ["tax_compliance", "tax_status"])
@pytest.mark.parametrize("non_terminal_status", ["pending", "running"])
def test_tax_doc_non_terminal_status_raises_not_relevant(extractor, non_terminal_status):
    """tax_compliance/tax_status en estado no terminal → SKIPPED."""
    extraction_id = uuid4()
    client = MagicMock()
    client.fetch_extraction.return_value = _make_extraction(
        extraction_id, status=non_terminal_status, extractor=extractor
    )

    payload = {
        "type": "extraction.updated",
        "resource": f"/extractions/{extraction_id}",
    }

    with pytest.raises(ExtractionNotRelevantError):
        fetch_extraction_for_event(client, "extraction.updated", payload)


def test_tax_doc_extractors_set_contains_expected():
    assert "tax_compliance" in TAX_DOC_EXTRACTORS
    assert "tax_status" in TAX_DOC_EXTRACTORS


# ─── Validación: taxpayer ─────────────────────────────────────────────

def test_missing_taxpayer_raises():
    extraction_id = uuid4()
    extraction = _make_extraction(extraction_id)
    del extraction["taxpayer"]

    client = MagicMock()
    client.fetch_extraction.return_value = extraction

    payload = {
        "type": "extraction.updated",
        "resource": f"/extractions/{extraction_id}",
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
        "resource": f"/extractions/{extraction_id}",
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
    payload = {"type": unsupported_event, "resource": "/whatever/abc"}

    with pytest.raises(UnsupportedEventTypeError, match=unsupported_event):
        fetch_extraction_for_event(client, unsupported_event, payload)

    client.fetch_extraction.assert_not_called()


def test_invalid_payload_propagates_error():
    client = MagicMock()
    payload = {"type": "extraction.updated"}  # sin resource

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