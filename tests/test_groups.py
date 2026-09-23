from conftest import login_headers

from app.main import DEV_GROUP_ID


def test_admin_manages_groups_and_members(client):
    admin_headers = login_headers(client, "admin-1")

    created = client.post(
        "/api/v1/groups",
        json={"name": "Comercial", "max_load_per_agent": 7},
        headers=admin_headers,
    )
    assert created.status_code == 201
    group = created.json()
    assert group["name"] == "Comercial"
    assert group["max_load_per_agent"] == 7

    updated = client.patch(
        f"/api/v1/groups/{group['id']}",
        json={"name": "Vendas", "max_load_per_agent": 6},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Vendas"

    linked = client.put(
        f"/api/v1/groups/{group['id']}/agents/agente-1",
        json={"max_load_override": 8},
        headers=admin_headers,
    )
    assert linked.status_code == 200
    assert linked.json()["max_load_override"] == 8

    my_groups = client.get(
        "/api/v1/me/groups", headers=login_headers(client, "agente-1")
    )
    assert my_groups.status_code == 200
    assert {item["id"] for item in my_groups.json()} == {DEV_GROUP_ID, group["id"]}

    unlinked = client.delete(
        f"/api/v1/groups/{group['id']}/agents/agente-1",
        headers=admin_headers,
    )
    assert unlinked.status_code == 204
    assert {item["id"] for item in client.get(
        "/api/v1/me/groups", headers=login_headers(client, "agente-1")
    ).json()} == {DEV_GROUP_ID}


def test_non_admin_cannot_create_group(client, agent_headers):
    response = client.post(
        "/api/v1/groups",
        json={"name": "Restrito", "max_load_per_agent": 5},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_owner_can_redirect_to_another_group_queue(client):
    admin = login_headers(client, "admin-1")
    agent = login_headers(client, "agente-1")
    target = client.post("/api/v1/groups", json={"name": "Financeiro", "max_load_per_agent": 5}, headers=admin).json()
    incoming = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "redirect-event", "external_message_id": "redirect-message",
        "contact_id": "redirect-contact", "content": "Cobrança", "group_id": DEV_GROUP_ID,
    }).json()["attendance"]
    claimed = client.post(f"/api/v1/attendances/{incoming['id']}/claim",
        json={"expected_version": incoming["version"]}, headers=agent).json()
    assert target["id"] in {group["id"] for group in client.get(
        "/api/v1/groups/transfer-targets", headers=agent).json()}
    assert target["id"] not in {group["id"] for group in client.get(
        "/api/v1/groups", headers=agent).json()}
    denied = client.post(f"/api/v1/attendances/{incoming['id']}/transfer", json={
        "target_group_id": target["id"], "target_actor_id": "agente-2",
        "expected_version": claimed["version"],
    }, headers=agent)
    assert denied.status_code == 403
    redirected = client.post(f"/api/v1/attendances/{incoming['id']}/transfer", json={
        "target_group_id": target["id"], "expected_version": claimed["version"],
    }, headers=agent)
    assert redirected.status_code == 200, redirected.text
    assert redirected.json()["group_id"] == target["id"]
    assert redirected.json()["assignee_id"] is None
    assert redirected.json()["status"] == "AGUARDANDO"
    assert client.get(f"/api/v1/attendances/{incoming['id']}", headers=agent).status_code == 403


def test_group_with_open_attendance_cannot_be_deactivated(client):
    admin = login_headers(client, "admin-1")
    client.post("/api/v1/inbox/messages", json={
        "external_event_id": "active-group-event", "external_message_id": "active-group-message",
        "contact_id": "active-group-contact", "content": "Olá", "group_id": DEV_GROUP_ID,
    })
    response = client.patch(f"/api/v1/groups/{DEV_GROUP_ID}", json={"active": False}, headers=admin)
    assert response.status_code == 409


def test_my_load_counts_active_attendances(client, inbound_payload, agent_headers):
    attendance = client.post("/api/v1/inbox/messages", json=inbound_payload).json()[
        "attendance"
    ]
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    )
    assert claimed.status_code == 200

    response = client.get("/api/v1/me/load", headers=agent_headers)

    assert response.status_code == 200
    assert response.json() == {"actor_id": "agente-1", "current_load": 1}


