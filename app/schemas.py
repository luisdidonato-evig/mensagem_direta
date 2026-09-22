from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import (
    ActorRole,
    AttendanceStatus,
    ClosureReason,
    ContactStage,
    DeliveryStatus,
    EventType,
    MessageDirection,
)


class Actor(BaseModel):
    id: str
    role: ActorRole


class LoginRequest(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    actor: Actor


class AgentCreate(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    role: ActorRole
    password: str = Field(min_length=8, max_length=200)


class AgentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    role: ActorRole
    active: bool
    created_at: datetime


class ContactUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=20)
    stage: ContactStage | None = None


class ContactStageUpdate(BaseModel):
    stage: ContactStage


class ContactRead(BaseModel):
    id: str
    display_name: str | None
    phone: str | None
    tags: list[str]
    stage: ContactStage
    created_at: datetime
    updated_at: datetime
    last_attendance_id: str | None = None
    last_attendance_status: AttendanceStatus | None = None


class InboundMessageCreate(BaseModel):
    external_event_id: str = Field(min_length=1, max_length=160)
    external_message_id: str = Field(min_length=1, max_length=160)
    contact_id: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=10_000)


class ClaimRequest(BaseModel):
    expected_version: int = Field(ge=1)


class TransferRequest(BaseModel):
    target_actor_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)


class StatusChangeRequest(BaseModel):
    status: AttendanceStatus
    expected_version: int = Field(ge=1)
    closure_reason: ClosureReason | None = None
    closure_note: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_closure(self) -> "StatusChangeRequest":
        if self.status == AttendanceStatus.ENCERRADO and self.closure_reason is None:
            raise ValueError("closure_reason é obrigatório ao encerrar")
        if self.status != AttendanceStatus.ENCERRADO and (
            self.closure_reason is not None or self.closure_note is not None
        ):
            raise ValueError("dados de encerramento só podem ser enviados ao encerrar")
        return self


class OutboundMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    client_message_id: str = Field(min_length=1, max_length=160)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attendance_id: str
    external_id: str | None
    client_message_id: str | None
    direction: MessageDirection
    content: str
    delivery_status: DeliveryStatus
    created_at: datetime


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attendance_id: str
    type: EventType
    actor_id: str | None
    from_status: AttendanceStatus | None
    to_status: AttendanceStatus | None
    details: dict
    created_at: datetime


class InternalNoteCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5_000)


class InternalNoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attendance_id: str
    actor_id: str
    content: str
    created_at: datetime


class ClosureRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reason: ClosureReason
    note: str | None
    actor_id: str
    created_at: datetime


class AttendanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    contact_id: str
    queue_id: str
    team_id: str | None
    status: AttendanceStatus
    assignee_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    tags: list[str] = Field(default_factory=list)
    stale: bool = False


class AttendanceTagsUpdate(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)


class QuickReplyCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=2_000)


class QuickReplyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    content: str
    created_by: str
    created_at: datetime


class OutboxItemRead(BaseModel):
    id: str
    message_id: str
    attendance_id: str
    contact_id: str
    content: str
    attempts: int
    max_attempts: int
    delivery_status: DeliveryStatus
    created_at: datetime


class AttendanceSummary(AttendanceRead):
    contact_display_name: str | None = None
    contact_phone: str | None = None
    last_message: str | None = None


class AttendanceDetail(AttendanceRead):
    messages: list[MessageRead]
    events: list[EventRead]
    notes: list[InternalNoteRead]
    closure: ClosureRead | None = None


class MetricsSummary(BaseModel):
    status_counts: dict[AttendanceStatus, int]
    total_open: int
    unassigned: int
    mine: int
    closed_today: int
    average_first_response_seconds: float | None
    average_resolution_seconds: float | None


class InboundResult(BaseModel):
    attendance: AttendanceRead
    message: MessageRead
    duplicate: bool
