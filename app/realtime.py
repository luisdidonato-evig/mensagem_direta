from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.domain import ActorRole
from app.models import Agent, Attendance, GroupAgent, Message, OutboxMessage
from app.schemas import Actor


class ConnectionManager:
    def __init__(self, session_factory: sessionmaker) -> None:
        self.session_factory = session_factory
        self._connections: dict[WebSocket, Actor] = {}

    def connect(self, websocket: WebSocket, actor: Actor) -> None:
        self._connections[websocket] = actor

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.pop(websocket, None)

    @staticmethod
    def _attendance_scope(actor: Actor):
        query = select(Attendance.id).where(Attendance.company_id == actor.company_id)
        if actor.role == ActorRole.ATENDENTE:
            groups = select(GroupAgent.group_id).where(
                GroupAgent.agent_id == actor.id, GroupAgent.active.is_(True)
            )
            query = query.where(Attendance.group_id.in_(groups))
        return query

    def _can_receive(self, session: Session, actor: Actor, event: dict) -> bool:
        if event.get("company_id") and event["company_id"] != actor.company_id:
            return False
        agent = session.get(Agent, actor.id)
        if agent is None or not agent.active or agent.company_id != actor.company_id or agent.role != actor.role:
            return False

        attendance = event.get("attendance") or {}
        if attendance.get("company_id") and attendance["company_id"] != actor.company_id:
            return False
        previous_group_id = event.get("previous_group_id")
        if previous_group_id and actor.role == ActorRole.ATENDENTE:
            link = session.get(GroupAgent, (previous_group_id, actor.id))
            if link is not None and link.active:
                return True
        attendance_id = attendance.get("id") or event.get("attendance_id")
        if attendance_id:
            return session.scalar(
                self._attendance_scope(actor).where(Attendance.id == attendance_id).limit(1)
            ) is not None

        contact_id = event.get("contact_id")
        if contact_id:
            return session.scalar(
                self._attendance_scope(actor)
                .where(Attendance.contact_id == contact_id)
                .limit(1)
            ) is not None

        outbox_id = event.get("outbox_id")
        if outbox_id and actor.role in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
            return session.scalar(
                self._attendance_scope(actor)
                .join(Message, Message.attendance_id == Attendance.id)
                .join(OutboxMessage, OutboxMessage.message_id == Message.id)
                .where(OutboxMessage.id == outbox_id)
                .limit(1)
            ) is not None
        return False

    async def publish(self, event: dict) -> None:
        stale: list[WebSocket] = []
        with self.session_factory() as session:
            for connection, actor in tuple(self._connections.items()):
                if not self._can_receive(session, actor, event):
                    continue
                try:
                    await connection.send_json(event)
                except Exception:
                    stale.append(connection)
        for connection in stale:
            self.disconnect(connection)

