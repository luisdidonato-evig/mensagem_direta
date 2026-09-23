import pytest
from starlette.websockets import WebSocketDisconnect

from conftest import login_headers

from app.domain import ActorRole
from app.main import DEV_GROUP_ID, DEV_SEED_PASSWORD
from app.models import Agent, Company, ServiceGroup
from app.security import hash_password


def create_group(client, admin_headers, name, max_load_per_agent=5):
    response = client.post(
        "/api/v1/groups",
        json={"name": name, "max_load_per_agent": max_load_per_agent},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def link_agent(client, admin_headers, group_id, agent_id, max_load_override=None):
    response = client.put(
        f"/api/v1/groups/{group_id}/agents/{agent_id}",
        json={"max_load_override": max_load_override},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def unlink_agent(client, admin_headers, group_id, agent_id):
    response = client.delete(
        f"/api/v1/groups/{group_id}/agents/{agent_id}", headers=admin_headers
    )
    assert response.status_code == 204, response.text


def send_inbound(client, contact_id, group_id, load_weight=1, event_suffix="1"):
    response = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": f"evt-{contact_id}-{event_suffix}",
            "external_message_id": f"msg-{contact_id}-{event_suffix}",
            "contact_id": contact_id,
            "content": "Preciso de atendimento",
            "group_id": group_id,
            "load_weight": load_weight,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["attendance"]


def test_agent_outside_group_cannot_claim(client):
    admin_headers = login_headers(client, "admin-1")
    other_group = create_group(client, admin_headers, "Financeiro")
    unlink_agent(client, admin_headers, DEV_GROUP_ID, "agente-1")
    link_agent(client, admin_headers, other_group["id"], "agente-2")

    attendance = send_inbound(client, "contato-scope-1", other_group["id"])

    agent_headers = login_headers(client, "agente-1")
    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_agent_outside_group_cannot_view_attendance_detail(client):
    admin_headers = login_headers(client, "admin-1")
    other_group = create_group(client, admin_headers, "RH")
    link_agent(client, admin_headers, other_group["id"], "agente-2")

    attendance = send_inbound(client, "contato-scope-2", other_group["id"])

    agent_headers = login_headers(client, "agente-1")
    response = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    )

    assert response.status_code == 403


def test_claim_enforces_capacity(client):
    admin_headers = login_headers(client, "admin-1")
    link_agent(client, admin_headers, DEV_GROUP_ID, "agente-1", max_load_override=1)
    agent_headers = login_headers(client, "agente-1")

    first = send_inbound(client, "contato-claim-cap-1", DEV_GROUP_ID)
    second = send_inbound(client, "contato-claim-cap-2", DEV_GROUP_ID, event_suffix="2")

    claimed_first = client.post(
        f"/api/v1/attendances/{first['id']}/claim",
        json={"expected_version": first["version"]},
        headers=agent_headers,
    )
    assert claimed_first.status_code == 200

    claimed_second = client.post(
        f"/api/v1/attendances/{second['id']}/claim",
        json={"expected_version": second["version"]},
        headers=agent_headers,
    )
    assert claimed_second.status_code == 409


def test_supervisor_transfers_attendance_to_another_group(client):
    admin_headers = login_headers(client, "admin-1")
    supervisor_headers = login_headers(client, "supervisor-1")
    destination = create_group(client, admin_headers, "Suporte N2")

    attendance = send_inbound(client, "contato-transfer-group", DEV_GROUP_ID)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=login_headers(client, "agente-1"),
    ).json()

    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/transfer",
        json={"target_group_id": destination["id"], "expected_version": claimed["version"]},
        headers=supervisor_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["group_id"] == destination["id"]
    assert body["assignee_id"] is None
    assert body["status"] == "AGUARDANDO"


