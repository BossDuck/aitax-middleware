"""
Tests unitarios para verify_syntage_signature.

Cubrimos:
- Firma válida (happy path)
- Firma inválida (secreto distinto)
- Header malformado en varias formas
- Timestamp fuera de tolerancia (pasado y futuro)
- Body vacío
- Que se use comparación constant-time (verificamos que la función
  use hmac.compare_digest mediante monkeypatch).
"""

import hashlib
import hmac
import time
from unittest.mock import patch

import pytest

from app.webhooks.signature import verify_syntage_signature


SECRET = "whsec_test_super_secret_value"


def _make_signature(
    raw_body: bytes,
    secret: str = SECRET,
    timestamp: int | None = None,
) -> tuple[str, int]:
    """
    Helper para generar un header de firma válido para los tests.
    Replica exactamente lo que haría Syntage al firmar.
    """
    if timestamp is None:
        timestamp = int(time.time())

    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=signed_payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    header = f"t={timestamp},s={signature}"
    return header, timestamp


# ─── Happy path ───────────────────────────────────────────────────────

def test_valid_signature_returns_true():
    body = b'{"id":"abc","type":"invoice.created"}'
    header, ts = _make_signature(body)

    is_valid, error, parsed_ts = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
    )

    assert is_valid is True
    assert error is None
    assert parsed_ts == ts


def test_valid_signature_with_unicode_body():
    """El body puede contener UTF-8 (acentos, ñ). Debe funcionar."""
    body = '{"empresa":"Administrategia S.A. de C.V.","ñ":"sí"}'.encode("utf-8")
    header, _ = _make_signature(body)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
    )

    assert is_valid is True
    assert error is None


# ─── Firma inválida ───────────────────────────────────────────────────

def test_wrong_secret_returns_false():
    body = b'{"id":"abc"}'
    header, ts = _make_signature(body, secret="otro_secreto")

    is_valid, error, parsed_ts = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,  # secreto distinto del usado para firmar
    )

    assert is_valid is False
    assert error == "Signature mismatch"
    assert parsed_ts == ts  # timestamp se devuelve igual aunque falle firma


def test_tampered_body_returns_false():
    """Si alguien modifica el body después de firmar, debe fallar."""
    original_body = b'{"id":"abc","amount":100}'
    header, _ = _make_signature(original_body)

    tampered_body = b'{"id":"abc","amount":99999}'

    is_valid, error, _ = verify_syntage_signature(
        raw_body=tampered_body,
        signature_header=header,
        signing_secret=SECRET,
    )

    assert is_valid is False
    assert error == "Signature mismatch"


# ─── Header malformado ────────────────────────────────────────────────

def test_missing_header_returns_false():
    is_valid, error, ts = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header=None,
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert error == "Missing signature header"
    assert ts is None


def test_empty_header_returns_false():
    is_valid, error, _ = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header="",
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert error == "Missing signature header"


def test_header_without_equals_returns_false():
    is_valid, error, _ = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header="garbage_value",
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert error is not None
    assert "Malformed" in error


def test_header_missing_t_component_returns_false():
    is_valid, error, _ = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header="s=abc123",
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert "missing 't' or 's'" in error


def test_header_missing_s_component_returns_false():
    is_valid, error, _ = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header="t=1700000000",
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert "missing 't' or 's'" in error


def test_header_with_non_numeric_timestamp_returns_false():
    is_valid, error, _ = verify_syntage_signature(
        raw_body=b'{"id":"abc"}',
        signature_header="t=not_a_number,s=abc123",
        signing_secret=SECRET,
    )
    assert is_valid is False
    assert "Invalid timestamp" in error


# ─── Tolerancia de timestamp ──────────────────────────────────────────

def test_timestamp_too_old_returns_false():
    body = b'{"id":"abc"}'
    old_ts = int(time.time()) - 600  # 10 minutos atrás
    header, _ = _make_signature(body, timestamp=old_ts)

    is_valid, error, parsed_ts = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
        tolerance_seconds=300,
    )

    assert is_valid is False
    assert "outside tolerance" in error
    assert parsed_ts == old_ts


def test_timestamp_too_far_in_future_returns_false():
    """Reloj del atacante adelantado: también se rechaza."""
    body = b'{"id":"abc"}'
    future_ts = int(time.time()) + 600
    header, _ = _make_signature(body, timestamp=future_ts)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
        tolerance_seconds=300,
    )

    assert is_valid is False
    assert "outside tolerance" in error


def test_timestamp_at_edge_of_tolerance_is_accepted():
    """Justo en el borde (299s) debe aceptarse."""
    body = b'{"id":"abc"}'
    edge_ts = int(time.time()) - 299
    header, _ = _make_signature(body, timestamp=edge_ts)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
        tolerance_seconds=300,
    )

    assert is_valid is True
    assert error is None


def test_custom_tolerance_works():
    """Con tolerancia más amplia, un timestamp viejo pasa."""
    body = b'{"id":"abc"}'
    old_ts = int(time.time()) - 1000
    header, _ = _make_signature(body, timestamp=old_ts)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
        tolerance_seconds=2000,  # 33 minutos
    )

    assert is_valid is True


# ─── Body / secret faltantes ──────────────────────────────────────────

def test_empty_body_returns_false():
    body = b""
    # Aunque generáramos una firma válida sobre body vacío, la rechazamos.
    header, _ = _make_signature(body)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret=SECRET,
    )

    assert is_valid is False
    assert error == "Empty request body"


def test_missing_signing_secret_returns_false():
    body = b'{"id":"abc"}'
    header, _ = _make_signature(body)

    is_valid, error, _ = verify_syntage_signature(
        raw_body=body,
        signature_header=header,
        signing_secret="",
    )

    assert is_valid is False
    assert "Missing signing secret" in error


# ─── Constant-time comparison ─────────────────────────────────────────

def test_uses_constant_time_comparison():
    """
    Verifica que la función use hmac.compare_digest y NO el operador ==.
    Lo hacemos espiando hmac.compare_digest en el módulo.
    """
    body = b'{"id":"abc"}'
    header, _ = _make_signature(body)

    with patch(
        "app.webhooks.signature.hmac.compare_digest",
        wraps=hmac.compare_digest,
    ) as spy:
        verify_syntage_signature(
            raw_body=body,
            signature_header=header,
            signing_secret=SECRET,
        )
        assert spy.called, (
            "verify_syntage_signature debe usar hmac.compare_digest "
            "para evitar timing attacks"
        )