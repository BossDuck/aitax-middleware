"""
Modelos SQLAlchemy del microservicio.

"""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SyntageWebhookEvent(Base):

    __tablename__ = "syntage_webhook_events"

    # ── Identidad ──────────────────────────────────────
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    syntage_event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        unique=True,
        nullable=False,
    )

    # ── Datos del evento ───────────────────────────────
    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Ej: 'invoice.created'",
    )

    # source y resource son IRIs (referencias internas de Syntage)
    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Payload completo recibido (JSON crudo) — sirve para debugging
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Headers HTTP relevantes recibidos (firma, timestamps, etc.)
    headers: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # ── Estado de procesamiento ────────────────────────
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=EventStatus.PENDING.value,
        index=True,
    )

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Timestamps ─────────────────────────────────────
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="Cuándo entró el webhook a nuestro endpoint",
    )

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Cuándo terminó el procesamiento (exitoso o fallido)",
    )

    syntage_timestamp: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
    )

    __table_args__ = (
        Index("ix_event_status_received", "status", "received_at"),
        Index("ix_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<SyntageWebhookEvent "
            f"id={self.id} "
            f"type={self.event_type} "
            f"status={self.status}>"
        )