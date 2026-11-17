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

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.attachments_shared import (
    attachment_response,
    attachment_store,
    created_by_for,
    read_upload,
    store_upload,
)
from eap_api.auth import Principal, current_principal, feedback_viewer
from eap_api.schemas import ErrorDetail
from eap_run_store import AgUiRunModel
from eap_run_store.chat_attachments import (
    FEEDBACK_COMMENT_MAX_CHARS,
    MessageFeedbackStore,
    max_attachment_bytes,
)

router = APIRouter(
    tags=["attachments"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]

_MESSAGE_ID = re.compile(r"^(?P<prefix>.+)-m(?P<n>\d+)$")


def feedback_store(request: Request) -> MessageFeedbackStore:
    store = getattr(request.app.state, "message_feedback_store", None)
    if store is None:
        store = request.app.state.message_feedback_store = MessageFeedbackStore(request.app.state.database_engine)
    return store


@router.post(
    "/attachments",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 415: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def upload_attachment(request: Request, principal: PrincipalDep, file: UploadFile) -> JSONResponse:
    uploaded = await read_upload(request, file, max_attachment_bytes())
    return await store_upload(request, principal.tenant_id, file, uploaded, created_by_for(principal))


@router.get("/attachments/{attachment_id}", responses={410: {"description": "the blob is gone"}})
async def get_attachment(attachment_id: str, request: Request, principal: PrincipalDep) -> Response:
    meta = await run_in_threadpool(attachment_store(request).get, principal.tenant_id, attachment_id)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return await attachment_response(request, meta)


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
