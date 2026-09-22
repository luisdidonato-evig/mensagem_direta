from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.domain import (
    ActorRole,
    AttendanceStatus,
    ClosureReason,
    ContactStage,
    DeliveryStatus,
    EventType,
    MessageDirection,
)


def new_id() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Attendance(Base):
    __tablename__ = "attendances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    queue_id: Mapped[str] = mapped_column(String(80), default="central", index=True)
    team_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus, native_enum=False),
        default=AttendanceStatus.AGUARDANDO,
        index=True,
    )
    assignee_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="attendance", cascade="all, delete-orphan"
    )
    events: Mapped[list["AttendanceEvent"]] = relationship(
        back_populates="attendance", cascade="all, delete-orphan"
    )
    notes: Mapped[list["InternalNote"]] = relationship(
        back_populates="attendance", cascade="all, delete-orphan"
    )
    closure: Mapped["AttendanceClosure"] = relationship(
        back_populates="attendance", cascade="all, delete-orphan", uselist=False
    )


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stage: Mapped[ContactStage] = mapped_column(
        Enum(ContactStage, native_enum=False),
        default=ContactStage.NAO_CLASSIFICADO,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class ContactTag(Base):
    __tablename__ = "contact_tags"

    contact_id: Mapped[str] = mapped_column(
        ForeignKey("contacts.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(40), primary_key=True)


class AttendanceTag(Base):
    __tablename__ = "attendance_tags"

    attendance_id: Mapped[str] = mapped_column(
        ForeignKey("attendances.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(40), primary_key=True)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    role: Mapped[ActorRole] = mapped_column(Enum(ActorRole, native_enum=False))
    password_hash: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class QuickReply(Base):
    __tablename__ = "quick_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ContactAuditEvent(Base):
    __tablename__ = "contact_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(String(120), index=True)
    actor_id: Mapped[str] = mapped_column(String(120), index=True)
    changed_fields: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attendance_id: Mapped[str] = mapped_column(
        ForeignKey("attendances.id"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(
        String(160), unique=True, nullable=True
    )
    client_message_id: Mapped[str | None] = mapped_column(
        String(160), unique=True, nullable=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(MessageDirection, native_enum=False)
    )
    content: Mapped[str] = mapped_column(Text)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    attendance: Mapped[Attendance] = relationship(back_populates="messages")


class AttendanceEvent(Base):
    __tablename__ = "attendance_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attendance_id: Mapped[str] = mapped_column(
        ForeignKey("attendances.id"), index=True
    )
    type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False))
    actor_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    from_status: Mapped[AttendanceStatus | None] = mapped_column(
        Enum(AttendanceStatus, native_enum=False), nullable=True
    )
    to_status: Mapped[AttendanceStatus | None] = mapped_column(
        Enum(AttendanceStatus, native_enum=False), nullable=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    attendance: Mapped[Attendance] = relationship(back_populates="events")


class InternalNote(Base):
    __tablename__ = "internal_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attendance_id: Mapped[str] = mapped_column(
        ForeignKey("attendances.id"), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(120), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    attendance: Mapped[Attendance] = relationship(back_populates="notes")


class AttendanceClosure(Base):
    __tablename__ = "attendance_closures"

    attendance_id: Mapped[str] = mapped_column(
        ForeignKey("attendances.id"), primary_key=True
    )
    reason: Mapped[ClosureReason] = mapped_column(
        Enum(ClosureReason, native_enum=False)
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(120), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    attendance: Mapped[Attendance] = relationship(back_populates="closure")


class IntegrationEvent(Base):
    __tablename__ = "integration_events"

    external_event_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    attendance_id: Mapped[str] = mapped_column(String(36), index=True)
    message_id: Mapped[str] = mapped_column(String(36))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    message_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    topic: Mapped[str] = mapped_column(String(120), default="whatsapp.send")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
