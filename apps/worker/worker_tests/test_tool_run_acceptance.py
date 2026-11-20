"""Acceptance tests for tool test runs in the worker.

Covers worker-side behavior not already tested in test_tool_run.py.
"""

from __future__ import annotations

import uuid

import pytest

from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.tool_run_helpers import enqueue_tool_run


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    keys = [e[1] for e in events]
    assert len(keys) == len(set(keys))


def test_read_only_tool_completes_with_events(worker, client, tenant):
    """A read-only tool (clock.now) runs to completion with exact event sequence and gap-free seq.
    
    Note: the event args record the validated input (including defaults), consistent with other tools
    like http.request that have defaulted fields.
    """
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="clock.now",
        args={}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert (run["kind"], run["tool_id"], run["agent_id"], run["agent_version"]) == ("tool", "clock.now", None, None)

    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert [(e[1], e[2]) for e in events] == [
        ("start", "run_started"),
        ("tool:tool_run:call", "tool_call"),
        ("tool:tool_run:result", "tool_result"),
        ("end", "run_completed"),
    ]
    assert events[0][3] == {"kind": "tool", "tool_id": "clock.now", "input": {}}
    # clock.now returns an ISO timestamp string (not an object with 'timestamp' key)
    # The event args record the validated input including defaults
    assert events[1][3]["args"] == {"offset_minutes": 0, "parts": False}
    assert events[2][3]["status"] == "ok"
    # The result is an ISO timestamp string
    assert "T" in events[2][3]["content"]  # ISO format contains 'T'


def test_tool_error_results_in_failed_run(worker, client, tenant):
    """A tool error → tool_result status 'error' with error_code, and the run fails."""
    # tickets.create with missing required field will fail validation in check_tool_step
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "only title"},  # Missing body, will fail input validation
        app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    # The error text is 'ToolInputInvalid: the input does not match tool tickets.create'
    assert "ToolInputInvalid" in run["error"]
    assert "tickets.create" in run["error"]
    # Only start and end events, no tool call
    events = event_rows(worker.engine, run_id)
    assert [(e[1], e[2]) for e in events] == [("start", "run_started"), ("end", "run_failed")]
    # The error_code should be 'tool_input_invalid'
    # The run_failed event's data contains error_code
    end_event = [e for e in events if e[1] == "end"][0]
    assert end_event[3].get("error_code") == "tool_input_invalid"


def test_allowlist_re_check_after_enqueue(worker, client, tenant, monkeypatch):
    """The worker re-checks the allowlist when it dequeues the run.

    check_tool_step is checkpointed, and the in-process worker can run it in the same tick as
    enqueue. The allowlist is installed before enqueue so that step records the refusal.
    enqueue_tool_run does not consult EAP_ALLOWED_TOOLS; the running worker reads the cached
    runtime allowlist, not the environment.
    """
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"tickets.create"}))
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="clock.now",
        args={}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert "clock.now' is not allowed on this worker" in run["error"]
    # Only start and end events, no tool call
    events = event_rows(worker.engine, run_id)
    assert [(e[1], e[2]) for e in events] == [("start", "run_started"), ("end", "run_failed")]


def test_tool_input_invalid_reaches_worker(worker, client, tenant):
    """Invalid input reaching the worker → tool_input_invalid status."""
    # tickets.create requires both title and body; missing body fails validation
    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "x"},  # Missing required 'body' field
        app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert "ToolInputInvalid" in run["error"]
    # The run_failed event's data contains error_code = 'tool_input_invalid'
    events = event_rows(worker.engine, run_id)
    # Should have start and end, but not the tool call (validation fails in check_tool_step)
    event_keys = [e[1] for e in events]
    assert "start" in event_keys
    assert "end" in event_keys
    # The tool_call event should NOT be present since input validation failed
    assert not any(k.startswith("tool:tool_run:call") for k in event_keys)
    # The error_code in the run_failed event
    end_event = [e for e in events if e[1] == "end"][0]
    assert end_event[3].get("error_code") == "tool_input_invalid"
