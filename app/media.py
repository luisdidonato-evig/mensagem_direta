"""Armazenamento privado e validação de imagens/PDF do atendimento."""

from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


def media_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "image", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "image/png"
    if data.startswith(b"%PDF-"):
        return "document", "application/pdf"
    raise ValueError("Arquivo deve ser JPEG, PNG ou PDF")


def validate_media(data: bytes) -> tuple[str, str]:
    if not data:
        raise ValueError("Arquivo vazio")
    kind, mime = media_type(data)
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_DOCUMENT_BYTES
    if len(data) > limit:
        raise ValueError("Arquivo excede o limite permitido")
    return kind, mime


def safe_filename(filename: str | None, mime: str) -> str:
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}[mime]
    name = (filename or "anexo").replace("\\", "/").split("/")[-1]
    stem = Path(name).stem[:100].strip(" .") or "anexo"
    return stem + suffix


def write_media(directory: Path, data: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    key = str(uuid4())
    (directory / key).write_bytes(data)
    return key


def media_path(directory: Path, key: str) -> Path:
    if len(key) != 36 or any(char not in "0123456789abcdef-" for char in key):
        raise ValueError("Chave de mídia inválida")
    return directory / key


class MetaMediaDownloader:
    def __init__(
        self,
        access_token: str,
        graph_version: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.access_token = access_token
        self.graph_version = graph_version
        self.transport = transport

    async def download(self, media_id: str) -> bytes:
        if not self.access_token:
            raise RuntimeError("Token Meta não configurado para baixar mídia")
        headers = {"Authorization": f"Bearer {self.access_token}"}
        async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
            metadata = await client.get(
                f"https://graph.facebook.com/{self.graph_version}/{media_id}",
                headers=headers,
            )
            metadata.raise_for_status()
            url = metadata.json().get("url", "")
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not (
                hostname == "fbsbx.com" or hostname.endswith(".fbsbx.com")
                or hostname == "fbcdn.net" or hostname.endswith(".fbcdn.net")
                or hostname == "facebook.com" or hostname.endswith(".facebook.com")
            ):
                raise ValueError("URL de mídia da Meta inválida")
            data = bytearray()
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_DOCUMENT_BYTES:
                        raise ValueError("Mídia recebida excede o limite permitido")
        validate_media(data)
        return bytes(data)
