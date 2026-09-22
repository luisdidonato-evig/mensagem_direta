from conftest import login_headers

from app.main import DEV_GROUP_ID


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


def send_inbound(client, contact_id, group_id, load_weight=1, priority=0, event_suffix="1"):
    response = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": f"evt-{contact_id}-{event_suffix}",
            "external_message_id": f"msg-{contact_id}-{event_suffix}",
            "contact_id": contact_id,
            "content": "Preciso de atendimento",
            "group_id": group_id,
            "load_weight": load_weight,
            "priority": priority,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["attendance"]


def test_pull_assigns_oldest_waiting_attendance(client):
    admin_headers = login_headers(client, "admin-1")
    agent_headers = login_headers(client, "agente-1")

    send_inbound(client, "contato-fifo-1", DEV_GROUP_ID)
    second = send_inbound(client, "contato-fifo-2", DEV_GROUP_ID)

    response = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contact_id"] == "contato-fifo-1"
    assert body["assignee_id"] == "agente-1"
    assert body["status"] == "EM_ATENDIMENTO"
    assert second["contact_id"] == "contato-fifo-2"


def test_pull_respects_priority_before_age(client):
    agent_headers = login_headers(client, "agente-1")

    send_inbound(client, "contato-prio-old", DEV_GROUP_ID)
    send_inbound(client, "contato-prio-new", DEV_GROUP_ID, priority=5, event_suffix="2")

    response = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )

    assert response.status_code == 200
    assert response.json()["contact_id"] == "contato-prio-new"


def test_pull_returns_204_when_queue_empty(client, agent_headers):
    response = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )

    assert response.status_code == 204


def test_pull_rejects_agent_outside_group(client):
    admin_headers = login_headers(client, "admin-1")
    other_group = create_group(client, admin_headers, "Isolado")
    send_inbound(client, "contato-isolado", other_group["id"])

    agent_headers = login_headers(client, "agente-1")
    response = client.post(
        f"/api/v1/groups/{other_group['id']}/attendances/pull", headers=agent_headers
    )

    assert response.status_code == 403


def test_pull_enforces_capacity(client):
    admin_headers = login_headers(client, "admin-1")
    link_agent(client, admin_headers, DEV_GROUP_ID, "agente-1", max_load_override=1)
    agent_headers = login_headers(client, "agente-1")

    send_inbound(client, "contato-cap-1", DEV_GROUP_ID)
    send_inbound(client, "contato-cap-2", DEV_GROUP_ID, event_suffix="2")

    first = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )
    assert second.status_code == 409


def test_pull_skips_attendance_too_heavy_for_remaining_capacity(client):
    admin_headers = login_headers(client, "admin-1")
    link_agent(client, admin_headers, DEV_GROUP_ID, "agente-1", max_load_override=2)
    agent_headers = login_headers(client, "agente-1")

    send_inbound(client, "contato-heavy", DEV_GROUP_ID, load_weight=3)
    light = send_inbound(client, "contato-light", DEV_GROUP_ID, load_weight=1, event_suffix="2")

    response = client.post(
        f"/api/v1/groups/{DEV_GROUP_ID}/attendances/pull", headers=agent_headers
    )

    assert response.status_code == 200
    assert response.json()["contact_id"] == light["contact_id"]
