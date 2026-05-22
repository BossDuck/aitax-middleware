"""
app/syntage/data_client.py

Cliente HTTP para obtener datos paginados de Syntage (facturas, conceptos, pagos).
Port de apps/sat/syntage_client.py de AITAX, adaptado para httpx y sin Django.

Distinto de app/syntage/client.py, que consulta el estado de una extracción
individual. Este módulo hace la paginación cursor-based para el sync masivo.
"""

from typing import Iterator

import httpx

from app.config import settings


SYNTAGE_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)


def _headers(extra: dict | None = None) -> dict:
    h = {
        "X-API-Key": settings.SYNTAGE_API_KEY,
        "Accept": "application/ld+json",
    }
    if extra:
        h.update(extra)
    return h


def _absolute_url(url_or_path: str) -> str:
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        return url_or_path
    base = settings.SYNTAGE_API_URL.rstrip("/")
    return f"{base}/{url_or_path.lstrip('/')}"


def _iter_hydra_cursor(
    url: str,
    params: dict | None = None,
    extra_headers: dict | None = None,
    items_per_page: int = 1000,
) -> Iterator[list[dict]]:
    """
    Recorre endpoints Hydra cursor-based usando hydra:view -> hydra:next.

    - El primer request envía los params originales.
    - Los siguientes usan exactamente la URL de hydra:next (ya lleva el cursor).
    """
    headers = _headers(extra_headers)
    base_params: dict | None = {"itemsPerPage": items_per_page}
    if params:
        base_params.update(params)

    seen_next_urls: set[str] = set()

    with httpx.Client(headers=headers, timeout=SYNTAGE_TIMEOUT) as client:
        next_url: str | None = url
        current_params = base_params

        while next_url:
            r = client.get(next_url, params=current_params)
            r.raise_for_status()
            data = r.json()

            batch = data.get("hydra:member", [])
            if batch:
                yield batch

            raw_next = (data.get("hydra:view") or {}).get("hydra:next")
            if not raw_next:
                break

            next_url = _absolute_url(raw_next)

            if next_url in seen_next_urls:
                raise RuntimeError(f"Bucle de paginación detectado en Syntage: {next_url}")

            seen_next_urls.add(next_url)
            # Después del primer request NO reenviar params: hydra:next ya trae el cursor.
            current_params = None


def iter_invoices(rfc: str, filters: dict | None = None) -> Iterator[list[dict]]:
    base = settings.SYNTAGE_API_URL.rstrip("/")
    url = f"{base}/taxpayers/{rfc}/invoices"
    params: dict = {"order[issuedAt]": "desc"}
    if filters:
        params.update(filters)
    yield from _iter_hydra_cursor(url, params=params, items_per_page=1000)


def iter_concepts(rfc: str, filters: dict | None = None) -> Iterator[list[dict]]:
    base = settings.SYNTAGE_API_URL.rstrip("/")
    url = f"{base}/taxpayers/{rfc}/invoices/line-items"
    yield from _iter_hydra_cursor(
        url,
        params=filters,
        extra_headers={"X-Pagination-Style": "cursor"},
        items_per_page=1000,
    )


def iter_payments(rfc: str, params: dict | None = None) -> Iterator[list[dict]]:
    base = settings.SYNTAGE_API_URL.rstrip("/")
    url = f"{base}/taxpayers/{rfc}/invoices/payments"
    yield from _iter_hydra_cursor(url, params=params, items_per_page=1000)
