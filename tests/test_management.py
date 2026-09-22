from conftest import login_headers

from app.main import DEV_GROUP_ID


def test_supervisor_manages_attendants_but_not_supervisors_or_admins(client):
    supervisor = login_headers(client, "supervisor-1")
    listed = client.get("/api/v1/agents", headers=supervisor)
    assert listed.status_code == 200
    assert {agent["id"] for agent in listed.json()} >= {"agente-1", "supervisor-1", "admin-1"}

    created = client.post(
        "/api/v1/agents",
        json={"id": "agente-supervisionado", "role": "ATENDENTE", "password": "senha-forte-123"},
        headers=supervisor,
    )
    assert created.status_code == 201
    assert client.patch(
        "/api/v1/agents/agente-supervisionado",
        json={"active": False},
        headers=supervisor,
    ).status_code == 200
    assert client.post(
        "/api/v1/agents",
        json={"id": "supervisor-indevido", "role": "SUPERVISOR", "password": "senha-forte-123"},
        headers=supervisor,
    ).status_code == 403
    assert client.patch(
        "/api/v1/agents/admin-1", json={"active": False}, headers=supervisor
    ).status_code == 403
    assert client.patch(
        "/api/v1/agents/agente-1", json={"role": "ADMIN"}, headers=supervisor
    ).status_code == 403


def test_last_administrator_cannot_be_disabled_or_demoted(client):
    admin = login_headers(client, "admin-1")
    assert client.patch(
        "/api/v1/agents/admin-1", json={"active": False}, headers=admin
    ).status_code == 409
    assert client.patch(
        "/api/v1/agents/admin-1", json={"role": "ATENDENTE"}, headers=admin
    ).status_code == 409


def test_group_memberships_are_visible_and_supervisor_can_link_attendant(client):
    supervisor = login_headers(client, "supervisor-1")
    agent = login_headers(client, "agente-1")
    group = client.post(
        "/api/v1/groups",
        json={"name": "Suporte", "max_load_per_agent": 3},
        headers=login_headers(client, "admin-1"),
    ).json()
    assert client.get("/api/v1/groups/memberships", headers=agent).status_code == 403
    linked = client.put(
        f"/api/v1/groups/{group['id']}/agents/agente-1",
        json={"max_load_override": 2},
        headers=supervisor,
    )
    assert linked.status_code == 200
    memberships = client.get("/api/v1/groups/memberships", headers=supervisor)
    assert memberships.status_code == 200
    assert any(
        link["group_id"] == group["id"] and link["agent_id"] == "agente-1"
        and link["max_load_override"] == 2
        for link in memberships.json()
    )
    assert client.put(
        f"/api/v1/groups/{group['id']}/agents/admin-1",
        json={},
        headers=supervisor,
    ).status_code == 403
    assert client.delete(
        f"/api/v1/groups/{group['id']}/agents/agente-1", headers=supervisor
    ).status_code == 204
    assert not any(
        link["group_id"] == group["id"] and link["agent_id"] == "agente-1"
        for link in client.get("/api/v1/groups/memberships", headers=supervisor).json()
    )


def test_group_attendance_and_transfer_reject_inactive_destination(client):
    admin = login_headers(client, "admin-1")
    supervisor = login_headers(client, "supervisor-1")
    group = client.post(
        "/api/v1/groups", json={"name": "Comercial"}, headers=admin
    ).json()
    client.put(
        f"/api/v1/groups/{group['id']}/agents/agente-2",
        json={}, headers=admin,
    )
    inbound = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": "evento-grupo-1",
            "external_message_id": "mensagem-grupo-1",
            "contact_id": "cliente-grupo",
            "content": "Preciso de atendimento comercial",
            "group_id": group["id"],
        },
    )
    assert inbound.status_code == 201
    attendance = inbound.json()["attendance"]
    assert attendance["group_id"] == group["id"]
    assert client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull",
        headers=login_headers(client, "agente-1"),
    ).status_code == 204
    pulled = client.post(
        f"/api/v1/groups/{group['id']}/attendances/pull",
        headers=login_headers(client, "agente-2"),
    )
    assert pulled.status_code == 200
    assert pulled.json()["assignee_id"] == "agente-2"

    client.patch(
        "/api/v1/agents/agente-1", json={"active": False}, headers=admin
    )
    rejected = client.post(
        f"/api/v1/attendances/{attendance['id']}/transfer",
        json={
            "target_group_id": DEV_GROUP_ID,
            "target_actor_id": "agente-1",
            "expected_version": pulled.json()["version"],
        },
        headers=supervisor,
    )
    assert rejected.status_code == 422
