"""`workflow_history` (eap_run_store.workflow_history): what a workflow's own recent runs returned."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store.workflow_history import NotAWorkflowRun, workflow_history


@pytest.fixture
def tenants(engine):
    made = [f"test_tenant_{uuid4().hex[:8]}" for _ in range(2)]
    yield made
    with engine.begin() as conn:
        for t in made:
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def add(engine, run_store, tenant, wf, output, *, status="completed", hours_ago=0.0, version=1):
    """A finished workflow run of `wf` with `output` (JSON-encoded unless it is already text)."""
    run_id = uuid4()
    run_store.create_workflow_run(tenant, wf, version, "{}", run_id)
    raw = output if isinstance(output, str) else json.dumps(output)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = :s, output = :o, created_at = :c WHERE id = :i"),
            {"s": status, "o": raw, "c": datetime.now(timezone.utc) - timedelta(hours=hours_ago), "i": str(run_id)},
        )
    return str(run_id)


def rejected(url, reason="verbatim"):
    return {"status": "check_failed", "reason": reason, "url": url}


def test_only_the_same_workflow_and_tenant_and_never_the_caller(engine, run_store, tenants):
    a, b = tenants
    wf, other = uuid4(), uuid4()
    add(engine, run_store, a, wf, rejected("u1"), hours_ago=3, version=1)
    add(engine, run_store, a, wf, rejected("u2"), hours_ago=2, version=2)  # another version of the same workflow
    add(engine, run_store, a, other, rejected("other-workflow"), hours_ago=1)
    add(engine, run_store, b, wf, rejected("other-tenant"), hours_ago=1)
    caller = add(engine, run_store, a, wf, rejected("caller"), status="running", hours_ago=0)
    out = workflow_history(engine, a, caller, select="url")
    assert out == {"items": ["u2", "u1"], "count": 2}  # newest first
    done = add(engine, run_store, a, wf, rejected("caller-done"), hours_ago=0)
    assert workflow_history(engine, a, caller, select="url")["items"] == ["caller-done", "u2", "u1"]  # another run is read
    assert workflow_history(engine, a, done, select="url")["items"] == ["u2", "u1"]  # the caller itself never is


def test_a_run_that_is_not_a_workflow_run_of_the_tenant_raises(engine, run_store, tenants):
    a, b = tenants
    wf = uuid4()
    run = add(engine, run_store, a, wf, {})
    with pytest.raises(NotAWorkflowRun):
        workflow_history(engine, b, run)  # another tenant's run
    with pytest.raises(NotAWorkflowRun):
        workflow_history(engine, a, str(uuid4()))


def test_since_hours_window(engine, run_store, tenants):
    a, _ = tenants
    wf = uuid4()
    add(engine, run_store, a, wf, rejected("old"), hours_ago=30)
    add(engine, run_store, a, wf, rejected("new"), hours_ago=2)
    caller = add(engine, run_store, a, wf, {}, status="running")
    assert workflow_history(engine, a, caller, select="url")["items"] == ["new"]
    assert workflow_history(engine, a, caller, select="url", since_hours=48)["items"] == ["new", "old"]
    assert workflow_history(engine, a, caller, select="url", since_hours=1)["items"] == []


def test_where_select_distinct_and_limit(engine, run_store, tenants):
    a, _ = tenants
    wf = uuid4()
    add(engine, run_store, a, wf, rejected("u1"), hours_ago=5)
    add(engine, run_store, a, wf, rejected("u2", "too_long"), hours_ago=4)
    add(engine, run_store, a, wf, rejected("u1"), hours_ago=3)  # the same url again
    add(engine, run_store, a, wf, {"status": "posted", "url": "u9"}, hours_ago=2)
    add(engine, run_store, a, wf, {"status": "check_failed"}, hours_ago=1)  # no url
    add(engine, run_store, a, wf, "plain text output", hours_ago=0.5)
    caller = add(engine, run_store, a, wf, {}, status="running")
    where = {"status": "check_failed"}
    assert workflow_history(engine, a, caller, where=where, select="url") == {"items": ["u1", "u2"], "count": 2}
    # distinct is the default with select; without it the repeated url is kept
    assert workflow_history(engine, a, caller, where=where, select="url", distinct=False)["items"] == ["u1", "u2", "u1"]
    # no select: whole outputs, not distinct by default
    whole = workflow_history(engine, a, caller, where=where)
    assert whole["count"] == 4 and whole["items"][0] == {"status": "check_failed"}
    assert workflow_history(engine, a, caller, where=where, distinct=True)["count"] == 4 - 1
    assert workflow_history(engine, a, caller, where={"status": "check_failed", "reason": "too_long"}, select="url")["items"] == ["u2"]
    assert workflow_history(engine, a, caller, where=where, select="url", distinct=False, limit=2)["items"] == ["u1", "u2"]
    assert workflow_history(engine, a, caller, select="status")["items"] == ["check_failed", "posted"]
    assert workflow_history(engine, a, caller)["items"][0] == "plain text output"  # not JSON: its text
    assert workflow_history(engine, a, caller, where=where, select="nothing.here")["items"] == []


def test_where_compares_types_strictly(engine, run_store, tenants):
    a, _ = tenants
    wf = uuid4()
    add(engine, run_store, a, wf, {"n": 1, "ok": True, "s": "1"}, hours_ago=1)
    caller = add(engine, run_store, a, wf, {}, status="running")
    hit = lambda w: workflow_history(engine, a, caller, where=w)["count"]  # noqa: E731
    assert hit({"n": 1}) == 1 and hit({"n": 1.0}) == 1 and hit({"ok": True}) == 1 and hit({"s": "1"}) == 1
    assert hit({"n": True}) == 0 and hit({"ok": 1}) == 0 and hit({"n": "1"}) == 0 and hit({"s": 1}) == 0


def test_statuses_filter(engine, run_store, tenants):
    a, _ = tenants
    wf = uuid4()
    add(engine, run_store, a, wf, {"u": "done"}, status="completed", hours_ago=4)
    add(engine, run_store, a, wf, {"u": "skip"}, status="skipped", hours_ago=3)
    add(engine, run_store, a, wf, {"u": "fail"}, status="failed", hours_ago=2)
    add(engine, run_store, a, wf, {"u": "busy"}, status="running", hours_ago=1)
    caller = add(engine, run_store, a, wf, {}, status="running")
    pick = lambda **kw: workflow_history(engine, a, caller, select="u", **kw)["items"]  # noqa: E731
    assert pick() == ["done"]
    assert pick(statuses=["skipped", "failed"]) == ["fail", "skip"]
    assert pick(statuses=["completed", "skipped", "failed"]) == ["fail", "skip", "done"]
    assert pick(statuses=["running"]) == []  # only completed, skipped and failed can be asked for


def test_the_result_is_cut_to_fit_a_step_output(engine, run_store, tenants):
    a, _ = tenants
    wf = uuid4()
    for i in range(10):
        add(engine, run_store, a, wf, {"url": f"{i}-" + "x" * 5000}, hours_ago=10 - i)
    caller = add(engine, run_store, a, wf, {}, status="running")
    out = workflow_history(engine, a, caller, select="url")
    assert out["truncated"] is True and 0 < out["count"] < 10
    assert len(json.dumps(out).encode()) < 32 * 1024
    assert out["items"][0].startswith("9-")  # the newest are kept
    assert "truncated" not in workflow_history(engine, a, caller, select="url", limit=2)
