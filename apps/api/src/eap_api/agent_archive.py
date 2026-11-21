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

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.service_errors import to_http_exception
from eap_api.services.agent_archive import AGENT_ARCHIVED, archived_step_problems  # noqa: F401 - re-exported
from eap_api.services.agent_archive import refuse_if_archived as _refuse_if_archived
from eap_api.services.errors import ServiceError
from eap_run_store.definitions import referencing_workflows as _referencing_workflows

def refuse_if_archived(agent: object) -> None:
    """409 AGENT_ARCHIVED when `agent` (an AgentModel) is archived (the rule: eap_api.services.agent_archive)."""
    try:
        _refuse_if_archived(agent)
    except ServiceError as exc:
        raise to_http_exception(exc) from None


def referencing_workflows(db: Session, tenant_id: str, agent_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """The tenant's non-archived workflows with a published version that runs `agent_id` as an
    agent step (see eap_run_store.definitions.referencing_workflows, which the archive and the
    Architect's archive tool share)."""
    return _referencing_workflows(db.connection(), tenant_id, agent_id)


__all__ = ["AGENT_ARCHIVED", "archived_step_problems", "referencing_workflows", "refuse_if_archived"]
