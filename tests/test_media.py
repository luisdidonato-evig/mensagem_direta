import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi.testclient import TestClient

from conftest import login_headers

from app.integration import MetaCloudApiSender
from app.main import DEV_GROUP_ID, create_app

PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"


def test_attachment_upload_and_private_download(client):
    headers = login_headers(client, "agente-1")
    inbound = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": "media-in-1",
            "external_message_id": "media-msg-1",
            "contact_id": "5511000000000",
            "content": "Envie o PDF",
            "group_id": DEV_GROUP_ID,
        },
    ).json()["attendance"]
    claim = client.post(
        f"/api/v1/attendances/{inbound['id']}/claim",
        json={"expected_version": inbound["version"]},
        headers=headers,
    )
    assert claim.status_code == 200

    upload = client.post(
        f"/api/v1/attendances/{inbound['id']}/attachments",
        headers=headers,
        data={"client_message_id": "media-out-1", "caption": "Contrato"},
        files={"file": ("../contrato.pdf", PDF, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    attachment = upload.json()["attachment"]
    assert attachment["mime_type"] == "application/pdf"
    assert attachment["filename"] == "contrato.pdf"
    assert client.get(f"/api/v1/attachments/{attachment['id']}").status_code == 401
    downloaded = client.get(f"/api/v1/attachments/{attachment['id']}", headers=headers)
    assert downloaded.content == PDF
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert client.get(
        f"/api/v1/attendances/{inbound['id']}", headers=headers
    ).json()["messages"][-1]["attachment"]["id"] == attachment["id"]

    invalid = client.post(
        f"/api/v1/attendances/{inbound['id']}/attachments",
        headers=headers,
        data={"client_message_id": "media-out-2"},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
    )
    assert invalid.status_code == 422


def test_attachment_download_respects_group_scope(client):
    admin = login_headers(client, "admin-1")
    group = client.post(
        "/api/v1/groups", json={"name": "Grupo privado"}, headers=admin
    ).json()
    assert client.put(
        f"/api/v1/groups/{group['id']}/agents/agente-2",
        json={}, headers=admin,
    ).status_code == 200
    inbound = client.post(
        "/api/v1/inbox/messages",
        json={
            "external_event_id": "private-media-in",
            "external_message_id": "private-media-msg",
            "contact_id": "5511888888888",
            "content": "PDF privado",
            "group_id": group["id"],
        },
    ).json()["attendance"]
    agent2 = login_headers(client, "agente-2")
    assert client.post(
        f"/api/v1/attendances/{inbound['id']}/claim",
        json={"expected_version": inbound["version"]}, headers=agent2,
    ).status_code == 200
    upload = client.post(
        f"/api/v1/attendances/{inbound['id']}/attachments",
        headers=agent2,
        data={"client_message_id": "private-media-out"},
        files={"file": ("privado.pdf", PDF, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    attachment_id = upload.json()["attachment"]["id"]
    assert client.get(
        f"/api/v1/attachments/{attachment_id}",
        headers=login_headers(client, "agente-1"),
    ).status_code == 403


def test_meta_sender_uploads_document_then_sends_media(tmp_path):
    key = "00000000-0000-0000-0000-000000000001"
    (tmp_path / key).write_bytes(PDF)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url).endswith("/media"):
            assert b"contrato.pdf" in request.content
            assert PDF in request.content
            return httpx.Response(200, json={"id": "meta-media-1"})
        payload = json.loads(request.content)
        assert payload["type"] == "document"
        assert payload["document"] == {
            "id": "meta-media-1", "filename": "contrato.pdf", "caption": "Contrato"
        }
        return httpx.Response(200, json={"messages": [{"id": "wamid.media.1"}]})

    sender = MetaCloudApiSender(
        "phone-1", "token", transport=httpx.MockTransport(handler),
        media_storage_dir=tmp_path,
    )
    result = asyncio.run(sender.send({
        "contact_id": "5511000000000",
        "content": "Contrato",
        "media": {
            "storage_key": key, "mime_type": "application/pdf",
            "filename": "contrato.pdf", "caption": "Contrato",
        },
    }, "outbox-media-1"))
    assert result == "wamid.media.1"
    assert len(requests) == 2


def test_meta_image_webhook_stores_media_once(tmp_path):
    app = create_app(
        f"sqlite:///{(tmp_path / 'media-inbound.db').as_posix()}",
        meta_app_secret="secret",
    )
    downloads = []

    async def download(media_id):
        downloads.append(media_id)
        return PNG

    app.state.meta_media_downloader.download = download
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messages": [{
                "from": "5511999999999", "id": "wamid.media.in.1",
                "type": "image", "image": {"id": "meta-image-1", "caption": "Foto"},
            }],
        }}]}],
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={signature}"}
    with TestClient(app) as client:
        first = client.post(
            "/api/v1/integrations/meta/whatsapp/webhook", content=body, headers=headers
        )
        second = client.post(
            "/api/v1/integrations/meta/whatsapp/webhook", content=body, headers=headers
        )
        assert first.status_code == 200, first.text
        assert second.json()["duplicates"] == 1
        assert downloads == ["meta-image-1"]
        attendance = client.get(
            "/api/v1/attendances", headers=login_headers(client, "supervisor-1")
        ).json()[0]
        detail = client.get(
            f"/api/v1/attendances/{attendance['id']}",
            headers=login_headers(client, "supervisor-1"),
        ).json()
        assert detail["messages"][0]["attachment"]["mime_type"] == "image/png"
