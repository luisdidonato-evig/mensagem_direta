"""Worker de projeção analítica.

Consome eventos do outbox de analytics (ex.: conversation.closed) e gera uma
projeção limpa em ConversationInsight, sem depender diretamente das tabelas
operacionais no momento da leitura pelo estrategista/IA.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import EventType, MessageDirection, SenderType
from app.models import (
    AnalyticsOutbox,
    Attendance,
    AttendanceEvent,
    AttendanceTag,
    Contact,
    ConversationInsight,
    Message,
    now_utc,
)

CONVERSATION_CLOSED_TOPIC = "conversation.closed"


def enqueue_conversation_closed(session: Session, attendance_id: str) -> None:
    session.add(
        AnalyticsOutbox(
            topic=CONVERSATION_CLOSED_TOPIC,
            attendance_id=attendance_id,
            payload={"attendance_id": attendance_id},
        )
    )


def _build_insight(session: Session, attendance: Attendance) -> ConversationInsight:
    messages = list(
        session.scalars(
            select(Message)
            .where(Message.attendance_id == attendance.id)
            .order_by(Message.created_at)
        )
    )
    transfer_count = len(
        list(
            session.scalars(
                select(AttendanceEvent.id).where(
                    AttendanceEvent.attendance_id == attendance.id,
                    AttendanceEvent.type == EventType.TRANSFERIDO,
                )
            )
        )
    )
    tags = list(
        session.scalars(
            select(AttendanceTag.name).where(
                AttendanceTag.attendance_id == attendance.id
            )
        )
    )
    contact = session.get(Contact, attendance.contact_id)

    first_inbound = next(
        (m for m in messages if m.direction == MessageDirection.ENTRADA), None
    )
    first_outbound = next(
        (m for m in messages if m.direction == MessageDirection.SAIDA), None
    )
    first_response_seconds = None
    if first_inbound is not None and first_outbound is not None:
        delta = first_outbound.created_at - first_inbound.created_at
        first_response_seconds = max(int(delta.total_seconds()), 0)

    closed_at = attendance.closure.created_at if attendance.closure else None
    duration_seconds = None
    if closed_at is not None:
        duration_seconds = max(int((closed_at - attendance.created_at).total_seconds()), 0)

    counts = {
        SenderType.CLIENTE: 0,
        SenderType.ATENDENTE: 0,
        SenderType.IA: 0,
        SenderType.BOT: 0,
    }
    for message in messages:
        if message.sender_type in counts:
            counts[message.sender_type] += 1

    return ConversationInsight(
        attendance_id=attendance.id,
        company_id=attendance.company_id,
        group_id=attendance.group_id,
        final_assignee_id=attendance.assignee_id,
        opened_at=attendance.created_at,
        closed_at=closed_at,
        duration_seconds=duration_seconds,
        first_response_seconds=first_response_seconds,
        transfer_count=transfer_count,
        message_count_client=counts[SenderType.CLIENTE],
        message_count_human=counts[SenderType.ATENDENTE],
        message_count_ai=counts[SenderType.IA],
        message_count_bot=counts[SenderType.BOT],
        closure_reason=attendance.closure.reason.value if attendance.closure else None,
        tags=tags,
        contact_stage=contact.stage.value if contact else None,
    )


def process_analytics_outbox(session: Session, limit: int = 50) -> int:
    """Processa até `limit` eventos pendentes. Retorna a quantidade processada."""
    pending = list(
        session.scalars(
            select(AnalyticsOutbox)
            .where(AnalyticsOutbox.processed_at.is_(None))
            .order_by(AnalyticsOutbox.created_at)
            .limit(limit)
        )
    )
    processed = 0
    for event in pending:
        event.attempts += 1
        attendance = session.get(Attendance, event.attendance_id)
        if attendance is None:
            event.processed_at = now_utc()
            processed += 1
            continue

        if event.topic == CONVERSATION_CLOSED_TOPIC:
            existing = session.scalar(
                select(ConversationInsight).where(
                    ConversationInsight.attendance_id == attendance.id
                )
            )
            insight = _build_insight(session, attendance)
            if existing is not None:
                insight.id = existing.id
                session.merge(insight)
            else:
                session.add(insight)

        event.processed_at = now_utc()
        processed += 1

    if processed:
        session.commit()
    return processed
