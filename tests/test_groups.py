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
