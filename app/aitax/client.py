import logging

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

class AitaxClientError(Exception):
    """Error base del cliente de AITAX."""
    pass


class AitaxRetryableError(AitaxClientError):
    """
    Errores transitorios que justifican reintento (5xx, 429, timeouts).
    Tenacity los reintenta automáticamente.
    """
    pass


class AitaxDefinitiveError(AitaxClientError):
    """
    Errores definitivos que NO se reintentan (404, 401, 403, 400).
    El worker los atrapará y marcará el evento como `failed`.
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# ─── Cliente principal ────────────────────────────────────────────────

class AitaxClient:
    """
    Cliente síncrono para los endpoints internos de AITAX.
    """

    DEFAULT_TIMEOUT_CONNECT = 10.0
    DEFAULT_TIMEOUT_WRITE = 10.0

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        read_timeout: float | None = None,
    ):
        self._base_url = (base_url or settings.AITAX_INTERNAL_API_URL).rstrip("/")
        self._token = token or settings.AITAX_INTERNAL_API_TOKEN
        self._read_timeout = read_timeout or float(settings.AITAX_INTERNAL_API_TIMEOUT)

        if not self._token:
            raise ValueError(
                "AITAX_INTERNAL_API_TOKEN no está configurado. Revisa tu archivo .env."
            )

        self._client = httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                connect=self.DEFAULT_TIMEOUT_CONNECT,
                read=self._read_timeout,
                write=self.DEFAULT_TIMEOUT_WRITE,
                pool=10.0,
            ),
        )

    # ─── API pública ──────────────────────────────────────────────────

    def notify_extraction_completed(
        self,
        rfc: str,
        extraction_id: str,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> dict:
        """
        POST /api/internal/sync/extraction-completed/

        Notifica a AITAX que una extracción de Syntage terminó.
        AITAX correrá sync_all(company) síncronamente y devolverá el resumen.

        Args:
            rfc: RFC de la empresa (ej. "BRP0001RP").
            extraction_id: ID de la extracción de Syntage (para audit).
            start_year: opcional, año inicial para el sync de invoices.
            end_year: opcional, año final.

        Returns:
            dict con:
                {
                    "status": "ok",
                    "rfc": "...",
                    "extraction_id": "...",
                    "company_id": "...",
                    "results": {...}
                }
        """
        body = {
            "rfc": rfc,
            "extraction_id": extraction_id,
        }
        if start_year is not None:
            body["start_year"] = start_year
        if end_year is not None:
            body["end_year"] = end_year

        return self._post("/api/internal/sync/extraction-completed/", body)

    def notify_extraction_status_update(
        self,
        extraction_id: str,
        extractor: str,
        status: str,
        rfc: str,
    ) -> dict:
        """
        POST /api/internal/sync/extraction-status-update/

        Notifica a AITAX el cambio de estado de una extracción de tipo
        tax_compliance u tax_status.

        Args:
            extraction_id: UUID de la extracción de Syntage.
            extractor: "tax_compliance" | "tax_status".
            status: estado actual ("finished" | "error" | "stopped").
            rfc: RFC del contribuyente.

        Returns:
            dict con la respuesta de AITAX.
        """
        body = {
            "extraction_id": extraction_id,
            "extractor": extractor,
            "status": status,
            "rfc": rfc,
        }
        return self._post("/api/internal/sync/extraction-status-update/", body)

    def close(self):
        """Cierra el cliente HTTP. Llamar al terminar de usar."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ─── Implementación interna ───────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(AitaxRetryableError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _post(self, path: str, body: dict) -> dict:
        """
        POST con reintentos automáticos para errores transitorios.

        Lógica:
        - Timeout/ConnectError → AitaxRetryableError → retry
        - 5xx (server error)   → AitaxRetryableError → retry
        - 4xx                  → AitaxDefinitiveError → NO retry
        - 200                  → devuelve JSON
        """
        try:
            response = self._client.post(path, json=body)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise AitaxRetryableError(
                f"Error de red al llamar a AITAX {path}: {exc}"
            ) from exc

        if response.status_code == 200:
            return response.json()

        if response.status_code >= 500:
            raise AitaxRetryableError(
                f"AITAX devolvió {response.status_code} en {path}: "
                f"{response.text[:300]}"
            )

        # 4xx: error definitivo, no reintentamos.
        raise AitaxDefinitiveError(
            f"AITAX devolvió {response.status_code} en {path}: "
            f"{response.text[:300]}",
            status_code=response.status_code,
        )