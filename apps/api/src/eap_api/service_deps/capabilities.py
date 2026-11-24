"""Provider of the capability registry service (the registry, its probes and the control-tool catalog)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from eap_mcp_runtime import McpRuntime
from eap_run_store.workflow_checks import allowed_tool_ids
from eap_schemas.models_policy import ModelPolicy
from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api import control_catalog, model_discovery
from eap_api.deps import EngineDep, SessionDep
from eap_api.service_deps.connections import (
    PolicyDep,
    RuntimeDep,
    build_mcp_connection_service,
    build_mcp_server_service,
)
from eap_api.services.capabilities import CapabilityRegistryService
from eap_api.services.capability_probes import ProbeRegistry, mcp_probe, provider_probe, tool_allowlist_probe
from eap_api.services.model_catalog import model_items
from eap_secrets.outbound import OutboundPolicy


def _allowed_providers() -> frozenset[str]:
    return frozenset(entry.partition(":")[0] for entry in ModelPolicy.from_env().entries)


def build_capability_service(
    db: Session, engine: Engine, policy: OutboundPolicy, runtime: McpRuntime, probes: ProbeRegistry | None = None,
) -> CapabilityRegistryService:
    """The registry with the platform's probes: tool allowlist, model providers, the tenant's MCP servers.
    A `probes` registry replaces them (tests)."""
    if probes is None:
        probes = ProbeRegistry()

        def servers(tenant_id: str) -> list[dict[str, Any]]:
            return build_mcp_server_service(db, engine, policy, runtime).list(tenant_id)

        async def tools(tenant_id: str, server_id: str) -> dict[str, Any]:
            return await build_mcp_connection_service(engine, policy, runtime).test(tenant_id, uuid.UUID(server_id))

        probes.register("tools", tool_allowlist_probe(allowed_tool_ids))
        probes.register("providers", provider_probe(
            model_discovery.all_provider_statuses, lambda: model_items(ModelPolicy.from_env()), _allowed_providers))
        probes.register("mcp", mcp_probe(servers, tools, probes.cache))
    return CapabilityRegistryService(probes, control_catalog.declared)


def get_capability_service(db: SessionDep, engine: EngineDep, policy: PolicyDep, runtime: RuntimeDep) -> CapabilityRegistryService:
    return build_capability_service(db, engine, policy, runtime)


CapabilityServiceDep = Annotated[CapabilityRegistryService, Depends(get_capability_service)]
