"""
Tests del cliente Syntage usando respx para mockear HTTP.

respx intercepta peticiones httpx y devuelve respuestas fake,
así no dependemos de la red ni de Syntage real.
"""

from uuid import UUID, uuid4

import httpx
import pytest
import respx

from app.syntage.client import (
    SyntageClient,
    SyntageDefinitiveError,
    SyntageRetryableError,
)


BASE_URL = "https://api.sandbox.syntage.com"
TEST_API_KEY = "test_api_key_12345"


@pytest.fixture
def client():
    """Cliente con base_url y api_key fijos para los tests."""
    c = SyntageClient(base_url=BASE_URL, api_key=TEST_API_KEY)
    yield c
    c.close()


# ─── Happy path ───────────────────────────────────────────────────────

@respx.mock
def test_fetch_invoice_returns_payload(client):
    invoice_id = uuid4()
    expected = {
        "id": str(invoice_id),
        "issuedAt": "2025-01-15T10:00:00.000Z",
        "total": 1000,
    }

    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.fetch_invoice(invoice_id)

    assert result == expected
    assert route.called


@respx.mock
def test_fetch_line_item_uses_correct_url(client):
    line_item_id = uuid4()
    expected = {"id": str(line_item_id), "invoice": {"id": str(uuid4())}}

    route = respx.get(f"{BASE_URL}/line-items/{line_item_id}").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.fetch_line_item(line_item_id)

    assert result == expected
    assert route.called


@respx.mock
def test_fetch_payment_uses_correct_url(client):
    payment_id = uuid4()
    expected = {"id": str(payment_id), "createdAt": "2025-01-15T10:00:00.000Z"}

    route = respx.get(f"{BASE_URL}/invoices/payments/{payment_id}").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.fetch_payment(payment_id)

    assert result == expected
    assert route.called


@respx.mock
def test_sends_x_api_key_header(client):
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(200, json={"id": str(invoice_id)})
    )

    client.fetch_invoice(invoice_id)

    request = route.calls.last.request
    assert request.headers["x-api-key"] == TEST_API_KEY
    assert request.headers["accept"] == "application/ld+json"


# ─── Errores definitivos (no retry) ───────────────────────────────────

@respx.mock
def test_404_raises_definitive_error_no_retry(client):
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(SyntageDefinitiveError) as exc_info:
        client.fetch_invoice(invoice_id)

    assert exc_info.value.status_code == 404
    # Solo se llamó una vez: NO hubo retries
    assert route.call_count == 1


@respx.mock
def test_401_raises_definitive_error_no_retry(client):
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with pytest.raises(SyntageDefinitiveError) as exc_info:
        client.fetch_invoice(invoice_id)

    assert exc_info.value.status_code == 401
    assert route.call_count == 1


# ─── Errores transitorios (retry) ─────────────────────────────────────

@respx.mock
def test_500_retries_three_times_then_raises(client):
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_invoice(invoice_id)

    # 3 intentos totales (el original + 2 retries)
    assert route.call_count == 3


@respx.mock
def test_429_retries(client):
    """Rate limit debe reintentarse."""
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_invoice(invoice_id)

    assert route.call_count == 3


@respx.mock
def test_500_then_200_succeeds_after_retry(client):
    """Si el primer intento falla con 5xx pero el segundo funciona, ok."""
    invoice_id = uuid4()
    expected = {"id": str(invoice_id), "issuedAt": "2025-01-15T10:00:00Z"}

    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        side_effect=[
            httpx.Response(500, text="Temporarily down"),
            httpx.Response(200, json=expected),
        ]
    )

    result = client.fetch_invoice(invoice_id)

    assert result == expected
    assert route.call_count == 2


@respx.mock
def test_timeout_retries(client):
    """Un timeout es retryable."""
    invoice_id = uuid4()
    route = respx.get(f"{BASE_URL}/invoices/{invoice_id}").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_invoice(invoice_id)

    assert route.call_count == 3


# ─── Configuración del cliente ────────────────────────────────────────

def test_missing_api_key_raises_value_error():
    with pytest.raises(ValueError, match="SYNTAGE_API_KEY"):
        SyntageClient(base_url=BASE_URL, api_key="")


def test_context_manager_closes_client():
    """El cliente debe poder usarse como `with SyntageClient() as c:`"""
    with SyntageClient(base_url=BASE_URL, api_key=TEST_API_KEY) as c:
        assert c is not None
    # No debe haber error al salir del with