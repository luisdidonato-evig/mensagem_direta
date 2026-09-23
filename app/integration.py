import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.domain import DeliveryStatus
from app.models import Message, OutboxMessage
from app.media import media_path

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5


def verify_meta_signature(body: bytes, signature: str | None, app_secret: str) -> None:
    """Valida X-Hub-Signature-256 enviada pelos Webhooks da Meta."""
    if not app_secret:
        raise RuntimeError("Webhook não configurado")
    if not signature:
        raise ValueError("Assinatura ausente")
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    supplied = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, supplied):
        raise ValueError("Assinatura inválida")


class MessageSender(Protocol):
    async def send(self, payload: dict, idempotency_key: str) -> str | None: ...


@dataclass
class TenantMetaSender:
    senders: dict[str, "MetaCloudApiSender"]
    fallback: "MetaCloudApiSender | None" = None

    async def send(self, payload: dict, idempotency_key: str) -> str | None:
        sender = self.senders.get(payload.get("company_id", ""))
        if sender is None and not payload.get("company_id"):
            sender = self.fallback
        if sender is None:
            raise RuntimeError("Credenciais Meta não configuradas para empresa")
        return await sender.send(payload, idempotency_key)


@dataclass
class MetaCloudApiSender:
    phone_number_id: str
    access_token: str
    graph_version: str = "v23.0"
    timeout_seconds: float = 10.0
    transport: httpx.AsyncBaseTransport | None = None
    media_storage_dir: Path | None = None

    async def send(self, payload: dict, idempotency_key: str) -> str | None:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        meta_payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": payload["contact_id"],
            "biz_opaque_callback_data": idempotency_key,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            media = payload.get("media")
            if media:
                if self.media_storage_dir is None:
                    raise RuntimeError("Armazenamento de mídia não configurado")
                path = media_path(self.media_storage_dir, media["storage_key"])
                with path.open("rb") as file:
                    uploaded = await client.post(
                        f"https://graph.facebook.com/{self.graph_version}/"
                        f"{self.phone_number_id}/media",
                        headers={"Authorization": headers["Authorization"]},
                        data={"messaging_product": "whatsapp"},
                        files={"file": (media["filename"], file, media["mime_type"])},
                    )
                uploaded.raise_for_status()
                media_id = uploaded.json()["id"]
                kind = "document" if media["mime_type"] == "application/pdf" else "image"
                media_body = {"id": media_id}
                if kind == "document":
                    media_body["filename"] = media["filename"]
                if media.get("caption"):
                    media_body["caption"] = media["caption"]
                meta_payload["type"] = kind
                meta_payload[kind] = media_body
            elif payload.get("buttons"):
                meta_payload["type"] = "interactive"
                meta_payload["interactive"] = {
                    "type": "button",
                    "body": {"text": payload["content"]},
                    "action": {"buttons": [
                        {"type": "reply", "reply": button}
                        for button in payload["buttons"]
                    ]},
                }
            elif payload.get("list_options"):
                meta_payload["type"] = "interactive"
                meta_payload["interactive"] = {
                    "type": "list",
                    "body": {"text": payload["content"]},
                    "action": {
                        "button": "Dar nota",
                        "sections": [{"title": "Avaliação", "rows": [
                            {"id": option["id"], "title": option["title"]}
                            for option in payload["list_options"]
                        ]}],
                    },
                }
            else:
                meta_payload["type"] = "text"
                meta_payload["text"] = {
                    "preview_url": False, "body": payload["content"]
                }
            response = await client.post(
                f"https://graph.facebook.com/{self.graph_version}/"
                f"{self.phone_number_id}/messages",
                json=meta_payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        messages = data.get("messages") or []
        external_id = messages[0].get("id") if messages else None
        if not external_id:
            raise RuntimeError("Meta não retornou o identificador da mensagem")
        return external_id


class OutboxProcessor:
    def __init__(
        self,
        session_factory: sessionmaker,
        sender: MessageSender,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        batch_size: int = 20,
    ) -> None:
        self.session_factory = session_factory
        self.sender = sender
        self.max_attempts = max_attempts
        self.batch_size = batch_size

    async def process_batch(self) -> tuple[int, int]:
        session = self.session_factory()
        try:
            pending_ids = list(
                session.scalars(
                    select(OutboxMessage.id)
                    .where(
                        OutboxMessage.processed_at.is_(None),
                        OutboxMessage.attempts < self.max_attempts,
                    )
                    .order_by(OutboxMessage.created_at)
                    .limit(self.batch_size)
                )
            )
        finally:
            session.close()

        delivered = 0
        failed = 0
        for outbox_id in pending_ids:
            session = self.session_factory()
            try:
                outbox = session.get(OutboxMessage, outbox_id)
                if outbox is None or outbox.processed_at is not None:
                    continue
                external_id = await self.sender.send(outbox.payload, outbox.id)
                message = session.get(Message, outbox.message_id)
                if message is None:
                    raise RuntimeError("Outbox sem mensagem associada")
                message.external_id = external_id or message.external_id
                message.delivery_status = DeliveryStatus.ACEITA_PELO_PROVEDOR
                outbox.processed_at = datetime.now(timezone.utc)
                session.commit()
                delivered += 1
            except Exception as exc:
                session.rollback()
                outbox = session.get(OutboxMessage, outbox_id)
                if outbox is not None:
                    outbox.attempts += 1
                    message = session.get(Message, outbox.message_id)
                    if message is not None and outbox.attempts >= self.max_attempts:
                        message.delivery_status = DeliveryStatus.FALHA
                    session.commit()
                failed += 1
                logger.warning("Falha no item outbox %s: %s", outbox_id, type(exc).__name__)
            finally:
                session.close()
        return delivered, failed


async def run_outbox_worker(processor: OutboxProcessor, interval_seconds: float) -> None:
    while True:
        await processor.process_batch()
        await asyncio.sleep(interval_seconds)
