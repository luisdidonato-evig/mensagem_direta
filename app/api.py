import asyncio
import hashlib
import json
from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.analytics import enqueue_conversation_closed
from app.auth import authenticate_token, get_actor
from app.database import get_session
from app.domain import (
    ACTIVE_STATUSES,
    ALLOWED_STATUS_TRANSITIONS,
    STALE_THRESHOLDS,
    ActorRole,
    AttendanceStatus,
    ClosureReason,
    ContactStage,
    DeliveryStatus,
    EventType,
    MessageDirection,
    SenderType,
)
from app.integration import DEFAULT_MAX_ATTEMPTS, verify_meta_signature
from app.media import MAX_DOCUMENT_BYTES, media_path, safe_filename, validate_media, write_media
from app.models import (
    Agent,
    Attendance,
    AttendanceClosure,
    AttendanceEvent,
    AttendanceTag,
    Contact,
    ContactAuditEvent,
    ContactTag,
    GroupAgent,
    IntegrationEvent,
    InternalNote,
    Message,
    MediaAttachment,
    OutboxMessage,
    QuickReply,
    ServiceGroup,
    now_utc,
)
from app.schemas import (
    Actor,
    AgentCreate,
    AgentRead,
    AgentUpdate,
    AttendanceDetail,
    AttendanceRead,
    AttendanceSummary,
    AttendanceTagsUpdate,
    ClaimRequest,
    ContactRead,
    ContactStageUpdate,
    ContactUpdate,
    GroupAgentRead,
    GroupAgentUpdate,
    GroupCreate,
    GroupRead,
    GroupUpdate,
    InboundMessageCreate,
    InboundResult,
    InternalNoteCreate,
    InternalNoteRead,
    LoginRequest,
    MessageRead,
    MetricsSummary,
    OutboundMessageCreate,
    OutboxItemRead,
    QuickReplyCreate,
    QuickReplyRead,
    StatusChangeRequest,
    TimelineItem,
    TokenResponse,
    TransferRequest,
)
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1")
SessionDep = Annotated[Session, Depends(get_session)]
ActorDep = Annotated[Actor, Depends(get_actor)]


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, session: SessionDep) -> dict:
    agent = session.get(Agent, payload.id)
    if agent is None or not agent.active or not verify_password(payload.password, agent.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    token, expires_at = create_access_token(
        agent.id, agent.role.value, agent.company_id, request.app.state.jwt_secret
    )
    return {
        "access_token": token,
        "expires_at": expires_at,
        "actor": {"id": agent.id, "role": agent.role, "company_id": agent.company_id},
    }


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(payload: AgentCreate, session: SessionDep, actor: ActorDep) -> Agent:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão cria contas de agente")
    if actor.role == ActorRole.SUPERVISOR and payload.role != ActorRole.ATENDENTE:
        raise HTTPException(status_code=403, detail="Supervisor só pode criar atendentes")
    if session.get(Agent, payload.id) is not None:
        raise HTTPException(status_code=409, detail="Já existe agente com esse identificador")
    agent = Agent(
        id=payload.id,
        company_id=actor.company_id,
        role=payload.role,
        password_hash=hash_password(payload.password),
    )
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


@router.get("/agents", response_model=list[AgentRead])
def list_agents(session: SessionDep, actor: ActorDep) -> list[Agent]:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão vê contas de agente")
    return list(
        session.scalars(
            select(Agent).where(Agent.company_id == actor.company_id).order_by(Agent.id)
        )
    )


@router.patch("/agents/{agent_id}", response_model=AgentRead)
def update_agent(
    agent_id: str, payload: AgentUpdate, session: SessionDep, actor: ActorDep
) -> Agent:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão edita contas de agente")
    agent = session.get(Agent, agent_id)
    if agent is None or agent.company_id != actor.company_id:
        raise HTTPException(status_code=404, detail="Agente não encontrado")
    if actor.role == ActorRole.SUPERVISOR:
        if agent.role != ActorRole.ATENDENTE or (payload.role is not None and payload.role != ActorRole.ATENDENTE):
            raise HTTPException(status_code=403, detail="Supervisor só pode editar atendentes")
    if agent.role == ActorRole.ADMIN and (
        (payload.role is not None and payload.role != ActorRole.ADMIN)
        or payload.active is False
    ):
        other_admin = session.scalar(
            select(Agent.id).where(
                Agent.company_id == actor.company_id,
                Agent.role == ActorRole.ADMIN,
                Agent.active.is_(True),
                Agent.id != agent_id,
            )
        )
        if other_admin is None:
            raise HTTPException(status_code=409, detail="A empresa precisa de um administrador ativo")
    if payload.role is not None:
        agent.role = payload.role
    if payload.password is not None:
        agent.password_hash = hash_password(payload.password)
    if payload.active is not None:
        if not payload.active:
            open_attendance = session.scalar(
                select(Attendance.id).where(
                    Attendance.assignee_id == agent_id,
                    Attendance.status.in_(ACTIVE_STATUSES),
                )
            )
            if open_attendance is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Usuário com atendimento ativo não pode ser desativado sem transferência",
                )
        agent.active = payload.active
    session.commit()
    session.refresh(agent)
    return agent


@router.post("/groups", response_model=GroupRead, status_code=201)
def create_group(payload: GroupCreate, session: SessionDep, actor: ActorDep) -> ServiceGroup:
    if actor.role != ActorRole.ADMIN:
        raise HTTPException(status_code=403, detail="Apenas administrador cria grupos")
    group = ServiceGroup(
        company_id=actor.company_id,
        name=payload.name.strip(),
        max_load_per_agent=payload.max_load_per_agent,
    )
    session.add(group)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Já existe grupo com esse nome") from None
    session.refresh(group)
    return group


@router.get("/groups", response_model=list[GroupRead])
def list_groups(session: SessionDep, actor: ActorDep) -> list[ServiceGroup]:
    return list(
        session.scalars(
            select(ServiceGroup)
            .where(ServiceGroup.company_id == actor.company_id)
            .order_by(ServiceGroup.name)
        )
    )


def load_group(session: Session, actor: Actor, group_id: str) -> ServiceGroup:
    group = session.get(ServiceGroup, group_id)
    if group is None or group.company_id != actor.company_id:
        raise HTTPException(status_code=404, detail="Grupo não encontrado")
    return group


@router.patch("/groups/{group_id}", response_model=GroupRead)
def update_group(
    group_id: str, payload: GroupUpdate, session: SessionDep, actor: ActorDep
) -> ServiceGroup:
    if actor.role != ActorRole.ADMIN:
        raise HTTPException(status_code=403, detail="Apenas administrador edita grupos")
    group = load_group(session, actor, group_id)
    if payload.name is not None:
        group.name = payload.name.strip()
    if payload.max_load_per_agent is not None:
        group.max_load_per_agent = payload.max_load_per_agent
    if payload.active is not None:
        group.active = payload.active
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Já existe grupo com esse nome") from None
    session.refresh(group)
    return group


