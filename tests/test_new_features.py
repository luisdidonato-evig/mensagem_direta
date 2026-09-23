import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from conftest import login_headers

from app.domain import ActorRole
from app.integration import MetaCloudApiSender
from app.main import DEV_COMPANY_ID, DEV_GROUP_ID, DEV_SEED_PASSWORD, create_app
from app.models import Agent, Company, ServiceGroup
from app.security import hash_password


def webhook(message_id, *, kind="text", content="Olá", phone_id="phone-a", contact="5511999999999"):
    message = {"from": contact, "id": message_id, "type": kind}
    if kind == "text":
        message["text"] = {"body": content}
    else:
        message["interactive"] = {
            "type": "list_reply", "list_reply": {"id": content, "title": "5 estrelas"}
        }
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{
        "field": "messages", "value": {
            "metadata": {"phone_number_id": phone_id},
            "contacts": [{"wa_id": contact, "profile": {"name": "Cliente"}}],
            "messages": [message],
        },
    }]}]}


def post_signed(client, route, body, secret):
    raw = json.dumps(body).encode()
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(route, content=raw, headers={"X-Hub-Signature-256": f"sha256={digest}"})


def test_quick_reply_edit_and_interactive_buttons(client):
    supervisor = login_headers(client, "supervisor-1")
    agent = login_headers(client, "agente-1")
    created = client.post("/api/v1/quick-replies", json={
        "title": "Original", "content": "Texto antigo", "group_id": DEV_GROUP_ID,
    }, headers=supervisor).json()
    assert client.patch(f"/api/v1/quick-replies/{created['id']}",
        json={"title": "Novo", "content": "Texto novo"}, headers=agent).status_code == 403
    changed = client.patch(f"/api/v1/quick-replies/{created['id']}",
        json={"title": "Novo", "content": "Texto novo"}, headers=supervisor)
    assert changed.status_code == 200
    assert changed.json()["group_id"] == DEV_GROUP_ID

    attendance = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "button-event", "external_message_id": "button-in",
        "contact_id": "5511888888888", "content": "Preciso de ajuda", "group_id": DEV_GROUP_ID,
    }).json()["attendance"]
    client.post(f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]}, headers=agent)
    sent = client.post(f"/api/v1/attendances/{attendance['id']}/messages", json={
        "client_message_id": "buttons-out", "content": "Escolha", "buttons": [
            {"id": "yes", "title": "Sim"}, {"id": "no", "title": "Não"},
        ],
    }, headers=agent)
    assert sent.status_code == 201
    assert sent.json()["buttons"][0]["title"] == "Sim"
    bad = client.post(f"/api/v1/attendances/{attendance['id']}/messages", json={
        "client_message_id": "buttons-bad", "content": "Escolha", "buttons": [
            {"id": "same", "title": "Sim"}, {"id": "same", "title": "Não"},
        ],
    }, headers=agent)
    assert bad.status_code == 422


def test_meta_sender_formats_reply_buttons_and_rating_list():
    sent = []
    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(sent)}"}]})
    sender = MetaCloudApiSender("phone-a", "token-a", transport=httpx.MockTransport(handler))
    asyncio.run(sender.send({"contact_id": "5511999999999", "content": "Escolha", "buttons": [
        {"id": "yes", "title": "Sim"},
    ]}, "outbox-1"))
    asyncio.run(sender.send({"contact_id": "5511999999999", "content": "Avalie", "list_options": [
        {"id": f"rating:attendance:{score}", "title": f"{score} estrelas"}
        for score in range(1, 6)
    ]}, "outbox-2"))
    assert sent[0]["interactive"]["type"] == "button"
    assert sent[0]["interactive"]["action"]["buttons"][0]["reply"]["id"] == "yes"
    assert len(sent[1]["interactive"]["action"]["sections"][0]["rows"]) == 5


