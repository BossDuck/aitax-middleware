import hashlib
import hmac
import json
import time
import uuid
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import CHAR, TypeDecorator
import sqlalchemy.types as sqltypes

from app.database import Base, get_db
from app.main import app
from app.models import SyntageWebhookEvent


# ─── Fix para usar tipos PG en SQLite ─────────────────────────────────
# SQLite no soporta JSONB ni UUID nativos. Le decimos a SQLAlchemy
# que use sus equivalentes JSON/CHAR cuando el dialecto sea sqlite.
# Esto solo aplica para los tests; en producción seguimos con Postgres.

@pytest.fixture(scope="module", autouse=True)
def _patch_pg_types_for_sqlite():
    """Hace que JSONB y PGUUID sean compatibles con SQLite en tests."""
    JSONB.impl = sqltypes.JSON  # type: ignore[attr-defined]
    yield


# ─── DB de pruebas ────────────────────────────────────────────────────

TEST_SIGNING_SECRET = "whsec_test_secret_for_endpoint_tests"


@pytest.fixture
def db_engine():
    """Crea un engine SQLite en memoria con las tablas del proyecto."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db_session(db_engine) -> Generator:
    """Sesión de DB para asserts dentro del test."""
    TestingSessionLocal = sessionmaker(
        bind=db_engine, autoflush=False, autocommit=False
    )
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_engine, monkeypatch) -> Generator[TestClient, None, None]:
    """
    TestClient con:
    - get_db override para usar la DB SQLite en memoria
    - SYNTAGE_WEBHOOK_SIGNING_SECRET fijo para que las firmas sean predecibles
    """
    # Forzamos el secret de pruebas vía monkeypatch al objeto settings.
    from app import config
    monkeypatch.setattr(
        config.settings, "SYNTAGE_WEBHOOK_SIGNING_SECRET", TEST_SIGNING_SECRET
    )

    TestingSessionLocal = sessionmaker(
        bind=db_engine, autoflush=False, autocommit=False
    )

    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ─── Helpers ──────────────────────────────────────────────────────────

def _sign(raw_body: bytes, secret: str = TEST_SIGNING_SECRET) -> str:
    """Genera el header X-Satws-Signature válido para un body dado."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=signed_payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},s={sig}"


def _make_payload(event_type: str = "invoice.created") -> dict:
    """Payload mínimo válido tipo Syntage."""
    event_id = str(uuid.uuid4())
    return {
        "@context": "/contexts/Event",
        "@id": f"/events/{event_id}",
        "@type": "Event",
        "id": event_id,
        "type": event_type,
        "source": "/extractions/abc",
        "createdAt": "2025-01-15 10:00:00",
        "updatedAt": "2025-01-15 10:00:00",
    }


# ─── Tests: happy path ────────────────────────────────────────────────

def test_valid_webhook_returns_200_and_persists(client, db_session):
    payload = _make_payload("invoice.created")
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["event_id"] == payload["id"]
    assert "internal_id" in body

    # Verificamos persistencia
    event = (
        db_session.query(SyntageWebhookEvent)
        .filter_by(syntage_event_id=uuid.UUID(payload["id"]))
        .one()
    )
    assert event.event_type == "invoice.created"
    assert event.status == "pending"
    assert event.attempts == 0
    assert event.payload["type"] == "invoice.created"
    assert event.source == "/extractions/abc"


def test_valid_webhook_stores_lowercase_headers(client, db_session):
    payload = _make_payload()
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    event = db_session.query(SyntageWebhookEvent).one()
    # Los headers deben estar en lowercase
    assert "x-satws-signature" in event.headers
    assert "X-Satws-Signature" not in event.headers


# ─── Tests: firma inválida ────────────────────────────────────────────

def test_missing_signature_returns_401(client, db_session):
    payload = _make_payload()
    raw = json.dumps(payload).encode("utf-8")

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid signature"}
    assert db_session.query(SyntageWebhookEvent).count() == 0


def test_wrong_signature_returns_401(client, db_session):
    payload = _make_payload()
    raw = json.dumps(payload).encode("utf-8")
    bad_signature = _sign(raw, secret="otro_secreto_diferente")

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": bad_signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert db_session.query(SyntageWebhookEvent).count() == 0


def test_tampered_body_returns_401(client, db_session):
    payload = _make_payload()
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    # Modificamos el body después de firmar
    tampered = json.dumps({**payload, "type": "invoice.deleted"}).encode("utf-8")

    response = client.post(
        "/webhooks/syntage/",
        content=tampered,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert db_session.query(SyntageWebhookEvent).count() == 0


# ─── Tests: payload inválido ──────────────────────────────────────────

def test_invalid_json_returns_400(client, db_session):
    raw = b"esto no es json"
    signature = _sign(raw)

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "Invalid JSON" in response.json()["detail"]
    assert db_session.query(SyntageWebhookEvent).count() == 0


def test_payload_missing_id_returns_400(client, db_session):
    payload = {"type": "invoice.created"}  # falta `id`
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400
    assert "missing required fields" in response.json()["detail"]


def test_payload_missing_type_returns_400(client, db_session):
    payload = {"id": str(uuid.uuid4())}  # falta `type`
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 400


# ─── Tests: idempotencia (duplicados) ─────────────────────────────────

def test_duplicate_event_returns_200_without_persisting_again(client, db_session):
    payload = _make_payload()
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    # Primer envío
    r1 = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "queued"

    # Segundo envío del mismo evento (Syntage reintentando)
    # Re-firmamos porque el timestamp puede ser distinto, pero el id es el mismo
    signature2 = _sign(raw)
    r2 = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature2,
            "Content-Type": "application/json",
        },
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    assert r2.json()["event_id"] == payload["id"]

    # Solo debe haber 1 registro en DB
    assert db_session.query(SyntageWebhookEvent).count() == 1


# ─── Tests: distintos tipos de eventos ────────────────────────────────

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
def test_accepts_all_subscribed_event_types(client, db_session, event_type):
    """Verifica que los 6 eventos a los que estamos suscritos pasen."""
    payload = _make_payload(event_type)
    raw = json.dumps(payload).encode("utf-8")
    signature = _sign(raw)

    response = client.post(
        "/webhooks/syntage/",
        content=raw,
        headers={
            "X-Satws-Signature": signature,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200

    event = db_session.query(SyntageWebhookEvent).one()
    assert event.event_type == event_type