@router.put("/groups/{group_id}/agents/{agent_id}", response_model=GroupAgentRead)
def link_group_agent(
    group_id: str,
    agent_id: str,
    payload: GroupAgentUpdate,
    session: SessionDep,
    actor: ActorDep,
) -> GroupAgent:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão vincula agentes a grupos")
    group = load_group(session, actor, group_id)
    if not group.active:
        raise HTTPException(status_code=409, detail="Grupo de atendimento inativo")
    agent = session.get(Agent, agent_id)
    if agent is None or agent.company_id != actor.company_id:
        raise HTTPException(status_code=404, detail="Agente não encontrado")
    if not agent.active:
        raise HTTPException(status_code=409, detail="Usuário inativo")
    if actor.role == ActorRole.SUPERVISOR and agent.role != ActorRole.ATENDENTE:
        raise HTTPException(status_code=403, detail="Supervisor só pode vincular atendentes")
    link = session.get(GroupAgent, (group_id, agent_id))
    if link is None:
        link = GroupAgent(group_id=group_id, agent_id=agent_id)
        session.add(link)
    link.active = True
    link.max_load_override = payload.max_load_override
    session.commit()
    session.refresh(link)
    return link


@router.delete("/groups/{group_id}/agents/{agent_id}", status_code=204)
def unlink_group_agent(
    group_id: str, agent_id: str, session: SessionDep, actor: ActorDep
) -> None:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão desvincula agentes de grupos")
    load_group(session, actor, group_id)
    agent = session.get(Agent, agent_id)
    if actor.role == ActorRole.SUPERVISOR and (agent is None or agent.role != ActorRole.ATENDENTE):
        raise HTTPException(status_code=403, detail="Supervisor só pode desvincular atendentes")
    link = session.get(GroupAgent, (group_id, agent_id))
    if link is None or not link.active:
        raise HTTPException(status_code=404, detail="Vínculo não encontrado")
    open_attendance = session.scalar(
        select(Attendance.id).where(
            Attendance.assignee_id == agent_id,
            Attendance.group_id == group_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        )
    )
    if open_attendance is not None:
        raise HTTPException(
            status_code=409,
            detail="Atendente com atendimento ativo no grupo não pode ser desvinculado sem transferência",
        )
    link.active = False
    session.commit()


@router.get("/groups/memberships", response_model=list[GroupAgentRead])
def list_group_memberships(session: SessionDep, actor: ActorDep) -> list[GroupAgent]:
    if actor.role not in {ActorRole.ADMIN, ActorRole.SUPERVISOR}:
        raise HTTPException(status_code=403, detail="Apenas gestão vê vínculos de grupos")
    return list(
        session.scalars(
            select(GroupAgent)
            .join(ServiceGroup, ServiceGroup.id == GroupAgent.group_id)
            .where(ServiceGroup.company_id == actor.company_id, GroupAgent.active.is_(True))
            .order_by(GroupAgent.group_id, GroupAgent.agent_id)
        )
    )


@router.get("/me/groups", response_model=list[GroupRead])
def list_my_groups(session: SessionDep, actor: ActorDep) -> list[ServiceGroup]:
    return list(
        session.scalars(
            select(ServiceGroup)
            .join(GroupAgent, GroupAgent.group_id == ServiceGroup.id)
            .where(
                GroupAgent.agent_id == actor.id,
                GroupAgent.active.is_(True),
                ServiceGroup.company_id == actor.company_id,
            )
            .order_by(ServiceGroup.name)
        )
    )


@router.get("/me/load")
def get_my_load(session: SessionDep, actor: ActorDep) -> dict:
    current_load = session.scalar(
        select(func.coalesce(func.sum(Attendance.load_weight), 0)).where(
            Attendance.assignee_id == actor.id,
            Attendance.company_id == actor.company_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        )
    )
    return {"actor_id": actor.id, "current_load": int(current_load or 0)}