def test_customer_rating_after_closure(tmp_path):
    app = create_app(f"sqlite:///{(tmp_path / 'rating.db').as_posix()}", meta_app_secret="secret-a")
    with TestClient(app) as client:
        inbound = post_signed(client, "/api/v1/integrations/meta/whatsapp/webhook",
            webhook("wamid.in.rating"), "secret-a")
        assert inbound.status_code == 200
        attendance = client.get("/api/v1/attendances", headers=login_headers(client, "agente-1")).json()[0]
        agent = login_headers(client, "agente-1")
        claimed = client.post(f"/api/v1/attendances/{attendance['id']}/claim",
            json={"expected_version": attendance["version"]}, headers=agent).json()
        closed = client.patch(f"/api/v1/attendances/{attendance['id']}/status", json={
            "status": "ENCERRADO", "expected_version": claimed["version"], "closure_reason": "RESOLVIDO",
        }, headers=agent)
        assert closed.status_code == 200
        rating = post_signed(client, "/api/v1/integrations/meta/whatsapp/webhook",
            webhook("wamid.rating", kind="interactive", content=f"rating:{attendance['id']}:5"),
            "secret-a")
        assert rating.status_code == 200
        duplicate = post_signed(client, "/api/v1/integrations/meta/whatsapp/webhook",
            webhook("wamid.rating", kind="interactive", content=f"rating:{attendance['id']}:5"),
            "secret-a")
        assert duplicate.json()["duplicates"] == 1
        detail = client.get(f"/api/v1/attendances/{attendance['id']}", headers=agent).json()
        assert detail["rating"]["score"] == 5
        assert detail["status"] == "ENCERRADO"
        assert len(client.get("/api/v1/attendances", headers=agent).json()) == 1
        metrics = client.get("/api/v1/metrics/summary", headers=agent).json()
        assert metrics["average_rating"] == 5
        assert metrics["ratings_count"] == 1


def test_tenant_webhooks_and_contact_profiles_isolated(tmp_path):
    company_b = "00000000-0000-0000-0000-000000000022"
    group_b = "00000000-0000-0000-0000-000000000022"
    app = create_app(f"sqlite:///{(tmp_path / 'tenants.db').as_posix()}", meta_tenants=[
        {"company_id": DEV_COMPANY_ID, "group_id": DEV_GROUP_ID,
         "phone_number_id": "phone-a", "access_token": "token-a", "app_secret": "secret-a", "verify_token": "verify-a"},
        {"company_id": company_b, "group_id": group_b,
         "phone_number_id": "phone-b", "access_token": "token-b", "app_secret": "secret-b", "verify_token": "verify-b"},
    ])
    with TestClient(app) as client:
        with app.state.database.session_factory() as session:
            session.add(Company(id=company_b, name="Empresa B"))
            session.add(ServiceGroup(id=group_b, company_id=company_b, name="Central B"))
            session.add(Agent(id="admin-b", company_id=company_b, role=ActorRole.ADMIN,
                              password_hash=hash_password(DEV_SEED_PASSWORD)))
            session.commit()
        route_a = "/api/v1/integrations/meta/whatsapp/webhook/phone-a"
        route_b = "/api/v1/integrations/meta/whatsapp/webhook/phone-b"
        assert post_signed(client, route_a, webhook("wamid.a"), "secret-a").status_code == 200
        assert post_signed(client, route_b, webhook("wamid.b", phone_id="phone-b"), "secret-b").status_code == 200
        assert post_signed(client, route_b, webhook("wamid.bad", phone_id="phone-b"), "secret-a").status_code == 401
        admin_a = login_headers(client, "admin-1")
        admin_b = login_headers(client, "admin-b")
        assert client.get("/api/v1/me/company", headers=admin_b).json()["name"] == "Empresa B"
        assert len(client.get("/api/v1/contacts", headers=admin_a).json()) == 1
        assert len(client.get("/api/v1/contacts", headers=admin_b).json()) == 1
        client.patch("/api/v1/contacts/5511999999999/stage", json={"stage": "CLIENTE_ATIVO"}, headers=admin_a)
        assert client.get("/api/v1/contacts/5511999999999", headers=admin_a).json()["stage"] == "CLIENTE_ATIVO"
        assert client.get("/api/v1/contacts/5511999999999", headers=admin_b).json()["stage"] == "NAO_CLASSIFICADO"
        assert app.state.outbox_processor.sender.senders[company_b].phone_number_id == "phone-b"
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"messages": [{"id": "wamid.out.b"}]})
        for sender in app.state.outbox_processor.sender.senders.values():
            sender.transport = httpx.MockTransport(handler)
        asyncio.run(app.state.outbox_processor.sender.send({
            "company_id": company_b, "contact_id": "5511999999999", "content": "Resposta B",
        }, "outbox-b"))
        assert str(requests[0].url).endswith("/phone-b/messages")
        assert requests[0].headers["Authorization"] == "Bearer token-b"
