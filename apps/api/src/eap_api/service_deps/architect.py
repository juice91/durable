"""Provider of the Architect service (seeding, allowlist, drafter runs, page save)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy.engine import Engine

from eap_api.deps import EngineDep, KnowledgeStoreDep, RunLauncherDep, SessionDep, StoresDep
from eap_api.service_deps.connections import PolicyDep, RuntimeDep, build_mcp_connection_service
from eap_api.service_deps.iam import AccessDep
from eap_api.services.access import Access
from eap_api.service_deps.runs import build_run_service
from eap_api.services.architect import ArchitectService
from eap_api.services.mcp_connection import McpConnectionService
from eap_run_store.knowledge import KnowledgeStore
from eap_api.ports import RunStarter


def build_architect_service(
    engine: Engine, runs: RunStarter, mcp: McpConnectionService | None = None, knowledge: KnowledgeStore | None = None,
    access: Access | None = None,
) -> ArchitectService:
    """An Architect service from plain collaborators (the MCP control tools call this with what they hold)."""
    return ArchitectService(engine, knowledge or KnowledgeStore(engine), runs, mcp, access)


def get_architect_service(
    engine: EngineDep, knowledge: KnowledgeStoreDep, db: SessionDep, stores: StoresDep, launcher: RunLauncherDep,
    policy: PolicyDep, runtime: RuntimeDep, access: AccessDep,
) -> ArchitectService:
    # The drafter runs are the platform's own (the Architect starts its helper agents on the user's behalf), so this
    # RunService carries no Access: the user's right to use the Architect is checked where the user starts it.
    # The live tool discovery of the allowlist is the platform's own (like the drafter runs): its MCP connection service
    # carries no Access, so an editor's options list is not narrowed by a right (admin) to test a server.
    mcp = build_mcp_connection_service(engine, policy, runtime, *stores)
    return build_architect_service(engine, build_run_service(db, stores[0], stores[1], launcher), mcp, knowledge, access)


ArchitectServiceDep = Annotated[ArchitectService, Depends(get_architect_service)]


class ArchitectSuggesterPorts:
    """The suggester agent and one run of it, through the Architect service's seeding and drafter run."""

    def __init__(self, architect: ArchitectService, tenant_id: str) -> None:
        self.architect, self.tenant_id = architect, tenant_id

    def ensure_suggester(self, tenant_id: str, user_id: str) -> tuple[str, str | None]:
        return self.architect.ensure_suggester(tenant_id, user_id)

    def run_drafter(self, suggester_id: str, prompt: str) -> tuple[dict[str, Any], list[str], str, str, str | None]:
        return self.architect.run_drafter(self.tenant_id, suggester_id, prompt, "scorer suggestions")
