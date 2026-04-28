import hmac
import hashlib
import time
from typing import Optional


def verify_syntage_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    signing_secret: str,
    tolerance_seconds: int = 300,
) -> tuple[bool, Optional[str], Optional[int]]:
    # ─── Validaciones tempranas ───────────────────────────────────────
    if not signature_header:
        return False, "Missing signature header", None

    if not signing_secret:
        return False, "Missing signing secret (server misconfiguration)", None

    if not raw_body:
        return False, "Empty request body", None

    # ─── Parseo del header ────────────────────────────────────────────
    parts = {}
    for chunk in signature_header.split(","):
        if "=" not in chunk:
            return False, f"Malformed signature header: '{signature_header}'", None
        key, _, value = chunk.partition("=")
        parts[key.strip()] = value.strip()

    timestamp_str = parts.get("t")
    received_signature = parts.get("s")

    if not timestamp_str or not received_signature:
        return (
            False,
            "Signature header missing 't' or 's' component",
            None,
        )

    # ─── Parseo del timestamp ─────────────────────────────────────────
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False, f"Invalid timestamp value: '{timestamp_str}'", None

    # ─── Tolerancia anti-replay ───────────────────────────────────────
    now = int(time.time())
    if abs(now - timestamp) > tolerance_seconds:
        return (
            False,
            f"Timestamp outside tolerance window "
            f"(diff={abs(now - timestamp)}s, max={tolerance_seconds}s)",
            timestamp,
        )

    # ─── Cálculo del HMAC esperado ────────────────────────────────────
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body

    expected_signature = hmac.new(
        key=signing_secret.encode("utf-8"),
        msg=signed_payload,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # ─── Comparación constant-time ────────────────────────────────────
    if not hmac.compare_digest(expected_signature, received_signature):
        return False, "Signature mismatch", timestamp

    return True, None, timestamp