import asyncio
import hashlib
import json
import hmac
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
    AI_CONTROLLED_MODES,
    ALLOWED_AUTOMATION_TRANSITIONS,
    ALLOWED_STATUS_TRANSITIONS,
    STALE_THRESHOLDS,
    ActorRole,
    AttendanceStatus,
    AutomationMode,
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
    AttendanceRating,
    AttendanceEvent,
    AttendanceTag,
    Contact,
    Company,
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
    new_id,
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
    AutomationModeCommand,
    ClaimRequest,
    ContactRead,
    HandoffRequest,
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
    QuickReplyUpdate,
    StatusChangeRequest,
    TimelineItem,
    TokenResponse,
    TransferRequest,
)
from app.security import create_access_token, hash_password, verify_password
from app.internal_contract import InternalInbound, InternalStatus, InternalHandoff

router = APIRouter(prefix="/api/v1")
SessionDep = Annotated[Session, Depends(get_session)]
ActorDep = Annotated[Actor, Depends(get_actor)]


def require_internal_key(request: Request) -> None:
    expected = request.app.state.channel_gateway_internal_key
    supplied = request.headers.get("X-Internal-Key", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Chave interna inválida")


def internal_event_key(tenant_id: str, event_id: str, channel_account_id: str = "") -> str:
    return "gateway:" + hashlib.sha256(
        f"{tenant_id}:{channel_account_id}:{event_id}".encode()
    ).hexdigest()


@router.post("/internal/messages/inbound", response_model=InboundResult, status_code=201)
async def receive_internal_message(payload: InternalInbound, request: Request, session: SessionDep) -> InboundResult:
    require_internal_key(request)
    tenant = session.get(Company, payload.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=404, detail="Tenant não provisionado")
    event_key = internal_event_key(payload.tenant_id, f"inbound:{payload.event_id}", payload.channel_account_id)
    message_key = internal_event_key(payload.tenant_id, f"message:{payload.message_id}", payload.channel_account_id)
    if payload.message.type == "choice":
        rating = await process_rating_reply(
            request, session, tenant_id=payload.tenant_id,
            contact_id=payload.sender.id, event_id=event_key,
            message_id=message_key, choice_id=payload.message.choice_id or "",
            title=payload.message.text,
            channel_account_id=payload.channel_account_id,
            conversation_id=payload.conversation_id,
        )
        if rating is not None:
            return rating
        if (payload.message.choice_id or "").startswith("rating:"):
            raise HTTPException(status_code=422, detail="Avaliação inválida")
    result = await process_inbound_message(
        InboundMessageCreate(
            external_event_id=event_key,
            external_message_id=message_key,
            contact_id=payload.sender.id,
            content=payload.message.text or payload.message.choice_id or "",
            company_id=payload.tenant_id,
            channel_account_id=payload.channel_account_id,
            channel_conversation_id=payload.conversation_id,
        ), request, session,
    )
    if payload.sender.name:
        contact = find_contact(session, payload.tenant_id, payload.sender.id)
        if contact and contact.display_name != payload.sender.name:
            contact.display_name = payload.sender.name
            session.commit()
    return result


@router.post("/internal/messages/status")
async def receive_internal_status(payload: InternalStatus, request: Request, session: SessionDep) -> dict[str, bool]:
    require_internal_key(request)
    tenant = session.get(Company, payload.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=404, detail="Tenant não provisionado")
    mapped = {
        "QUEUED": DeliveryStatus.ENVIADA_AO_MIDDLEWARE,
        "SENT": DeliveryStatus.ACEITA_PELO_PROVEDOR,
        "DELIVERED": DeliveryStatus.ENTREGUE,
        "READ": DeliveryStatus.LIDA,
        "FAILED": DeliveryStatus.FALHA,
    }[payload.status]
    return {"processed": await process_delivery_status(
        session, request, tenant_id=payload.tenant_id,
        event_id=internal_event_key(payload.tenant_id, f"status:{payload.event_id}", payload.channel_account_id or ""),
        status=mapped, external_id=payload.message_id,
        outbox_id=payload.idempotency_key,
    )}


@router.post("/internal/handoff", response_model=AttendanceRead)
async def receive_internal_handoff(payload: InternalHandoff, request: Request, session: SessionDep) -> Attendance:
    require_internal_key(request)
    tenant = session.get(Company, payload.tenant_id)
    if tenant is None or not tenant.active:
        raise HTTPException(status_code=404, detail="Tenant não provisionado")
    event_key = internal_event_key(payload.tenant_id, f"handoff:{payload.event_id}", payload.channel_account_id)
    previous = session.get(IntegrationEvent, event_key)
    if previous:
        attendance = load_attendance(session, previous.attendance_id)
        if attendance.company_id != payload.tenant_id:
            raise HTTPException(status_code=409, detail="Evento pertence a outro tenant")
        return attendance
    attendance = session.scalar(select(Attendance).where(
        Attendance.company_id == payload.tenant_id,
        Attendance.channel_account_id == payload.channel_account_id,
        Attendance.channel_conversation_id == payload.conversation_id,
        Attendance.status.in_(ACTIVE_STATUSES),
    ).order_by(Attendance.created_at.desc()))
    if attendance is None:
        group = session.scalar(select(ServiceGroup).where(
            ServiceGroup.company_id == payload.tenant_id,
            ServiceGroup.active.is_(True),
        ).order_by(ServiceGroup.created_at))
        if group is None:
            raise HTTPException(status_code=422, detail="Tenant sem grupo ativo")
        contact_id = payload.sender_id or payload.conversation_id
        if find_contact(session, payload.tenant_id, contact_id) is None:
            session.add(Contact(id=new_id(), company_id=payload.tenant_id, external_id=contact_id))
        attendance = Attendance(
            contact_id=contact_id, company_id=payload.tenant_id, group_id=group.id,
            channel_account_id=payload.channel_account_id,
            channel_conversation_id=payload.conversation_id,
        )
        session.add(attendance)
        session.flush()
        session.add(AttendanceEvent(
            attendance_id=attendance.id, type=EventType.CRIADO,
            to_status=AttendanceStatus.AGUARDANDO,
            details={"source": "internal_handoff"},
        ))
    actor = Actor(id="middleware", role=ActorRole.ADMIN, company_id=payload.tenant_id)
    if attendance.automation_mode != AutomationMode.HUMAN_REQUESTED:
        attendance = _apply_automation_transition(
            session, attendance, actor, AutomationMode.HUMAN_REQUESTED,
            attendance.version, payload.reason, payload.summary,
            "attendance.handoff_requested",
            commit=False,
        )
    session.add(IntegrationEvent(
        external_event_id=event_key, attendance_id=attendance.id, message_id="",
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        previous = session.get(IntegrationEvent, event_key)
        if previous is None:
            raise
        attendance = load_attendance(session, previous.attendance_id)
    await broadcast(request, "attendance.handoff_requested", attendance)
    return attendance


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, session: SessionDep) -> dict:
    agent = session.get(Agent, payload.id)
    if agent is None or not agent.active or not verify_password(payload.password, agent.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    company = session.get(Company, agent.company_id)
    if company is None or not company.active:
        raise HTTPException(status_code=401, detail="Empresa inativa")
    token, expires_at = create_access_token(
        agent.id, agent.role.value, agent.company_id, request.app.state.jwt_secret
    )
    return {
        "access_token": token,
        "expires_at": expires_at,
        "actor": {"id": agent.id, "role": agent.role, "company_id": agent.company_id},
    }


@router.get("/me/company")
def get_my_company(session: SessionDep, actor: ActorDep) -> dict[str, str]:
    company = session.get(Company, actor.company_id)
    if company is None:
        raise HTTPException(status_code=404, detail="Empresa não encontrada")
    return {"id": company.id, "name": company.name}


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
    if payload.load_cost_per_attendance > (payload.max_active_attendances or payload.max_load_per_agent):
        raise HTTPException(status_code=422, detail="Custo de carga não pode superar capacidade do grupo")
    group = ServiceGroup(
        company_id=actor.company_id,
        name=payload.name.strip(),
        max_load_per_agent=payload.max_load_per_agent,
        max_active_attendances=payload.max_active_attendances or payload.max_load_per_agent,
        load_cost_per_attendance=payload.load_cost_per_attendance,
        queue_wait_message=payload.queue_wait_message.strip(),
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
    query = select(ServiceGroup).where(ServiceGroup.company_id == actor.company_id)
    if actor.role == ActorRole.ATENDENTE:
        query = query.where(ServiceGroup.id.in_(select(GroupAgent.group_id).where(
            GroupAgent.agent_id == actor.id, GroupAgent.active.is_(True)
        )))
    return list(session.scalars(query.order_by(ServiceGroup.name)))


@router.get("/groups/transfer-targets", response_model=list[GroupRead])
def list_transfer_targets(session: SessionDep, actor: ActorDep) -> list[ServiceGroup]:
    return list(session.scalars(select(ServiceGroup).where(
        ServiceGroup.company_id == actor.company_id,
        ServiceGroup.active.is_(True),
    ).order_by(ServiceGroup.name)))


def load_group(session: Session, actor: Actor, group_id: str) -> ServiceGroup:
    group = session.get(ServiceGroup, group_id)
    if group is None or group.company_id != actor.company_id:
        raise HTTPException(status_code=404, detail="Grupo não encontrado")
    return group


def group_active_load(session: Session, group: ServiceGroup) -> int:
    """Return current load units consumed by assigned conversations."""
    active_count = int(session.scalar(select(func.count(Attendance.id)).where(
        Attendance.group_id == group.id,
        Attendance.assignee_id.is_not(None),
        Attendance.status.in_(ACTIVE_STATUSES),
    )) or 0)
    return active_count * group.load_cost_per_attendance


def ensure_group_capacity(session: Session, group: ServiceGroup, attendance: Attendance | None = None) -> None:
    """Limit concurrent assigned conversation load across the entire group."""
    if attendance is not None and attendance.group_id == group.id and attendance.assignee_id is not None and attendance.status in ACTIVE_STATUSES:
        return
    if group_active_load(session, group) + group.load_cost_per_attendance > group.max_active_attendances:
        raise HTTPException(status_code=409, detail="Grupo atingiu a capacidade total de carga")


@router.patch("/groups/{group_id}", response_model=GroupRead)
def update_group(
    group_id: str, payload: GroupUpdate, session: SessionDep, actor: ActorDep
) -> ServiceGroup:
    if actor.role != ActorRole.ADMIN:
        raise HTTPException(status_code=403, detail="Apenas administrador edita grupos")
    group = load_group(session, actor, group_id)
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=422, detail="Nome do grupo obrigatório")
        group.name = payload.name.strip()
    if payload.max_load_per_agent is not None:
        group.max_load_per_agent = payload.max_load_per_agent
    if payload.max_active_attendances is not None:
        group.max_active_attendances = payload.max_active_attendances
    if payload.load_cost_per_attendance is not None:
        group.load_cost_per_attendance = payload.load_cost_per_attendance
    if payload.queue_wait_message is not None:
        if not payload.queue_wait_message.strip():
            raise HTTPException(status_code=422, detail="Mensagem de espera obrigatória")
        group.queue_wait_message = payload.queue_wait_message.strip()
    if group.load_cost_per_attendance > group.max_active_attendances:
        raise HTTPException(status_code=422, detail="Custo de carga não pode superar capacidade do grupo")
    if payload.active is not None:
        if not payload.active and group.active:
            open_attendance = session.scalar(select(Attendance.id).where(
                Attendance.group_id == group.id,
                Attendance.status.in_(ACTIVE_STATUSES),
            ))
            if open_attendance is not None:
                raise HTTPException(status_code=409, detail="Transfira atendimentos abertos antes de desativar o grupo")
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
    if not group.active:
        raise HTTPException(status_code=409, detail="Grupo de atendimento inativo")
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
    ensure_group_capacity(session, group)
    capacity = link.max_load_override or group.max_active_attendances
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
    if attendance.automation_mode == AutomationMode.HUMAN_REQUESTED:
        attendance.automation_mode = AutomationMode.HUMAN_ACTIVE
        attendance.automation_updated_at = now_utc()
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
    if attendance.group_id is None:
        return
    group = session.get(ServiceGroup, attendance.group_id)
    if group is None or not group.active:
        raise HTTPException(status_code=409, detail="Grupo de atendimento inativo")
    ensure_group_capacity(session, group, attendance)
    if actor.role != ActorRole.ATENDENTE:
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
    capacity = link.max_load_override or group.max_active_attendances
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


async def broadcast(request: Request, kind: str, attendance: Attendance, previous_group_id: str | None = None) -> None:
    await request.app.state.realtime.publish(
        {
            "type": kind,
            "attendance": jsonable_encoder(AttendanceRead.model_validate(attendance)),
            "previous_group_id": previous_group_id,
        }
    )


def find_contact(session: Session, company_id: str, external_id: str) -> Contact | None:
    return session.scalar(select(Contact).where(
        Contact.company_id == company_id,
        Contact.external_id == external_id,
    ))


async def process_inbound_message(
    payload: InboundMessageCreate,
    request: Request,
    session: Session,
    media_file: tuple[bytes, str, str] | None = None,
) -> InboundResult:
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

    processed = session.get(IntegrationEvent, payload.external_event_id)
    if processed:
        attendance = load_attendance(session, processed.attendance_id)
        if attendance.company_id != group.company_id:
            raise HTTPException(status_code=409, detail="Evento externo pertence a outra empresa")
        message = session.get(Message, processed.message_id)
        if message is None:
            raise HTTPException(status_code=500, detail="Evento processado sem mensagem")
        return InboundResult(attendance=attendance, message=message, duplicate=True)

    previous_message = session.scalar(select(Message).where(Message.external_id == payload.external_message_id))
    if previous_message is not None:
        attendance = load_attendance(session, previous_message.attendance_id)
        if attendance.company_id != group.company_id:
            raise HTTPException(status_code=409, detail="Mensagem externa pertence a outra empresa")
        return InboundResult(attendance=attendance, message=previous_message, duplicate=True)

    contact = find_contact(session, group.company_id, payload.contact_id)
    if contact is None:
        session.add(Contact(id=new_id(), company_id=group.company_id, external_id=payload.contact_id))

    attendance = None
    if payload.channel_account_id and payload.channel_conversation_id:
        attendance = session.scalar(select(Attendance).where(
            Attendance.company_id == group.company_id,
            Attendance.channel_account_id == payload.channel_account_id,
            Attendance.channel_conversation_id == payload.channel_conversation_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        ).order_by(Attendance.created_at.desc()))
        if attendance is not None and attendance.contact_id != payload.contact_id:
            attendance.contact_id = payload.contact_id

    attendance_query = (
        select(Attendance)
        .where(
            Attendance.contact_id == payload.contact_id,
            Attendance.company_id == group.company_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Attendance.created_at.desc())
    )
    if payload.channel_account_id:
        attendance_query = attendance_query.where(
            Attendance.channel_account_id == payload.channel_account_id
        )
    if payload.channel_conversation_id:
        attendance_query = attendance_query.where(
            Attendance.channel_conversation_id == payload.channel_conversation_id
        )
    if attendance is None:
        attendance = session.scalar(attendance_query)
    created = attendance is None
    if created:
        attendance = Attendance(
            contact_id=payload.contact_id,
            company_id=group.company_id,
            group_id=group.id,
            load_weight=payload.load_weight,
            priority=payload.priority,
            channel_account_id=payload.channel_account_id,
            channel_conversation_id=payload.channel_conversation_id,
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

    if payload.channel_account_id and attendance.channel_account_id != payload.channel_account_id:
        attendance.channel_account_id = payload.channel_account_id
    if (
        payload.channel_conversation_id
        and attendance.channel_conversation_id != payload.channel_conversation_id
    ):
        attendance.channel_conversation_id = payload.channel_conversation_id

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
    if created and group_active_load(session, group) + group.load_cost_per_attendance > group.max_active_attendances:
        waiting_reply = Message(
            attendance_id=attendance.id,
            direction=MessageDirection.SAIDA,
            sender_type=SenderType.BOT,
            content=group.queue_wait_message,
            delivery_status=DeliveryStatus.PENDENTE,
        )
        session.add(waiting_reply)
        session.flush()
        session.add_all([
            OutboxMessage(
                message_id=waiting_reply.id,
                payload={
                    "message_id": waiting_reply.id,
                    "attendance_id": attendance.id,
                    "contact_id": attendance.contact_id,
                    "content": waiting_reply.content,
                    "company_id": attendance.company_id,
                    "channel_account_id": attendance.channel_account_id,
                    "channel_conversation_id": attendance.channel_conversation_id,
                },
            ),
            AttendanceEvent(
                attendance_id=attendance.id,
                type=EventType.MENSAGEM_ENVIADA,
                details={"message_id": waiting_reply.id, "reason": "group_capacity_wait"},
            ),
        ])
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
                details={
                    "message_id": message.id,
                    # P0: gateway de IA ainda não integrado. Registramos apenas
                    # se o domínio consideraria esta mensagem "para a IA" para
                    # que fluxos futuros possam rotear sem reprocessar. Nada é
                    # enviado à IA neste estágio.
                    "ai_candidate": attendance.automation_mode in AI_CONTROLLED_MODES,
                    "automation_mode": attendance.automation_mode.value,
                },
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
        if attendance.company_id != group.company_id:
            raise HTTPException(status_code=409, detail="Evento externo pertence a outra empresa")
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
@router.get("/integrations/meta/whatsapp/webhook/{phone_number_id}", response_class=PlainTextResponse)
async def verify_meta_webhook(request: Request, phone_number_id: str | None = None):
    if phone_number_id is None and request.app.state.meta_tenants:
        raise HTTPException(status_code=404, detail="Use webhook da empresa")
    tenant = request.app.state.meta_tenants.get(phone_number_id) if phone_number_id else None
    if phone_number_id and tenant is None:
        raise HTTPException(status_code=404, detail="Número Meta não configurado")
    expected_token = tenant["verify_token"] if tenant else request.app.state.meta_verify_token
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if (
        mode != "subscribe"
        or not challenge
        or not expected_token
        or token != expected_token
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


async def process_delivery_status(
    session: Session, request: Request, *, tenant_id: str | None,
    event_id: str, status: DeliveryStatus, external_id: str | None,
    outbox_id: str | None = None,
) -> bool:
    if session.get(IntegrationEvent, event_id):
        return True
    outbox = session.get(OutboxMessage, outbox_id) if outbox_id else None
    message = session.get(Message, outbox.message_id) if outbox else None
    if message is None and external_id:
        message = session.scalar(select(Message).where(Message.external_id == external_id))
    if message is None:
        return False
    attendance = session.get(Attendance, message.attendance_id)
    if attendance is None or (tenant_id is not None and attendance.company_id != tenant_id):
        session.rollback()
        return False
    if external_id and message.external_id != external_id:
        other = session.scalar(select(Message).where(Message.external_id == external_id))
        if other is not None and other.id != message.id:
            return False
        message.external_id = external_id
    current_rank = DELIVERY_RANK.get(message.delivery_status, 0)
    next_rank = DELIVERY_RANK[status]
    if outbox is None:
        outbox = session.scalar(
            select(OutboxMessage).where(OutboxMessage.message_id == message.id)
        )
    if status == DeliveryStatus.FALHA:
        if current_rank < DELIVERY_RANK[DeliveryStatus.ENTREGUE]:
            message.delivery_status = status
            if outbox is not None:
                outbox.processed_at = None
                outbox.attempts = DEFAULT_MAX_ATTEMPTS
    elif message.delivery_status != DeliveryStatus.FALHA and next_rank >= current_rank:
        message.delivery_status = status
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
    await broadcast(request, "message.status_changed", attendance)
    return True


async def process_meta_status(status: dict, request: Request, session: Session, company_id: str | None = None) -> bool:
    external_id = status.get("id")
    mapped = META_STATUS_MAP.get(status.get("status"))
    if not external_id or mapped is None:
        return False
    fingerprint = hashlib.sha256(
        json.dumps(status, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return await process_delivery_status(
        session, request, tenant_id=company_id,
        event_id=f"meta-status:{fingerprint}", status=mapped,
        external_id=external_id, outbox_id=status.get("biz_opaque_callback_data"),
    )


async def process_rating_reply(
    request: Request, session: Session, *, tenant_id: str | None,
    contact_id: str, event_id: str, message_id: str,
    choice_id: str, title: str | None,
    channel_account_id: str | None = None, conversation_id: str | None = None,
) -> InboundResult | None:
    parts = choice_id.split(":")
    if len(parts) != 3 or parts[0] != "rating" or parts[2] not in {"1", "2", "3", "4", "5"}:
        return None
    attendance = session.get(Attendance, parts[1])
    if (
        attendance is None or attendance.status != AttendanceStatus.ENCERRADO
        or attendance.contact_id != contact_id
        or (tenant_id is not None and attendance.company_id != tenant_id)
        or (channel_account_id is not None and attendance.channel_account_id != channel_account_id)
        or (conversation_id is not None and attendance.channel_conversation_id != conversation_id)
    ):
        return None
    processed = session.get(IntegrationEvent, event_id)
    if processed:
        message = session.get(Message, processed.message_id)
        return InboundResult(attendance=attendance, message=message, duplicate=True) if message else None
    survey = session.scalar(select(Message.id).where(
        Message.attendance_id == attendance.id,
        Message.interaction_kind == "list",
        Message.direction == MessageDirection.SAIDA,
    ))
    if survey is None:
        return None
    previous_rating = session.get(AttendanceRating, attendance.id)
    if previous_rating:
        message = session.get(Message, previous_rating.message_id)
        return InboundResult(attendance=attendance, message=message, duplicate=True) if message else None
    message = Message(
        id=new_id(),
        attendance_id=attendance.id,
        external_id=message_id,
        direction=MessageDirection.ENTRADA,
        sender_type=SenderType.CLIENTE,
        content=title or f"Nota {parts[2]}",
        delivery_status=DeliveryStatus.RECEBIDA,
    )
    session.add(message)
    session.add(AttendanceRating(attendance_id=attendance.id, score=int(parts[2]), message_id=message.id))
    session.add(AttendanceEvent(
        attendance_id=attendance.id, type=EventType.AVALIADO,
        details={"score": int(parts[2]), "message_id": message.id},
    ))
    session.add(IntegrationEvent(
        external_event_id=event_id, attendance_id=attendance.id, message_id=message.id,
    ))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        previous_rating = session.get(AttendanceRating, attendance.id)
        if previous_rating:
            previous_message = session.get(Message, previous_rating.message_id)
            if previous_message:
                return InboundResult(attendance=attendance, message=previous_message, duplicate=True)
        raise
    await broadcast(request, "attendance.rated", attendance)
    return InboundResult(attendance=attendance, message=message, duplicate=False)


async def process_meta_rating(
    item: dict, reply: dict, request: Request, session: Session, company_id: str | None
) -> tuple[bool, bool]:
    if not item.get("id") or not item.get("from"):
        return False, False
    result = await process_rating_reply(
        request, session, tenant_id=company_id,
        contact_id=item["from"], event_id=f"meta-message:{item['id']}",
        message_id=item["id"], choice_id=reply.get("id") or "",
        title=reply.get("title"),
    )
    return (result is not None, result.duplicate if result else False)


@router.post("/integrations/meta/whatsapp/webhook")
@router.post("/integrations/meta/whatsapp/webhook/{phone_number_id}")
async def receive_meta_webhook(
    request: Request,
    session: SessionDep,
    phone_number_id: str | None = None,
) -> dict:
    if phone_number_id is None and request.app.state.meta_tenants:
        raise HTTPException(status_code=404, detail="Use webhook da empresa")
    tenant = request.app.state.meta_tenants.get(phone_number_id) if phone_number_id else None
    if phone_number_id and tenant is None:
        raise HTTPException(status_code=404, detail="Número Meta não configurado")
    body = await request.body()
    try:
        verify_meta_signature(
            body,
            request.headers.get("X-Hub-Signature-256"),
            tenant["app_secret"] if tenant else request.app.state.meta_app_secret,
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

    webhook_company_id = tenant["company_id"] if tenant else None
    if webhook_company_id is None:
        default_group = (
            session.get(ServiceGroup, request.app.state.meta_default_group_id)
            if request.app.state.meta_default_group_id else None
        )
        if default_group is None:
            groups = list(session.scalars(select(ServiceGroup).where(ServiceGroup.active.is_(True)).limit(2)))
            default_group = groups[0] if len(groups) == 1 else None
        webhook_company_id = default_group.company_id if default_group else None

    processed = 0
    duplicates = 0
    ignored = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                ignored += 1
                continue
            value = change.get("value") or {}
            if tenant and (value.get("metadata") or {}).get("phone_number_id") != phone_number_id:
                raise HTTPException(status_code=422, detail="Número Meta do webhook não corresponde à rota")
            profiles = {
                item.get("wa_id"): (item.get("profile") or {}).get("name")
                for item in value.get("contacts", [])
            }
            for item in value.get("messages", []):
                contact_id = item.get("from")
                external_id = item.get("id")
                kind = item.get("type")
                if kind not in {"text", "image", "document", "interactive", "button"} or not contact_id or not external_id:
                    ignored += 1
                    continue
                media_file = None
                if kind == "text":
                    content = (item.get("text") or {}).get("body")
                    if not content:
                        ignored += 1
                        continue
                elif kind in {"interactive", "button"}:
                    if kind == "interactive":
                        interactive = item.get("interactive") or {}
                        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                        if interactive.get("type") == "list_reply":
                            rated, duplicate = await process_meta_rating(
                                item, reply, request, session,
                                webhook_company_id,
                            )
                            if rated:
                                processed += 1
                                duplicates += int(duplicate)
                                continue
                    else:
                        reply = item.get("button") or {}
                    content = reply.get("title") or reply.get("text")
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
                            downloader = (
                                request.app.state.meta_media_downloaders[phone_number_id]
                                if tenant else request.app.state.meta_media_downloader
                            )
                            data = await downloader.download(media_id)
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
                        company_id=webhook_company_id,
                        group_id=tenant["group_id"] if tenant else request.app.state.meta_default_group_id or None,
                    ),
                    request,
                    session,
                    media_file,
                )
                contact = find_contact(session, result.attendance.company_id, contact_id)
                if contact:
                    contact.phone = contact_id
                    contact.display_name = profiles.get(contact_id) or contact.display_name
                    session.commit()
                processed += 1
                duplicates += int(result.duplicate)
            for status in value.get("statuses", []):
                if await process_meta_status(status, request, session, webhook_company_id):
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
    last_attendance = latest_attendance_for_contact(session, contact_id, actor)
    if last_attendance is None:
        raise HTTPException(status_code=404, detail="Contato não encontrado")
    contact = find_contact(session, actor.company_id, contact_id)
    if contact:
        tags = list(session.scalars(
            select(ContactTag.name)
            .where(ContactTag.contact_id == contact.id)
            .order_by(ContactTag.name)
        ))
        return ContactRead(
            id=contact.external_id,
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
        Contact.company_id == actor.company_id,
        Contact.external_id.in_(
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
            | (Contact.external_id.ilike(pattern))
        )
    query = query.order_by(Contact.updated_at.desc())
    contacts = list(session.scalars(query))
    if not contacts:
        return []

    contact_ids = [contact.id for contact in contacts]
    external_ids = [contact.external_id for contact in contacts]
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
            Attendance.contact_id.in_(external_ids),
            Attendance.id.in_(visible_attendance_ids(actor)),
        )
        .order_by(Attendance.updated_at.desc())
    ):
        last_attendance_by_contact.setdefault(attendance.contact_id, attendance)

    results = []
    for contact in contacts:
        last = last_attendance_by_contact.get(contact.external_id)
        results.append(
            ContactRead(
                id=contact.external_id,
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

    contact = find_contact(session, actor.company_id, contact_id)
    if contact is None:
        contact = Contact(id=new_id(), company_id=actor.company_id, external_id=contact_id)
        session.add(contact)
        session.flush()
    if contact.stage != payload.stage:
        contact.stage = payload.stage
        contact.updated_at = now_utc()
        session.add(
            ContactAuditEvent(
                contact_id=contact_id,
                company_id=actor.company_id,
                actor_id=actor.id,
                changed_fields=["stage"],
            )
        )
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "contact.updated", "contact_id": contact_id, "company_id": actor.company_id}
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

    contact = find_contact(session, actor.company_id, contact_id)
    current_tags = set(session.scalars(
        select(ContactTag.name).where(ContactTag.contact_id == contact.id)
    )) if contact else set()
    changed_fields: list[str] = []
    if contact is None:
        contact = Contact(id=new_id(), company_id=actor.company_id, external_id=contact_id)
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
        session.execute(delete(ContactTag).where(ContactTag.contact_id == contact.id))
        session.add_all(
            [ContactTag(contact_id=contact.id, name=tag) for tag in normalized_tags]
        )
        changed_fields.append("tags")
    if changed_fields:
        contact.updated_at = now_utc()
        session.add(
            ContactAuditEvent(
                contact_id=contact_id,
                company_id=actor.company_id,
                actor_id=actor.id,
                changed_fields=changed_fields,
            )
        )
    session.commit()
    await request.app.state.realtime.publish(
        {"type": "contact.updated", "contact_id": contact_id, "company_id": actor.company_id}
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
                Message.sender_type == SenderType.ATENDENTE,
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
    ratings = list(session.scalars(
        select(AttendanceRating.score).where(
            AttendanceRating.attendance_id.in_(visible_attendance_ids(actor))
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
        "average_rating": sum(ratings) / len(ratings) if ratings else None,
        "ratings_count": len(ratings),
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
        .outerjoin(Contact, (Contact.external_id == Attendance.contact_id) & (Contact.company_id == Attendance.company_id))
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
    human_handoff = attendance.automation_mode == AutomationMode.HUMAN_REQUESTED
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
            automation_mode=AutomationMode.HUMAN_ACTIVE if human_handoff else attendance.automation_mode,
            automation_updated_at=now_utc() if human_handoff else attendance.automation_updated_at,
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
        survey_options = [
            {"id": f"rating:{attendance_id}:{score}", "title": f"{score} estrela{'s' if score > 1 else ''}"}
            for score in range(1, 6)
        ]
        survey = Message(
            attendance_id=attendance_id,
            direction=MessageDirection.SAIDA,
            sender_type=SenderType.BOT,
            content="Como você avalia este atendimento? Escolha uma nota de 1 a 5.",
            buttons=survey_options,
            interaction_kind="list",
            delivery_status=DeliveryStatus.PENDENTE,
        )
        session.add(survey)
        session.flush()
        session.add(OutboxMessage(
            message_id=survey.id,
            payload={
                "message_id": survey.id,
                "attendance_id": attendance_id,
                "company_id": attendance.company_id,
                "contact_id": attendance.contact_id,
                "content": survey.content,
                "channel_account_id": attendance.channel_account_id,
                "channel_conversation_id": attendance.channel_conversation_id,
                "list_options": survey_options,
            },
        ))
    session.commit()
    attendance = load_attendance(session, attendance_id, actor)
    await broadcast(request, "attendance.status_changed", attendance)
    return attendance


_AUTOMATION_EVENT_TYPE = {
    AutomationMode.HUMAN_REQUESTED: EventType.HANDOFF_SOLICITADO,
    AutomationMode.HUMAN_ACTIVE: EventType.HANDOFF_SOLICITADO,
    AutomationMode.AI_ACTIVE: EventType.IA_RETOMADA,
    AutomationMode.PAUSED: EventType.AUTOMACAO_PAUSADA,
}


def _apply_automation_transition(
    session: Session,
    attendance: Attendance,
    actor: Actor,
    target: AutomationMode,
    expected_version: int,
    reason: str | None,
    ai_summary: str | None,
    broadcast_kind: str,
    commit: bool = True,
) -> Attendance:
    """Aplica transição de modo de automação com trava otimista e auditoria.

    Centraliza validação/versionamento/evento para os três endpoints de handoff
    (uma única guarda em vez de repetir em cada rota).
    """
    ensure_can_operate(attendance, actor)
    current = attendance.automation_mode
    if target != current and target not in ALLOWED_AUTOMATION_TRANSITIONS.get(current, set()):
        raise HTTPException(
            status_code=422,
            detail=f"Transição de automação inválida: {current.value} -> {target.value}",
        )

    values = {
        "automation_mode": target,
        "automation_updated_at": now_utc(),
        "version": Attendance.version + 1,
        "updated_at": now_utc(),
    }
    if reason is not None:
        values["handoff_reason"] = reason.strip() or None
    if ai_summary is not None:
        values["ai_summary"] = ai_summary.strip() or None

    result = session.execute(
        update(Attendance)
        .where(
            Attendance.id == attendance.id,
            Attendance.company_id == actor.company_id,
            Attendance.version == expected_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="Versão desatualizada")

    session.add(
        AttendanceEvent(
            attendance_id=attendance.id,
            type=_AUTOMATION_EVENT_TYPE[target],
            actor_id=actor.id,
            details={
                "from_mode": current.value,
                "to_mode": target.value,
                "reason": (reason.strip() if reason else None),
            },
        )
    )
    if commit:
        session.commit()
    else:
        session.flush()
        session.expire(attendance)
    refreshed = load_attendance(session, attendance.id, actor)
    return refreshed


@router.post("/attendances/{attendance_id}/handoff", response_model=AttendanceRead)
async def request_handoff(
    attendance_id: str,
    payload: HandoffRequest,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    target = AutomationMode.HUMAN_ACTIVE if payload.take_over else AutomationMode.HUMAN_REQUESTED
    attendance = _apply_automation_transition(
        session,
        attendance,
        actor,
        target=target,
        expected_version=payload.expected_version,
        reason=payload.reason,
        ai_summary=payload.ai_summary,
        broadcast_kind="attendance.handoff_requested",
    )
    await broadcast(request, "attendance.handoff_requested", attendance)
    return attendance


@router.post("/attendances/{attendance_id}/resume-ai", response_model=AttendanceRead)
async def resume_ai(
    attendance_id: str,
    payload: AutomationModeCommand,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    attendance = _apply_automation_transition(
        session,
        attendance,
        actor,
        target=AutomationMode.AI_ACTIVE,
        expected_version=payload.expected_version,
        reason=payload.reason,
        ai_summary=None,
        broadcast_kind="attendance.ai_resumed",
    )
    await broadcast(request, "attendance.ai_resumed", attendance)
    return attendance


@router.post("/attendances/{attendance_id}/pause-ai", response_model=AttendanceRead)
async def pause_ai(
    attendance_id: str,
    payload: AutomationModeCommand,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    attendance = _apply_automation_transition(
        session,
        attendance,
        actor,
        target=AutomationMode.PAUSED,
        expected_version=payload.expected_version,
        reason=payload.reason,
        ai_summary=None,
        broadcast_kind="attendance.automation_paused",
    )
    await broadcast(request, "attendance.automation_paused", attendance)
    return attendance


@router.post("/attendances/{attendance_id}/transfer", response_model=AttendanceRead)
async def transfer_attendance(
    attendance_id: str,
    payload: TransferRequest,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> Attendance:
    attendance = load_attendance(session, attendance_id, actor)
    if actor.role == ActorRole.ATENDENTE:
        ensure_can_operate(attendance, actor)
        if payload.target_group_id is None or payload.target_actor_id is not None:
            raise HTTPException(status_code=403, detail="Atendente só pode redirecionar para fila de outro grupo")
        if payload.target_group_id == attendance.group_id:
            raise HTTPException(status_code=422, detail="Escolha outro grupo")
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
            link = session.scalar(select(GroupAgent).where(
                GroupAgent.group_id == target_group_id,
                GroupAgent.agent_id == target_actor_id,
                GroupAgent.active.is_(True),
            ).with_for_update())
            if link is None or not link.active:
                raise HTTPException(
                    status_code=422, detail="Atendente de destino não pertence ao grupo"
                )
            group = session.get(ServiceGroup, target_group_id)
            if group is None or not group.active:
                raise HTTPException(status_code=422, detail="Grupo de destino inativo")
            ensure_group_capacity(session, group, attendance)
            capacity = link.max_load_override or group.max_active_attendances
            current_load = int(session.scalar(select(func.coalesce(func.sum(Attendance.load_weight), 0)).where(
                Attendance.assignee_id == target_actor_id,
                Attendance.company_id == actor.company_id,
                Attendance.status.in_(ACTIVE_STATUSES),
            )) or 0)
            additional = 0 if target_actor_id == attendance.assignee_id else attendance.load_weight
            if current_load + additional > capacity:
                raise HTTPException(status_code=409, detail="Atendente de destino atingiu a capacidade")

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
    attendance = load_attendance(session, attendance_id)
    await broadcast(request, "attendance.transferred", attendance, previous_group_id)
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
        buttons=[button.model_dump() for button in payload.buttons],
        interaction_kind="button" if payload.buttons else None,
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
                    "company_id": attendance.company_id,
                    "channel_account_id": attendance.channel_account_id,
                    "channel_conversation_id": attendance.channel_conversation_id,
                    **({"buttons": message.buttons} if message.buttons else {}),
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
                    "company_id": attendance.company_id,
                    "channel_account_id": attendance.channel_account_id,
                    "channel_conversation_id": attendance.channel_conversation_id,
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
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=422, detail="Título e conteúdo são obrigatórios")
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


@router.patch("/quick-replies/{quick_reply_id}", response_model=QuickReplyRead)
def update_quick_reply(
    quick_reply_id: str,
    payload: QuickReplyUpdate,
    session: SessionDep,
    actor: ActorDep,
) -> QuickReply:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode editar resposta rápida")
    reply = session.get(QuickReply, quick_reply_id)
    if reply is None or reply.company_id != actor.company_id or not reply.active:
        raise HTTPException(status_code=404, detail="Resposta rápida não encontrada")
    changes = payload.model_fields_set
    if "title" in changes:
        if not payload.title or not payload.title.strip():
            raise HTTPException(status_code=422, detail="Título obrigatório")
        reply.title = payload.title.strip()
    if "content" in changes:
        if not payload.content or not payload.content.strip():
            raise HTTPException(status_code=422, detail="Conteúdo obrigatório")
        reply.content = payload.content.strip()
    if "group_id" in changes:
        if payload.group_id is not None:
            load_group(session, actor, payload.group_id)
        reply.group_id = payload.group_id
    session.commit()
    session.refresh(reply)
    return reply


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
