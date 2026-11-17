"""Chat attachments and message feedback (W2.4).

- POST /attachments                  multipart upload (field `file`) -> 201 {id, name, media_type, size}.
                                     413 over EAP_CHAT_ATTACHMENT_MAX_BYTES (default 10 MiB), 415 for a
                                     media type outside the allowlist (images, PDF, plain text, markdown,
                                     CSV, JSON) or bytes that are not what the type says.
- GET  /attachments/{id}             the bytes, tenant-scoped (another tenant's id is 404), with the stored
                                     content type, `Content-Disposition` and `X-Content-Type-Options: nosniff`.
                                     410 `attachment_gone` when the row exists but its blob does not.

The bytes live in the BlobStore (`app.state.blob_store`, built from EAP_BLOB_* at startup), never in
Postgres: an upload is validated (size, type, sniff) and then streamed into the store, the row records
its `storage_key`; a download streams out of the store.
- PUT    /conversations/{id}/messages/{message_id}/feedback   {rating: up|down, comment?}: set (replaces).
- DELETE /conversations/{id}/messages/{message_id}/feedback   clear the caller's feedback (204).
- GET    /conversations/{id}/feedback                          the caller's feedback on the thread's messages.

A message sends an attachment as an AG-UI 1.0 `image` / `document` content part whose `source` is the
URL `/api/v1/attachments/{id}` (see eap_api.ag_ui.input). Feedback is one row per (tenant, message,
user) tied to the run that produced the message; AG-UI thread snapshots return it under `feedback`.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import re
import uuid
from typing import Annotated, Any, AsyncIterator, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_blob_store import BlobNotFound, BlobStore, attachment_key
from eap_api.auth import Principal, current_principal, feedback_viewer
from eap_api.schemas import ErrorDetail
from eap_run_store import AgUiRunModel
from eap_run_store.chat_attachments import (
    ALLOWED_MEDIA_TYPES,
    ATTACHMENT_NAME_MAX_CHARS,
    FEEDBACK_COMMENT_MAX_CHARS,
    IMAGE_TYPES,
    ChatAttachmentStore,
    MessageFeedbackStore,
    PDF_TYPE,
    is_text_type,
    max_attachment_bytes,
)

router = APIRouter(
    tags=["attachments"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]

logger = logging.getLogger(__name__)
_CHUNK = 64 * 1024
_MESSAGE_ID = re.compile(r"^(?P<prefix>.+)-m(?P<n>\d+)$")


def attachment_store(request: Request) -> ChatAttachmentStore:
    store = getattr(request.app.state, "chat_attachment_store", None)
    if store is None:
        store = request.app.state.chat_attachment_store = ChatAttachmentStore(request.app.state.database_engine)
    return store


def blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


def feedback_store(request: Request) -> MessageFeedbackStore:
    store = getattr(request.app.state, "message_feedback_store", None)
    if store is None:
        store = request.app.state.message_feedback_store = MessageFeedbackStore(request.app.state.database_engine)
    return store


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


@router.post(
    "/attachments",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 415: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def upload_attachment(request: Request, principal: PrincipalDep, file: UploadFile) -> JSONResponse:
    limit = max_attachment_bytes()
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
    # Pass 1 over the spooled upload: size, sniff and hash before anything reaches the store, so a
    # refused upload leaves no blob and no row.
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
    attachment_id = str(uuid.uuid4())
    key = attachment_key(principal.tenant_id, attachment_id)
    await file.seek(0)
    store = blob_store(request)

    async def chunks() -> AsyncIterator[bytes]:
        while piece := await file.read(_CHUNK):
            yield piece

    try:
        await store.put(key, chunks(), total, media_type)
    except Exception as exc:  # noqa: BLE001 - the object store is unreachable or refused the write
        logger.exception("attachment blob put failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="attachment storage is unavailable") from exc
    try:
        meta = await run_in_threadpool(
            attachment_store(request).create,
            principal.tenant_id, attachment_id=attachment_id, name=_safe_name(file.filename), media_type=media_type,
            size=total, sha256=digest.hexdigest(), storage_key=key, created_by=principal.user_id,
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


@router.get("/attachments/{attachment_id}", responses={410: {"description": "the blob is gone"}})
async def get_attachment(attachment_id: str, request: Request, principal: PrincipalDep) -> Response:
    meta = await run_in_threadpool(attachment_store(request).get, principal.tenant_id, attachment_id)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    stream = blob_store(request).open(meta.storage_key)
    try:
        first = await anext(stream)  # a missing blob raises here, before any byte is sent
    except BlobNotFound:
        raise HTTPException(
            status.HTTP_410_GONE,
            detail={"code": "attachment_gone", "message": "The attachment's file is no longer available"},
        ) from None
    except StopAsyncIteration:
        first = b""
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


# --------------------------------------------------------------------------- message feedback


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)


def _turn_for_message(request: Request, tenant_id: str, conversation_id: str, message_id: str) -> str:
    """The native run id of the turn whose assistant message `message_id` is (404 otherwise)."""
    run_store, _ = request.app.state.run_stores
    if run_store.get_conversation(tenant_id, conversation_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    found = _MESSAGE_ID.match(message_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found")
    prefix = found.group("prefix")
    turns = run_store.list_turns(tenant_id, conversation_id)
    ids = [str(t["id"]) for t in turns]
    if prefix in ids:
        return prefix
    with Session(request.app.state.database_engine) as session:
        row = session.execute(
            select(AgUiRunModel.run_id).where(
                AgUiRunModel.tenant_id == tenant_id,
                AgUiRunModel.external_run_id == prefix,
                AgUiRunModel.run_id.in_(ids or [""]),
            )
        ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Message not found")
    return str(row[0])


@router.put("/conversations/{conversation_id}/messages/{message_id}/feedback")
async def put_feedback(
    conversation_id: str, message_id: str, body: FeedbackBody, request: Request, principal: PrincipalDep
) -> JSONResponse:
    def run() -> dict[str, Any]:
        run_id = _turn_for_message(request, principal.tenant_id, conversation_id, message_id)
        comment = (body.comment or "").strip() or None
        record = feedback_store(request).set(
            principal.tenant_id, run_id, message_id, rating=body.rating, comment=comment,
            created_by=principal.user_id,
        )
        return record.as_json()

    return JSONResponse(content=await run_in_threadpool(run))


@router.delete("/conversations/{conversation_id}/messages/{message_id}/feedback", status_code=204)
async def delete_feedback(
    conversation_id: str, message_id: str, request: Request, principal: PrincipalDep
) -> Response:
    def run() -> None:
        _turn_for_message(request, principal.tenant_id, conversation_id, message_id)
        # Idempotent: deleting feedback that does not exist writes nothing.
        feedback_store(request).delete(principal.tenant_id, message_id, created_by=principal.user_id)

    await run_in_threadpool(run)
    return Response(status_code=204)


def conversation_feedback(request: Request, tenant_id: str, user_id: str, conversation_id: str) -> dict[str, Any]:
    """{message_id: {rating, comment, updated_at}} of one user on a conversation (shared with the AG-UI
    thread view)."""
    run_store, _ = request.app.state.run_stores
    ids = [str(t["id"]) for t in run_store.list_turns(tenant_id, conversation_id)]
    rows = feedback_store(request).for_runs(tenant_id, ids, created_by=user_id)
    return {
        r.message_id: {"rating": r.rating, "comment": r.comment, "updated_at": r.updated_at.isoformat()}
        for r in rows
    }


@router.get("/conversations/{conversation_id}/feedback")
async def list_feedback(conversation_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    def run() -> dict[str, Any]:
        run_store, _ = request.app.state.run_stores
        if run_store.get_conversation(principal.tenant_id, conversation_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        viewer = feedback_viewer(principal)
        if viewer is None:
            return {"feedback": {}}
        return {"feedback": conversation_feedback(request, principal.tenant_id, viewer, conversation_id)}

    return JSONResponse(content=await run_in_threadpool(run))
