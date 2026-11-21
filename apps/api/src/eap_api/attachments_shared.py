"""Chat attachment upload and download, shared by the dev routes (`/attachments`) and their AG-UI
twins for API keys (`/ag-ui/attachments`): one validation, one storage path, one response shape.

An upload is validated (size, type, sniff) and hashed first, then streamed into the BlobStore
(the app's `blob_store`); the row records its `storage_key`. A download streams out of the store.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote

from eap_api.deps import get_chat_attachment_store, get_blob_store
from fastapi import HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
import anyio.from_thread
from starlette.concurrency import run_in_threadpool

from eap_blob_store import BlobNotFound, attachment_key
from eap_run_store.chat_attachments import (
    ALLOWED_MEDIA_TYPES,
    ATTACHMENT_NAME_MAX_CHARS,
    IMAGE_TYPES,
    AttachmentMeta,
    PDF_TYPE,
    is_text_type,
)

logger = logging.getLogger(__name__)
_CHUNK = 64 * 1024


def created_by_for(principal: Any) -> str:
    """Who an upload belongs to: the user, or `key:{key_id}` for an API key (it has no user)."""
    user_id = getattr(principal, "user_id", None)
    if user_id:
        return str(user_id)
    return f"key:{principal.key_id}"


@dataclass(frozen=True)
class UploadedBytes:
    media_type: str
    size: int
    sha256: str
    name: str


def sniff_ok(media_type: str, data: bytes) -> bool:
    """Whether the bytes are what the declared type says (signatures at offset 0; text must be UTF-8)."""
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if media_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if media_type == PDF_TYPE:
        return data.lstrip()[:5] == b"%PDF-"
    if is_text_type(media_type):
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return b"\x00" not in data
    return False


class _Sniffer:
    """`sniff_ok` over a stream: signatures are checked on the first bytes, text must decode as UTF-8
    without NUL across the whole file (checked chunk by chunk)."""

    _HEAD = 1024

    def __init__(self, media_type: str) -> None:
        self.media_type = media_type
        self.head = b""
        self.text = is_text_type(media_type)
        self.decoder = codecs.getincrementaldecoder("utf-8")() if self.text else None

    def feed(self, chunk: bytes) -> bool:
        if self.decoder is not None:
            if b"\x00" in chunk:
                return False
            try:
                self.decoder.decode(chunk)
            except UnicodeDecodeError:
                return False
        elif len(self.head) < self._HEAD:
            self.head += chunk[: self._HEAD - len(self.head)]
        return True

    def finish(self) -> bool:
        if self.decoder is not None:
            try:
                self.decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                return False
            return True
        return sniff_ok(self.media_type, self.head)


def _safe_name(name: str | None) -> str:
    cleaned = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable()).strip()
    return (cleaned or "attachment")[:ATTACHMENT_NAME_MAX_CHARS]


async def read_upload(request: Request, file: UploadFile, limit: int) -> UploadedBytes:
    """Validate a multipart upload before anything reaches the store: 413 over `limit`, 415 for a type
    outside the allowlist or bytes that are not what it says, 422 when empty. The file is left at 0."""
    declared = request.headers.get("content-length")
    # The multipart envelope adds a little; refuse a body that is clearly over before reading it.
    if declared is not None and declared.isdigit() and int(declared) > limit + 64 * 1024:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=f"the attachment is over {limit} bytes")
    media_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"media type {media_type or 'unknown'!r} is not allowed; allowed: {', '.join(sorted(ALLOWED_MEDIA_TYPES))}",
        )
    sniffer = _Sniffer(media_type)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=f"the attachment is over {limit} bytes")
        if not sniffer.feed(chunk):
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"the bytes are not a valid {media_type} file"
            )
        digest.update(chunk)
    if total == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="the attachment is empty")
    if not sniffer.finish():
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"the bytes are not a valid {media_type} file"
        )
    await file.seek(0)
    return UploadedBytes(media_type=media_type, size=total, sha256=digest.hexdigest(), name=_safe_name(file.filename))


async def store_upload(
    request: Request, tenant_id: str, file: UploadFile, uploaded: UploadedBytes, created_by: str
) -> JSONResponse:
    """Put the validated bytes in the BlobStore (503 when it is down), record the row, 201 with its meta."""
    attachment_id = str(uuid.uuid4())
    key = attachment_key(tenant_id, attachment_id)
    store = get_blob_store(request)

    async def chunks() -> AsyncIterator[bytes]:
        while piece := await file.read(_CHUNK):
            yield piece

    try:
        await store.put(key, chunks(), uploaded.size, uploaded.media_type)
    except Exception as exc:  # noqa: BLE001 - the object store is unreachable or refused the write
        logger.exception("attachment blob put failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="attachment storage is unavailable") from exc
    try:
        meta = await run_in_threadpool(
            get_chat_attachment_store(request).create,
            tenant_id, attachment_id=attachment_id, name=uploaded.name, media_type=uploaded.media_type,
            size=uploaded.size, sha256=uploaded.sha256, storage_key=key, created_by=created_by,
        )
    except BaseException:
        try:  # the row failed: do not leave an orphan blob
            await store.delete(key)
        except Exception:  # noqa: BLE001
            logger.exception("could not remove the blob of a failed attachment upload")
        raise
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=meta.as_json())


async def _with_first(first: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    try:
        yield first
        async for chunk in rest:
            yield chunk
    finally:
        await rest.aclose()  # type: ignore[attr-defined]


async def attachment_response(request: Request, meta: AttachmentMeta) -> Response:
    """The bytes of a found attachment (410 `attachment_gone` when its blob is missing), nosniff, inline
    only for images and PDFs."""
    stream = get_blob_store(request).open(meta.storage_key)
    try:
        first = await anext(stream)  # a missing blob raises here, before any byte is sent
    except BlobNotFound:
        raise HTTPException(
            status.HTTP_410_GONE,
            detail={"code": "attachment_gone", "message": "The attachment's file is no longer available"},
        ) from None
    except StopAsyncIteration:
        first = b""
    except Exception as exc:  # noqa: BLE001 - the object store is unreachable (not a missing blob)
        logger.exception("attachment blob read failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="attachment storage is unavailable") from exc
    # Only images and PDFs may render inline; everything else downloads. nosniff always.
    disposition = "inline" if meta.media_type in IMAGE_TYPES or meta.media_type == PDF_TYPE else "attachment"
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", meta.name) or "attachment"
    return StreamingResponse(
        _with_first(first, stream),
        media_type=meta.media_type,
        headers={
            "Content-Length": str(meta.size),
            "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(meta.name)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=300",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )




async def delete_unsent_attachment(request: Request, tenant_id: str, attachment_id: str, created_by: str) -> Response:
    """Discard an upload that no message references: 204; 409 `attachment_in_use` once a message
    references it; 404 when it is unknown, another tenant's or another creator's. The row is read
    locked, then the blob is deleted, then the row (see `ChatAttachmentStore.delete_unsent`)."""
    store = get_blob_store(request)

    def remove_blob(key: str) -> None:  # runs in the worker thread that holds the row lock
        anyio.from_thread.run(store.delete, key)

    outcome = await run_in_threadpool(
        get_chat_attachment_store(request).delete_unsent, tenant_id, attachment_id, created_by, remove_blob=remove_blob
    )
    if outcome == "missing":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    if outcome == "in_use":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "attachment_in_use", "message": "The attachment is already sent in a message"},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
