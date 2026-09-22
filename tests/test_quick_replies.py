from conftest import login_headers

from app.main import DEV_GROUP_ID


def test_quick_reply_scoped_to_group(client):
    admin_headers = login_headers(client, "admin-1")
    supervisor_headers = login_headers(client, "supervisor-1")

    other_group = client.post(
        "/api/v1/groups",
        json={"name": "Outro Grupo", "max_load_per_agent": 5},
        headers=admin_headers,
    ).json()

    global_reply = client.post(
        "/api/v1/quick-replies",
        json={"title": "Saudação", "content": "Olá, como posso ajudar?"},
        headers=supervisor_headers,
    )
    assert global_reply.status_code == 201
    assert global_reply.json()["group_id"] is None

    scoped_reply = client.post(
        "/api/v1/quick-replies",
        json={
            "title": "Encerramento grupo B",
            "content": "Obrigado por contatar o grupo B",
            "group_id": other_group["id"],
        },
        headers=supervisor_headers,
    )
    assert scoped_reply.status_code == 201

    agent_headers = login_headers(client, "agente-1")
    default_list = client.get("/api/v1/quick-replies", headers=agent_headers)
    assert default_list.status_code == 200
    titles = {item["title"] for item in default_list.json()}
    assert "Saudação" in titles
    assert "Encerramento grupo B" not in titles

    client.put(
        f"/api/v1/groups/{other_group['id']}/agents/agente-1",
        json={},
        headers=admin_headers,
    )
    scoped_list = client.get(
        "/api/v1/quick-replies",
        params={"group_id": other_group["id"]},
        headers=agent_headers,
    )
    assert scoped_list.status_code == 200
    scoped_titles = {item["title"] for item in scoped_list.json()}
    assert "Encerramento grupo B" in scoped_titles
    assert "Saudação" in scoped_titles


def test_delete_quick_reply_deactivates_instead_of_removing(client):
    supervisor_headers = login_headers(client, "supervisor-1")
    created = client.post(
        "/api/v1/quick-replies",
        json={"title": "Temporária", "content": "Mensagem temporária"},
        headers=supervisor_headers,
    ).json()

    deleted = client.delete(
        f"/api/v1/quick-replies/{created['id']}", headers=supervisor_headers
    )
    assert deleted.status_code == 204

    agent_headers = login_headers(client, "agente-1")
    listed = client.get("/api/v1/quick-replies", headers=agent_headers)
    assert created["title"] not in {item["title"] for item in listed.json()}
