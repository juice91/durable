"""Provenance of the Architect's drafts and publish cards, read only (system-aware Architect, phase C).

- GET /definitions/{kind}/{id}/provenance   `kind` is `agent` or `workflow`, `id` a slug or UUID. Rows newest first
                                            (optionally one `spec_hash`) and, for the newest draft row,
                                            `capabilities_changed`: the capabilities the draft uses whose status
                                            changed since it was drafted. 404 for a definition of another tenant.

Written only by the Architect's own run through the MCP tool `provenance.record`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.system_context import ProvenanceServiceDep
from eap_api.services.capabilities import Viewer

router = APIRouter(prefix="/definitions", tags=["provenance"], responses={404: {"model": ErrorDetail}})
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("/{kind}/{definition_id}/provenance")
async def get_provenance(
    kind: str, definition_id: str, principal: PrincipalDep, service: ProvenanceServiceDep,
    spec_hash: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """`{definition_id, kind, items: [{id, spec_hash, version, run_id, card, platform_version, manifest_hash,
    capability_snapshot_hash, tool_calls, ...}], capabilities_changed: [{id, was, now}]}`."""
    return await service.get(
        Viewer(principal.tenant_id, "user", principal.user_id), kind=kind, definition_id=definition_id,
        spec_hash=spec_hash, limit=limit,
    )
