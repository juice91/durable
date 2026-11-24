"""The capability registry, read only (system-aware Architect, phase A).

- GET /capabilities       what the platform can do now: entries (compact unless `include_schemas=true`) and the
                          known gaps. Filters: `kind`, `status`, `q`. `ETag` is the registry snapshot, so a client
                          polls cheaply with `If-None-Match` (304 while nothing changed; adding or removing an MCP
                          server, a provider going down or a deploy changes it).
- GET /capabilities/{id}  one entry with its schemas and the gaps that concern it; 404 `unknown_capability` with the
                          nearest ids.

Tenant-scoped: the tenant's MCP servers and tools appear only in its own listing. Routes are HTTP only; the
registry lives in `eap_api.services.capabilities`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.capabilities import CapabilityServiceDep
from eap_api.services.capabilities import Viewer, etag_of

router = APIRouter(prefix="/capabilities", tags=["capabilities"], responses={404: {"model": ErrorDetail}})
PrincipalDep = Annotated[Principal, Depends(current_principal)]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]


def _matches(header: str | None, etag: str) -> bool:
    """True when `If-None-Match` names the current ETag (or `*`); weak and strong tags compare alike."""
    if not header:
        return False
    return any(tag.strip() in ("*", etag) or tag.strip().removeprefix("W/") == etag for tag in header.split(","))


def _answer(body: dict[str, Any], snapshot_hash: str, if_none_match: str | None) -> Response:
    tag = etag_of(snapshot_hash)
    if _matches(if_none_match, tag):
        return Response(status_code=304, headers={"ETag": tag})
    return JSONResponse(content=body, headers={"ETag": tag})


@router.get("", responses={304: {"description": "unchanged since the ETag in If-None-Match"}})
async def list_capabilities(
    principal: PrincipalDep, service: CapabilityServiceDep, if_none_match: IfNoneMatch = None,
    kind: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(max_length=40)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    include_schemas: bool = False,
) -> Response:
    """`{registry_version, snapshot_hash, total, matched, entries, known_gaps, degraded}`; each entry is
    `{id, kind, title, description, status, status_reason, requires_approval, side_effect, limits, scope,
    writable_by_architect, docs_ref, designed_in, needs, permission}` (+ `input_schema` / `result_schema` with
    `include_schemas`)."""
    body = await service.list(
        Viewer(principal.tenant_id, "user", principal.user_id),
        kind=kind, status=status, query=q, include_schemas=include_schemas,
    )
    return _answer(body, body["snapshot_hash"], if_none_match)


@router.get("/{capability_id:path}", responses={304: {"description": "unchanged since the ETag in If-None-Match"}})
async def get_capability(
    capability_id: str, principal: PrincipalDep, service: CapabilityServiceDep, if_none_match: IfNoneMatch = None,
) -> Response:
    """`{entry, gaps, snapshot_hash, registry_version}` for one id such as `tool.http.request` or `step.foreach`."""
    body = await service.get(Viewer(principal.tenant_id, "user", principal.user_id), capability_id)
    return _answer(body, body["snapshot_hash"], if_none_match)