@router.post(
    "/groups/{group_id}/attendances/pull",
    response_model=AttendanceRead,
    responses={204: {"description": "Fila vazia"}},
)
async def pull_attendance(
    group_id: str, request: Request, session: SessionDep, actor: ActorDep
) -> Attendance | Response:
    group = load_group(session, actor, group_id)
    link = session.scalar(
        select(GroupAgent)
        .where(
            GroupAgent.group_id == group_id,
            GroupAgent.agent_id == actor.id,
            GroupAgent.active.is_(True),
        )
        .with_for_update()
    )
    if link is None:
        raise HTTPException(status_code=403, detail="Atendente não pertence ao grupo")
    capacity = link.max_load_override or group.max_load_per_agent
    current_load = int(
        session.scalar(
            select(func.coalesce(func.sum(Attendance.load_weight), 0)).where(
                Attendance.assignee_id == actor.id,
                Attendance.company_id == actor.company_id,
                Attendance.status.in_(ACTIVE_STATUSES),
            )
        )
        or 0
    )
    available = capacity - current_load
    if available <= 0:
        raise HTTPException(status_code=409, detail="Atendente atingiu a capacidade")

    attendance = session.scalar(
        select(Attendance)
        .where(
            Attendance.company_id == actor.company_id,
            Attendance.group_id == group_id,
            Attendance.status == AttendanceStatus.AGUARDANDO,
            Attendance.assignee_id.is_(None),
            Attendance.load_weight <= available,
        )
        .order_by(Attendance.priority.desc(), Attendance.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if attendance is None:
        has_waiting = session.scalar(
            select(Attendance.id).where(
                Attendance.company_id == actor.company_id,
                Attendance.group_id == group_id,
                Attendance.status == AttendanceStatus.AGUARDANDO,
                Attendance.assignee_id.is_(None),
            )
        )
        session.rollback()
        if has_waiting is not None:
            raise HTTPException(status_code=409, detail="Atendimento não cabe na capacidade disponível")
        return Response(status_code=204)

    attendance.assignee_id = actor.id
    attendance.status = AttendanceStatus.EM_ATENDIMENTO
    attendance.version += 1
    attendance.updated_at = now_utc()
    session.add(
        AttendanceEvent(
            attendance_id=attendance.id,
            type=EventType.ASSUMIDO,
            actor_id=actor.id,
            from_status=AttendanceStatus.AGUARDANDO,
            to_status=AttendanceStatus.EM_ATENDIMENTO,
            details={"source": "pull", "group_id": group_id},
        )
    )
    session.commit()
    session.refresh(attendance)
    await broadcast(request, "attendance.claimed", attendance)
    return attendance


def load_attendance(
    session: Session, attendance_id: str, actor: Actor | None = None
) -> Attendance:
    attendance = session.get(Attendance, attendance_id)
    if attendance is None:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
    if actor is not None:
        if attendance.company_id != actor.company_id:
            raise HTTPException(status_code=404, detail="Atendimento não encontrado")
        if actor.role == ActorRole.ATENDENTE and attendance.group_id is not None:
            link = session.get(GroupAgent, (attendance.group_id, actor.id))
            if link is None or not link.active:
                raise HTTPException(status_code=403, detail="Atendente não pertence ao grupo")
    return attendance


def is_stale(attendance: Attendance) -> bool:
    threshold = STALE_THRESHOLDS.get(attendance.status)
    if threshold is None:
        return False
    return datetime.now(timezone.utc) - attendance.updated_at.replace(tzinfo=timezone.utc) > threshold


def ensure_can_operate(attendance: Attendance, actor: Actor) -> None:
    if actor.role in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        return
    if attendance.assignee_id != actor.id:
        raise HTTPException(status_code=403, detail="Atendimento pertence a outro agente")


def ensure_claim_capacity(session: Session, attendance: Attendance, actor: Actor) -> None:
    if actor.role != ActorRole.ATENDENTE or attendance.group_id is None:
        return
    link = session.scalar(
        select(GroupAgent)
        .where(
            GroupAgent.group_id == attendance.group_id,
            GroupAgent.agent_id == actor.id,
            GroupAgent.active.is_(True),
        )
        .with_for_update()
    )
    if link is None:
        raise HTTPException(status_code=403, detail="Atendente não pertence ao grupo")
    group = session.get(ServiceGroup, attendance.group_id)
    if group is None or not group.active:
        raise HTTPException(status_code=409, detail="Grupo de atendimento inativo")
    capacity = link.max_load_override or group.max_load_per_agent
    current_load = int(
        session.scalar(
            select(func.coalesce(func.sum(Attendance.load_weight), 0)).where(
                Attendance.assignee_id == actor.id,
                Attendance.company_id == actor.company_id,
                Attendance.status.in_(ACTIVE_STATUSES),
            )
        )
        or 0
    )
    if current_load + attendance.load_weight > capacity:
        raise HTTPException(status_code=409, detail="Atendente atingiu a capacidade")


async def broadcast(request: Request, kind: str, attendance: Attendance) -> None:
    await request.app.state.realtime.publish(
        {
            "type": kind,
            "attendance": jsonable_encoder(AttendanceRead.model_validate(attendance)),
        }
    )


async def process_inbound_message(
    payload: InboundMessageCreate,
    request: Request,
    session: Session,
    media_file: tuple[bytes, str, str] | None = None,
) -> InboundResult:
    processed = session.get(IntegrationEvent, payload.external_event_id)
    if processed:
        attendance = load_attendance(session, processed.attendance_id)
        message = session.get(Message, processed.message_id)
        if message is None:
            raise HTTPException(status_code=500, detail="Evento processado sem mensagem")
        return InboundResult(attendance=attendance, message=message, duplicate=True)

    group = session.get(ServiceGroup, payload.group_id) if payload.group_id else None
    if group is None and payload.company_id:
        group = session.scalar(
            select(ServiceGroup)
            .where(
                ServiceGroup.company_id == payload.company_id,
                ServiceGroup.active.is_(True),
            )
            .order_by(ServiceGroup.created_at)
        )
    if group is None and payload.group_id is None and payload.company_id is None:
        groups = list(
            session.scalars(
                select(ServiceGroup).where(ServiceGroup.active.is_(True)).limit(2)
            )
        )
        group = groups[0] if len(groups) == 1 else None
    if group is None or not group.active:
        raise HTTPException(status_code=422, detail="Grupo de atendimento inválido")
    if payload.company_id is not None and group.company_id != payload.company_id:
        raise HTTPException(status_code=422, detail="Grupo não pertence à empresa")

    contact = session.get(Contact, payload.contact_id)
    if contact is None:
        session.add(Contact(id=payload.contact_id))

    attendance = session.scalar(
        select(Attendance)
        .where(
            Attendance.contact_id == payload.contact_id,
            Attendance.company_id == group.company_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Attendance.created_at.desc())
    )
    created = attendance is None
    if created:
        attendance = Attendance(
            contact_id=payload.contact_id,
            company_id=group.company_id,
            group_id=group.id,
            load_weight=payload.load_weight,
            priority=payload.priority,
        )
        session.add(attendance)
        session.flush()
        session.add(
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.CRIADO,
                to_status=AttendanceStatus.AGUARDANDO,
                details={"source": "inbox"},
            )
        )
    elif attendance.status == AttendanceStatus.AGUARDANDO_CLIENTE:
        previous = attendance.status
        attendance.status = AttendanceStatus.EM_ATENDIMENTO
        attendance.version += 1
        session.add(
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.STATUS_ALTERADO,
                from_status=previous,
                to_status=attendance.status,
                details={"reason": "customer_replied"},
            )
        )

    message = Message(
        attendance_id=attendance.id,
        external_id=payload.external_message_id,
        direction=MessageDirection.ENTRADA,
        sender_type=SenderType.CLIENTE,
        content=payload.content,
        delivery_status=DeliveryStatus.RECEBIDA,
    )
    session.add(message)
    session.flush()
    stored_key = None
    if media_file is not None:
        data, filename, external_media_id = media_file
        _, mime = validate_media(data)
        stored_key = write_media(request.app.state.media_storage_dir, data)
        session.add(
            MediaAttachment(
                message_id=message.id,
                storage_key=stored_key,
                mime_type=mime,
                filename=safe_filename(filename, mime),
                size_bytes=len(data),
                external_media_id=external_media_id,
            )
        )
    session.add_all(
        [
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.MENSAGEM_RECEBIDA,
                details={"message_id": message.id},
            ),
            IntegrationEvent(
                external_event_id=payload.external_event_id,
                attendance_id=attendance.id,
                message_id=message.id,
            ),
        ]
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        if stored_key:
            media_path(request.app.state.media_storage_dir, stored_key).unlink(missing_ok=True)
        processed = session.get(IntegrationEvent, payload.external_event_id)
        if processed is None:
            raise HTTPException(status_code=409, detail="Mensagem externa duplicada") from None
        attendance = load_attendance(session, processed.attendance_id)
        message = session.get(Message, processed.message_id)
        if message is None:
            raise HTTPException(status_code=500, detail="Evento processado sem mensagem")
        return InboundResult(attendance=attendance, message=message, duplicate=True)

    session.refresh(attendance)
    await broadcast(request, "attendance.created" if created else "attendance.updated", attendance)
    return InboundResult(attendance=attendance, message=message, duplicate=False)


@router.post("/inbox/messages", response_model=InboundResult, status_code=201)
async def receive_inbound_message(
    payload: InboundMessageCreate,
    request: Request,
    session: SessionDep,
) -> InboundResult:
    if not request.app.state.enable_simulator:
        raise HTTPException(status_code=404, detail="Simulador desabilitado")
    return await process_inbound_message(payload, request, session)


@router.get("/integrations/meta/whatsapp/webhook", response_class=PlainTextResponse)
async def verify_meta_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if (
        mode != "subscribe"
        or not challenge
        or not request.app.state.meta_verify_token
        or token != request.app.state.meta_verify_token
    ):
        raise HTTPException(status_code=403, detail="Verificação do webhook inválida")
    return PlainTextResponse(challenge)


META_STATUS_MAP = {
    "sent": DeliveryStatus.ACEITA_PELO_PROVEDOR,
    "delivered": DeliveryStatus.ENTREGUE,
    "read": DeliveryStatus.LIDA,
    "failed": DeliveryStatus.FALHA,
}
DELIVERY_RANK = {
    DeliveryStatus.PENDENTE: 0,
    DeliveryStatus.ENVIADA_AO_MIDDLEWARE: 1,
    DeliveryStatus.ACEITA_PELO_PROVEDOR: 2,
    DeliveryStatus.ENTREGUE: 3,
    DeliveryStatus.LIDA: 4,
    DeliveryStatus.FALHA: -1,
}


