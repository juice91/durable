"""Chat attachments and message feedback (W2.4).

- POST /attachments                  multipart upload (field `file`) -> 201 {id, name, media_type, size}.
                                     413 over EAP_CHAT_ATTACHMENT_MAX_BYTES (default 10 MiB), 415 for a
                                     media type outside the allowlist (images, PDF, plain text, markdown,
                                     CSV, JSON) or bytes that are not what the type says.
- GET  /attachments/{id}             the bytes, tenant-scoped (another tenant's id is 404), with the stored
                                     content type, `Content-Disposition` and `X-Content-Type-Options: nosniff`.
                                     410 `attachment_gone` when the row exists but its blob does not;
                                     503 when the object store itself is unreachable.
- DELETE /attachments/{id}           discard the caller's own unsent upload: 204; 409 `attachment_in_use`
                                     once a message references it; 404 unknown, another tenant's or another
                                     user's. Unsent uploads also expire (EAP_ATTACHMENT_ORPHAN_HOURS, 24 h).

Delete order: DELETE reads the row `FOR UPDATE` and checks it is not in use first, then deletes the
blob, then the row; the retention pass deletes the row first (conditional on `message_id IS NULL`),
then the blob. A crash can leave a row without a blob (410) or a blob without a row, never a message
pointing at nothing.

The bytes live in the BlobStore (the app's `blob_store`, built from EAP_BLOB_* at startup), never in
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

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from eap_api.attachments_shared import (
    attachment_response,
    created_by_for,
    delete_unsent_attachment,
    read_upload,
    store_upload,
)
from eap_api.auth import Principal, current_principal, feedback_viewer
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.knowledge import AttachmentServiceDep
from eap_api.services.attachments import FEEDBACK_COMMENT_MAX_CHARS, max_attachment_bytes

router = APIRouter(
    tags=["attachments"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.post(
    "/attachments",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 415: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def upload_attachment(
    request: Request, principal: PrincipalDep, file: UploadFile, service: AttachmentServiceDep
) -> JSONResponse:
    await run_in_threadpool(service.require_write)
    uploaded = await read_upload(request, file, max_attachment_bytes())
    return await store_upload(request, principal.tenant_id, file, uploaded, created_by_for(principal))


@router.get("/attachments/{attachment_id}", responses={410: {"description": "the blob is gone"}})
async def get_attachment(
    attachment_id: str, request: Request, principal: PrincipalDep, service: AttachmentServiceDep
) -> Response:
    meta = await run_in_threadpool(service.get_attachment, principal.tenant_id, attachment_id)
    return await attachment_response(request, meta)


@router.delete(
    "/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail}},
)
async def delete_attachment(
    attachment_id: str, request: Request, principal: PrincipalDep, service: AttachmentServiceDep
) -> Response:
    """Discard the caller's own upload before it is sent: 204; 409 `attachment_in_use` once a message
    references it; 404 when unknown, another tenant's or another user's."""
    await run_in_threadpool(service.require_write)
    return await delete_unsent_attachment(request, principal.tenant_id, attachment_id, created_by_for(principal))


# --------------------------------------------------------------------------- message feedback


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=FEEDBACK_COMMENT_MAX_CHARS)


@router.put("/conversations/{conversation_id}/messages/{message_id}/feedback")
async def put_feedback(
    conversation_id: str, message_id: str, body: FeedbackBody, principal: PrincipalDep, service: AttachmentServiceDep
) -> JSONResponse:
    record = await run_in_threadpool(
        lambda: service.set_feedback(
            principal.tenant_id, principal.user_id, conversation_id, message_id,
            rating=body.rating, comment=body.comment,
        )
    )
    return JSONResponse(content=record)


@router.delete("/conversations/{conversation_id}/messages/{message_id}/feedback", status_code=204)
async def delete_feedback(
    conversation_id: str, message_id: str, principal: PrincipalDep, service: AttachmentServiceDep
) -> Response:
    await run_in_threadpool(service.clear_feedback, principal.tenant_id, principal.user_id, conversation_id, message_id)
    return Response(status_code=204)


@router.get("/conversations/{conversation_id}/feedback")
async def list_feedback(conversation_id: str, principal: PrincipalDep, service: AttachmentServiceDep) -> JSONResponse:
    content: dict[str, Any] = await run_in_threadpool(
        service.list_feedback, principal.tenant_id, conversation_id, feedback_viewer(principal)
    )
    return JSONResponse(content=content)
