from conftest import login_headers

from app.domain import ActorRole
from app.main import DEV_GROUP_ID, DEV_SEED_PASSWORD
from app.models import Agent, Company, ServiceGroup
from app.security import hash_password


def send_inbound(client, contact_id, group_id, event_suffix="1"):
    response = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": f"evt-{contact_id}-{event_suffix}",
            "external_message_id": f"msg-{contact_id}-{event_suffix}",
            "contact_id": contact_id,
            "content": "Preciso de atendimento",
            "group_id": group_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["attendance"]


def claim(client, headers, attendance):
    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_default_mode_is_ai_active(client):
    attendance = send_inbound(client, "contato-handoff-default", DEV_GROUP_ID)
    assert attendance["automation_mode"] == "AI_ACTIVE"
    assert attendance["handoff_reason"] is None
    assert attendance["automation_updated_at"] is None


def test_inbound_message_not_sent_to_ai_but_domain_distinguishes(client):
    attendance = send_inbound(client, "contato-handoff-inbound", DEV_GROUP_ID)
    admin = login_headers(client, "admin-1")
    detail = client.get(f"/api/v1/attendances/{attendance['id']}", headers=admin).json()
    received = [e for e in detail["events"] if e["type"] == "MENSAGEM_RECEBIDA"]
    assert received, detail["events"]
    # Nenhum evento MENSAGEM_ENVIADA gerado pela IA (gateway não integrado).
    ai_sent = [
        e
        for e in detail["events"]
        if e["type"] == "MENSAGEM_ENVIADA" and e.get("details", {}).get("source") == "ai"
    ]
    assert ai_sent == []


def test_handoff_transitions_mode_and_records_event(client):
    attendance = send_inbound(client, "contato-handoff-1", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    response = client.post(
        f"/api/v1/attendances/{claimed['id']}/handoff",
        json={"expected_version": claimed["version"], "reason": "cliente pediu humano"},
        headers=agent,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["automation_mode"] == "HUMAN_REQUESTED"
    assert body["handoff_reason"] == "cliente pediu humano"
    assert body["automation_updated_at"] is not None
    assert body["version"] == claimed["version"] + 1

    detail = client.get(f"/api/v1/attendances/{claimed['id']}", headers=agent).json()
    assert any(e["type"] == "HANDOFF_SOLICITADO" for e in detail["events"])


def test_handoff_take_over_goes_human_active(client):
    attendance = send_inbound(client, "contato-handoff-takeover", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    response = client.post(
        f"/api/v1/attendances/{claimed['id']}/handoff",
        json={"expected_version": claimed["version"], "take_over": True},
        headers=agent,
    )
    assert response.status_code == 200, response.text
    assert response.json()["automation_mode"] == "HUMAN_ACTIVE"


def test_resume_ai_after_handoff(client):
    attendance = send_inbound(client, "contato-handoff-resume", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    handed = client.post(
        f"/api/v1/attendances/{claimed['id']}/handoff",
        json={"expected_version": claimed["version"]},
        headers=agent,
    ).json()

    resumed = client.post(
        f"/api/v1/attendances/{claimed['id']}/resume-ai",
        json={"expected_version": handed["version"]},
        headers=agent,
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["automation_mode"] == "AI_ACTIVE"


def test_pause_ai(client):
    attendance = send_inbound(client, "contato-handoff-pause", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    paused = client.post(
        f"/api/v1/attendances/{claimed['id']}/pause-ai",
        json={"expected_version": claimed["version"]},
        headers=agent,
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["automation_mode"] == "PAUSED"


def test_stale_version_is_rejected(client):
    attendance = send_inbound(client, "contato-handoff-version", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    first = client.post(
        f"/api/v1/attendances/{claimed['id']}/pause-ai",
        json={"expected_version": claimed["version"]},
        headers=agent,
    )
    assert first.status_code == 200

    # Reusa a versão antiga -> conflito de concorrência.
    conflict = client.post(
        f"/api/v1/attendances/{claimed['id']}/resume-ai",
        json={"expected_version": claimed["version"]},
        headers=agent,
    )
    assert conflict.status_code == 409


def test_invalid_transition_rejected(client):
    # HUMAN_ACTIVE -> HUMAN_REQUESTED não é permitido.
    attendance = send_inbound(client, "contato-handoff-invalid", DEV_GROUP_ID)
    agent = login_headers(client, "agente-1")
    claimed = claim(client, agent, attendance)

    active = client.post(
        f"/api/v1/attendances/{claimed['id']}/handoff",
        json={"expected_version": claimed["version"], "take_over": True},
        headers=agent,
    ).json()
    assert active["automation_mode"] == "HUMAN_ACTIVE"

    response = client.post(
        f"/api/v1/attendances/{claimed['id']}/handoff",
        json={"expected_version": active["version"]},
        headers=agent,
    )
    assert response.status_code == 422


def test_handoff_respects_company_scope(client):
    attendance = send_inbound(client, "contato-handoff-scope", DEV_GROUP_ID)
    app = client.app
    other_company_id = "00000000-0000-0000-0000-000000000002"
    other_group_id = "00000000-0000-0000-0000-000000000002"
    with app.state.database.session_factory() as session:
        session.add(Company(id=other_company_id, name="Outra empresa"))
        session.add(ServiceGroup(id=other_group_id, company_id=other_company_id, name="Grupo externo"))
        session.add(
            Agent(
                id="admin-externo",
                company_id=other_company_id,
                role=ActorRole.ADMIN,
                password_hash=hash_password(DEV_SEED_PASSWORD),
            )
        )
        session.commit()

    external = login_headers(client, "admin-externo")
    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/handoff",
        json={"expected_version": attendance["version"]},
        headers=external,
    )
    assert response.status_code == 404


def test_handoff_requires_auth(client):
    attendance = send_inbound(client, "contato-handoff-auth", DEV_GROUP_ID)
    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/handoff",
        json={"expected_version": attendance["version"]},
    )
    assert response.status_code == 401