async def process_meta_status(status: dict, request: Request, session: Session) -> bool:
    external_id = status.get("id")
    mapped = META_STATUS_MAP.get(status.get("status"))
    if not external_id or mapped is None:
        return False
    fingerprint = hashlib.sha256(
        json.dumps(status, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event_id = f"meta-status:{fingerprint}"
    if session.get(IntegrationEvent, event_id):
        return True
    message = session.scalar(select(Message).where(Message.external_id == external_id))
    if message is None:
        callback_id = status.get("biz_opaque_callback_data")
        outbox = session.get(OutboxMessage, callback_id) if callback_id else None
        if outbox is None:
            return False
        message = session.get(Message, outbox.message_id)
        if message is None or (message.external_id and message.external_id != external_id):
            return False
        message.external_id = external_id
        outbox.processed_at = now_utc()
    current_rank = DELIVERY_RANK.get(message.delivery_status, 0)
    next_rank = DELIVERY_RANK[mapped]
    outbox = session.scalar(
        select(OutboxMessage).where(OutboxMessage.message_id == message.id)
    )
    if mapped == DeliveryStatus.FALHA:
        if current_rank < DELIVERY_RANK[DeliveryStatus.ENTREGUE]:
            message.delivery_status = mapped
            if outbox is not None:
                outbox.processed_at = None
                outbox.attempts = DEFAULT_MAX_ATTEMPTS
    elif next_rank >= current_rank:
        message.delivery_status = mapped
        if outbox is not None:
            outbox.processed_at = outbox.processed_at or now_utc()
    session.add(
        IntegrationEvent(
            external_event_id=event_id,
            attendance_id=message.attendance_id,
            message_id=message.id,
        )
    )
    session.commit()
    attendance = load_attendance(session, message.attendance_id)
    await broadcast(request, "message.status_changed", attendance)
    return True


@router.post("/integrations/meta/whatsapp/webhook")
async def receive_meta_webhook(
    request: Request,
    session: SessionDep,
) -> dict:
    body = await request.body()
    try:
        verify_meta_signature(
            body,
            request.headers.get("X-Hub-Signature-256"),
            request.app.state.meta_app_secret,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise HTTPException(status_code=422, detail="Objeto Meta não suportado")

    processed = 0
    duplicates = 0
    ignored = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                ignored += 1
                continue
            value = change.get("value") or {}
            profiles = {
                item.get("wa_id"): (item.get("profile") or {}).get("name")
                for item in value.get("contacts", [])
            }
            for item in value.get("messages", []):
                contact_id = item.get("from")
                external_id = item.get("id")
                kind = item.get("type")
                if kind not in {"text", "image", "document"} or not contact_id or not external_id:
                    ignored += 1
                    continue
                media_file = None
                if kind == "text":
                    content = (item.get("text") or {}).get("body")
                    if not content:
                        ignored += 1
                        continue
                else:
                    media_info = item.get(kind) or {}
                    media_id = media_info.get("id")
                    if not media_id:
                        ignored += 1
                        continue
                    filename = media_info.get("filename") or (
                        "imagem.jpg" if kind == "image" else "documento.pdf"
                    )
                    content = media_info.get("caption") or filename
                    if session.get(IntegrationEvent, f"meta-message:{external_id}") is None:
                        try:
                            data = await request.app.state.meta_media_downloader.download(media_id)
                            actual_kind, _ = validate_media(data)
                            if actual_kind != kind:
                                raise ValueError("Tipo de mídia recebido não corresponde ao webhook")
                            media_file = (data, filename, media_id)
                        except Exception as exc:
                            raise HTTPException(
                                status_code=503, detail="Falha ao obter mídia da Meta"
                            ) from exc
                result = await process_inbound_message(
                    InboundMessageCreate(
                        external_event_id=f"meta-message:{external_id}",
                        external_message_id=external_id,
                        contact_id=contact_id,
                        content=content,
                        group_id=request.app.state.meta_default_group_id or None,
                    ),
                    request,
                    session,
                    media_file,
                )
                contact = session.get(Contact, contact_id)
                if contact:
                    contact.phone = contact_id
                    contact.display_name = profiles.get(contact_id) or contact.display_name
                    session.commit()
                processed += 1
                duplicates += int(result.duplicate)
            for status in value.get("statuses", []):
                if await process_meta_status(status, request, session):
                    processed += 1
                else:
                    ignored += 1
    return {"processed": processed, "duplicates": duplicates, "ignored": ignored}


@router.post("/integrations/outbox/process")
async def process_outbox(
    request: Request,
    actor: ActorDep,
) -> dict[str, int]:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode processar outbox")
    processor = request.app.state.outbox_processor
    if processor is None:
        raise HTTPException(status_code=503, detail="Meta Cloud API não configurada")
    delivered, failed = await processor.process_batch()
    return {"delivered": delivered, "failed": failed}


def visible_attendance_ids(actor: Actor):
    query = select(Attendance.id).where(Attendance.company_id == actor.company_id)
    if actor.role == ActorRole.ATENDENTE:
        groups = select(GroupAgent.group_id).where(
            GroupAgent.agent_id == actor.id, GroupAgent.active.is_(True)
        )
        query = query.where(Attendance.group_id.in_(groups))
    return query


def latest_attendance_for_contact(
    session: Session, contact_id: str, actor: Actor
) -> Attendance | None:
    return session.scalar(
        select(Attendance)
        .where(
            Attendance.contact_id == contact_id,
            Attendance.id.in_(visible_attendance_ids(actor)),
        )
        .order_by(Attendance.updated_at.desc())
        .limit(1)
    )


def build_contact_read(session: Session, contact_id: str, actor: Actor) -> ContactRead:
    contact = session.get(Contact, contact_id)
    tags = list(
        session.scalars(
            select(ContactTag.name)
            .where(ContactTag.contact_id == contact_id)
            .order_by(ContactTag.name)
        )
    )
    last_attendance = latest_attendance_for_contact(session, contact_id, actor)
    if last_attendance is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    if contact:
        return ContactRead(
            id=contact.id,
            display_name=contact.display_name,
            phone=contact.phone,
            tags=tags,
            stage=contact.stage,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
            last_attendance_id=last_attendance.id if last_attendance else None,
            last_attendance_status=last_attendance.status if last_attendance else None,
        )

    if last_attendance is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    return ContactRead(
        id=contact_id,
        display_name=None,
        phone=None,
        tags=[],
        stage=ContactStage.NAO_CLASSIFICADO,
        created_at=last_attendance.created_at,
        updated_at=last_attendance.updated_at,
        last_attendance_id=last_attendance.id,
        last_attendance_status=last_attendance.status,
    )


def ensure_can_edit_contact(session: Session, contact_id: str, actor: Actor) -> None:
    if latest_attendance_for_contact(session, contact_id, actor) is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    if actor.role in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        return
    assigned = session.scalar(
        select(Attendance.id).where(
            Attendance.contact_id == contact_id,
            Attendance.assignee_id == actor.id,
            Attendance.id.in_(visible_attendance_ids(actor)),
        )
    )
    if assigned is None:
        raise HTTPException(
            status_code=403,
            detail="Somente responsável ou supervisão pode editar contato",
        )


@router.get("/contacts", response_model=list[ContactRead])
def list_contacts(
    session: SessionDep,
    actor: ActorDep,
    stage: Annotated[list[ContactStage] | None, Query()] = None,
    tag: str | None = None,
    search: str | None = None,
) -> list[ContactRead]:
    query = select(Contact).where(
        Contact.id.in_(
            select(Attendance.contact_id).where(
                Attendance.id.in_(visible_attendance_ids(actor))
            )
        )
    )
    if stage:
        query = query.where(Contact.stage.in_(stage))
    if tag:
        query = query.where(
            Contact.id.in_(select(ContactTag.contact_id).where(ContactTag.name == tag))
        )
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            (Contact.display_name.ilike(pattern))
            | (Contact.phone.ilike(pattern))
            | (Contact.id.ilike(pattern))
        )
    query = query.order_by(Contact.updated_at.desc())
    contacts = list(session.scalars(query))
    if not contacts:
        return []

    contact_ids = [contact.id for contact in contacts]
    tags_by_contact: dict[str, list[str]] = {contact_id: [] for contact_id in contact_ids}
    for row_contact_id, row_tag in session.execute(
        select(ContactTag.contact_id, ContactTag.name)
        .where(ContactTag.contact_id.in_(contact_ids))
        .order_by(ContactTag.name)
    ):
        tags_by_contact[row_contact_id].append(row_tag)

    last_attendance_by_contact: dict[str, Attendance] = {}
    for attendance in session.scalars(
        select(Attendance)
        .where(
            Attendance.contact_id.in_(contact_ids),
            Attendance.id.in_(visible_attendance_ids(actor)),
        )
        .order_by(Attendance.updated_at.desc())
    ):
        last_attendance_by_contact.setdefault(attendance.contact_id, attendance)

    results = []
    for contact in contacts:
        last = last_attendance_by_contact.get(contact.id)
        results.append(
            ContactRead(
                id=contact.id,
                display_name=contact.display_name,
                phone=contact.phone,
                tags=tags_by_contact.get(contact.id, []),
                stage=contact.stage,
                created_at=contact.created_at,
                updated_at=contact.updated_at,
                last_attendance_id=last.id if last else None,
                last_attendance_status=last.status if last else None,
            )
        )
    return results


@router.get("/contacts/{contact_id}", response_model=ContactRead)
def get_contact(
    contact_id: str,
    session: SessionDep,
    actor: ActorDep,
) -> ContactRead:
    return build_contact_read(session, contact_id, actor)


@router.get("/contacts/{contact_id}/timeline", response_model=list[TimelineItem])
def get_contact_timeline(
    contact_id: str,
    session: SessionDep,
    actor: ActorDep,
) -> list[TimelineItem]:
    attendance_ids = list(
        session.scalars(
            select(Attendance.id).where(
                Attendance.contact_id == contact_id,
                Attendance.id.in_(visible_attendance_ids(actor)),
            )
        )
    )
    if not attendance_ids:
        raise HTTPException(status_code=404, detail="Contato não encontrado")

    items: list[TimelineItem] = []
    for message in session.scalars(
        select(Message)
        .where(Message.attendance_id.in_(attendance_ids))
        .order_by(Message.created_at)
    ):
        items.append(
            TimelineItem(
                id=message.id,
                kind="message",
                attendance_id=message.attendance_id,
                actor_id=message.actor_id,
                sender_type=message.sender_type,
                content=message.content,
                details={
                    "direction": message.direction.value,
                    "delivery_status": message.delivery_status.value,
                },
                created_at=message.created_at,
            )
        )
    for event in session.scalars(
        select(AttendanceEvent)
        .where(AttendanceEvent.attendance_id.in_(attendance_ids))
        .order_by(AttendanceEvent.created_at)
    ):
        items.append(
            TimelineItem(
                id=event.id,
                kind="event",
                attendance_id=event.attendance_id,
                actor_id=event.actor_id,
                content=event.type.value,
                details={
                    **event.details,
                    "from_status": event.from_status.value if event.from_status else None,
                    "to_status": event.to_status.value if event.to_status else None,
                },
                created_at=event.created_at,
            )
        )
    for note in session.scalars(
        select(InternalNote)
        .where(InternalNote.attendance_id.in_(attendance_ids))
        .order_by(InternalNote.created_at)
    ):
        items.append(
            TimelineItem(
                id=note.id,
                kind="note",
                attendance_id=note.attendance_id,
                actor_id=note.actor_id,
                content=note.content,
                created_at=note.created_at,
            )
        )
    for closure in session.scalars(
        select(AttendanceClosure).where(
            AttendanceClosure.attendance_id.in_(attendance_ids)
        )
    ):
        items.append(
            TimelineItem(
                id=f"closure-{closure.attendance_id}",
                kind="closure",
                attendance_id=closure.attendance_id,
                actor_id=closure.actor_id,
                content=closure.note,
                details={"reason": closure.reason.value},
                created_at=closure.created_at,
            )
        )

    items.sort(key=lambda item: item.created_at)
    return items


@router.patch("/contacts/{contact_id}/stage", response_model=ContactRead)
async def update_contact_stage(
    contact_id: str,
    payload: ContactStageUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> ContactRead:
    if latest_attendance_for_contact(session, contact_id, actor) is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    ensure_can_edit_contact(session, contact_id, actor)

    contact = session.get(Contact, contact_id)
    if contact is None:
        contact = Contact(id=contact_id)
        session.add(contact)
        session.flush()
    if contact.stage != payload.stage:
        contact.stage = payload.stage
        contact.updated_at = now_utc()
        session.add(
            ContactAuditEvent(
                contact_id=contact_id,
                actor_id=actor.id,
                changed_fields=["stage"],
            )
        )
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "contact.updated", "contact_id": contact_id}
    )
    return build_contact_read(session, contact_id, actor)


@router.put("/contacts/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: str,
    payload: ContactUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> ContactRead:
    if latest_attendance_for_contact(session, contact_id, actor) is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    ensure_can_edit_contact(session, contact_id, actor)

    display_name = payload.display_name.strip() if payload.display_name else None
    phone = payload.phone.strip() if payload.phone else None
    normalized_tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in payload.tags:
        tag = raw_tag.strip()
        if not tag:
            continue
        if len(tag) > 40:
            raise HTTPException(status_code=422, detail="Tag excede 40 caracteres")
        key = tag.casefold()
        if key not in seen_tags:
            seen_tags.add(key)
            normalized_tags.append(tag)

    contact = session.get(Contact, contact_id)
    current_tags = set(
        session.scalars(select(ContactTag.name).where(ContactTag.contact_id == contact_id))
    )
    changed_fields: list[str] = []
    if contact is None:
        contact = Contact(id=contact_id)
        session.add(contact)
        session.flush()
    if contact.display_name != display_name:
        contact.display_name = display_name
        changed_fields.append("display_name")
    if contact.phone != phone:
        contact.phone = phone
        changed_fields.append("phone")
    if payload.stage is not None and contact.stage != payload.stage:
        contact.stage = payload.stage
        changed_fields.append("stage")
    if current_tags != set(normalized_tags):
        session.execute(delete(ContactTag).where(ContactTag.contact_id == contact_id))
        session.add_all(
            [ContactTag(contact_id=contact_id, name=tag) for tag in normalized_tags]
        )
        changed_fields.append("tags")
    if changed_fields:
        contact.updated_at = now_utc()
        session.add(
            ContactAuditEvent(
                contact_id=contact_id,
                actor_id=actor.id,
                changed_fields=changed_fields,
            )
        )
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "contact.updated", "contact_id": contact_id}
    )
    return build_contact_read(session, contact_id, actor)


