import pytest
from fastapi.testclient import TestClient

from app.main import DEV_SEED_PASSWORD, create_app


@pytest.fixture
def client(tmp_path):
    database_path = tmp_path / "test.db"
    app = create_app(f"sqlite:///{database_path.as_posix()}")
    with TestClient(app) as test_client:
        yield test_client


def login_headers(client: TestClient, actor_id: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login", json={"id": actor_id, "password": DEV_SEED_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture
def agent_headers(client):
    return login_headers(client, "agente-1")


@pytest.fixture
def supervisor_headers(client):
    return login_headers(client, "supervisor-1")


@pytest.fixture
def inbound_payload():
    return {
        "external_event_id": "evt-1",
        "external_message_id": "msg-1",
        "contact_id": "contato-anonimo-1",
        "content": "Preciso de atendimento",
    }

