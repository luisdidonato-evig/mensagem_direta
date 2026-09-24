import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.integration import ChannelGatewaySender, OutboxProcessor
from app.domain import DeliveryStatus, MessageDirection
from app.main import DEV_COMPANY_ID, DEV_GROUP_ID
from app.models import Company, Message, OutboxMessage, now_utc

ACCOUNT_ID = "00000000-0000-0000-0000-000000000101"
CONVERSATION_ID = "00000000-0000-0000-0000-000000000201"


def inbound(event_id="event-1", message_id="message-1", tenant_id=DEV_COMPANY_ID):
    return {
        "event_id": event_id, "tenant_id": tenant_id, "channel": "whatsapp",
        "channel_account_id": ACCOUNT_ID, "conversation_id": CONVERSATION_ID,
        "message_id": message_id, "sender": {"id": "5511999999999", "name": "Cliente"},
        "message": {"type": "text", "text": "Olá"},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def test_internal_inbound_auth_scope_and_idempotency(client):
    client.app.state.channel_gateway_internal_key = "secret"
    path = "/api/v1/internal/messages/inbound"
    assert client.post(path, json=inbound()).status_code == 401
    headers = {"X-Internal-Key": "secret"}
    first = client.post(path, json=inbound(), headers=headers)
    assert first.status_code == 201, first.text
    assert first.json()["attendance"]["group_id"] == DEV_GROUP_ID
    assert first.json()["attendance"]["channel_conversation_id"] == CONVERSATION_ID
    repeated = client.post(path, json=inbound(), headers=headers)
    assert repeated.status_code == 201 and repeated.json()["duplicate"] is True
    same_message = client.post(path, json=inbound("event-2"), headers=headers)
    assert same_message.status_code == 201 and same_message.json()["duplicate"] is True
    unknown_tenant = client.post(path, json=inbound(tenant_id="00000000-0000-0000-0000-000000000002"), headers=headers)
    assert unknown_tenant.status_code == 404
    invalid_account = inbound("event-invalid", "message-invalid")
    invalid_account["channel_account_id"] = "not-a-uuid"
    assert client.post(path, json=invalid_account, headers=headers).status_code == 422


def test_internal_handoff_reuses_conversation(client):
    client.app.state.channel_gateway_internal_key = "secret"
    headers = {"X-Internal-Key": "secret"}
    received = client.post("/api/v1/internal/messages/inbound", json=inbound(), headers=headers).json()
    handoff = {
        "event_id": "handoff-1", "tenant_id": DEV_COMPANY_ID,
        "conversation_id": CONVERSATION_ID, "channel_account_id": ACCOUNT_ID,
        "reason": "cliente solicitou", "summary": "Resumo",
    }
    first = client.post("/api/v1/internal/handoff", json=handoff, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["id"] == received["attendance"]["id"]
    assert first.json()["automation_mode"] == "HUMAN_REQUESTED"
    second = client.post("/api/v1/internal/handoff", json=handoff, headers=headers)
    assert second.status_code == 200 and second.json()["version"] == first.json()["version"]
    from conftest import login_headers
    agent = login_headers(client, "agente-1")
    claimed = client.post(f"/api/v1/attendances/{first.json()['id']}/claim", json={
        "expected_version": first.json()["version"],
    }, headers=agent)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["automation_mode"] == "HUMAN_ACTIVE"


def test_handoff_before_first_message_keeps_same_attendance(client):
    client.app.state.channel_gateway_internal_key = "secret"
    headers = {"X-Internal-Key": "secret"}
    handoff = {
        "event_id": "handoff-first", "tenant_id": DEV_COMPANY_ID,
        "conversation_id": CONVERSATION_ID, "channel_account_id": ACCOUNT_ID,
        "reason": "cliente solicitou",
    }
    first = client.post("/api/v1/internal/handoff", json=handoff, headers=headers)
    assert first.status_code == 200, first.text
    received = client.post("/api/v1/internal/messages/inbound", json=inbound(), headers=headers)
    assert received.status_code == 201, received.text
    assert received.json()["attendance"]["id"] == first.json()["id"]
    from conftest import login_headers
    pulled = client.post(f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=login_headers(client, "agente-1"))
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["automation_mode"] == "HUMAN_ACTIVE"


def test_internal_status_respects_tenant(client):
    client.app.state.channel_gateway_internal_key = "secret"
    headers = {"X-Internal-Key": "secret"}
    attendance = client.post("/api/v1/internal/messages/inbound", json=inbound(), headers=headers).json()["attendance"]
    with client.app.state.database.session_factory() as session:
        message = Message(attendance_id=attendance["id"], content="saída", direction=MessageDirection.SAIDA, delivery_status=DeliveryStatus.PENDENTE)
        session.add(message)
        session.flush()
        outbox = OutboxMessage(message_id=message.id, payload={"content": "saída"})
        session.add(outbox)
        session.commit()
        outbox_id, message_id = outbox.id, message.id
    body = {
        "event_id": "status-1", "tenant_id": DEV_COMPANY_ID,
        "idempotency_key": outbox_id, "status": "DELIVERED",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    response = client.post("/api/v1/internal/messages/status", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["processed"] is True
    read = {**body, "event_id": "status-2", "status": "READ", "message_id": "wamid.provider.1"}
    assert client.post("/api/v1/internal/messages/status", json=read, headers=headers).json()["processed"] is True
    late_queued = {**body, "event_id": "status-3", "status": "QUEUED"}
    assert client.post("/api/v1/internal/messages/status", json=late_queued, headers=headers).json()["processed"] is True
    with client.app.state.database.session_factory() as session:
        stored = session.get(Message, message_id)
        assert stored.delivery_status.value == "LIDA"
        assert stored.external_id == "wamid.provider.1"
        session.add(Company(id="00000000-0000-0000-0000-000000000002", name="Outro tenant"))
        session.commit()
    wrong_tenant = {**body, "tenant_id": "00000000-0000-0000-0000-000000000002", "event_id": "status-cross", "status": "FAILED"}
    assert client.post("/api/v1/internal/messages/status", json=wrong_tenant, headers=headers).json()["processed"] is False
    with client.app.state.database.session_factory() as session:
        assert session.get(Message, message_id).delivery_status.value == "LIDA"


def test_failed_delivery_is_terminal_until_explicit_retry(client):
    client.app.state.channel_gateway_internal_key = "secret"
    headers = {"X-Internal-Key": "secret"}
    attendance = client.post("/api/v1/internal/messages/inbound", json=inbound(), headers=headers).json()["attendance"]
    with client.app.state.database.session_factory() as session:
        message = Message(attendance_id=attendance["id"], content="saída", direction=MessageDirection.SAIDA, delivery_status=DeliveryStatus.PENDENTE)
        session.add(message)
        session.flush()
        outbox = OutboxMessage(message_id=message.id, payload={"content": "saída"})
        session.add(outbox)
        session.commit()
        message_id, outbox_id = message.id, outbox.id
    base = {
        "tenant_id": DEV_COMPANY_ID, "idempotency_key": outbox_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    assert client.post("/api/v1/internal/messages/status", json={
        **base, "event_id": "failure", "status": "FAILED",
    }, headers=headers).json()["processed"] is True
    assert client.post("/api/v1/internal/messages/status", json={
        **base, "event_id": "late-sent", "status": "SENT",
    }, headers=headers).json()["processed"] is True
    with client.app.state.database.session_factory() as session:
        assert session.get(Message, message_id).delivery_status == DeliveryStatus.FALHA


def test_internal_choice_records_rating_without_reopening(client):
    client.app.state.channel_gateway_internal_key = "secret"
    headers = {"X-Internal-Key": "secret"}
    attendance = client.post("/api/v1/internal/messages/inbound", json=inbound(), headers=headers).json()["attendance"]
    from conftest import login_headers
    agent = login_headers(client, "agente-1")
    claimed = client.post(f"/api/v1/attendances/{attendance['id']}/claim", json={
        "expected_version": attendance["version"],
    }, headers=agent).json()
    closed = client.patch(f"/api/v1/attendances/{attendance['id']}/status", json={
        "status": "ENCERRADO", "expected_version": claimed["version"],
        "closure_reason": "RESOLVIDO",
    }, headers=agent)
    assert closed.status_code == 200, closed.text
    reply = inbound("rating-event", "rating-message")
    reply["message"] = {"type": "choice", "choice_id": f"rating:{attendance['id']}:5", "text": "5 estrelas"}
    rated = client.post("/api/v1/internal/messages/inbound", json=reply, headers=headers)
    assert rated.status_code == 201, rated.text
    assert rated.json()["attendance"]["status"] == "ENCERRADO"
    assert rated.json()["attendance"]["rating"]["score"] == 5
    duplicate = client.post("/api/v1/internal/messages/inbound", json=reply, headers=headers)
    assert duplicate.status_code == 201 and duplicate.json()["duplicate"] is True


def test_gateway_sender_uses_real_message_contract():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(202, json={"status": "queued", "delivery_id": "delivery-1"})

    sender = ChannelGatewaySender("https://gateway.test", "secret", transport=httpx.MockTransport(handler))
    result = asyncio.run(sender.send({
        "company_id": DEV_COMPANY_ID, "channel_conversation_id": CONVERSATION_ID,
        "content": "Olá", "contact_id": "5511999999999",
    }, "outbox-1"))
    assert result == "delivery-1"
    assert calls[0].url.path == "/internal/v1/messages"
    assert json.loads(calls[0].content)["message"] == {"kind": "text", "text": "Olá"}
    assert calls[0].headers["X-Internal-Key"] == "secret"
    asyncio.run(sender.send({
        "company_id": DEV_COMPANY_ID, "channel_conversation_id": CONVERSATION_ID,
        "content": "Escolha", "buttons": [{"id": "yes", "title": "Sim"}],
    }, "outbox-2"))
    assert json.loads(calls[1].content)["message"] == {
        "kind": "choices", "body": "Escolha", "options": [{"id": "yes", "label": "Sim"}],
    }
    asyncio.run(sender.send({
        "company_id": DEV_COMPANY_ID, "channel_account_id": ACCOUNT_ID,
        "contact_id": "5511999999999", "content": "Avalie",
        "list_options": [{"id": "rating:a:5", "title": "5 estrelas"}],
    }, "outbox-3"))
    request = json.loads(calls[2].content)
    assert request["channel_account_id"] == ACCOUNT_ID
    assert request["recipient_id"] == "5511999999999"
    assert request["message"]["kind"] == "list"


def test_gateway_sender_rejects_unconfirmed_enqueue():
    sender = ChannelGatewaySender(
        "https://gateway.test", "secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(202, json={"status": "queued"})),
    )
    with pytest.raises(RuntimeError, match="não confirmou"):
        asyncio.run(sender.send({
            "company_id": DEV_COMPANY_ID, "channel_conversation_id": CONVERSATION_ID,
            "content": "Olá",
        }, "outbox-1"))


def test_outbox_response_does_not_regress_early_delivery_status(client):
    attendance = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "outbox-race-in", "external_message_id": "outbox-race-msg",
        "contact_id": "5511888888888", "content": "Oi", "group_id": DEV_GROUP_ID,
    }).json()["attendance"]
    with client.app.state.database.session_factory() as session:
        message = Message(attendance_id=attendance["id"], content="saída", direction=MessageDirection.SAIDA, delivery_status=DeliveryStatus.PENDENTE)
        session.add(message)
        session.flush()
        session.add(OutboxMessage(message_id=message.id, payload={"content": "saída", "contact_id": "5511888888888"}))
        session.commit()
        message_id = message.id

    class EarlyStatusSender:
        async def send(self, payload, idempotency_key):
            with client.app.state.database.session_factory() as session:
                stored = session.get(Message, message_id)
                stored.delivery_status = DeliveryStatus.LIDA
                stored.external_id = "wamid.early"
                session.get(OutboxMessage, idempotency_key).processed_at = now_utc()
                session.commit()
            return "gateway-delivery-id"

    processor = OutboxProcessor(client.app.state.database.session_factory, EarlyStatusSender())
    assert asyncio.run(processor.process_batch()) == (1, 0)
    with client.app.state.database.session_factory() as session:
        stored = session.get(Message, message_id)
        assert stored.delivery_status == DeliveryStatus.LIDA
        assert stored.external_id == "wamid.early"