@router.get("/metrics/summary", response_model=MetricsSummary)
def metrics_summary(
    session: SessionDep,
    actor: ActorDep,
) -> dict:
    scope = visible_attendance_ids(actor)
    attendances = list(session.scalars(select(Attendance).where(Attendance.id.in_(scope))))
    outbound_messages = list(
        session.scalars(
            select(Message)
            .where(
                Message.direction == MessageDirection.SAIDA,
                Message.attendance_id.in_(visible_attendance_ids(actor)),
            )
            .order_by(Message.created_at)
        )
    )
    closures = list(session.scalars(
        select(AttendanceClosure).where(
            AttendanceClosure.attendance_id.in_(visible_attendance_ids(actor))
        )
    ))

    status_counts = {status: 0 for status in AttendanceStatus}
    for attendance in attendances:
        status_counts[attendance.status] += 1

    first_response_by_attendance: dict[str, datetime] = {}
    for message in outbound_messages:
        first_response_by_attendance.setdefault(message.attendance_id, message.created_at)
    attendance_by_id = {attendance.id: attendance for attendance in attendances}
    first_response_seconds = [
        (responded_at - attendance_by_id[attendance_id].created_at).total_seconds()
        for attendance_id, responded_at in first_response_by_attendance.items()
        if attendance_id in attendance_by_id
    ]
    resolution_seconds = [
        (closure.created_at - attendance_by_id[closure.attendance_id].created_at).total_seconds()
        for closure in closures
        if closure.attendance_id in attendance_by_id
    ]
    today = datetime.now(timezone.utc).date()

    return {
        "status_counts": status_counts,
        "total_open": sum(
            count
            for status, count in status_counts.items()
            if status != AttendanceStatus.ENCERRADO
        ),
        "unassigned": sum(
            1
            for attendance in attendances
            if attendance.status != AttendanceStatus.ENCERRADO
            and attendance.assignee_id is None
        ),
        "mine": sum(
            1
            for attendance in attendances
            if attendance.status != AttendanceStatus.ENCERRADO
            and attendance.assignee_id == actor.id
        ),
        "closed_today": sum(
            1 for closure in closures if closure.created_at.date() == today
        ),
        "average_first_response_seconds": (
            sum(first_response_seconds) / len(first_response_seconds)
            if first_response_seconds
            else None
        ),
        "average_resolution_seconds": (
            sum(resolution_seconds) / len(resolution_seconds)
            if resolution_seconds
            else None
        ),
    }


