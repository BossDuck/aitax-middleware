from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SyntageWebhookPayload(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )

    id: UUID = Field(..., description="UUID único del evento")
    type: str = Field(..., description="Tipo de evento, ej. invoice.created")
    source: Optional[str] = Field(default=None, description="Recurso origen")

    created_at: Optional[str] = Field(default=None, alias="createdAt")
    updated_at: Optional[str] = Field(default=None, alias="updatedAt")