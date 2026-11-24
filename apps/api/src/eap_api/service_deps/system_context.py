"""Providers of the system context and provenance services (system-aware Architect, phase C)."""

from __future__ import annotations

from typing import Annotated

from eap_mcp_runtime import McpRuntime
from eap_run_store import platform_reads
from eap_run_store.provenance import ProvenanceStore
from eap_run_store.secrets import SecretStore
from eap_secrets.outbound import OutboundPolicy
from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.deps import EngineDep, SessionDep
from eap_api.service_deps.capabilities import build_capability_service
from eap_api.service_deps.connections import PolicyDep, RuntimeDep, build_connection_service
from eap_api.service_deps.iam import AuthzServiceDep
from eap_api.services.authz import AuthzService
from eap_api.services.capabilities import CapabilityRegistryService
from eap_api.services.provenance import ProvenanceService
from eap_api.services.system_context import SystemContextService, TenantReads


def tenant_reads(engine: Engine) -> TenantReads:
    """The bounded, tenant-scoped reads of the Tenant and Operational layers (names and ids only)."""
    secrets = SecretStore(engine, None)
    connections = build_connection_service(engine)
    return TenantReads(
        agents=lambda t: platform_reads.recent_definitions(engine, t, "agents"),
        workflows=lambda t: platform_reads.recent_definitions(engine, t, "workflows"),
        knowledge_bases=lambda t: platform_reads.list_knowledge_bases(engine, t),
        schedules=lambda t: platform_reads.list_schedules(engine, t, limit=50),
        connections=lambda t: connections.list(t),
        secret_names=lambda t: [r["name"] for r in secrets.names(t)],
        failures=lambda t: platform_reads.list_recent_runs(engine, t, limit=5, status="failed"),
    )


def build_provenance_store(engine: Engine) -> ProvenanceStore:
    return ProvenanceStore(engine)


def build_system_context_service(
    db: Session, engine: Engine, policy: OutboundPolicy, runtime: McpRuntime, authz: AuthzService | None = None,
    registry: CapabilityRegistryService | None = None,
) -> SystemContextService:
    return SystemContextService(
        registry or build_capability_service(db, engine, policy, runtime), tenant_reads(engine), authz,
        ProvenanceStore(engine).put_snapshot,
    )


def build_provenance_service(
    db: Session, engine: Engine, policy: OutboundPolicy, runtime: McpRuntime,
    registry: CapabilityRegistryService | None = None,
) -> ProvenanceService:
    return ProvenanceService(ProvenanceStore(engine), registry or build_capability_service(db, engine, policy, runtime))


def get_system_context_service(
    db: SessionDep, engine: EngineDep, policy: PolicyDep, runtime: RuntimeDep, authz: AuthzServiceDep
) -> SystemContextService:
    return build_system_context_service(db, engine, policy, runtime, authz)


def get_provenance_service(db: SessionDep, engine: EngineDep, policy: PolicyDep, runtime: RuntimeDep) -> ProvenanceService:
    return build_provenance_service(db, engine, policy, runtime)


SystemContextServiceDep = Annotated[SystemContextService, Depends(get_system_context_service)]
ProvenanceServiceDep = Annotated[ProvenanceService, Depends(get_provenance_service)]