@router.get("/attendances", response_model=list[AttendanceSummary])
def list_attendances(
    session: SessionDep,
    actor: ActorDep,
    status: Annotated[list[AttendanceStatus] | None, Query()] = None,
    queue_id: str | None = None,
    group_id: str | None = None,
) -> list[dict]:
    last_message = (
        select(Message.content)
        .where(Message.attendance_id == Attendance.id)
        .order_by(Message.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(
            Attendance,
            Contact.display_name,
            Contact.phone,
            last_message.label("last_message"),
        )
        .outerjoin(Contact, Contact.id == Attendance.contact_id)
        .where(Attendance.id.in_(visible_attendance_ids(actor)))
        .order_by(Attendance.updated_at.desc())
    )
    if status:
        query = query.where(Attendance.status.in_(status))
    if queue_id:
        query = query.where(Attendance.queue_id == queue_id)
    if group_id:
        query = query.where(Attendance.group_id == group_id)
    rows = list(session.execute(query))
    if not rows:
        return []

    attendance_ids = [attendance.id for attendance, *_ in rows]
    tags_by_attendance: dict[str, list[str]] = {aid: [] for aid in attendance_ids}
    for row_attendance_id, row_tag in session.execute(
        select(AttendanceTag.attendance_id, AttendanceTag.name)
        .where(AttendanceTag.attendance_id.in_(attendance_ids))
        .order_by(AttendanceTag.name)
    ):
        tags_by_attendance[row_attendance_id].append(row_tag)

    return [
        {
            **AttendanceRead.model_validate(attendance).model_dump(),
            "contact_display_name": display_name,
            "contact_phone": phone,
            "last_message": message_preview,
            "tags": tags_by_attendance.get(attendance.id, []),
            "stale": is_stale(attendance),
        }
        for attendance, display_name, phone, message_preview in rows
    ]


@router.get("/attendances/{attendance_id}", response_model=AttendanceDetail)
def get_attendance(
    attendance_id: str,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    load_attendance(session, attendance_id, actor)
    attendance = session.scalar(
        select(Attendance)
        .options(
            selectinload(Attendance.messages),
            selectinload(Attendance.events),
            selectinload(Attendance.notes),
            selectinload(Attendance.closure),
        )
        .where(Attendance.id == attendance_id)
    )
    if attendance is None:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
    attendance.messages.sort(key=lambda item: item.created_at)
    attendance.events.sort(key=lambda item: item.created_at)
    attendance.notes.sort(key=lambda item: item.created_at)
    attendance.tags = list(
        session.scalars(
            select(AttendanceTag.name)
            .where(AttendanceTag.attendance_id == attendance_id)
            .order_by(AttendanceTag.name)
        )
    )
    attendance.stale = is_stale(attendance)
    return attendance


@router.put("/attendances/{attendance_id}/tags", response_model=AttendanceRead)
async def update_attendance_tags(
    attendance_id: str,
    payload: AttendanceTagsUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> dict:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_can_operate(attendance, actor)

    normalized_tags: list[str] = []
    seen_tags: set[str] = set()
    for raw_tag in payload.tags:
        tag = raw_tag.strip()
        if not tag:
            continue
        if len(tag) > 40:
            raise HTTPException(status_code=422, detail="Tag excede 40 caracteres")
        key = tag.casefold()
        if key not in seen_tags:
            seen_tags.add(key)
            normalized_tags.append(tag)

    session.execute(delete(AttendanceTag).where(AttendanceTag.attendance_id == attendance_id))
    session.add_all(
        [AttendanceTag(attendance_id=attendance_id, name=tag) for tag in normalized_tags]
    )
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "attendance.tags_updated", "attendance_id": attendance_id}
    )
    return {
        **AttendanceRead.model_validate(attendance).model_dump(),
        "tags": normalized_tags,
        "stale": is_stale(attendance),
    }


@router.post(
    "/attendances/{attendance_id}/notes",
    response_model=InternalNoteRead,
    status_code=201,
)
async def add_internal_note(
    attendance_id: str,
    payload: InternalNoteCreate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> InternalNote:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_can_operate(attendance, actor)
    note = InternalNote(
        attendance_id=attendance_id,
        actor_id=actor.id,
        content=payload.content.strip(),
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    await request.app.state.realtime.publish(
        {
            "type": "attendance.note_added",
            "attendance_id": attendance_id,
        }
    )
    return note


@router.post("/attendances/{attendance_id}/claim", response_model=AttendanceRead)
async def claim_attendance(
    attendance_id: str,
    payload: ClaimRequest,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_claim_capacity(session, attendance, actor)
    statement = (
        update(Attendance)
        .where(
            Attendance.id == attendance_id,
            Attendance.status == AttendanceStatus.AGUARDANDO,
            Attendance.assignee_id.is_(None),
            Attendance.version == payload.expected_version,
        )
        .values(
            assignee_id=actor.id,
            status=AttendanceStatus.EM_ATENDIMENTO,
            version=Attendance.version + 1,
            updated_at=now_utc(),
        )
    )
    result = session.execute(statement)
    if result.rowcount != 1:
        session.rollback()
        attendance = load_attendance(session, attendance_id, actor)
        if attendance.assignee_id is not None:
            raise HTTPException(status_code=409, detail="Atendimento já assumido")
        raise HTTPException(status_code=409, detail="Versão desatualizada")

    session.add(
        AttendanceEvent(
            attendance_id=attendance_id,
            type=EventType.ASSUMIDO,
            actor_id=actor.id,
            from_status=AttendanceStatus.AGUARDANDO,
            to_status=AttendanceStatus.EM_ATENDIMENTO,
        )
    )
    session.commit()
    attendance = load_attendance(session, attendance_id, actor)
    await broadcast(request, "attendance.claimed", attendance)
    return attendance


@router.patch("/attendances/{attendance_id}/status", response_model=AttendanceRead)
async def change_status(
    attendance_id: str,
    payload: StatusChangeRequest,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_can_operate(attendance, actor)
    allowed = ALLOWED_STATUS_TRANSITIONS.get(attendance.status, set())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Transição inválida: {attendance.status.value} -> {payload.status.value}",
        )

    previous = attendance.status
    result = session.execute(
        update(Attendance)
        .where(
            Attendance.id == attendance_id,
            Attendance.version == payload.expected_version,
        )
        .values(
            status=payload.status,
            version=Attendance.version + 1,
            updated_at=now_utc(),
        )
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="Versão desatualizada")
    session.add(
        AttendanceEvent(
            attendance_id=attendance_id,
            type=EventType.STATUS_ALTERADO,
            actor_id=actor.id,
            from_status=previous,
            to_status=payload.status,
        )
    )
    if payload.status == AttendanceStatus.ENCERRADO:
        session.add(
            AttendanceClosure(
                attendance_id=attendance_id,
                reason=payload.closure_reason or ClosureReason.OUTRO,
                note=payload.closure_note.strip() if payload.closure_note else None,
                actor_id=actor.id,
            )
        )
        enqueue_conversation_closed(session, attendance_id)
    session.commit()
    attendance = load_attendance(session, attendance_id, actor)
    await broadcast(request, "attendance.status_changed", attendance)
    return attendance


@router.post("/attendances/{attendance_id}/transfer", response_model=AttendanceRead)
async def transfer_attendance(
    attendance_id: str,
    payload: TransferRequest,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode transferir")
    attendance = load_attendance(session, attendance_id, actor)
    previous_assignee = attendance.assignee_id
    previous_group_id = attendance.group_id

    target_actor_id = payload.target_actor_id
    target_group_id = payload.target_group_id or attendance.group_id

    if payload.target_group_id is not None:
        target_group = session.get(ServiceGroup, payload.target_group_id)
        if target_group is None or target_group.company_id != actor.company_id:
            raise HTTPException(status_code=404, detail="Grupo de destino não encontrado")
        if not target_group.active:
            raise HTTPException(status_code=422, detail="Grupo de destino inativo")
    if target_actor_id is not None:
        target_agent = session.get(Agent, target_actor_id)
        if target_agent is None or target_agent.company_id != actor.company_id:
            raise HTTPException(status_code=404, detail="Atendente de destino não encontrado")
        if not target_agent.active:
            raise HTTPException(status_code=422, detail="Atendente de destino inativo")
        if target_group_id is not None:
            link = session.get(GroupAgent, (target_group_id, target_actor_id))
            if link is None or not link.active:
                raise HTTPException(
                    status_code=422, detail="Atendente de destino não pertence ao grupo"
                )

    values = {
        "status": AttendanceStatus.EM_ATENDIMENTO,
        "version": Attendance.version + 1,
        "updated_at": now_utc(),
        "group_id": target_group_id,
    }
    if target_actor_id is not None:
        values["assignee_id"] = target_actor_id
    elif payload.target_group_id is not None:
        values["assignee_id"] = None
        values["status"] = AttendanceStatus.AGUARDANDO

    result = session.execute(
        update(Attendance)
        .where(
            Attendance.id == attendance_id,
            Attendance.version == payload.expected_version,
            Attendance.status != AttendanceStatus.ENCERRADO,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="Versão desatualizada ou atendimento encerrado")
    session.add(
        AttendanceEvent(
            attendance_id=attendance_id,
            type=EventType.TRANSFERIDO,
            actor_id=actor.id,
            from_status=attendance.status,
            to_status=values["status"],
            details={
                "from_actor_id": previous_assignee,
                "to_actor_id": target_actor_id,
                "from_group_id": previous_group_id,
                "to_group_id": target_group_id,
            },
        )
    )
    session.commit()
    attendance = load_attendance(session, attendance_id, actor)
    await broadcast(request, "attendance.transferred", attendance)
    return attendance


@router.post(
    "/attendances/{attendance_id}/messages",
    response_model=MessageRead,
    status_code=201,
)
async def send_message(
    attendance_id: str,
    payload: OutboundMessageCreate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Message:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_can_operate(attendance, actor)
    if attendance.status == AttendanceStatus.ENCERRADO:
        raise HTTPException(status_code=409, detail="Atendimento encerrado")

    existing = session.scalar(
        select(Message).where(Message.client_message_id == payload.client_message_id)
    )
    if existing:
        if existing.attendance_id != attendance.id:
            raise HTTPException(status_code=409, detail="Identificador de mensagem já usado")
        return existing

    message = Message(
        attendance_id=attendance.id,
        client_message_id=payload.client_message_id,
        direction=MessageDirection.SAIDA,
        sender_type=SenderType.ATENDENTE,
        actor_id=actor.id,
        content=payload.content,
        delivery_status=DeliveryStatus.PENDENTE,
    )
    session.add(message)
    session.flush()
    session.add_all(
        [
            OutboxMessage(
                message_id=message.id,
                payload={
                    "message_id": message.id,
                    "attendance_id": attendance.id,
                    "contact_id": attendance.contact_id,
                    "content": message.content,
                },
            ),
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.MENSAGEM_ENVIADA,
                actor_id=actor.id,
                details={"message_id": message.id},
            ),
        ]
    )
    session.commit()
    await broadcast(request, "message.created", attendance)
    return message


@router.post(
    "/attendances/{attendance_id}/attachments",
    response_model=MessageRead,
    status_code=201,
)
async def send_attachment(
    attendance_id: str,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    file: UploadFile = File(...),
    client_message_id: str = Form(...),
    caption: str = Form(""),
) -> Message:
    attendance = load_attendance(session, attendance_id, actor)
    ensure_can_operate(attendance, actor)
    if attendance.status == AttendanceStatus.ENCERRADO:
        raise HTTPException(status_code=409, detail="Atendimento encerrado")
    if not client_message_id or len(client_message_id) > 160 or len(caption) > 1024:
        raise HTTPException(status_code=422, detail="Identificador ou legenda inválidos")
    existing = session.scalar(
        select(Message).where(Message.client_message_id == client_message_id)
    )
    if existing:
        if existing.attendance_id != attendance.id:
            raise HTTPException(status_code=409, detail="Identificador de mensagem já usado")
        return existing

    data = await file.read(MAX_DOCUMENT_BYTES + 1)
    try:
        _, mime = validate_media(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    filename = safe_filename(file.filename, mime)
    storage_key = write_media(request.app.state.media_storage_dir, data)
    try:
        message = Message(
            attendance_id=attendance.id,
            client_message_id=client_message_id,
            direction=MessageDirection.SAIDA,
            sender_type=SenderType.ATENDENTE,
            actor_id=actor.id,
            content=caption.strip() or filename,
            delivery_status=DeliveryStatus.PENDENTE,
        )
        session.add(message)
        session.flush()
        session.add(MediaAttachment(
            message_id=message.id,
            storage_key=storage_key,
            mime_type=mime,
            filename=filename,
            size_bytes=len(data),
        ))
        session.add_all([
            OutboxMessage(
                message_id=message.id,
                payload={
                    "message_id": message.id,
                    "attendance_id": attendance.id,
                    "contact_id": attendance.contact_id,
                    "content": message.content,
                    "media": {
                        "storage_key": storage_key,
                        "mime_type": mime,
                        "filename": filename,
                        "caption": caption.strip(),
                    },
                },
            ),
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.MENSAGEM_ENVIADA,
                actor_id=actor.id,
                details={"message_id": message.id, "attachment": True},
            ),
        ])
        session.commit()
    except Exception:
        session.rollback()
        media_path(request.app.state.media_storage_dir, storage_key).unlink(missing_ok=True)
        raise
    await broadcast(request, "message.created", attendance)
    return message


@router.get("/attachments/{attachment_id}", response_class=FileResponse)
def download_attachment(
    attachment_id: str,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> FileResponse:
    attachment = session.get(MediaAttachment, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    message = session.get(Message, attachment.message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Anexo não encontrado")
    load_attendance(session, message.attendance_id, actor)
    path = media_path(request.app.state.media_storage_dir, attachment.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo indisponível")
    return FileResponse(
        path,
        media_type=attachment.mime_type,
        filename=attachment.filename,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/quick-replies", response_model=list[QuickReplyRead])
def list_quick_replies(
    session: SessionDep,
    actor: ActorDep,
    group_id: str | None = None,
) -> list[QuickReply]:
    query = select(QuickReply).where(
        QuickReply.company_id == actor.company_id,
        QuickReply.active.is_(True),
    )
    if group_id is not None:
        group = load_group(session, actor, group_id)
        if actor.role == ActorRole.ATENDENTE:
            link = session.get(GroupAgent, (group.id, actor.id))
            if link is None or not link.active:
                raise HTTPException(status_code=403, detail="Atendente não pertence ao grupo")
        query = query.where(
            (QuickReply.group_id.is_(None)) | (QuickReply.group_id == group_id)
        )
    elif actor.role == ActorRole.ATENDENTE:
        my_group_ids = select(GroupAgent.group_id).where(
            GroupAgent.agent_id == actor.id, GroupAgent.active.is_(True)
        )
        query = query.where(
            (QuickReply.group_id.is_(None)) | (QuickReply.group_id.in_(my_group_ids))
        )
    return list(session.scalars(query.order_by(QuickReply.title)))


@router.post("/quick-replies", response_model=QuickReplyRead, status_code=201)
def create_quick_reply(
    payload: QuickReplyCreate,
    session: SessionDep,
    actor: ActorDep,
) -> QuickReply:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode criar resposta rápida")
    if payload.group_id is not None:
        load_group(session, actor, payload.group_id)
    quick_reply = QuickReply(
        company_id=actor.company_id,
        group_id=payload.group_id,
        title=payload.title.strip(),
        content=payload.content.strip(),
        created_by=actor.id,
    )
    session.add(quick_reply)
    session.commit()
    session.refresh(quick_reply)
    return quick_reply


@router.delete("/quick-replies/{quick_reply_id}", status_code=204)
def delete_quick_reply(
    quick_reply_id: str,
    session: SessionDep,
    actor: ActorDep,
) -> None:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode remover resposta rápida")
    quick_reply = session.get(QuickReply, quick_reply_id)
    if quick_reply is None or quick_reply.company_id != actor.company_id:
        raise HTTPException(status_code=404, detail="Resposta rápida não encontrada")
    quick_reply.active = False
    session.commit()


@router.get("/integrations/outbox", response_model=list[OutboxItemRead])
def list_outbox(
    session: SessionDep,
    actor: ActorDep,
) -> list[dict]:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão vê a fila de envio")
    max_attempts = DEFAULT_MAX_ATTEMPTS
    rows = session.execute(
        select(OutboxMessage, Message, Attendance.contact_id)
        .join(Message, Message.id == OutboxMessage.message_id)
        .join(Attendance, Attendance.id == Message.attendance_id)
        .where(
            OutboxMessage.processed_at.is_(None),
            Attendance.id.in_(visible_attendance_ids(actor)),
        )
        .order_by(OutboxMessage.created_at)
    )
    return [
        {
            "id": outbox.id,
            "message_id": message.id,
            "attendance_id": message.attendance_id,
            "contact_id": contact_id,
            "content": message.content,
            "attempts": outbox.attempts,
            "max_attempts": max_attempts,
            "delivery_status": message.delivery_status,
            "created_at": outbox.created_at,
        }
        for outbox, message, contact_id in rows
    ]


@router.post("/integrations/outbox/{outbox_id}/retry", response_model=OutboxItemRead)
async def retry_outbox_item(
    outbox_id: str,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> dict:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode reprocessar envio")
    outbox = session.get(OutboxMessage, outbox_id)
    if outbox is None:
        raise HTTPException(status_code=404, detail="Item de envio não encontrado")
    if outbox.processed_at is not None:
        raise HTTPException(status_code=409, detail="Item já entregue")
    message = session.get(Message, outbox.message_id)
    if message is None:
        raise HTTPException(status_code=500, detail="Item de envio sem mensagem associada")
    load_attendance(session, message.attendance_id, actor)

    outbox.attempts = 0
    if message.delivery_status == DeliveryStatus.FALHA:
        message.delivery_status = DeliveryStatus.PENDENTE
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "outbox.retry_requested", "outbox_id": outbox_id}
    )
    return {
        "id": outbox.id,
        "message_id": message.id,
        "attendance_id": message.attendance_id,
        "contact_id": load_attendance(session, message.attendance_id).contact_id,
        "content": message.content,
        "attempts": outbox.attempts,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "delivery_status": message.delivery_status,
        "created_at": outbox.created_at,
    }


@router.websocket("/ws")
async def websocket_events(websocket: WebSocket) -> None:
    manager = websocket.app.state.realtime
    await websocket.accept()
    try:
        credentials = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        token = credentials.get("token") if isinstance(credentials, dict) else None
        if not isinstance(token, str) or not token:
            await websocket.close(code=1008)
            return
        with websocket.app.state.database.session_factory() as session:
            try:
                actor = authenticate_token(token, websocket.app.state.jwt_secret, session)
            except HTTPException:
                await websocket.close(code=1008)
                return
        manager.connect(websocket, actor)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except (asyncio.TimeoutError, ValueError):
        await websocket.close(code=1008)
    finally:
        manager.disconnect(websocket)
