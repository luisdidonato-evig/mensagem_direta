from conftest import login_headers


def test_timeline_unifies_messages_events_and_notes(client, inbound_payload, agent_headers):
    attendance = client.post("/api/v1/inbox/messages", json=inbound_payload).json()[
        "attendance"
    ]

    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()

    client.post(
        f"/api/v1/attendances/{attendance['id']}/messages",
        json={"client_message_id": "client-msg-tl", "content": "Como posso ajudar?"},
        headers=agent_headers,
    )

    client.post(
        f"/api/v1/attendances/{attendance['id']}/notes",
        json={"content": "Cliente ligou reclamando de atraso"},
        headers=agent_headers,
    )

    response = client.get(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/timeline",
        headers=agent_headers,
    )

    assert response.status_code == 200
    items = response.json()
    kinds = [item["kind"] for item in items]
    assert "message" in kinds
    assert "event" in kinds
    assert "note" in kinds

    inbound_message = next(
        item for item in items if item["kind"] == "message" and item["details"]["direction"] == "ENTRADA"
    )
    assert inbound_message["sender_type"] == "CLIENTE"
    assert inbound_message["actor_id"] is None

    outbound_message = next(
        item for item in items if item["kind"] == "message" and item["details"]["direction"] == "SAIDA"
    )
    assert outbound_message["sender_type"] == "ATENDENTE"
    assert outbound_message["actor_id"] == "agente-1"

    timestamps = [item["created_at"] for item in items]
    assert timestamps == sorted(timestamps)


def test_timeline_returns_404_for_unknown_contact(client, agent_headers):
    response = client.get(
        "/api/v1/contacts/contato-inexistente/timeline", headers=agent_headers
    )

    assert response.status_code == 404


def test_timeline_isolated_by_company(client, inbound_payload):
    attendance_response = client.post("/api/v1/inbox/messages", json=inbound_payload)
    assert attendance_response.status_code == 201

    from app.domain import ActorRole
    from app.main import DEV_SEED_PASSWORD
    from app.models import Agent, Company
    from app.security import hash_password

    app = client.app
    with app.state.database.session_factory() as session:
        session.add(Company(id="empresa-externa", name="Externa"))
        session.add(
            Agent(
                id="admin-externo-tl",
                company_id="empresa-externa",
                role=ActorRole.ADMIN,
                password_hash=hash_password(DEV_SEED_PASSWORD),
            )
        )
        session.commit()

    external_headers = login_headers(client, "admin-externo-tl")

    response = client.get(
        f"/api/v1/contacts/{inbound_payload['contact_id']}/timeline",
        headers=external_headers,
    )

    assert response.status_code == 404
