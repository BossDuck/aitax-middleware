import uuid
from unittest.mock import patch

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


def _make_extraction_event(db_session, event_type="extraction.updated"):
    extraction_id = uuid.uuid4()
    payload = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "source": f"/extractions/{extraction_id}",
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


# ─── Happy path ───────────────────────────────────────────────────────

def test_finished_invoice_extraction_marks_processed(eager_celery, db_session):
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.return_value = _fake_extraction(extraction_id)

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "processed"
    assert result["rfc"] == "PEIC211118IS0"
    assert result["extraction_id"] == str(extraction_id)

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.PROCESSED.value
    assert refreshed.attempts == 1
    assert refreshed.processed_at is not None


# ─── Filtros: SKIPPED ─────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["pending", "running", "failed", "stopped"])
def test_non_finished_extraction_marks_skipped(eager_celery, db_session, status):
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, status=status
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "skipped"

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.SKIPPED.value


@pytest.mark.parametrize("extractor", ["monthly_tax_return", "tax_status", "rpc"])
def test_non_invoice_extractor_marks_skipped(eager_celery, db_session, extractor):
    event, extraction_id = _make_extraction_event(db_session)

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.return_value = _fake_extraction(
            extraction_id, extractor=extractor
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "skipped"

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.SKIPPED.value


# ─── Eventos no soportados ────────────────────────────────────────────

def test_unsupported_event_type_marks_skipped(eager_celery, db_session):
    payload = {
        "id": str(uuid.uuid4()),
        "type": "credential.updated",
        "source": "/credentials/abc",
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

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

        # Ni siquiera se llamó a Syntage, porque el evento se descartó antes.
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.assert_not_called()

    assert result["status"] == "skipped"

    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.SKIPPED.value


# ─── Errores definitivos: FAILED ──────────────────────────────────────

def test_404_from_syntage_marks_failed(eager_celery, db_session):
    event, extraction_id = _make_extraction_event(db_session)

    from app.syntage.client import SyntageDefinitiveError

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.side_effect = SyntageDefinitiveError(
            "Not found", status_code=404
        )

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"
    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.FAILED.value


def test_invalid_payload_marks_failed(eager_celery, db_session):
    payload = {
        "id": str(uuid.uuid4()),
        "type": "extraction.updated",
    }
    event = SyntageWebhookEvent(
        syntage_event_id=uuid.uuid4(),
        event_type="extraction.updated",   # ← evento SOPORTADO
        source=None,
        payload=payload,
        headers={},
        status=EventStatus.PENDING.value,
        attempts=0,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    with patch("app.tasks.process_event.SyntageClient"):
        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"
    db_session.expire_all()
    refreshed = db_session.get(SyntageWebhookEvent, event.id)
    assert refreshed.status == EventStatus.FAILED.value


def test_missing_taxpayer_marks_failed(eager_celery, db_session):
    event, extraction_id = _make_extraction_event(db_session)

    extraction_without_taxpayer = _fake_extraction(extraction_id)
    del extraction_without_taxpayer["taxpayer"]

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.fetch_extraction.return_value = extraction_without_taxpayer

        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

    assert result["status"] == "failed"


# ─── Idempotencia ─────────────────────────────────────────────────────

def test_already_processed_event_is_skipped(eager_celery, db_session):
    event, _ = _make_extraction_event(db_session)
    event.status = EventStatus.PROCESSED.value
    db_session.commit()

    with patch("app.tasks.process_event.SyntageClient") as MockClient:
        from app.tasks.process_event import process_webhook_event
        result = process_webhook_event.apply(args=[str(event.id)]).get()

        assert result["status"] == "already_processed"
        MockClient.assert_not_called()


# ─── Evento no encontrado ─────────────────────────────────────────────

def test_missing_event_returns_not_found(eager_celery, db_session):
    fake_id = uuid.uuid4()

    from app.tasks.process_event import process_webhook_event
    result = process_webhook_event.apply(args=[str(fake_id)]).get()

    assert result["status"] == "not_found"