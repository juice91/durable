"""Provider of the Architect service (seeding, allowlist, drafter runs, page save)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.engine import Engine

from eap_api.deps import EngineDep, KnowledgeStoreDep
from eap_api.service_deps.connections import McpConnectionServiceDep
from eap_api.service_deps.runs import RunServiceDep
from eap_api.services.architect import ArchitectService
from eap_api.services.mcp_connection import McpConnectionService
from eap_run_store.knowledge import KnowledgeStore
from eap_api.ports import RunStarter


def build_architect_service(
    engine: Engine, runs: RunStarter, mcp: McpConnectionService | None = None, knowledge: KnowledgeStore | None = None
) -> ArchitectService:
    """An Architect service from plain collaborators (the MCP control tools call this with what they hold)."""
    return ArchitectService(engine, knowledge or KnowledgeStore(engine), runs, mcp)


def get_architect_service(
    engine: EngineDep, knowledge: KnowledgeStoreDep, runs: RunServiceDep, mcp: McpConnectionServiceDep
) -> ArchitectService:
    return build_architect_service(engine, runs, mcp, knowledge)


ArchitectServiceDep = Annotated[ArchitectService, Depends(get_architect_service)]
