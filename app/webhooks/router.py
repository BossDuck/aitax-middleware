import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import EventStatus, SyntageWebhookEvent
from app.tasks import process_webhook_event
from app.webhooks.schemas import SyntageWebhookPayload
from app.webhooks.signature import verify_syntage_signature


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post(
    "/syntage/",
    status_code=status.HTTP_200_OK,
    summary="Recibe webhooks de Syntage",
)
async def receive_syntage_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    x_satws_signature: Annotated[str | None, Header()] = None,
):
    """
    Endpoint público que Syntage llama cada vez que ocurre un evento.
    """
    raw_body = await request.body()

    is_valid, error, syntage_timestamp = verify_syntage_signature(
        raw_body=raw_body,
        signature_header=x_satws_signature,
        signing_secret=settings.SYNTAGE_WEBHOOK_SIGNING_SECRET,
        tolerance_seconds=settings.SYNTAGE_WEBHOOK_TOLERANCE,
    )

    if not is_valid:
        logger.warning(
            "Webhook de Syntage rechazado por firma inválida: %s",
            error,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid signature",
        )

    try:
        payload_dict = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning("Webhook con JSON inválido: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON body",
        )

    try:
        payload = SyntageWebhookPayload.model_validate(payload_dict)
    except ValueError as exc:
        logger.warning("Webhook con estructura inválida: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Payload missing required fields (id, type)",
        )

    # ─── Detectar duplicados ──────────────────────────────────────────
    existing = db.execute(
        select(SyntageWebhookEvent).where(
            SyntageWebhookEvent.syntage_event_id == payload.id
        )
    ).scalar_one_or_none()

    if existing is not None:
        logger.info(
            "Webhook duplicado recibido (event_id=%s, type=%s). Ignorado.",
            payload.id,
            payload.type,
        )
        return {
            "status": "duplicate",
            "event_id": str(payload.id),
        }

    # ─── Guardar evento como `pending` ────────────────────────────────
    headers_dict = {k.lower(): v for k, v in request.headers.items()}

    event = SyntageWebhookEvent(
        syntage_event_id=payload.id,
        event_type=payload.type,
        source=payload.source,
        resource=None,
        payload=payload_dict,
        headers=headers_dict,
        status=EventStatus.PENDING.value,
        attempts=0,
        syntage_timestamp=syntage_timestamp,
    )

    db.add(event)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Error guardando webhook en DB: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error",
        )

    db.refresh(event)

    logger.info(
        "Webhook guardado (event_id=%s, type=%s, internal_id=%s)",
        payload.id,
        payload.type,
        event.id,
    )

    # ─── Encolar en Celery (Bloque 5) ─────────────────────────────────
    process_webhook_event.delay(str(event.id))

    return {
        "status": "queued",
        "event_id": str(payload.id),
        "internal_id": str(event.id),
    }