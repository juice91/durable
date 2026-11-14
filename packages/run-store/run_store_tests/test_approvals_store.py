"""AC.3: the approvals projection (0014_approvals) written with its events, the inbox, and the
`waiting_approval` display status. Implementer's focused tests; the acceptance suite adds more."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_run_store import ApprovalCursor, ApprovalStore, InvalidRunCursor, RunNotFound


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        # Children before parents (fk_runs_tenant_parent_run); events and projection rows cascade
        # or are deleted first.
        conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def approvals(engine):
    return ApprovalStore(engine)


def _deadline(hours: float = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _running_agent_run(run_store, tenant, agent_id=None):
    run_id = uuid4()
    run_store.create_run(tenant, agent_id or uuid4(), 1, "hi", run_id)
    run_store.set_status(tenant, run_id, "running")
    run_store.heartbeat(tenant, run_id)
    return run_id


def _wait_tool(event_log, tenant, run_id, call_id="call_1", tool="send_email", args=None, deadline=None):
    return event_log.append_approval_waiting(
        tenant, run_id, "tool", call_id,
        {"tool_call_id": call_id, "tool_name": tool, "args": args if args is not None else {"to": "a@b.c"},
         "deadline": deadline or _deadline(), "workflow_id": str(run_id)},
    )


def _wait_step(event_log, tenant, run_id, step_id="approve", deadline=None):
    return event_log.append_approval_waiting(
        tenant, run_id, "step", step_id,
        {"step_id": step_id, "prompt": "Ship it?", "timeout_hours": 1, "workflow_id": str(run_id),
         "deadline": deadline or _deadline()},
    )


def _row(engine, tenant, run_id, kind, key):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT * FROM pending_approvals WHERE tenant_id = :t AND run_id = :r AND kind = :k AND approval_key = :a"
        ), {"t": tenant, "r": str(run_id), "k": kind, "a": key}).mappings().one_or_none()


def _event_count(engine, run_id) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).scalar_one()


# ---------------------------------------------------------------------------- waiting


def test_tool_waiting_writes_event_and_open_row(run_store, event_log, engine, tenant):
    agent_id = uuid4()
    run_id = _running_agent_run(run_store, tenant, agent_id)
    deadline = _deadline(2)
    event, created = _wait_tool(event_log, tenant, run_id, deadline=deadline)
    assert created and event.type == "tool_approval_waiting"
    assert event.data["deadline"] == deadline.isoformat() and event.data["tool_name"] == "send_email"
    row = _row(engine, tenant, run_id, "tool", "call_1")
    assert row["root_run_id"] == str(run_id) and row["agent_id"] == str(agent_id)
    assert row["tool_name"] == "send_email" and row["args"] == {"to": "a@b.c"} and row["prompt"] is None
    assert row["deadline"] == deadline and row["requested_at"] == event.ts
    assert row["decided_at"] is None and row["decision"] is None


def test_waiting_replay_keeps_first_event_and_deadline(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    first = _deadline(1)
    e1, c1 = _wait_tool(event_log, tenant, run_id, deadline=first)
    e2, c2 = _wait_tool(event_log, tenant, run_id, deadline=_deadline(5))
    assert c1 and not c2 and e2.seq == e1.seq and e2.data["deadline"] == first.isoformat()
    assert _row(engine, tenant, run_id, "tool", "call_1")["deadline"] == first
    assert _event_count(engine, run_id) == 1


def test_large_args_are_truncated_in_the_projection_only(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    big = {"body": "x" * 9000}
    event, _ = _wait_tool(event_log, tenant, run_id, args=big)
    assert event.data["args"] == big
    assert _row(engine, tenant, run_id, "tool", "call_1")["args"] == {"truncated": True}


def test_waiting_validates_inputs_and_tenant(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    with pytest.raises(ValueError):
        event_log.append_approval_waiting(tenant, run_id, "other", "k", {"deadline": _deadline()})
    with pytest.raises(ValueError):  # a tool approval needs its tool name
        event_log.append_approval_waiting(tenant, run_id, "tool", "c", {"deadline": _deadline()})
    with pytest.raises(ValueError):  # no deadline: nothing is written
        event_log.append_approval_waiting(tenant, run_id, "tool", "c", {"tool_name": "t"})
    with pytest.raises(RunNotFound):
        _wait_tool(event_log, "other_tenant", run_id)
    assert _event_count(engine, run_id) == 0


def test_child_run_row_names_its_workflow_run_and_workflow(run_store, event_log, engine, tenant):
    wf_run, wf_id, agent_id = uuid4(), uuid4(), uuid4()
    run_store.create_workflow_run(tenant, wf_id, 1, "{}", wf_run)
    run_store.set_status(tenant, wf_run, "running")
    child = uuid4()
    run_store.create_child_agent_run(tenant, agent_id, 1, "hi", child, wf_run, "agent_step")
    run_store.set_status(tenant, child, "running")
    _wait_tool(event_log, tenant, child)
    row = _row(engine, tenant, child, "tool", "call_1")
    assert row["root_run_id"] == str(wf_run) and row["workflow_id"] == str(wf_id) and row["agent_id"] == str(agent_id)
    # Both the child and its workflow run show the wait.
    assert run_store.get_run(tenant, child)["display_status"] == "waiting_approval"
    assert run_store.get_run(tenant, wf_run)["display_status"] == "waiting_approval"


def test_failed_child_with_open_row_does_not_keep_parent_waiting(run_store, event_log, engine, tenant, approvals):
    """A child run that ends while waiting leaves its row open (no decision exists to project):
    the workflow run, still running, no longer shows waiting_approval, and the inbox hides the row."""
    wf_run = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", wf_run)
    run_store.set_status(tenant, wf_run, "running")
    run_store.heartbeat(tenant, wf_run)
    child = uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, "hi", child, wf_run, "agent_step")
    run_store.set_status(tenant, child, "running")
    _wait_tool(event_log, tenant, child)
    assert run_store.get_run(tenant, wf_run)["display_status"] == "waiting_approval"

    run_store.set_status(tenant, child, "failed", error="boom")
    assert _row(engine, tenant, child, "tool", "call_1")["decided_at"] is None  # left open
    assert run_store.get_run(tenant, child)["display_status"] == "failed"
    assert run_store.get_run(tenant, wf_run)["display_status"] == "running"
    assert run_store.list_runs(tenant, status="waiting_approval") == []
    assert approvals.list(tenant, "pending")[0] == []
    assert event_log.append_tool_decision(tenant, child, "call_1", "tool_approved", {}) == ("not_waiting", None)

    # The workflow's own approval step still shows it waiting.
    _wait_step(event_log, tenant, wf_run)
    assert run_store.get_run(tenant, wf_run)["display_status"] == "waiting_approval"


# ---------------------------------------------------------------------------- decisions


def test_tool_decision_closes_the_row_then_exists(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    _wait_tool(event_log, tenant, run_id)
    data = {"tool_call_id": "call_1", "decided_by": "user_1", "comment": "ok"}
    outcome, event = event_log.append_tool_decision(tenant, run_id, "call_1", "tool_approved", data)
    assert outcome == "created" and event.type == "tool_approved" and event.seq == 1
    row = _row(engine, tenant, run_id, "tool", "call_1")
    assert (row["decision"], row["decided_by"], row["decided_at"]) == ("approved", "user_1", event.ts)
    again, earlier = event_log.append_tool_decision(tenant, run_id, "call_1", "tool_denied", {"decided_by": "u2"})
    assert again == "exists" and earlier.type == "tool_approved"
    assert _row(engine, tenant, run_id, "tool", "call_1")["decision"] == "approved"


def test_decision_after_timeout_is_exists(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    _wait_tool(event_log, tenant, run_id)
    outcome, _ = event_log.append_tool_decision(tenant, run_id, "call_1", "tool_approval_timed_out", {"tool_call_id": "call_1"})
    assert outcome == "created"
    row = _row(engine, tenant, run_id, "tool", "call_1")
    assert row["decision"] == "timed_out" and row["decided_by"] is None
    late, event = event_log.append_tool_decision(tenant, run_id, "call_1", "tool_approved", {"decided_by": "u"})
    assert late == "exists" and event.type == "tool_approval_timed_out"


def test_decision_without_wait_or_on_ended_run_is_not_waiting(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    assert event_log.append_tool_decision(tenant, run_id, "nope", "tool_approved", {}) == ("not_waiting", None)
    _wait_tool(event_log, tenant, run_id)
    run_store.set_status(tenant, run_id, "failed", error="boom")
    assert event_log.append_tool_decision(tenant, run_id, "call_1", "tool_approved", {}) == ("not_waiting", None)
    with pytest.raises(ValueError):  # a step type is not a tool decision
        event_log.append_tool_decision(tenant, run_id, "call_1", "step_approved", {})
    with pytest.raises(RunNotFound):
        event_log.append_tool_decision("other_tenant", run_id, "call_1", "tool_approved", {})


def test_concurrent_decisions_exactly_one_wins(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    _wait_tool(event_log, tenant, run_id)
    barrier = threading.Barrier(6)
    results: list[str] = []

    def decide(i: int) -> None:
        barrier.wait()
        kind = "tool_approved" if i % 2 else "tool_denied"
        results.append(event_log.append_tool_decision(tenant, run_id, "call_1", kind, {"decided_by": f"u{i}"})[0])

    threads = [threading.Thread(target=decide, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["created"] + ["exists"] * 5
    assert _event_count(engine, run_id) == 2
    assert _row(engine, tenant, run_id, "tool", "call_1")["decision"] in ("approved", "denied")


def test_projection_failure_rolls_back_the_event(run_store, event_log, engine, tenant, monkeypatch):
    from eap_run_store import approvals as module

    run_id = _running_agent_run(run_store, tenant)

    def boom(*args, **kwargs):
        raise RuntimeError("projection write failed")

    monkeypatch.setattr(module, "insert_waiting_in_session", boom)
    with pytest.raises(RuntimeError):
        _wait_tool(event_log, tenant, run_id)
    assert _event_count(engine, run_id) == 0 and _row(engine, tenant, run_id, "tool", "call_1") is None
    monkeypatch.undo()

    _wait_tool(event_log, tenant, run_id)
    monkeypatch.setattr(module, "record_decision_in_session", boom)
    with pytest.raises(RuntimeError):
        event_log.append_tool_decision(tenant, run_id, "call_1", "tool_approved", {"decided_by": "u"})
    assert _event_count(engine, run_id) == 1
    assert _row(engine, tenant, run_id, "tool", "call_1")["decided_at"] is None


def test_step_decision_updates_the_projection(run_store, event_log, engine, tenant):
    wf_run = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", wf_run)
    run_store.set_status(tenant, wf_run, "running")
    event, _ = _wait_step(event_log, tenant, wf_run)
    assert event.type == "step_waiting"
    row = _row(engine, tenant, wf_run, "step", "approve")
    assert row["prompt"] == "Ship it?" and row["tool_name"] is None and row["args"] is None
    outcome, _ = event_log.append_step_decision(
        tenant, wf_run, "approve", "step_rejected", {"step_id": "approve", "decided_by": "boss", "comment": None}
    )
    assert outcome == "created"
    row = _row(engine, tenant, wf_run, "step", "approve")
    assert (row["decision"], row["decided_by"]) == ("denied", "boss")


def test_step_decision_without_projection_row_still_records(run_store, event_log, engine, tenant):
    """A wait written with the plain append (before AC.8 switches the worker) has no row: the
    decision still lands."""
    wf_run = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", wf_run)
    run_store.set_status(tenant, wf_run, "running")
    event_log.append(tenant, wf_run, "step:s:waiting", "step_waiting", {"step_id": "s"})
    assert event_log.append_step_decision(tenant, wf_run, "s", "step_timed_out", {"step_id": "s"})[0] == "created"


# ---------------------------------------------------------------------------- display status


def test_display_status_waiting_approval_in_run_reads(run_store, event_log, engine, tenant):
    agent_id = uuid4()
    conv, _ = run_store.create_conversation(tenant, agent_id)
    run_id = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "hi", run_id)
    run_store.set_status(tenant, run_id, "running")
    run_store.heartbeat(tenant, run_id)
    assert run_store.get_run(tenant, run_id)["display_status"] == "running"
    _wait_tool(event_log, tenant, run_id)
    assert run_store.get_run(tenant, run_id)["display_status"] == "waiting_approval"
    assert run_store.get_run(tenant, run_id)["status"] == "running"
    assert [r["display_status"] for r in run_store.list_turns(tenant, conv["id"])] == ["waiting_approval"]
    assert run_store.get_conversation(tenant, conv["id"])["last_turn_display_status"] == "waiting_approval"
    listed = run_store.list_runs(tenant, status="waiting_approval")
    assert [r["id"] for r in listed] == [str(run_id)] and listed[0]["display_status"] == "waiting_approval"
    assert [r["id"] for r in run_store.list_runs(tenant, status="running")] == [str(run_id)]

    # A stale heartbeat shows recovering first (recovery replays to the wait).
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET heartbeat_at = now() - interval '1 minute' WHERE id = :r"), {"r": str(run_id)})
    assert run_store.get_run(tenant, run_id)["display_status"] == "recovering"
    assert run_store.list_runs(tenant, status="waiting_approval") == []
    run_store.heartbeat(tenant, run_id)

    event_log.append_tool_decision(tenant, run_id, "call_1", "tool_denied", {"decided_by": "u"})
    assert run_store.get_run(tenant, run_id)["display_status"] == "running"
    assert run_store.get_conversation(tenant, conv["id"])["last_turn_display_status"] == "running"


def test_output_format_column_and_check(run_store, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    assert run_store.get_run(tenant, run_id)["output_format"] is None
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET output_format = 'json' WHERE id = :r"), {"r": str(run_id)})
    assert run_store.get_run(tenant, run_id)["output_format"] == "json"
    with pytest.raises(IntegrityError, match="ck_runs_output_format"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE runs SET output_format = 'xml' WHERE id = :r"), {"r": str(run_id)})


# ---------------------------------------------------------------------------- the inbox


def test_inbox_lists_filters_paginates_and_is_tenant_scoped(run_store, event_log, engine, tenant, approvals):
    agent_a, agent_b = uuid4(), uuid4()
    runs = [_running_agent_run(run_store, tenant, agent_a if i < 3 else agent_b) for i in range(5)]
    for run_id in runs:
        _wait_tool(event_log, tenant, run_id)
    wf_run = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", wf_run)
    run_store.set_status(tenant, wf_run, "running")
    _wait_step(event_log, tenant, wf_run)

    page, cursor = approvals.list(tenant, "pending", limit=4)
    assert len(page) == 4 and cursor is not None
    rest, end = approvals.list(tenant, "pending", cursor=cursor, limit=4)
    assert len(rest) == 2 and end is None
    keys = [(r["run_id"], r["kind"]) for r in page + rest]
    assert len(set(keys)) == 6
    stamps = [r["requested_at"] for r in page + rest]
    assert stamps == sorted(stamps, reverse=True)

    assert [r["kind"] for r in approvals.list(tenant, "pending", kind="step")[0]] == ["step"]
    assert len(approvals.list(tenant, "pending", agent_id=str(agent_a))[0]) == 3
    assert approvals.list("other_tenant", "pending")[0] == []
    assert approvals.get("other_tenant", str(runs[0]), "tool", "call_1") is None

    event_log.append_tool_decision(tenant, runs[0], "call_1", "tool_approved", {"decided_by": "u"})
    decided, _ = approvals.list(tenant, "decided")
    assert [(r["run_id"], r["decision"], r["status"]) for r in decided] == [(str(runs[0]), "approved", "decided")]
    assert len(approvals.list(tenant, "pending")[0]) == 5

    # A run that ended while waiting no longer appears as pending.
    run_store.set_status(tenant, runs[1], "failed", error="boom")
    assert len(approvals.list(tenant, "pending")[0]) == 4

    with pytest.raises(InvalidRunCursor):
        approvals.list(tenant, "pending", cursor="not-a-cursor")
    with pytest.raises(ValueError):
        approvals.list(tenant, "everything")


def test_cursor_round_trip():
    at = datetime.now(timezone.utc)
    cursor = ApprovalCursor(at, "r1", "tool", "call:with:colons")
    assert ApprovalCursor.decode(cursor.encode()) == cursor
