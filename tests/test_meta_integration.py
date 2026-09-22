import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import login_headers

from app.domain import DeliveryStatus, MessageDirection
from app.integration import MetaCloudApiSender, OutboxProcessor
from app.main import create_app
from app.models import Message, OutboxMessage

APP_SECRET = "meta-app-secret-de-teste"
VERIFY_TOKEN = "meta-verify-token-de-teste"


def signed_headers(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={digest}",
    }


def inbound_webhook(message_id: str = "wamid.in.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "phone-number-1"},
                            "contacts": [
                                {"profile": {"name": "Maria Cliente"}, "wa_id": "5511999999999"}
                            ],
                            "messages": [
                                {
                                    "from": "5511999999999",
                                    "id": message_id,
                                    "timestamp": "1789977600",
                                    "type": "text",
                                    "text": {"body": "Mensagem recebida da Meta"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_webhook(message_id: str, status: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {
                                    "id": message_id,
                                    "status": status,
                                    "timestamp": "1789977601",
                                    "recipient_id": "5511999999999",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def post_meta(client: TestClient, payload: dict):
    body = json.dumps(payload, separators=(",", ":")).encode()
    return client.post(
        "/api/v1/integrations/meta/whatsapp/webhook",
        content=body,
        headers=signed_headers(body),
    )


def test_meta_webhook_verification(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'verify.db').as_posix()}",
        meta_verify_token=VERIFY_TOKEN,
    )
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/integrations/meta/whatsapp/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "challenge-123",
            },
        )
        denied = client.get(
            "/api/v1/integrations/meta/whatsapp/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "x"},
        )

    assert response.status_code == 200
    assert response.text == "challenge-123"
    assert denied.status_code == 403


def test_meta_webhook_requires_valid_signature(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'signed.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/integrations/meta/whatsapp/webhook", json=inbound_webhook()
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Assinatura ausente"


def test_meta_inbound_is_idempotent_and_updates_contact(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'idempotent.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        first = post_meta(client, inbound_webhook())
        second = post_meta(client, inbound_webhook())
        contact = client.get(
            "/api/v1/contacts/5511999999999",
            headers=login_headers(client, "supervisor-1"),
        )

    assert first.status_code == 200
    assert first.json() == {"processed": 1, "duplicates": 0, "ignored": 0}
    assert second.json() == {"processed": 1, "duplicates": 1, "ignored": 0}
    assert contact.json()["display_name"] == "Maria Cliente"
    assert contact.json()["phone"] == "5511999999999"


def test_meta_sender_uses_cloud_api_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert str(request.url) == "https://graph.facebook.com/v23.0/phone-number-1/messages"
        assert request.headers["Authorization"] == "Bearer access-token"
        assert payload == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "5511999999999",
            "type": "text",
            "text": {"preview_url": False, "body": "Olá pela Cloud API"},
            "biz_opaque_callback_data": "outbox-1",
        }
        return httpx.Response(200, json={"messages": [{"id": "wamid.out.2"}]})

    sender = MetaCloudApiSender(
        phone_number_id="phone-number-1",
        access_token="access-token",
        transport=httpx.MockTransport(handler),
    )
    external_id = asyncio.run(
        sender.send(
            {"contact_id": "5511999999999", "content": "Olá pela Cloud API"},
            "outbox-1",
        )
    )

    assert external_id == "wamid.out.2"


class SuccessfulSender:
    async def send(self, payload: dict, idempotency_key: str) -> str:
        assert payload["content"] == "Resposta pela Meta"
        assert payload["contact_id"] == "5511999999999"
        assert idempotency_key
        return "wamid.out.1"


class FailingSender:
    async def send(self, payload: dict, idempotency_key: str) -> str:
        raise RuntimeError("indisponível")


def prepare_outbound(client: TestClient) -> str:
    assert post_meta(client, inbound_webhook()).status_code == 200
    headers = login_headers(client, "agente-1")
    board = client.get(
        "/api/v1/attendances",
        headers=headers,
    ).json()
    attendance = board[0]
    claimed = client.post(
        f"/api/v1/attendances/{attendance['id']}/claim",
        json={"expected_version": attendance["version"]},
        headers=headers,
    ).json()
    response = client.post(
        f"/api/v1/attendances/{attendance['id']}/messages",
        json={"client_message_id": "client-meta-1", "content": "Resposta pela Meta"},
        headers=headers,
    )
    assert claimed["assignee_id"] == "agente-1"
    assert response.status_code == 201
    return attendance["id"]


def test_outbox_delivery_and_meta_status_callback(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'outbox.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        attendance_id = prepare_outbound(client)
        processor = OutboxProcessor(app.state.database.session_factory, SuccessfulSender())
        assert asyncio.run(processor.process_batch()) == (1, 0)
        assert post_meta(client, status_webhook("wamid.out.1", "delivered")).status_code == 200
        assert post_meta(client, status_webhook("wamid.out.1", "read")).status_code == 200

        detail = client.get(
            f"/api/v1/attendances/{attendance_id}",
            headers=login_headers(client, "agente-1"),
        ).json()
        outbound = [
            item for item in detail["messages"] if item["direction"] == MessageDirection.SAIDA
        ][0]
        assert outbound["external_id"] == "wamid.out.1"
        assert outbound["delivery_status"] == DeliveryStatus.LIDA


def test_meta_callback_reconciles_uncertain_send(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'uncertain.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        attendance_id = prepare_outbound(client)
        with app.state.database.session_factory() as session:
            outbox_id = session.scalar(select(OutboxMessage.id))
        callback = status_webhook("wamid.uncertain.1", "delivered")
        callback["entry"][0]["changes"][0]["value"]["statuses"][0][
            "biz_opaque_callback_data"
        ] = outbox_id
        assert post_meta(client, callback).status_code == 200
        with app.state.database.session_factory() as session:
            outbox = session.get(OutboxMessage, outbox_id)
            assert outbox.processed_at is not None
        detail = client.get(
            f"/api/v1/attendances/{attendance_id}",
            headers=login_headers(client, "agente-1"),
        ).json()
        outbound = [m for m in detail["messages"] if m["direction"] == "SAIDA"][0]
        assert outbound["external_id"] == "wamid.uncertain.1"
        assert outbound["delivery_status"] == "ENTREGUE"


def test_meta_failed_status_is_visible_for_assisted_retry(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'meta-failed.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        prepare_outbound(client)
        processor = OutboxProcessor(app.state.database.session_factory, SuccessfulSender())
        assert asyncio.run(processor.process_batch()) == (1, 0)
        assert post_meta(client, status_webhook("wamid.out.1", "failed")).status_code == 200
        failed = client.get(
            "/api/v1/integrations/outbox",
            headers=login_headers(client, "supervisor-1"),
        ).json()
        assert len(failed) == 1
        assert failed[0]["delivery_status"] == "FALHA"
        assert failed[0]["attempts"] == 5


def test_outbox_marks_message_failed_after_limit(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'failure.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        prepare_outbound(client)
        processor = OutboxProcessor(
            app.state.database.session_factory, FailingSender(), max_attempts=2
        )
        asyncio.run(processor.process_batch())
        asyncio.run(processor.process_batch())

        with app.state.database.session_factory() as session:
            outbox = session.scalar(select(OutboxMessage))
            message = session.scalar(
                select(Message).where(Message.direction == MessageDirection.SAIDA)
            )
            assert outbox is not None and outbox.attempts == 2
            assert message is not None
            assert message.delivery_status == DeliveryStatus.FALHA


def test_assisted_outbox_reprocessing(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'retry.db').as_posix()}",
        meta_app_secret=APP_SECRET,
    )
    with TestClient(app) as client:
        supervisor_headers = login_headers(client, "supervisor-1")
        prepare_outbound(client)
        processor = OutboxProcessor(
            app.state.database.session_factory, FailingSender(), max_attempts=2
        )
        asyncio.run(processor.process_batch())
        asyncio.run(processor.process_batch())

        failed_list = client.get(
            "/api/v1/integrations/outbox", headers=supervisor_headers
        ).json()
        assert len(failed_list) == 1
        assert failed_list[0]["attempts"] == 2
        assert failed_list[0]["delivery_status"] == DeliveryStatus.FALHA

        retried = client.post(
            f"/api/v1/integrations/outbox/{failed_list[0]['id']}/retry",
            headers=supervisor_headers,
        )
        assert retried.status_code == 200
        assert retried.json()["attempts"] == 0
        assert retried.json()["delivery_status"] == DeliveryStatus.PENDENTE

        still_listed = client.get(
            "/api/v1/integrations/outbox", headers=supervisor_headers
        ).json()
        assert len(still_listed) == 1
        assert still_listed[0]["attempts"] == 0

        assert asyncio.run(
            OutboxProcessor(
                app.state.database.session_factory, SuccessfulSender()
            ).process_batch()
        ) == (1, 0)
        assert client.get(
            "/api/v1/integrations/outbox", headers=supervisor_headers
        ).json() == []
