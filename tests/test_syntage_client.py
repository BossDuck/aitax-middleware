"""
Tests del cliente Syntage usando respx para mockear HTTP.
"""

from uuid import uuid4

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
    c = SyntageClient(base_url=BASE_URL, api_key=TEST_API_KEY)
    yield c
    c.close()


# ─── Happy path ───────────────────────────────────────────────────────

@respx.mock
def test_fetch_extraction_returns_payload(client):
    extraction_id = uuid4()
    expected = {
        "id": str(extraction_id),
        "status": "finished",
        "extractor": "invoice",
        "taxpayer": {"id": "PEIC211118IS0", "name": "Pedro"},
    }

    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(200, json=expected)
    )

    result = client.fetch_extraction(extraction_id)

    assert result == expected
    assert route.called


@respx.mock
def test_sends_x_api_key_header(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(200, json={"id": str(extraction_id)})
    )

    client.fetch_extraction(extraction_id)

    request = route.calls.last.request
    assert request.headers["x-api-key"] == TEST_API_KEY
    assert request.headers["accept"] == "application/ld+json"


# ─── Errores definitivos (no retry) ───────────────────────────────────

@respx.mock
def test_404_raises_definitive_error_no_retry(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(SyntageDefinitiveError) as exc_info:
        client.fetch_extraction(extraction_id)

    assert exc_info.value.status_code == 404
    assert route.call_count == 1


@respx.mock
def test_401_raises_definitive_error_no_retry(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )

    with pytest.raises(SyntageDefinitiveError):
        client.fetch_extraction(extraction_id)

    assert route.call_count == 1


# ─── Errores transitorios (retry) ─────────────────────────────────────

@respx.mock
def test_500_retries_three_times_then_raises(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_extraction(extraction_id)

    assert route.call_count == 3


@respx.mock
def test_429_retries(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        return_value=httpx.Response(429, text="Too Many Requests")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_extraction(extraction_id)

    assert route.call_count == 3


@respx.mock
def test_500_then_200_succeeds_after_retry(client):
    extraction_id = uuid4()
    expected = {"id": str(extraction_id), "status": "finished"}

    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        side_effect=[
            httpx.Response(500, text="Temporarily down"),
            httpx.Response(200, json=expected),
        ]
    )

    result = client.fetch_extraction(extraction_id)

    assert result == expected
    assert route.call_count == 2


@respx.mock
def test_timeout_retries(client):
    extraction_id = uuid4()
    route = respx.get(f"{BASE_URL}/extractions/{extraction_id}").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    with pytest.raises(SyntageRetryableError):
        client.fetch_extraction(extraction_id)

    assert route.call_count == 3


# ─── Configuración del cliente ────────────────────────────────────────

def test_missing_api_key_raises_value_error():
    with pytest.raises(ValueError, match="SYNTAGE_API_KEY"):
        SyntageClient(base_url=BASE_URL, api_key="")


def test_context_manager_closes_client():
    with SyntageClient(base_url=BASE_URL, api_key=TEST_API_KEY) as c:
        assert c is not None