def test_another_company_cannot_list_or_view_attendance(client):
    attendance = send_inbound(client, "contato-company-scope", DEV_GROUP_ID)
    app = client.app
    other_company_id = "00000000-0000-0000-0000-000000000002"
    other_group_id = "00000000-0000-0000-0000-000000000002"
    with app.state.database.session_factory() as session:
        session.add(Company(id=other_company_id, name="Outra empresa"))
        session.add(
            ServiceGroup(
                id=other_group_id,
                company_id=other_company_id,
                name="Grupo externo",
            )
        )
        session.add(
            Agent(
                id="admin-externo",
                company_id=other_company_id,
                role=ActorRole.ADMIN,
                password_hash=hash_password(DEV_SEED_PASSWORD),
            )
        )
        session.commit()

    external_headers = login_headers(client, "admin-externo")

    listed = client.get("/api/v1/attendances", headers=external_headers)
    detail = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=external_headers
    )

    assert listed.status_code == 200
    assert listed.json() == []
    assert detail.status_code == 404

    assert client.get("/api/v1/contacts", headers=external_headers).json() == []
    assert client.get(
        "/api/v1/contacts/contato-company-scope", headers=external_headers
    ).status_code == 404
    assert client.get("/api/v1/metrics/summary", headers=external_headers).json()[
        "total_open"
    ] == 0


def test_agent_only_sees_contacts_and_metrics_from_own_groups(client):
    admin_headers = login_headers(client, "admin-1")
    other_group = create_group(client, admin_headers, "Privado")
    link_agent(client, admin_headers, other_group["id"], "agente-2")
    send_inbound(client, "contato-privado", other_group["id"])
    agent_headers = login_headers(client, "agente-1")

    assert client.get("/api/v1/contacts", headers=agent_headers).json() == []
    assert client.get(
        "/api/v1/contacts/contato-privado", headers=agent_headers
    ).status_code == 404
    assert client.get(
        "/api/v1/contacts/contato-privado/timeline", headers=agent_headers
    ).status_code == 404
    assert client.get("/api/v1/metrics/summary", headers=agent_headers).json()[
        "total_open"
    ] == 0


def test_management_sees_all_company_groups_but_operator_only_memberships(client):
    admin = login_headers(client, "admin-1")
    supervisor = login_headers(client, "supervisor-1")
    operator = login_headers(client, "agente-1")
    private_group = create_group(client, admin, "Fila exclusiva")
    link_agent(client, admin, private_group["id"], "agente-2")
    private_attendance = send_inbound(client, "contato-exclusivo", private_group["id"])
    for manager in (admin, supervisor):
        assert private_attendance["id"] in {
            item["id"] for item in client.get("/api/v1/attendances", headers=manager).json()
        }
    assert private_attendance["id"] not in {
        item["id"] for item in client.get("/api/v1/attendances", headers=operator).json()
    }
    assert client.get(f"/api/v1/attendances/{private_attendance['id']}", headers=operator).status_code == 403


def test_websocket_requires_token_and_delivers_visible_event(client):
    with client.websocket_connect("/api/v1/ws") as socket:
        socket.send_json({"token": "invalid"})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008

    token = login_headers(client, "agente-1")["Authorization"].removeprefix("Bearer ")
    with client.websocket_connect("/api/v1/ws") as socket:
        socket.send_json({"token": token})
        send_inbound(client, "contato-websocket", DEV_GROUP_ID)
        event = socket.receive_json()
        assert event["type"] == "attendance.created"
        assert event["attendance"]["contact_id"] == "contato-websocket"


def test_transfer_rejects_target_agent_outside_destination_group(client):
    admin_headers = login_headers(client, "admin-1")
    supervisor_headers = login_headers(client, "supervisor-1")
    destination = create_group(client, admin_headers, "Cobranca")

    attendance = send_inbound(client, "contato-transfer-invalid", DEV_GROUP_ID)
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=login_headers(client, "agente-1"),
    ).json()

    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/transfer",
        json={
            "target_actor_id": "agente-2",
            "target_group_id": destination["id"],
            "expected_version": claimed["version"],
        },
        headers=supervisor_headers,
    )

    assert response.status_code == 422
