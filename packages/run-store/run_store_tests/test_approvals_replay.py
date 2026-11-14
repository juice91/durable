"""AC.8 review fixes in the store: a replayed waiting write adds the missing inbox row (rolling
deploy) but never reopens a decided approval; the inbox cursor names its list; the shared
tool-approval topic."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from eap_run_store import ApprovalCursor, InvalidRunCursor
from eap_run_store.approvals import tool_approval_topic

from .test_approvals_store import (  # noqa: F401 - fixtures
    _deadline,
    _row,
    _running_agent_run,
    _wait_tool,
    approvals,
    tenant,
)


def _old_worker_wait(event_log, tenant, run_id, step_id="approve", deadline=None):
    """The step_waiting event as a worker without the projection wrote it: the event, no row."""
    data = {"step_id": step_id, "prompt": "Ship it?", "timeout_hours": 1, "workflow_id": str(run_id)}
    if deadline is not False:
        data["deadline"] = (deadline or _deadline()).isoformat()
    event, created = event_log.append(tenant, run_id, f"step:{step_id}:waiting", "step_waiting", data)
    assert created
    return event


def _replay(event_log, tenant, run_id, step_id="approve"):
    return event_log.append_approval_waiting(
        tenant, run_id, "step", step_id,
        {"step_id": step_id, "prompt": "Ship it?", "timeout_hours": 1, "workflow_id": str(run_id),
         "deadline": _deadline(hours=5)},
    )


def test_replay_adds_the_missing_row_from_the_first_event(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    first = _old_worker_wait(event_log, tenant, run_id)
    assert _row(engine, tenant, run_id, "step", "approve") is None
    event, created = _replay(event_log, tenant, run_id)
    assert created is False and event.seq == first.seq
    row = _row(engine, tenant, run_id, "step", "approve")
    assert row is not None and row["decided_at"] is None and row["prompt"] == "Ship it?"
    # The first event's deadline and time stand, not the replay's.
    assert row["deadline"] == datetime.fromisoformat(first.data["deadline"])
    assert row["requested_at"] == first.ts
    # A second replay changes nothing.
    _replay(event_log, tenant, run_id)
    assert _row(engine, tenant, run_id, "step", "approve") == row


def test_replay_never_reopens_a_decided_approval(run_store, event_log, engine, tenant):
    # Decided without a row (both writes by code without the projection): no row appears.
    run_id = _running_agent_run(run_store, tenant)
    _old_worker_wait(event_log, tenant, run_id)
    event_log.append(tenant, run_id, "step:approve:decision", "step_approved", {"decided_by": "alice"})
    _replay(event_log, tenant, run_id)
    assert _row(engine, tenant, run_id, "step", "approve") is None

    # Decided with a row: the row stays decided.
    other = _running_agent_run(run_store, tenant)
    _wait_tool(event_log, tenant, other)
    assert event_log.append_tool_decision(
        tenant, other, "call_1", "tool_denied", {"tool_call_id": "call_1", "decided_by": "bob"})[0] == "created"
    _wait_tool(event_log, tenant, other)
    row = _row(engine, tenant, other, "tool", "call_1")
    assert (row["decision"], row["decided_by"]) == ("denied", "bob") and row["decided_at"] is not None


def test_replay_of_an_event_without_a_deadline_writes_no_row_and_does_not_fail(run_store, event_log, engine, tenant):
    run_id = _running_agent_run(run_store, tenant)
    _old_worker_wait(event_log, tenant, run_id, deadline=False)
    _replay(event_log, tenant, run_id)
    assert _row(engine, tenant, run_id, "step", "approve") is None


def test_cursor_names_its_list_and_a_mismatch_is_invalid(run_store, event_log, approvals, tenant):
    for _ in range(2):
        run_id = _running_agent_run(run_store, tenant)
        _wait_tool(event_log, tenant, run_id)
    rows, token = approvals.list(tenant, "pending", limit=1)
    assert len(rows) == 1 and token
    assert ApprovalCursor.decode(token).status == "pending"
    assert len(approvals.list(tenant, "pending", cursor=token, limit=1)[0]) == 1
    with pytest.raises(InvalidRunCursor):
        approvals.list(tenant, "decided", cursor=token)
    decided = ApprovalCursor(_deadline(), "r", "tool", "k", "decided")
    assert ApprovalCursor.decode(decided.encode()) == decided
    with pytest.raises(InvalidRunCursor):
        approvals.list(tenant, "pending", cursor=decided)


def test_tool_approval_topic_is_pinned():
    assert tool_approval_topic("4b7c") == "tool_approval:4b7c"
    run_id = str(uuid4())
    assert tool_approval_topic(run_id) == f"tool_approval:{run_id}"
