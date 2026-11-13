"""Start and drive workflow runs the way the API does, without the API (worker tests).

- `publish_agent(engine, tenant, spec)` / `publish_workflow(engine, tenant, spec)`: insert
  published versions directly (what POST /agents/{id}/versions and /workflows/{id}/versions write).
- `enqueue_workflow_run(...)`: insert the kind='workflow' runs row, then enqueue
  `eap.workflow_run(run_id, tenant_id, workflow_version_id, input_json)` (DBOSRunLauncher.launch_workflow).
- `decide(...)`: what POST .../approve|reject does: the atomic decision event, then DBOS send.
- `cleanup_definitions(engine, tenant)`: delete the tenant's agents and workflows (the `tenant`
  fixture of conftest.py already deletes runs, events and side effects).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from worker_tests.conftest import wait_for


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def agent_spec(model: str = "echo", tools: tuple[str, ...] = (), name: str = "Helper") -> dict[str, Any]:
    """An agent spec; `test:echo` answers "Echo: <prompt>", `test:ticket` opens a ticket."""
    return {
        "spec_version": 1,
        "name": name,
        "instructions": "Help.",
        "model": {"provider": "test", "name": model},
        "tools": list(tools),
        "model_settings": {},
    }


def publish_agent(engine, tenant_id: str, spec: dict[str, Any], *, agent_id: str | None = None, version: int = 1) -> str:
    agent_id = agent_id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, created_at, updated_at)"
                " VALUES (:id, :t, :n, CAST(:s AS JSONB), false, :now, :now) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": agent_id, "t": tenant_id, "n": spec["name"], "s": json.dumps(spec), "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by)"
                " VALUES (:id, :a, :t, :v, CAST(:s AS JSONB), :h, :now, 'tester')"
            ),
            {"id": str(uuid.uuid4()), "a": agent_id, "t": tenant_id, "v": version, "s": json.dumps(spec),
             "h": hashlib.sha256(canonical(spec).encode()).hexdigest(), "now": now},
        )
    return agent_id


def publish_workflow(
    engine, tenant_id: str, spec: dict[str, Any], *, workflow_id: str | None = None, version: int = 1
) -> tuple[str, str]:
    """Validate `spec` (WorkflowSpec) and insert it as a published version; (workflow_id, version_id)."""
    from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash

    model = WorkflowSpec.model_validate(spec)
    workflow_id = workflow_id or str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    dumped = json.dumps(model.model_dump(mode="json"))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workflows (id, tenant_id, name, draft_spec, archived, created_at, updated_at)"
                " VALUES (:id, :t, :n, CAST(:s AS JSONB), false, :now, :now) ON CONFLICT (id) DO NOTHING"
            ),
            {"id": workflow_id, "t": tenant_id, "n": model.name, "s": dumped, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO workflow_versions (id, workflow_id, tenant_id, version, spec, spec_hash, created_at, created_by)"
                " VALUES (:id, :w, :t, :v, CAST(:s AS JSONB), :h, :now, 'tester')"
            ),
            {"id": version_id, "w": workflow_id, "t": tenant_id, "v": version, "s": dumped,
             "h": canonical_workflow_hash(model), "now": now},
        )
    return workflow_id, version_id


def enqueue_workflow_run(
    client,
    run_store,
    *,
    tenant_id: str,
    workflow_id: str,
    version_id: str,
    input_value: dict[str, Any],
    app_version: str,
    version: int = 1,
    run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run_id = run_id or uuid.uuid4()
    input_json = canonical(input_value)
    run_store.create_workflow_run(tenant_id, workflow_id, version, input_json, run_id)
    client.enqueue(
        {
            "queue_name": "eap-agent-runs",
            "workflow_name": "eap.workflow_run",
            "workflow_id": str(run_id),
            "app_version": app_version,
        },
        str(run_id),
        tenant_id,
        version_id,
        input_json,
    )
    return run_id


def wait_event(engine, run_id, event_key: str, timeout: float = 60.0) -> dict[str, Any]:
    def found():
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT type, data FROM execution_events WHERE run_id = :r AND event_key = :k"),
                {"r": str(run_id), "k": event_key},
            ).fetchone()
        return {"type": row.type, "data": row.data} if row else None

    return wait_for(found, timeout=timeout, what=f"event {event_key} of run {run_id}")


def decide(event_log, client, *, tenant_id: str, run_id, step_id: str, approve: bool = True,
           user: str = "alice", comment: str | None = None) -> str:
    """Approve/reject like the API: returns the append outcome ("created" | "exists" | "not_waiting")
    and sends the wake-up only when this call recorded the decision."""
    event_type = "step_approved" if approve else "step_rejected"
    outcome, _ = event_log.append_step_decision(
        tenant_id, run_id, step_id, event_type, {"step_id": step_id, "decided_by": user, "comment": comment}
    )
    if outcome == "created":
        waiting = event_log.get_event(tenant_id, run_id, f"step:{step_id}:waiting")
        client.send(waiting.data["workflow_id"], {"decision": "approved" if approve else "rejected"},
                    f"approval:{step_id}")
    return outcome


def cleanup_definitions(engine, tenant_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM agent_versions WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM agents WHERE tenant_id = :t"), {"t": tenant_id})


def demo_spec(agent_id: str, *, tool_id: str = "clock.now", timeout_hours: int = 1) -> dict[str, Any]:
    """The milestone-3 acceptance shape: agent -> tool -> branch -> approval -> agent (end)."""
    return {
        "name": "Triage",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}, "next": "now"},
            {"id": "now", "type": "tool", "tool_id": tool_id, "input": {}, "next": "route"},
            {
                "id": "route",
                "type": "branch",
                "cases": [{"when": {"op": "contains", "left": {"$ref": "steps.ask.output"}, "right": "urgent"},
                           "next": "approve"}],
                "default": "done",
            },
            {"id": "approve", "type": "approval", "prompt": "Escalate?", "timeout_hours": timeout_hours, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "steps.ask.output"}}},
        ],
    }
