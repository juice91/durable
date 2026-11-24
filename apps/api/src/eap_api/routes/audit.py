"""`GET /audit`: the tenant's audit log (design 9). Admin only (the policy engine decides `tenant.read_audit`); people
only, never an API key; `actor=me` lists the caller's own rows and needs no admin. Filters: actor (the principal or the person it acted for), action, resource kind and id,
outcome, a time window. Cursor pagination, newest first."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from eap_api.auth import CorePrincipalDep
from eap_api.service_deps.audit import AuditServiceDep
from eap_api.services.audit import DEFAULT_RETENTION_DAYS, MAX_LIMIT

router = APIRouter(prefix="/audit", tags=["audit"])


class AuditRow(BaseModel):
    id: str
    at: str
    actor_kind: str
    actor_subject: str
    on_behalf_of: str | None
    action: str
    resource_kind: str | None
    resource_id: str | None
    outcome: Literal["success", "denied", "failed"]
    request_id: str | None
    run_id: str | None
    detail: dict | None


class AuditPage(BaseModel):
    items: list[AuditRow]
    next_cursor: str | None
    retention_days: int | None = DEFAULT_RETENTION_DAYS  # null: kept without limit


@router.get("", response_model=AuditPage)
async def list_audit(
    principal: CorePrincipalDep,
    service: AuditServiceDep,
    actor: Annotated[str | None, Query(max_length=254)] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    resource_kind: Annotated[str | None, Query(max_length=60)] = None,
    resource_id: Annotated[str | None, Query(max_length=200)] = None,
    outcome: Literal["success", "denied", "failed"] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
) -> dict:
    page = await service.list(
        principal, actor=actor, action=action, resource_kind=resource_kind, resource_id=resource_id,
        outcome=outcome, since=since, until=until, limit=limit, cursor=cursor,
    )
    return {**page, "retention_days": service.retention_days}
