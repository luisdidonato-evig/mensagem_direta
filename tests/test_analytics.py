from conftest import login_headers

from app.analytics import process_analytics_outbox
from app.models import AnalyticsOutbox, ConversationInsight


def close_attendance(client, attendance_id, version, headers, reason="RESOLVIDO"):
    response = client.patch(
        f"/api/v1/attendances/{attendance_id}/status",
        json={
            "status": "ENCERRADO",
            "expected_version": version,
            "closure_reason": reason,
            "closure_note": "Concluído nos testes",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_closing_attendance_enqueues_analytics_event(client, inbound_payload, agent_headers):
    attendance = client.post("/api/v1/inbox/messages", json=inbound_payload).json()[
        "attendance"
    ]
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()

    close_attendance(client, attendance["id"], claimed["version"], agent_headers)

    app = client.app
    with app.state.database.session_factory() as session:
        pending = session.query(AnalyticsOutbox).filter_by(
            attendance_id=attendance["id"], topic="conversation.closed"
        ).all()
        assert len(pending) == 1
        assert pending[0].processed_at is None


def test_worker_builds_conversation_insight_projection(client, inbound_payload, agent_headers):
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
        json={"client_message_id": "client-msg-analytics", "content": "Resposta ao cliente"},
        headers=agent_headers,
    )

    current = client.get(
        f"/api/v1/attendances/{attendance['id']}", headers=agent_headers
    ).json()
    close_attendance(client, attendance["id"], current["version"], agent_headers)

    app = client.app
    with app.state.database.session_factory() as session:
        processed = process_analytics_outbox(session)
        assert processed == 1

        insight = session.query(ConversationInsight).filter_by(
            attendance_id=attendance["id"]
        ).one()
        assert insight.company_id is not None
        assert insight.closed_at is not None
        assert insight.duration_seconds is not None
        assert insight.message_count_client == 1
        assert insight.message_count_human == 1
        assert insight.closure_reason == "RESOLVIDO"

        outbox_entry = session.query(AnalyticsOutbox).filter_by(
            attendance_id=attendance["id"]
        ).one()
        assert outbox_entry.processed_at is not None


def test_worker_is_idempotent_when_rerun(client, inbound_payload, agent_headers):
    attendance = client.post("/api/v1/inbox/messages", json=inbound_payload).json()[
        "attendance"
    ]
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=agent_headers,
    ).json()
    close_attendance(client, attendance["id"], claimed["version"], agent_headers)

    app = client.app
    with app.state.database.session_factory() as session:
        first_run = process_analytics_outbox(session)
        second_run = process_analytics_outbox(session)
        assert first_run == 1
        assert second_run == 0

        count = session.query(ConversationInsight).filter_by(
            attendance_id=attendance["id"]
        ).count()
        assert count == 1
