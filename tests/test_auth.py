from conftest import login_headers

from app.main import DEV_COMPANY_ID, DEV_SEED_PASSWORD


def test_login_succeeds_with_valid_credentials(client):
    response = client.post(
        "/api/v1/auth/login", json={"id": "agente-1", "password": DEV_SEED_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"
    assert body["actor"] == {
        "id": "agente-1",
        "role": "ATENDENTE",
        "company_id": DEV_COMPANY_ID,
    }


def test_login_fails_with_wrong_password(client):
    response = client.post(
        "/api/v1/auth/login", json={"id": "agente-1", "password": "senha-errada"}
    )

    assert response.status_code == 401


def test_login_fails_with_unknown_agent(client):
    response = client.post(
        "/api/v1/auth/login", json={"id": "fantasma", "password": "qualquer"}
    )

    assert response.status_code == 401


def test_protected_route_requires_bearer_token(client):
    response = client.get("/api/v1/attendances")

    assert response.status_code == 401


def test_protected_route_rejects_garbage_token(client):
    response = client.get(
        "/api/v1/attendances", headers={"Authorization": "Bearer token-invalido"}
    )

    assert response.status_code == 401


def test_admin_can_create_agent_and_new_agent_can_login(client):
    admin_headers = login_headers(client, "admin-1")

    created = client.post(
        "/api/v1/agents",
        json={"id": "agente-novo", "role": "ATENDENTE", "password": "senha-forte-123"},
        headers=admin_headers,
    )
    listed = client.get("/api/v1/agents", headers=admin_headers)
    new_login = client.post(
        "/api/v1/auth/login",
        json={"id": "agente-novo", "password": "senha-forte-123"},
    )

    assert created.status_code == 201
    assert created.json()["role"] == "ATENDENTE"
    assert any(agent["id"] == "agente-novo" for agent in listed.json())
    assert new_login.status_code == 200


def test_non_admin_cannot_create_agent(client, agent_headers):
    response = client.post(
        "/api/v1/agents",
        json={"id": "outro", "role": "ATENDENTE", "password": "senha-forte-123"},
        headers=agent_headers,
    )

    assert response.status_code == 403


def test_duplicate_agent_id_conflicts(client):
    admin_headers = login_headers(client, "admin-1")

    response = client.post(
        "/api/v1/agents",
        json={"id": "agente-1", "role": "ATENDENTE", "password": "senha-forte-123"},
        headers=admin_headers,
    )

    assert response.status_code == 409


def test_deactivated_agent_token_stops_working(client):
    agent_headers = login_headers(client, "agente-1")
    admin_headers = login_headers(client, "admin-1")
    deactivated = client.patch(
        "/api/v1/agents/agente-1",
        json={"active": False},
        headers=admin_headers,
    )
    assert deactivated.status_code == 200
    assert client.get("/api/v1/attendances", headers=agent_headers).status_code == 401
