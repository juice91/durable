"""Archived agents (agents.archived; POST /agents/{id}/archive and /unarchive).

Archiving hides an agent from GET /agents (listed with `?archived=true`) and stops new work on it,
while everything it already produced stays readable:

- refused with 409 AGENT_ARCHIVED: a new run (published or draft) via POST /agents/{id}/runs, a new
  conversation via POST /agents/{id}/conversations, a new turn via POST /conversations/{id}/runs,
  a publish (POST /agents/{id}/versions) and a restore (which publishes a version);
- still allowed: GET of the agent, its versions, runs, conversations, traces, logs and memory; a
  retry of a run, turn or conversation that was recorded before the agent was archived (the
  idempotent replay of an existing row, so a crashed hand-off still heals); editing the draft
  (PATCH), which starts nothing;
- workflows: archiving an agent that a published version of a (non-archived) workflow runs as an
  agent step answers 409 with those workflows and versions (`referencing_workflows`) unless
  `?force=true`. A run of such a version is then refused up front by POST /workflows/{id}/runs
  (409 naming the step), a workflow cannot be published with an archived agent step
  (routes/workflows.py), and a run already in flight fails its agent step with `agent_archived`
  (eap_worker.workflow_run) instead of reporting the agent as missing.

Unarchive makes the agent usable again. Both calls are idempotent and tenant-scoped (another
tenant's agent is 404 like an unknown one).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

AGENT_ARCHIVED = "Agent is archived; unarchive it to start runs, conversations or publish versions"


def refuse_if_archived(agent: object) -> None:
    """409 AGENT_ARCHIVED when `agent` (an AgentModel) is archived."""
    if getattr(agent, "archived", False):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=AGENT_ARCHIVED)


def referencing_workflows(db: Session, tenant_id: str, agent_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """The tenant's non-archived workflows with a published version that runs `agent_id` as an
    agent step, one entry per workflow: `{workflow_id, workflow_name, versions (ascending),
    latest_version}` (`latest_version` is the workflow's newest version, which a run without
    `version` uses). Steps are a flat list (parallel branches name step ids), so a JSONB
    containment test on `spec->'steps'` finds every agent step. One query."""
    needle = json.dumps([{"type": "agent", "agent_id": str(agent_id)}])
    rows = db.execute(
        text(
            "SELECT w.id, w.name, array_agg(v.version ORDER BY v.version) AS versions,"
            " (SELECT max(lv.version) FROM workflow_versions lv"
            "   WHERE lv.workflow_id = w.id AND lv.tenant_id = w.tenant_id) AS latest"
            " FROM workflows w JOIN workflow_versions v ON v.workflow_id = w.id AND v.tenant_id = w.tenant_id"
            " WHERE w.tenant_id = :t AND w.archived IS NOT TRUE"
            "   AND v.spec -> 'steps' @> CAST(:needle AS jsonb)"
            " GROUP BY w.id, w.name, w.tenant_id ORDER BY w.name, w.id"
        ),
        {"t": tenant_id, "needle": needle},
    ).fetchall()
    return [
        {"workflow_id": str(r.id), "workflow_name": r.name, "versions": list(r.versions), "latest_version": r.latest}
        for r in rows
    ]


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


__all__ = ["AGENT_ARCHIVED", "archived_step_problems", "referencing_workflows", "refuse_if_archived"]