def test_group_total_capacity_applies_across_agents_and_load_cost_is_editable(client):
    admin = login_headers(client, "admin-1")
    group_response = client.post("/api/v1/groups", json={
        "name": "Operações", "max_load_per_agent": 5,
        "max_active_attendances": 1, "load_cost_per_attendance": 1,
        "queue_wait_message": "Aguarde sua vez, por favor.",
    }, headers=admin)
    assert group_response.status_code == 201, group_response.text
    group = group_response.json()
    assert group["max_active_attendances"] == 1
    assert group["load_cost_per_attendance"] == 1
    assert group["queue_wait_message"] == "Aguarde sua vez, por favor."
    for agent_id in ("agente-1", "agente-2"):
        assert client.put(f"/api/v1/groups/{group['id']}/agents/{agent_id}", json={}, headers=admin).status_code == 200
    first = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "total-cap-1", "external_message_id": "total-cap-msg-1",
        "contact_id": "total-cap-contact-1", "content": "Olá", "group_id": group["id"],
    }).json()["attendance"]
    assert client.post(f"/api/v1/attendances/{first['id']}/claim", json={
        "expected_version": first["version"],
    }, headers=login_headers(client, "agente-1")).status_code == 200
    second = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "total-cap-2", "external_message_id": "total-cap-msg-2",
        "contact_id": "total-cap-contact-2", "content": "Olá", "group_id": group["id"],
    }).json()["attendance"]
    blocked = client.post(f"/api/v1/attendances/{second['id']}/claim", json={
        "expected_version": second["version"],
    }, headers=login_headers(client, "agente-2"))
    assert blocked.status_code == 409
    assert "Grupo atingiu" in blocked.json()["detail"]
    waiting = client.get(f"/api/v1/attendances/{second['id']}", headers=admin).json()
    assert any(message["content"] == "Aguarde sua vez, por favor." for message in waiting["messages"])
    updated = client.patch(f"/api/v1/groups/{group['id']}", json={
        "max_active_attendances": 2, "load_cost_per_attendance": 2,
    }, headers=admin)
    assert updated.status_code == 200
    assert updated.json()["load_cost_per_attendance"] == 2
    assert client.patch(f"/api/v1/groups/{group['id']}", json={
        "max_active_attendances": 2, "load_cost_per_attendance": 1,
    }, headers=admin).status_code == 200
    assert client.post(f"/api/v1/attendances/{second['id']}/claim", json={
        "expected_version": second["version"],
    }, headers=login_headers(client, "agente-2")).status_code == 200


def test_group_load_units_allow_only_whole_attendances_and_queue_once(client):
    admin = login_headers(client, "admin-1")
    group = client.post("/api/v1/groups", json={
        "name": "Ilha", "max_active_attendances": 5,
        "load_cost_per_attendance": 2,
        "queue_wait_message": "Você entrou na fila de espera.",
    }, headers=admin).json()
    client.put(f"/api/v1/groups/{group['id']}/agents/agente-1", json={}, headers=admin)
    agent = login_headers(client, "agente-1")
    claimed_items = []
    for number in range(2):
        item = client.post("/api/v1/inbox/messages", json={
            "external_event_id": f"island-event-{number}",
            "external_message_id": f"island-message-{number}",
            "contact_id": f"island-contact-{number}", "content": "Olá",
            "group_id": group["id"],
        }).json()["attendance"]
        claimed = client.post(f"/api/v1/attendances/{item['id']}/claim", json={
            "expected_version": item["version"],
        }, headers=agent)
        assert claimed.status_code == 200
        claimed_items.append(claimed.json())
    queued = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "island-event-queue",
        "external_message_id": "island-message-queue",
        "contact_id": "island-contact-queue", "content": "Olá",
        "group_id": group["id"],
    }).json()["attendance"]
    assert queued["status"] == "AGUARDANDO"
    detail = client.get(f"/api/v1/attendances/{queued['id']}", headers=admin).json()
    assert [item["content"] for item in detail["messages"]] == ["Olá", "Você entrou na fila de espera."]
    assert any(item["contact_id"] == "island-contact-queue" for item in
        client.get("/api/v1/integrations/outbox", headers=admin).json())
    duplicate = client.post("/api/v1/inbox/messages", json={
        "external_event_id": "island-event-queue", "external_message_id": "island-message-queue",
        "contact_id": "island-contact-queue", "content": "Olá", "group_id": group["id"],
    }).json()
    assert duplicate["duplicate"] is True
    assert client.post(f"/api/v1/attendances/{queued['id']}/claim", json={
        "expected_version": queued["version"],
    }, headers=agent).status_code == 409
    closed = client.patch(f"/api/v1/attendances/{claimed_items[0]['id']}/status", json={
        "status": "ENCERRADO", "expected_version": claimed_items[0]["version"],
        "closure_reason": "RESOLVIDO",
    }, headers=agent)
    assert closed.status_code == 200
    assert client.post(f"/api/v1/attendances/{queued['id']}/claim", json={
        "expected_version": queued["version"],
    }, headers=agent).status_code == 200
