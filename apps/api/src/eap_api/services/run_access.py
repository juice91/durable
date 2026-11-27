"""Per-resource access to a run (IAM P2b): a run is as readable as the agent or workflow it belongs to.

The model's `run` type inherits `can_read`, `can_approve` and `can_cancel` from its parent, so no tuple is written per
run; checking the parent directly is the same decision. Shared by the run, trace, log and score services."""

from __future__ import annotations

import uuid
from typing import Any

from eap_api.services.access import Access


def authorize_run(access: Access | None, run_store: Any, run: dict[str, Any], action: str = "read") -> None:
    """Return when the caller may `action` the run, else NotFound / Forbidden (Access.require).

    A workflow run answers for its workflow, an agent (or eval) run for its agent, a child run for the top-level run it
    belongs to; a knowledge-base run for its knowledge base. Any other run (a direct tool run) is tenant data: `read`
    on the tenant."""
    if access is None:
        return
    for _ in range(3):  # runs nest two levels; the bound keeps a corrupt chain from looping
        if run.get("kind") == "workflow" and run.get("workflow_id"):
            return access.require(action, "workflow", str(run["workflow_id"]))
        parent = run.get("parent_run_id")
        if parent is None:
            break
        found = run_store.get_run(access.tenant_id, uuid.UUID(str(parent)))
        if found is None:
            break
        run = found
    if run.get("workflow_id") and not run.get("agent_id"):
        return access.require(action, "workflow", str(run["workflow_id"]))
    if run.get("agent_id") is not None:
        return access.require(action, "agent", str(run["agent_id"]))
    if run.get("knowledge_base_id") is not None:
        return access.require("read" if action == "read" else "edit", "knowledge_base", str(run["knowledge_base_id"]))
    access.require("read", "tenant", None)
