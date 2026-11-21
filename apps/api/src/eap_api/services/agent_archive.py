"""The archived-agent rule, independent of the transport (see eap_api.agent_archive for the policy)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.services.errors import Conflict

AGENT_ARCHIVED = "Agent is archived; unarchive it to start runs, conversations or publish versions"


def refuse_if_archived(agent: object) -> None:
    """Conflict AGENT_ARCHIVED when `agent` (an AgentModel) is archived."""
    if getattr(agent, "archived", False):
        raise Conflict(AGENT_ARCHIVED)


def archived_step_problems(db: Session, tenant_id: str, steps: Any) -> list[str]:
    """One message per agent step (of a workflow spec's `steps`) whose agent is the tenant's and
    archived: POST /workflows/{id}/runs refuses such a run with 409 before anything runs."""
    ids = {str(s.agent_id): s.id for s in steps if getattr(s, "type", None) == "agent"}
    if not ids:
        return []
    rows = db.execute(
        text("SELECT id, name FROM agents WHERE tenant_id = :t AND archived IS TRUE AND id = ANY(:ids)"),
        {"t": tenant_id, "ids": list(ids)},
    ).fetchall()
    archived = {str(r.id): r.name for r in rows}
    return [
        f"step '{s.id}': agent '{archived[str(s.agent_id)]}' is archived; unarchive it to run this workflow"
        for s in steps
        if getattr(s, "type", None) == "agent" and str(s.agent_id) in archived
    ]
