import logging
from uuid import UUID

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

from app.config import settings


logger = logging.getLogger(__name__)


# ─── Excepciones del cliente ──────────────────────────────────────────

class SyntageClientError(Exception):
    """Error base del cliente de Syntage."""
    pass


class SyntageRetryableError(SyntageClientError):
    """
    Errores transitorios que justifican reintento (5xx, 429, timeouts).
    Tenacity los reintenta automáticamente.
    """
    pass


class SyntageDefinitiveError(SyntageClientError):
    """
    Errores definitivos que NO se reintentan (404, 401, 403).
    El worker los atrapará y marcará el evento como `failed`.
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# ─── Cliente principal ────────────────────────────────────────────────

class SyntageClient:
    """
    Cliente síncrono para la API de Syntage.

    Usa httpx síncrono porque el worker Celery (que es quien lo invoca)
    corre en threads síncronos.
    """

    # Mismos timeouts que usa AITAX en su syntage_client.py: 10s connect, 180s read.
    DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        self._base_url = (base_url or settings.SYNTAGE_API_URL).rstrip("/")
        self._api_key = api_key or settings.SYNTAGE_API_KEY
        self._timeout = timeout or self.DEFAULT_TIMEOUT

        if not self._api_key:
            raise ValueError(
                "SYNTAGE_API_KEY no está configurado. Revisa tu archivo .env."
            )

        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "X-API-Key": self._api_key,
                "Accept": "application/ld+json",
            },
            timeout=self._timeout,
        )

    # ─── API pública ──────────────────────────────────────────────────

    def fetch_extraction(self, extraction_id: UUID) -> dict:
        """
        GET /extractions/{id}

        Devuelve la extracción con todos sus campos:
        - status: "pending" | "running" | "finished" | "failed" | ...
        - extractor: "invoice" | "monthly_tax_return" | ...
        - taxpayer.id: RFC del contribuyente
        - createdDataPoints, updatedDataPoints
        - finishedAt, errorCode
        """
        return self._get(f"/extractions/{extraction_id}")

    def close(self):
        """Cierra el cliente HTTP. Llamar al terminar de usar."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ─── Implementación interna ───────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(SyntageRetryableError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _get(self, path: str) -> dict:
        """
        GET con reintentos automáticos para errores transitorios.

        Lógica de errores:
        - Timeout/ConnectError → SyntageRetryableError → retry
        - 5xx (server error)   → SyntageRetryableError → retry
        - 429 (rate limit)     → SyntageRetryableError → retry
        - 4xx (excepto 429)    → SyntageDefinitiveError → NO retry
        - 200                  → devuelve JSON
        """
        try:
            response = self._client.get(path)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise SyntageRetryableError(
                f"Error de red al llamar a Syntage {path}: {exc}"
            ) from exc

        if response.status_code == 200:
            return response.json()

        if response.status_code >= 500 or response.status_code == 429:
            raise SyntageRetryableError(
                f"Syntage devolvió {response.status_code} en {path}: "
                f"{response.text[:200]}"
            )

        raise SyntageDefinitiveError(
            f"Syntage devolvió {response.status_code} en {path}: "
            f"{response.text[:200]}",
            status_code=response.status_code,
        )