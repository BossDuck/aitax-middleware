"""
Cliente HTTP para la API de Syntage.

Responsabilidades:
- Hacer GET a los endpoints individuales de Invoice, LineItem, Payment.
- Manejar autenticación con X-API-Key.
- Reintentar automáticamente fallos transitorios (5xx, 429, timeouts).
- NO reintentar errores definitivos (4xx que no sean 429).

Uso típico:
    client = SyntageClient()
    invoice = client.fetch_invoice(UUID("abc-123-..."))
"""

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


class SyntageRetryableError(SyntageClientError):
    """
    Errores transitorios que justifican reintento (5xx, 429, timeouts).
    Tenacity los reintenta automáticamente.
    """


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

    Usa httpx síncrono porque el worker Celery (que es quien lo va a
    invocar en el Bloque 5) corre en threads síncronos.
    """

    # Mismos timeouts que usa AITAX en su syntage_client.py: 10s connect, 180s read.
    DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: httpx.Timeout | None = None,
    ):
        # Permitimos sobreescribir en tests; por defecto vienen del settings.
        self._base_url = (base_url or settings.SYNTAGE_API_URL).rstrip("/")
        self._api_key = api_key or settings.SYNTAGE_API_KEY
        self._timeout = timeout or self.DEFAULT_TIMEOUT

        if not self._api_key:
            raise ValueError(
                "SYNTAGE_API_KEY no está configurado. "
                "Revisa tu archivo .env."
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

    def fetch_invoice(self, invoice_id: UUID) -> dict:
        """
        GET /invoices/{id}

        Devuelve la factura completa con todos sus campos
        (estructura coincide con la usada por sync_invoices en AITAX).
        """
        return self._get(f"/invoices/{invoice_id}")

    def fetch_line_item(self, line_item_id: UUID) -> dict:
        """
        GET /line-items/{id}

        Devuelve el line-item con la factura padre EXPANDIDA en el
        campo `invoice` (estructura coincide con la usada por
        sync_concepts en AITAX, que espera c["invoice"]["id"] e
        c["invoice"]["issuedAt"]).
        """
        return self._get(f"/line-items/{line_item_id}")

    def fetch_payment(self, payment_id: UUID) -> dict:
        """
        GET /invoices/payments/{id}

        Devuelve el pago. Nota: el schema documentado solo trae
        `createdAt` (no `date`), así que sync_payments usará createdAt.
        """
        return self._get(f"/invoices/payments/{payment_id}")

    def close(self):
        """Cierra el cliente HTTP. Llamar al terminar de usar."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ─── Implementación interna ───────────────────────────────────────

    @retry(
        # Solo reintenta excepciones marcadas como retryable.
        retry=retry_if_exception_type(SyntageRetryableError),
        # 3 intentos en total.
        stop=stop_after_attempt(3),
        # Backoff exponencial: 1s, 2s, 4s entre intentos (con tope de 10s).
        wait=wait_exponential(multiplier=1, min=1, max=10),
        # Log cada vez que reintentamos.
        before_sleep=before_sleep_log(logger, logging.WARNING),
        # Re-lanza la excepción original tras agotar reintentos.
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
            # Errores de red: reintentables.
            raise SyntageRetryableError(
                f"Error de red al llamar a Syntage {path}: {exc}"
            ) from exc

        if response.status_code == 200:
            return response.json()

        # Diferenciamos transitorios vs definitivos para tenacity.
        if response.status_code >= 500 or response.status_code == 429:
            raise SyntageRetryableError(
                f"Syntage devolvió {response.status_code} en {path}: "
                f"{response.text[:200]}"
            )

        # 4xx que no sea 429: error definitivo, no reintentamos.
        raise SyntageDefinitiveError(
            f"Syntage devolvió {response.status_code} en {path}: "
            f"{response.text[:200]}",
            status_code=response.status_code,
        )