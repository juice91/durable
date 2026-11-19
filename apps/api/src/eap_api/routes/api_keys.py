"""Tenant API keys for third-party AG-UI frontends (AG-UI milestone, AGUI.3; D16).

- POST   /api-keys            create a key: 201 with the plaintext `key` (`pos_<id>_<secret>`), the
                              only time it is ever returned (`Cache-Control: no-store`);
- GET    /api-keys            the tenant's keys, revoked and expired ones included, newest first,
                              never a secret or hash;
- DELETE /api-keys/{key_id}   revoke (204, idempotent); another tenant's or an unknown key is 404.

These management routes take the dev bearer only (`current_principal`): an API key gets 401. A key
is accepted only by `eap_api.auth.agui_principal`, on `/api/v1/ag-ui/*`. Nothing here logs a key.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.auth import Principal, current_principal, get_api_key_store
from eap_api.database import get_session
from eap_api.models import AgentModel
from eap_api.request_guard import guarded_json_route
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel
from eap_run_store import ApiKeyLimitExceeded, ApiKeyRecord, ApiKeyStore
from eap_run_store.api_keys import (
    API_KEY_ALLOWLIST_ID_MAX_CHARS,
    API_KEY_ALLOWLIST_MAX_ITEMS,
    API_KEY_NAME_MAX_CHARS,
    API_KEY_ORIGIN_MAX_CHARS,
    API_KEY_ORIGINS_MAX_ITEMS,
)

MAX_API_KEY_BODY_BYTES = 64 * 1024
MAX_API_KEY_BODY_DEPTH = 8
API_KEY_NOT_FOUND_DETAIL = "API key not found"

ApiKeyRoute = guarded_json_route(max_body_bytes=MAX_API_KEY_BODY_BYTES, max_depth=MAX_API_KEY_BODY_DEPTH)

router = APIRouter(
    tags=["api-keys"], responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}}, route_class=ApiKeyRoute
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]
ApiKeyStoreDep = Annotated[ApiKeyStore, Depends(get_api_key_store)]

IdItem = Annotated[str, Field(min_length=1, max_length=API_KEY_ALLOWLIST_ID_MAX_CHARS)]
OriginItem = Annotated[str, Field(min_length=1, max_length=API_KEY_ORIGIN_MAX_CHARS)]


class ApiKeyCreate(BaseModel):
    """Body of POST /api-keys.

    Leaving out both `agent_ids` and `workflow_ids` makes a tenant-wide key (every agent and
    workflow). Giving only one restricts the key on both: the other is stored as [] (nothing), so
    an agent-only key runs no workflow. A null (or absent) `allowed_origins` means any origin."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=API_KEY_NAME_MAX_CHARS)
    scopes: list[Literal["agui:run", "mcp", "manage", "otlp"]] = Field(default_factory=lambda: ["agui:run"], min_length=1, max_length=8)
    agent_ids: list[IdItem] | None = Field(default=None, max_length=API_KEY_ALLOWLIST_MAX_ITEMS)
    workflow_ids: list[IdItem] | None = Field(default=None, max_length=API_KEY_ALLOWLIST_MAX_ITEMS)
    allowed_origins: list[OriginItem] | None = Field(default=None, max_length=API_KEY_ORIGINS_MAX_ITEMS)
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    """A key as listed: the non-secret `prefix` (`pos_<id>_`) identifies it; no secret, no hash."""

    id: str
    name: str
    prefix: str
    status: Literal["active", "revoked", "expired"]
    scopes: list[str]
    agent_ids: list[str] | None
    workflow_ids: list[str] | None
    allowed_origins: list[str] | None
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    """POST /api-keys: the key plus its plaintext, `key`, shown this once."""

    key: str


class ApiKeyList(BaseModel):
    items: list[ApiKeyOut]


def _out(record: ApiKeyRecord) -> dict:
    return dict(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        status=record.status(),
        scopes=list(record.scopes),
        agent_ids=None if record.agent_ids is None else list(record.agent_ids),
        workflow_ids=None if record.workflow_ids is None else list(record.workflow_ids),
        allowed_origins=None if record.allowed_origins is None else list(record.allowed_origins),
        created_by=record.created_by,
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        last_used_at=record.last_used_at,
    )


def _unknown_ids(session: Session, model, tenant_id: str, ids: list[str] | None) -> list[str]:
    if not ids:
        return []
    found = set(
        session.execute(select(model.id).where(model.tenant_id == tenant_id, model.id.in_(ids))).scalars()
    )
    return [i for i in dict.fromkeys(ids) if i not in found]


@router.post(
    "/api-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreated,
    responses={409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def create_api_key(
    body: ApiKeyCreate, principal: PrincipalDep, session: SessionDep, store: ApiKeyStoreDep
) -> JSONResponse:
    """Create a key. 422 for an unknown agent or workflow id of this tenant, a bad origin or a past
    `expires_at`; 409 past the tenant's active-key cap. The response is the only place the
    plaintext key ever appears."""
    unknown_agents = await run_in_threadpool(_unknown_ids, session, AgentModel, principal.tenant_id, body.agent_ids)
    if unknown_agents:
        raise HTTPException(status_code=422, detail=f"unknown agent ids: {', '.join(unknown_agents)}")
    unknown_workflows = await run_in_threadpool(
        _unknown_ids, session, WorkflowModel, principal.tenant_id, body.workflow_ids
    )
    if unknown_workflows:
        raise HTTPException(status_code=422, detail=f"unknown workflow ids: {', '.join(unknown_workflows)}")
    try:
        created = await run_in_threadpool(
            lambda: store.create(
                principal.tenant_id,
                name=body.name,
                created_by=principal.user_id,
                scopes=body.scopes,
                agent_ids=body.agent_ids,
                workflow_ids=body.workflow_ids,
                allowed_origins=body.allowed_origins,
                expires_at=body.expires_at,
            )
        )
    except ApiKeyLimitExceeded as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    payload = ApiKeyCreated(**_out(created.record), key=created.key)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=payload.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api-keys", response_model=ApiKeyList)
async def list_api_keys(principal: PrincipalDep, store: ApiKeyStoreDep) -> ApiKeyList:
    records = await run_in_threadpool(store.list, principal.tenant_id)
    return ApiKeyList(items=[ApiKeyOut(**_out(r)) for r in records])


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_api_key(
    principal: PrincipalDep,
    store: ApiKeyStoreDep,
    key_id: Annotated[str, Path(max_length=64)],
) -> Response:
    """Revoke a key (idempotent). The key stops working at once (every request re-reads it)."""
    record = await run_in_threadpool(store.revoke, principal.tenant_id, key_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=API_KEY_NOT_FOUND_DETAIL)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
