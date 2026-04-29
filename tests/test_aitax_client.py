import httpx
import pytest
import respx

from app.aitax.client import (
    AitaxClient,
    AitaxDefinitiveError,
    AitaxRetryableError,
)


BASE_URL = "http://localhost:8000"
TEST_TOKEN = "test_token_xyz_123"
ENDPOINT_PATH = "/api/internal/sync/extraction-completed/"


@pytest.fixture
def client():
    c = AitaxClient(base_url=BASE_URL, token=TEST_TOKEN, read_timeout=5.0)
    yield c
    c.close()


# ─── Happy path ───────────────────────────────────────────────────────

@respx.mock
def test_notify_extraction_completed_returns_payload(client):
    expected = {
        "status": "ok",
        "rfc": "GIN200414CC8",
        "extraction_id": "abc-123",
        "company_id": "32",
        "results": {
            "invoices": {"2024": {"processed": 100, "upserted": 100}},
            "concepts": {"processed": 200, "upserted": 200},
            "payments": {"processed": 5, "upserted": 5},
        },
    }

    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.notify_extraction_completed(
        rfc="GIN200414CC8",
        extraction_id="abc-123",
    )

    assert result == expected
    assert route.called


@respx.mock
def test_sends_correct_body_with_optional_params(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    client.notify_extraction_completed(
        rfc="ABC123",
        extraction_id="xyz",
        start_year=2020,
        end_year=2024,
    )

    request = route.calls.last.request
    body = request.read().decode()
    assert '"rfc":"ABC123"' in body or '"rfc": "ABC123"' in body
    assert '"start_year":2020' in body or '"start_year": 2020' in body


@respx.mock
def test_omits_optional_params_when_none(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    client.notify_extraction_completed(
        rfc="ABC123",
        extraction_id="xyz",
    )

    request = route.calls.last.request
    body = request.read().decode()
    assert "start_year" not in body
    assert "end_year" not in body


@respx.mock
def test_sends_authorization_bearer_header(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    client.notify_extraction_completed(rfc="ABC123", extraction_id="xyz")

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {TEST_TOKEN}"
    assert request.headers["content-type"] == "application/json"


# ─── Errores definitivos (no retry) ───────────────────────────────────

@respx.mock
def test_404_raises_definitive_error_no_retry(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(404, text='{"error": "Company not found"}')
    )

    with pytest.raises(AitaxDefinitiveError) as exc_info:
        client.notify_extraction_completed(rfc="NOEXISTE", extraction_id="xyz")

    assert exc_info.value.status_code == 404
    assert route.call_count == 1


@respx.mock
def test_401_raises_definitive_error_no_retry(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(401, text='{"error": "Invalid token"}')
    )

    with pytest.raises(AitaxDefinitiveError) as exc_info:
        client.notify_extraction_completed(rfc="ABC", extraction_id="xyz")

    assert exc_info.value.status_code == 401
    assert route.call_count == 1


@respx.mock
def test_400_raises_definitive_error_no_retry(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(400, text='{"error": "Missing field"}')
    )

    with pytest.raises(AitaxDefinitiveError):
        client.notify_extraction_completed(rfc="ABC", extraction_id="xyz")

    assert route.call_count == 1


# ─── Errores transitorios (retry) ─────────────────────────────────────

@respx.mock
def test_500_retries_three_times_then_raises(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    with pytest.raises(AitaxRetryableError):
        client.notify_extraction_completed(rfc="ABC", extraction_id="xyz")

    assert route.call_count == 3


@respx.mock
def test_500_then_200_succeeds_after_retry(client):
    expected = {"status": "ok"}

    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        side_effect=[
            httpx.Response(500, text="Temporarily down"),
            httpx.Response(200, json=expected),
        ]
    )

    result = client.notify_extraction_completed(rfc="ABC", extraction_id="xyz")

    assert result == expected
    assert route.call_count == 2


@respx.mock
def test_timeout_retries(client):
    route = respx.post(f"{BASE_URL}{ENDPOINT_PATH}").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    with pytest.raises(AitaxRetryableError):
        client.notify_extraction_completed(rfc="ABC", extraction_id="xyz")

    assert route.call_count == 3


# ─── Configuración del cliente ────────────────────────────────────────

def test_missing_token_raises_value_error(monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "AITAX_INTERNAL_API_TOKEN", "")

    with pytest.raises(ValueError, match="AITAX_INTERNAL_API_TOKEN"):
        # No pasamos token; debe leer del settings (vacío).
        AitaxClient(base_url=BASE_URL)


def test_context_manager_closes_client():
    with AitaxClient(base_url=BASE_URL, token=TEST_TOKEN) as c:
        assert c is not None