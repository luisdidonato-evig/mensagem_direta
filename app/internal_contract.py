"""Contrato neutro da entrada de serviço. IDs do canal permanecem opacos."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class GatewayIds(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=36)
    channel_account_id: str = Field(min_length=1, max_length=160)

    @field_validator("tenant_id", "channel_account_id")
    @classmethod
    def valid_uuid(cls, value: str) -> str:
        return str(UUID(value))


class InternalSender(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, max_length=160)


class InternalMessage(BaseModel):
    type: Literal["text", "choice"]
    text: str | None = Field(default=None, max_length=10_000)
    choice_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def require_content(self) -> "InternalMessage":
        if self.type == "text" and not self.text:
            raise ValueError("text obrigatório")
        if self.type == "choice" and not self.choice_id:
            raise ValueError("choice_id obrigatório")
        return self


class InternalInbound(GatewayIds):
    event_id: str = Field(min_length=1, max_length=160)
    channel: Literal["whatsapp"]
    conversation_id: str = Field(min_length=1, max_length=160)
    message_id: str = Field(min_length=1, max_length=160)
    sender: InternalSender
    message: InternalMessage
    occurred_at: datetime

    @field_validator("conversation_id")
    @classmethod
    def valid_conversation_uuid(cls, value: str) -> str:
        return str(UUID(value))


class InternalStatus(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=36)
    channel_account_id: str | None = Field(default=None, max_length=160)
    message_id: str | None = Field(default=None, max_length=160)
    idempotency_key: str | None = Field(default=None, max_length=160)
    status: Literal["QUEUED", "SENT", "DELIVERED", "READ", "FAILED"]
    occurred_at: datetime

    @field_validator("tenant_id", "channel_account_id")
    @classmethod
    def valid_uuid(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value else value

    @model_validator(mode="after")
    def require_reference(self) -> "InternalStatus":
        if not self.message_id and not self.idempotency_key:
            raise ValueError("message_id ou idempotency_key obrigatório")
        return self


class InternalHandoff(GatewayIds):
    event_id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    sender_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=10_000)

    @field_validator("conversation_id")
    @classmethod
    def valid_conversation_uuid(cls, value: str) -> str:
        return str(UUID(value))
