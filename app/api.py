import hashlib
import json
from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth import get_actor
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
)
from app.integration import DEFAULT_MAX_ATTEMPTS, verify_meta_signature
from app.models import (
    Agent,
    Attendance,
    AttendanceClosure,
    AttendanceEvent,
    AttendanceTag,
    Contact,
    ContactAuditEvent,
    ContactTag,
    IntegrationEvent,
    InternalNote,
    Message,
    OutboxMessage,
    QuickReply,
    now_utc,
)
from app.schemas import (
    Actor,
    AgentCreate,
    AgentRead,
    AttendanceDetail,
    AttendanceRead,
    AttendanceSummary,
    AttendanceTagsUpdate,
    ClaimRequest,
    ContactRead,
    ContactStageUpdate,
    ContactUpdate,
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
    token, expires_at = create_access_token(agent.id, agent.role.value, request.app.state.jwt_secret)
    return {
        "access_token": token,
        "expires_at": expires_at,
        "actor": {"id": agent.id, "role": agent.role},
    }


@router.post("/agents", response_model=AgentRead, status_code=201)
def create_agent(payload: AgentCreate, session: SessionDep, actor: ActorDep) -> Agent:
    if actor.role != ActorRole.ADMIN:
        raise HTTPException(status_code=403, detail="Apenas administrador cria contas de agente")
    if session.get(Agent, payload.id) is not None:
        raise HTTPException(status_code=409, detail="Já existe agente com esse identificador")
    agent = Agent(id=payload.id, role=payload.role, password_hash=hash_password(payload.password))
    session.add(agent)
    session.commit()
    session.refresh(agent)
    return agent


@router.get("/agents", response_model=list[AgentRead])
def list_agents(session: SessionDep, actor: ActorDep) -> list[Agent]:
    if actor.role != ActorRole.ADMIN:
        raise HTTPException(status_code=403, detail="Apenas administrador vê contas de agente")
    return list(session.scalars(select(Agent).order_by(Agent.id)))


def load_attendance(session: Session, attendance_id: str) -> Attendance:
    attendance = session.get(Attendance, attendance_id)
    if attendance is None:
        raise HTTPException(status_code=404, detail="Atendimento não encontrado")
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
) -> InboundResult:
    processed = session.get(IntegrationEvent, payload.external_event_id)
    if processed:
        attendance = load_attendance(session, processed.attendance_id)
        message = session.get(Message, processed.message_id)
        if message is None:
            raise HTTPException(status_code=500, detail="Evento processado sem mensagem")
        return InboundResult(attendance=attendance, message=message, duplicate=True)

    contact = session.get(Contact, payload.contact_id)
    if contact is None:
        session.add(Contact(id=payload.contact_id))

    attendance = session.scalar(
        select(Attendance)
        .where(
            Attendance.contact_id == payload.contact_id,
            Attendance.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Attendance.created_at.desc())
    )
    created = attendance is None
    if created:
        attendance = Attendance(contact_id=payload.contact_id)
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
        content=payload.content,
        delivery_status=DeliveryStatus.RECEBIDA,
    )
    session.add(message)
    session.flush()
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
        return False
    current_rank = DELIVERY_RANK.get(message.delivery_status, 0)
    next_rank = DELIVERY_RANK[mapped]
    if mapped == DeliveryStatus.FALHA:
        if current_rank < DELIVERY_RANK[DeliveryStatus.ENTREGUE]:
            message.delivery_status = mapped
    elif next_rank >= current_rank:
        message.delivery_status = mapped
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
                content = (item.get("text") or {}).get("body")
                if item.get("type") != "text" or not contact_id or not external_id or not content:
                    ignored += 1
                    continue
                result = await process_inbound_message(
                    InboundMessageCreate(
                        external_event_id=f"meta-message:{external_id}",
                        external_message_id=external_id,
                        contact_id=contact_id,
                        content=content,
                    ),
                    request,
                    session,
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


def latest_attendance_for_contact(session: Session, contact_id: str) -> Attendance | None:
    return session.scalar(
        select(Attendance)
        .where(Attendance.contact_id == contact_id)
        .order_by(Attendance.updated_at.desc())
        .limit(1)
    )


def build_contact_read(session: Session, contact_id: str) -> ContactRead:
    contact = session.get(Contact, contact_id)
    tags = list(
        session.scalars(
            select(ContactTag.name)
            .where(ContactTag.contact_id == contact_id)
            .order_by(ContactTag.name)
        )
    )
    last_attendance = latest_attendance_for_contact(session, contact_id)
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
    if actor.role in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        return
    assigned = session.scalar(
        select(Attendance.id).where(
            Attendance.contact_id == contact_id,
            Attendance.assignee_id == actor.id,
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
    del actor  # Escopo por fila/equipe entra junto com RBAC real.
    query = select(Contact)
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
        .where(Attendance.contact_id.in_(contact_ids))
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
    del actor
    return build_contact_read(session, contact_id)


@router.patch("/contacts/{contact_id}/stage", response_model=ContactRead)
async def update_contact_stage(
    contact_id: str,
    payload: ContactStageUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> ContactRead:
    if latest_attendance_for_contact(session, contact_id) is None:
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
    return build_contact_read(session, contact_id)


@router.put("/contacts/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: str,
    payload: ContactUpdate,
    request: Request,
    session: SessionDep,
    actor: ActorDep,
) -> ContactRead:
    if latest_attendance_for_contact(session, contact_id) is None:
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
    return build_contact_read(session, contact_id)


@router.get("/metrics/summary", response_model=MetricsSummary)
def metrics_summary(
    session: SessionDep,
    actor: ActorDep,
) -> dict:
    attendances = list(session.scalars(select(Attendance)))
    outbound_messages = list(
        session.scalars(
            select(Message)
            .where(Message.direction == MessageDirection.SAIDA)
            .order_by(Message.created_at)
        )
    )
    closures = list(session.scalars(select(AttendanceClosure)))

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
) -> list[dict]:
    del actor  # Escopo por fila entra junto com RBAC real.
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
        .order_by(Attendance.updated_at.desc())
    )
    if status:
        query = query.where(Attendance.status.in_(status))
    if queue_id:
        query = query.where(Attendance.queue_id == queue_id)
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
    del actor
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
    attendance = load_attendance(session, attendance_id)
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
    attendance = load_attendance(session, attendance_id)
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
        attendance = load_attendance(session, attendance_id)
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
    attendance = load_attendance(session, attendance_id)
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
    attendance = load_attendance(session, attendance_id)
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
    session.commit()
    attendance = load_attendance(session, attendance_id)
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
    attendance = load_attendance(session, attendance_id)
    previous_assignee = attendance.assignee_id
    result = session.execute(
        update(Attendance)
        .where(
            Attendance.id == attendance_id,
            Attendance.version == payload.expected_version,
            Attendance.status != AttendanceStatus.ENCERRADO,
        )
        .values(
            assignee_id=payload.target_actor_id,
            status=AttendanceStatus.EM_ATENDIMENTO,
            version=Attendance.version + 1,
            updated_at=now_utc(),
        )
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
            to_status=AttendanceStatus.EM_ATENDIMENTO,
            details={"from_actor_id": previous_assignee, "to_actor_id": payload.target_actor_id},
        )
    )
    session.commit()
    attendance = load_attendance(session, attendance_id)
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
    attendance = load_attendance(session, attendance_id)
    ensure_can_operate(attendance, actor)
    if attendance.status == AttendanceStatus.ENCERRADO:
        raise HTTPException(status_code=409, detail="Atendimento encerrado")

    existing = session.scalar(
        select(Message).where(Message.client_message_id == payload.client_message_id)
    )
    if existing:
        return existing

    message = Message(
        attendance_id=attendance.id,
        client_message_id=payload.client_message_id,
        direction=MessageDirection.SAIDA,
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


@router.get("/quick-replies", response_model=list[QuickReplyRead])
def list_quick_replies(
    session: SessionDep,
    actor: ActorDep,
) -> list[QuickReply]:
    del actor
    return list(session.scalars(select(QuickReply).order_by(QuickReply.title)))


@router.post("/quick-replies", response_model=QuickReplyRead, status_code=201)
def create_quick_reply(
    payload: QuickReplyCreate,
    session: SessionDep,
    actor: ActorDep,
) -> QuickReply:
    if actor.role not in {ActorRole.SUPERVISOR, ActorRole.ADMIN}:
        raise HTTPException(status_code=403, detail="Apenas supervisão pode criar resposta rápida")
    quick_reply = QuickReply(
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
    if quick_reply is None:
        raise HTTPException(status_code=404, detail="Resposta rápida não encontrada")
    session.delete(quick_reply)
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
        .where(OutboxMessage.processed_at.is_(None))
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
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
