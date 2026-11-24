"""The system context the built-in Architect is given at run start, read only (system-aware Architect, phase C).

- GET /system-context   the capability, known-gap, tenant and operational layers for the caller: what the same
                        principal sees through the MCP tool `system.context` and what the Architect's
                        `eap.load_system_context` step records. Tenant layers are names and ids only; secrets
                        are names. `degraded` lists a layer that could not be built.

Routes are HTTP only; the composition lives in `eap_api.services.system_context`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from eap_api.auth import Principal, current_principal
from eap_api.service_deps.system_context import SystemContextServiceDep
from eap_api.services.capabilities import Viewer

router = APIRouter(prefix="/system-context", tags=["system-context"])
PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("")
async def get_system_context(principal: PrincipalDep, service: SystemContextServiceDep) -> dict[str, Any]:
    """`{registry_version, snapshot_hash, capabilities, known_gaps, tenant, policies, principal, degraded}`."""
    return await service.context(Viewer(principal.tenant_id, "user", principal.user_id))
