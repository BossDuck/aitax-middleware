import uuid
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import EventStatus, SyntageWebhookEvent


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(element, compiler, **kw):
    return "JSON"


# ─── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    Sess = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    s = Sess()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def eager_celery(db_engine, monkeypatch):
    from app import celery_app as celery_module
    celery_module.celery_app.conf.task_always_eager = True
    celery_module.celery_app.conf.task_eager_propagates = True

    Sess = sessionmaker(bind=db_engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("app.tasks.process_event.SessionLocal", Sess)

    yield

    celery_module.celery_app.conf.task_always_eager = False
    celery_module.celery_app.conf.task_eager_propagates = False


# ─── Helpers ──────────────────────────────────────────────────────────

def _make_extraction_event(db_session, event_type="extraction.updated"):
    extraction_id = uuid.uuid4()
    payload = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "resource": f"/extractions/{extraction_id}",
    }
    event = SyntageWebhookEvent(
        syntage_event_id=uuid.uuid4(),
        event_type=event_type,
        source=f"/extractions/{extraction_id}",
        payload=payload,
        headers={},
        status=EventStatus.PENDING.value,
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event, extraction_id


def _fake_extraction(extraction_id, status="finished", extractor="invoice", rfc="PEIC211118IS0"):
    return {
        "id": str(extraction_id),
        "status": status,
        "extractor": extractor,
        "taxpayer": {"id": rfc, "name": "Test"},
        "finishedAt": "2025-01-15T10:05:00Z",
        "createdDataPoints": 100,
        "updatedDataPoints": 5,
    }


def _fake_aitax_response(rfc="PEIC211118IS0"):
    return {
        "status": "ok",
        "rfc": rfc,
        "extraction_id": "test",
        "company_id": "1",
        "results": {
            "invoices": {"2024": {"processed": 10, "upserted": 10}},
            "concepts": {"processed": 20, "upserted": 20},
            "payments": {"processed": 1, "upserted": 1},
        },
    }


# ─── Happy path: full flow ────────────────────────────────────────────

def test_full_flow_finished_invoice_extraction_marks_processed(eager_celery, db_session):
    """Flujo completo: extraction válida → llamada a Syntage → llamada a AITAX → PROCESSED."""
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        # Syntage devuelve la extracción
        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(extraction_id)

        # AITAX responde OK
        aitax_instance = MockAitax.return_value.__enter__.return_value
        aitax_instance.notify_extraction_completed.return_value = _fake_aitax_response()

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "processed"
    assert result["rfc"] == "PEIC211118IS0"
    assert result["extraction_id"] == str(extraction_id)
    assert result["action"] == "notify_completed"
    assert "aitax_result" in result

    # Verifica que se llamó a AITAX con los argumentos correctos
    aitax_instance.notify_extraction_completed.assert_called_once_with(
        rfc="PEIC211118IS0",
        extraction_id=str(extraction_id),
    )

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.PROCESSED.value


# ─── Filtros: SKIPPED (no se llama a AITAX) ───────────────────────────

@pytest.mark.parametrize("status", ["pending", "running", "failed", "stopped"])
def test_non_finished_extraction_marks_skipped_no_aitax_call(eager_celery, db_session, status):
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, status=status
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "skipped"
    # Confirmamos que NO se llamó a AITAX (extracción se filtró antes).
    MockAitax.assert_not_called()

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.SKIPPED.value


@pytest.mark.parametrize("extractor", ["monthly_tax_return", "rpc"])
def test_unsupported_extractor_marks_skipped(eager_celery, db_session, extractor):
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, extractor=extractor
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "skipped"
    MockAitax.assert_not_called()


# ─── Eventos no soportados ────────────────────────────────────────────

def test_unsupported_event_type_marks_skipped(eager_celery, db_session):
    payload = {
        "id": str(uuid.uuid4()),
        "type": "credential.updated",
        "resource": "/credentials/abc",
    }
    event = SyntageWebhookEvent(
        syntage_event_id=uuid.uuid4(),
        event_type="credential.updated",
        source="/credentials/abc",
        payload=payload,
        headers={},
        status=EventStatus.PENDING.value,
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

        # Ni Syntage ni AITAX se llamaron
        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.assert_not_called()
        MockAitax.assert_not_called()

    assert result["status"] == "skipped"


# ─── Errores de Syntage: FAILED (no se llama a AITAX) ─────────────────

def test_syntage_404_marks_failed_no_aitax_call(eager_celery, db_session):
    event, extraction_id = _make_extraction_event(db_session)

    from app.syntage.client import SyntageDefinitiveError

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.side_effect = SyntageDefinitiveError(
            "Not found", status_code=404
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"
    MockAitax.assert_not_called()

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.FAILED.value


# ─── Errores de AITAX ─────────────────────────────

def test_aitax_404_marks_failed(eager_celery, db_session):
    """AITAX devuelve 404 (Company no existe en su DB) → FAILED."""
    event, extraction_id = _make_extraction_event(db_session)

    from app.aitax.client import AitaxDefinitiveError

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(extraction_id)

        aitax_instance = MockAitax.return_value.__enter__.return_value
        aitax_instance.notify_extraction_completed.side_effect = AitaxDefinitiveError(
            "Company not found", status_code=404
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    # Validamos el resultado del task
    assert result["status"] == "failed"
    assert "Company not found" in result["reason"]

    # Validamos que la DB sí guardó el status_code 404 en last_error
    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.FAILED.value
    assert "404" in refreshed.last_error
    assert "AITAX" in refreshed.last_error


def test_aitax_401_marks_failed(eager_celery, db_session):
    """AITAX devuelve 401 (token mal configurado) → FAILED."""
    event, extraction_id = _make_extraction_event(db_session)

    from app.aitax.client import AitaxDefinitiveError

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(extraction_id)

        aitax_instance = MockAitax.return_value.__enter__.return_value
        aitax_instance.notify_extraction_completed.side_effect = AitaxDefinitiveError(
            "Invalid token", status_code=401
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"


# ─── Idempotencia ─────────────────────────────────────────────────────

def test_already_processed_event_is_skipped(eager_celery, db_session):
    event, _ = _make_extraction_event(db_session)
    event.status = EventStatus.PROCESSED.value
    db_session.commit()

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

        assert result["status"] == "already_processed"
        MockSyntage.assert_not_called()
        MockAitax.assert_not_called()


def test_invalid_payload_marks_failed(eager_celery, db_session):
    """Evento sin campo resource."""
    payload = {"id": str(uuid.uuid4()), "type": "extraction.updated"}
    event = SyntageWebhookEvent(
        syntage_event_id=uuid.uuid4(),
        event_type="extraction.updated",
        source=None,
        payload=payload,
        headers={},
        status=EventStatus.PENDING.value,
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    with patch("app.tasks.process_event.SyntageClient"), \
         patch("app.tasks.process_event.AitaxClient"):
        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"


def test_missing_taxpayer_marks_failed(eager_celery, db_session):
    event, extraction_id = _make_extraction_event(db_session)

    extraction_without_taxpayer = _fake_extraction(extraction_id)
    del extraction_without_taxpayer["taxpayer"]

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = extraction_without_taxpayer

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"
    MockAitax.assert_not_called()


def test_missing_event_returns_not_found(eager_celery, db_session):
    fake_id = uuid.uuid4()

    with patch("app.tasks.process_event.SyntageClient"), \
         patch("app.tasks.process_event.AitaxClient"):
        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(fake_id)]).get()

    assert result["status"] == "not_found"


# ─── Ruta B: tax_compliance / tax_status ─────────────────────────────

@pytest.mark.parametrize("extractor", ["tax_compliance", "tax_status"])
@pytest.mark.parametrize("status", ["finished", "error", "stopped"])
def test_tax_doc_terminal_status_calls_status_update_endpoint(
    eager_celery, db_session, extractor, status
):
    """tax_compliance/tax_status en estado terminal → notify_extraction_status_update."""
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, status=status, extractor=extractor
        )

        aitax_instance = MockAitax.return_value.__enter__.return_value
        aitax_instance.notify_extraction_status_update.return_value = {
            "status": "ok",
            "extraction_id": str(extraction_id),
            "extractor": extractor,
        }

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "processed"
    assert result["action"] == "notify_status_update"
    assert result["extractor"] == extractor
    assert result["extraction_status"] == status

    aitax_instance.notify_extraction_status_update.assert_called_once_with(
        extraction_id=str(extraction_id),
        extractor=extractor,
        status=status,
        rfc="PEIC211118IS0",
    )
    aitax_instance.notify_extraction_completed.assert_not_called()

    db_session.expire_all()
    assert db_session.get(SyntageWebhookEvent, event.id).status == EventStatus.PROCESSED.value


@pytest.mark.parametrize("extractor", ["tax_compliance", "tax_status"])
@pytest.mark.parametrize("non_terminal_status", ["pending", "running"])
def test_tax_doc_non_terminal_status_marks_skipped(
    eager_celery, db_session, extractor, non_terminal_status
):
    """tax_compliance/tax_status en estado no terminal → SKIPPED, sin llamar a AITAX."""
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockSyntage, \
         patch("app.tasks.process_event.AitaxClient") as MockAitax:

        syntage_instance = MockSyntage.return_value.__enter__.return_value
        syntage_instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, status=non_terminal_status, extractor=extractor
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "skipped"
    MockAitax.assert_not_called()

    db_session.expire_all()
    assert db_session.get(SyntageWebhookEvent, event.id).status == EventStatus.SKIPPED.value