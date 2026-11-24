"""`workflow.history`: a workflow reads what its own earlier runs returned, as one recorded tool step."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from eap_schemas.tools import TOOL_DECLARATIONS, WorkflowHistoryInput
from worker_tests.conftest import APP_VERSION, wait_terminal
from worker_tests.workflow_run_helpers import (
    cleanup_definitions,
    enqueue_workflow_run,
    publish_workflow,
    wait_event,
)

SPEC = {
    "name": "Posting",
    "input_schema": {"type": "object"},
    "start": "rejected",
    "steps": [
        {"id": "rejected", "type": "tool", "tool_id": "workflow.history",
         "input": {"since_hours": 24, "where": {"status": "check_failed"}, "select": "url"}, "next": "done"},
        {"id": "done", "type": "tool", "tool_id": "workflow.return",
         "input": {"value": {"$ref": "steps.rejected.output"}}},
    ],
}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def earlier_run(worker, tenant, workflow_id, output, hours_ago):
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, workflow_id, 1, "{}", run_id)
    with worker.engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = 'completed', output = :o, created_at = :c WHERE id = :i"),
            {"o": json.dumps(output), "c": datetime.now(timezone.utc) - timedelta(hours=hours_ago), "i": str(run_id)},
        )


def test_a_run_gets_the_urls_its_earlier_runs_rejected_and_the_result_is_recorded(worker, client, tenant, defs):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, SPEC)
    earlier_run(worker, tenant, workflow_id, {"status": "check_failed", "reason": "verbatim", "url": "https://x/1"}, 3)
    earlier_run(worker, tenant, workflow_id, {"status": "check_failed", "reason": "too_long", "url": "https://x/2"}, 2)
    earlier_run(worker, tenant, workflow_id, {"status": "posted", "url": "https://x/3"}, 1)
    earlier_run(worker, tenant, workflow_id, {"status": "check_failed", "url": "https://x/old"}, 40)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == {"items": ["https://x/2", "https://x/1"], "count": 2}
    recorded = wait_event(worker.engine, run_id, "step:rejected:completed")["data"]["output"]
    assert recorded == {"items": ["https://x/2", "https://x/1"], "count": 2}
    # Replay safety: the step's result is the recorded one. A later run that also failed a check does not
    # change it (the history is read once, inside the recorded step), and the tool has no side effect.
    earlier_run(worker, tenant, workflow_id, {"status": "check_failed", "url": "https://x/late"}, 0)
    assert wait_event(worker.engine, run_id, "step:rejected:completed")["data"]["output"] == recorded
    declaration = TOOL_DECLARATIONS["workflow.history"]
    assert declaration.side_effect is False


def test_a_tool_run_outside_a_workflow_is_refused(worker):
    from eap_worker.workflow_history_tool import workflow_history

    import asyncio

    ctx = SimpleNamespace(deps=SimpleNamespace(tenant_id="t_none", run_id=uuid.uuid4()))
    out = asyncio.run(workflow_history(ctx))
    assert out["error"]["code"] == "not_in_workflow"


def test_input_bounds():
    ok = WorkflowHistoryInput.model_validate({})
    assert ok.since_hours == 24 and ok.limit == 100 and ok.statuses == ["completed"] and ok.where is None and ok.distinct is None
    for good in ({"since_hours": 1}, {"since_hours": 168}, {"limit": 1}, {"limit": 500},
                 {"statuses": ["completed", "skipped", "failed"]}, {"where": {"a": "x", "b": 1, "c": 1.5, "d": True}}):
        WorkflowHistoryInput.model_validate(good)
    for bad in ({"since_hours": 0}, {"since_hours": 169}, {"limit": 0}, {"limit": 501}, {"statuses": []},
                {"statuses": ["running"]}, {"where": {"a": [1]}}, {"where": {"a": None}}, {"select": ""}, {"extra": 1}):
        with pytest.raises(ValidationError):
            WorkflowHistoryInput.model_validate(bad)
