"""Workflow runs survive killed processes, whole-system (milestone 3, task 3.10).

Everything goes through the real API in its own process (`api_server.py`, real HTTP): agents and
workflows are created and published over HTTP, runs are started over HTTP, approvals are decided
over HTTP, and SSE is read over HTTP. Workers are real `python -m eap_worker` processes killed by
their own crash hook (EAP_CRASH_AT / EAP_CRASH_EVENT_KEY, `os._exit(137)`) or by SIGKILL, and a
fresh worker with the same executor id recovers the run. The worker-level crash tests
(apps/worker/worker_tests/test_workflow_run_crash*.py) enqueue directly and approve through an
in-process app; these do not.

Every scenario then asserts, for the workflow run:

- it ends EXACTLY ONCE: one terminal status (completed, or needs_attention where a side effect's
  outcome is unknown), exactly one `end` event, and exactly one DBOS workflow with the run id, in
  SUCCESS; the only other DBOS workflows prefixed by the run id are its parallel branches
  (`{run_id}:{parallel}:{branch}`), each exactly once and in SUCCESS;
- gap-free seq from 0 and no duplicate event_key across the parent and its branch workflows (they
  write to the same run), with the expected story;
- every side effect exactly once: fake_external_tickets rows for the run (the number of calls the
  idempotent service saw is checked too), and exactly one child agent run per agent step that ran
  (id = the step_started event's child_run_id), each completed by exactly one DBOS workflow with
  its own gap-free events; nothing more appears after a further settle period;
- SSE resume (`?after_seq=` and `Last-Event-ID`) from any seq, including the last seq stored before
  the crash, returns exactly the database's events after it, ending on the terminal event.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT, Harness, wait_for

# Short knobs. The approval poll is read once when a run starts (recorded with the run), so every
# process gets it. An hour stays 3600 s: no approval in these tests may time out.
WF_ENV: dict[str, str | None] = {"EAP_APPROVAL_POLL_SECONDS": "1", "EAP_WORKFLOW_HOUR_SECONDS": "3600"}
ECHO_SPEC = {
    "name": "Echo",
    "instructions": "Repeat the request.",
    "model": {"provider": "test", "name": "echo"},
    "tools": [],
}
HISTORY_SPEC = {**ECHO_SPEC, "model": {"provider": "test", "name": "history"}}
INPUT = {"q": "printer on fire"}
Q_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
TERMINAL = {"run_completed", "run_failed"}


# --------------------------------------------------------------------------- specs


def linear_spec(agent_id: str) -> dict[str, Any]:
    """ask (agent) -> t (tickets.create, a side effect) -> done (agent)."""
    return {
        "name": "Linear",
        "input_schema": Q_SCHEMA,
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}, "next": "t"},
            {
                "id": "t", "type": "tool", "tool_id": "tickets.create",
                "input": {"title": "Workflow ticket", "body": {"$ref": "steps.ask.output"}}, "next": "done",
            },
            {"id": "done", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "steps.ask.output"}}},
        ],
    }


LINEAR_KEYS = [
    "start",
    "step:ask:started", "step:ask:completed",
    "step:t:started", "step:t:completed",
    "step:done:started", "step:done:completed",
    "end",
]


def approval_spec(agent_id: str) -> dict[str, Any]:
    """ask (agent) -> t (tickets.create) -> approve (approval) -> done (agent)."""
    spec = linear_spec(agent_id)
    spec["name"] = "Approval"
    spec["steps"][1]["next"] = "approve"
    spec["steps"].insert(
        2, {"id": "approve", "type": "approval", "prompt": "Ship it?", "timeout_hours": 1, "next": "done"}
    )
    return spec


APPROVAL_KEYS = [
    "start",
    "step:ask:started", "step:ask:completed",
    "step:t:started", "step:t:completed",
    "step:approve:started", "step:approve:waiting", "step:approve:decision", "step:approve:completed",
    "step:done:started", "step:done:completed",
    "end",
]


def parallel_spec(agent_id: str) -> dict[str, Any]:
    """fan: branch a (agent) and branch b1 (agent) -> b2 (tickets.create); join (agent) reads both ends."""
    return {
        "name": "Fan",
        "input_schema": Q_SCHEMA,
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b1"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"prompt": "left"}, "next": "join"},
            {"id": "b1", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}, "next": "b2"},
            {
                "id": "b2", "type": "tool", "tool_id": "tickets.create",
                "input": {"title": "Branch ticket", "body": {"$ref": "steps.b1.output"}}, "next": "join",
            },
            {
                "id": "join", "type": "agent", "agent_id": agent_id,
                "input": {"left": {"$ref": "steps.a.output"}, "right": {"$ref": "steps.b2.output"}},
            },
        ],
    }


PARALLEL_BRANCH_KEYS = {
    "step:a:started", "step:a:completed",
    "step:b1:started", "step:b1:completed",
    "step:b2:started", "step:b2:completed",
}


def parallel_spec_branch_a_gated(agent_id: str) -> dict[str, Any]:
    """Branch a (agent) beside branch hold (approval) -> b2 (tickets.create).

    The ticket cannot start until `hold` is approved, so a kill inside branch a is never a kill
    during the ticket call. The join reads both branch ends."""
    return {
        "name": "Fan gated",
        "input_schema": Q_SCHEMA,
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "hold"], "join": "join"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"prompt": "left"}, "next": "join"},
            {"id": "hold", "type": "approval", "prompt": "Release branch b?", "timeout_hours": 1, "next": "b2"},
            {
                "id": "b2", "type": "tool", "tool_id": "tickets.create",
                "input": {"title": "Branch ticket", "body": "gated"}, "next": "join",
            },
            {
                "id": "join", "type": "agent", "agent_id": agent_id,
                "input": {"left": {"$ref": "steps.a.output"}, "right": {"$ref": "steps.b2.output"}},
            },
        ],
    }


# --------------------------------------------------------------------------- shared setup


@pytest.fixture
def wf(harness: Harness) -> Harness:
    """The harness with the real API process started (workflow knobs set) and an echo agent."""
    harness.start_api(extra_env=WF_ENV)
    harness.agent_id = harness.create_published_agent(ECHO_SPEC)  # type: ignore[attr-defined]
    return harness


def start_run(h: Harness, workflow_id: str, input_value: dict[str, Any] | None = None) -> str:
    run_id = str(uuid.uuid4())
    r = h.post_workflow_run(workflow_id, run_id, INPUT if input_value is None else input_value)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["run_id"] == run_id and body["kind"] == "workflow" and body["workflow_id"] == workflow_id
    return run_id


def crash_run(
    h: Harness, workflow_id: str, *, crash_at: str, event_key: str, input_value: dict[str, Any] | None = None
) -> tuple[str, list]:
    """Start a crashing worker, start a run over HTTP, wait for exit 137; (run_id, events before)."""
    first = h.start_worker("worker1-crashing", crash_at=crash_at, event_key=event_key, extra_env=WF_ENV)
    run_id = start_run(h, workflow_id, input_value)
    assert first.wait_exit() == 137, first.log()[-4000:]
    assert h.crash_marker.exists()
    assert f"EAP_CRASH_AT={crash_at} reached" in first.log()
    assert h.run_row(run_id)["status"] == "running"  # nothing finished it
    before = h.events(run_id)
    assert "end" not in [e.event_key for e in before]
    return run_id, before


def recover(h: Harness, name: str = "worker2-recovering") -> None:
    second = h.start_worker(name, extra_env=WF_ENV)
    assert second.proc.poll() is None


def _keys(events: list) -> list[str]:
    return [e.event_key for e in events]


def assert_exactly_once(
    h: Harness,
    run_id: str,
    *,
    status: str,
    agent_steps: list[str],
    tickets: int,
    calls: int | None = None,
    keys: list[str] | None = None,
    key_set: set[str] | None = None,
    branches: tuple[str, ...] = (),
    child_terminal: str = "completed",
    reconnect_from: int = -1,
) -> dict[str, Any]:
    """The task 3.10 invariants for a recovered workflow run (see the module docstring).

    `child_terminal` is the status of every child agent run. It is `completed` unless the child
    itself is the run whose side effect is unknown (`needs_attention`)."""
    run = h.wait_terminal(run_id)
    assert run["status"] == status, (run, h.worker_logs())
    api_run = h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers)
    assert api_run.status_code == 200 and api_run.json()["status"] == status, api_run.text
    assert api_run.json()["kind"] == "workflow"

    # Gap-free seq, no duplicate event_key (parent and branch workflows share the run's events).
    events = h.events(run_id)
    seqs, ks = [e.seq for e in events], _keys(events)
    assert seqs == list(range(len(events))), f"seq not contiguous from 0: {seqs}"
    assert len(ks) == len(set(ks)), f"duplicate event_key: {ks}"
    if keys is not None:
        assert ks == keys, ks
    if key_set is not None:
        assert set(ks) == key_set and len(ks) == len(key_set), ks
    assert ks[-1] == "end" and events[-1].type in TERMINAL
    assert events[-1].type == ("run_completed" if status == "completed" else "run_failed")
    if status == "completed":
        assert run["output"] == events[-1].data["output"]  # canonical JSON text, both

    # Exactly one DBOS workflow for the run id, in SUCCESS; and nothing but its branches besides.
    assert h.workflows(run_id) and [w[0] for w in h.workflows(run_id)] == ["SUCCESS"], h.workflows(run_id)
    prefixed = h.workflows_with_prefix(run_id)
    assert sorted(w[0] for w in prefixed) == sorted([run_id, *(f"{run_id}:{b}" for b in branches)]), prefixed
    assert all(w[2] == "SUCCESS" for w in prefixed), prefixed

    # Exactly one child agent run per agent step that ran, each run once by one DBOS workflow.
    started = {e.data["step_id"]: e.data for e in events if e.type == "step_started"}
    expected_children = sorted((started[s]["child_run_id"], s) for s in agent_steps)
    children = h.child_runs(run_id)
    assert sorted((c[0], c[1]) for c in children) == expected_children, children
    for child_id, step_id, child_status, _version in children:
        assert child_status == child_terminal, (step_id, child_status)
        assert [w[0] for w in h.workflows(child_id)] == ["SUCCESS"], (step_id, h.workflows(child_id))
        child_events = h.events(child_id)
        assert [e.seq for e in child_events] == list(range(len(child_events)))
        assert len({e.event_key for e in child_events}) == len(child_events)
        assert [e.event_key for e in child_events].count("end") == 1
    listed = h.client.get(f"/api/v1/runs?parent_run_id={run_id}", headers=h.headers)
    assert listed.status_code == 200, listed.text
    assert sorted(i["run_id"] for i in listed.json()["items"]) == sorted(c[0] for c in children)

    # Every external side effect exactly once.
    stored = h.tickets(run_id)
    assert len(stored) == tickets, f"expected {tickets} external ticket(s) for run {run_id}, got {len(stored)}"
    seen_calls = h.ticket_calls(run_id)
    if calls is not None:
        assert seen_calls == calls, seen_calls

    # SSE resume from any seq: exactly the database's events after it, ending on the terminal event.
    db = [(e.seq, e.type, e.data) for e in events]
    last = events[-1].seq
    for point in sorted({-1, 0, reconnect_from, last // 2, last - 1, last}):
        for how in ("after_seq", "last_event_id"):
            frames = h.sse(run_id, **{how: point})
            assert frames and frames[0].event == "status", frames[:1]
            replayed = [f for f in frames if f.event == "run_event"]
            got = [(f.data["seq"], f.data["type"], f.data["data"]) for f in replayed]
            assert got == [row for row in db if row[0] > point], f"replay {how}={point} differs from the DB"
            assert [int(f.id) for f in replayed] == [f.data["seq"] for f in replayed]
            if replayed:
                assert frames[-1] is replayed[-1] and replayed[-1].data["type"] in TERMINAL

    # Settle: a live worker must not add a call, a ticket, a child or an event afterwards.
    time.sleep(1.0)
    assert len(h.events(run_id)) == len(events)
    assert len(h.tickets(run_id)) == tickets and h.ticket_calls(run_id) == seen_calls
    assert h.child_runs(run_id) == children
    return {"run": run, "events": events, "tickets": stored, "calls": seen_calls, "children": children}


# --------------------------------------------------------------------------- W1: kill mid-step


@pytest.mark.parametrize(
    ("step", "pre_children", "pre_keys"),
    [
        ("ask", [], ["start", "step:ask:started"]),
        ("t", ["ask"], ["start", "step:ask:started", "step:ask:completed", "step:t:started"]),
    ],
    ids=["agent-step", "tool-step"],
)
def test_w1_kill_mid_step(
    wf: Harness, report: Callable[[str], None], step: str, pre_children: list[str], pre_keys: list[str]
) -> None:
    """kill -9 right after `step:{step}:started` is recorded, before the step does its work."""
    h = wf
    workflow_id = h.create_published_workflow(linear_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id, before = crash_run(h, workflow_id, crash_at="workflow_mid_step", event_key=f"step:{step}:started")
    assert _keys(before) == pre_keys
    assert [c[1] for c in h.child_runs(run_id)] == pre_children
    assert h.ticket_calls(run_id) == 0 and h.tickets(run_id) == []

    recover(h)
    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["ask", "done"], tickets=1, calls=1, keys=LINEAR_KEYS,
        reconnect_from=before[-1].seq,
    )
    assert facts["run"]["output"] == '"Echo: Echo: printer on fire"'
    ticket = facts["tickets"][0]
    assert ticket.title == "Workflow ticket" and ticket.body == "Echo: printer on fire"
    t_out = next(e for e in facts["events"] if e.event_key == "step:t:completed").data["output"]
    assert t_out["ticket_id"] == ticket.ticket_id
    report(
        f"W1 workflow_mid_step[{step}]: completed once, tickets=1 calls={facts['calls']}, "
        f"children={len(facts['children'])}, events=0..{len(facts['events']) - 1}"
    )


# --------------------------------------------------------------------------- W2: kill after the tool call


def test_w2_kill_after_side_effecting_tool_call_needs_attention(wf: Harness, report: Callable[[str], None]) -> None:
    """kill -9 after tickets.create returned, before its result is recorded: the outcome is unknown to
    us, so recovery ends the run needs_attention without calling the tool again (one ticket)."""
    h = wf
    workflow_id = h.create_published_workflow(linear_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id, before = crash_run(h, workflow_id, crash_at="after_workflow_tool_call", event_key="step:t:call")
    assert _keys(before) == ["start", "step:ask:started", "step:ask:completed", "step:t:started"]
    assert len(h.tickets(run_id)) == 1 and h.ticket_calls(run_id) == 1
    assert "reserved" in h.side_effect_states()

    recover(h)
    facts = assert_exactly_once(
        h, run_id, status="needs_attention", agent_steps=["ask"], tickets=1, calls=1,
        keys=["start", "step:ask:started", "step:ask:completed", "step:t:started", "step:t:failed", "end"],
        reconnect_from=before[-1].seq,
    )
    failed = next(e for e in facts["events"] if e.event_key == "step:t:failed").data
    assert failed["error_code"] == "side_effect_outcome_unknown" and failed["status"] == "needs_attention"
    end = facts["events"][-1].data
    assert end["status"] == "needs_attention" and end["step_id"] == "t"
    assert facts["run"]["error"].startswith("SideEffectOutcomeUnknown")
    report(
        f"W2 after_workflow_tool_call: needs_attention once, tickets=1 calls={facts['calls']} (not re-called), "
        f"side_effects={sorted(h.side_effect_states())}"
    )


# --------------------------------------------------------------------------- W3: kill inside a parallel branch


@pytest.mark.parametrize(
    ("crash_at", "event_key", "status"),
    [
        ("workflow_mid_step", "step:b2:started", "completed"),
        ("workflow_mid_step", "step:join:started", "completed"),
        ("after_workflow_tool_call", "step:b2:call", "needs_attention"),
    ],
    ids=["branch-b-tool", "join", "branch-b-after-tool-call"],
)
def test_w3_kill_inside_a_parallel_branch(
    wf: Harness, report: Callable[[str], None], crash_at: str, event_key: str, status: str
) -> None:
    """kill -9 inside branch b (before its side effect, or after it), or at the join.

    Branch a is `test_w3_kill_inside_branch_a_while_sibling_ticket_is_gated`: killing at
    `step:a:started` while branch b can already be inside tickets.create made the terminal status
    depend on scheduling."""
    h = wf
    workflow_id = h.create_published_workflow(parallel_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id, before = crash_run(h, workflow_id, crash_at=crash_at, event_key=event_key)
    crashed_key = event_key.replace(":call", ":started")
    assert crashed_key in _keys(before)
    recover(h)

    if status == "completed":
        key_set = {"start", "step:fan:started", *PARALLEL_BRANCH_KEYS, "step:fan:completed",
                   "step:join:started", "step:join:completed", "end"}
        agent_steps = ["a", "b1", "join"]
    else:
        key_set = {"start", "step:fan:started", "step:a:started", "step:a:completed", "step:b1:started",
                   "step:b1:completed", "step:b2:started", "step:b2:failed", "step:fan:failed", "end"}
        agent_steps = ["a", "b1"]
    facts = assert_exactly_once(
        h, run_id, status=status, agent_steps=agent_steps, tickets=1, calls=1, key_set=key_set,
        branches=("fan:a", "fan:b1"), reconnect_from=before[-1].seq,
    )
    ks = _keys(facts["events"])
    fan_end = ks.index("step:fan:completed" if status == "completed" else "step:fan:failed")
    for key in ks:
        if key.startswith(("step:a:", "step:b1:", "step:b2:")):
            assert ks.index("step:fan:started") < ks.index(key) < fan_end, ks
    assert ks.index("step:b1:completed") < ks.index("step:b2:started")
    if status == "completed":
        assert fan_end < ks.index("step:join:started")
        answer = json.loads(facts["run"]["output"])
        payload = json.loads(answer.removeprefix("Echo: "))
        ticket = facts["tickets"][0]
        assert payload == {"left": "Echo: left", "right": {"ticket_id": ticket.ticket_id, "created": True}}
        assert ticket.body == "Echo: printer on fire"
    else:
        assert facts["events"][-1].data["status"] == "needs_attention"
        b2 = next(e for e in facts["events"] if e.event_key == "step:b2:failed").data
        assert b2["error_code"] == "side_effect_outcome_unknown"
    report(
        f"W3 {crash_at}[{event_key}]: {status} once, tickets=1 calls={facts['calls']}, "
        f"children={len(facts['children'])}, branch workflows={len(h.workflows_with_prefix(run_id)) - 1}"
    )


BRANCH_A_KEYS = {
    "start", "step:fan:started",
    "step:a:started", "step:a:completed",
    "step:hold:started", "step:hold:waiting", "step:hold:decision", "step:hold:completed",
    "step:b2:started", "step:b2:completed",
    "step:fan:completed",
    "step:join:started", "step:join:completed",
    "end",
}


def test_w3_kill_inside_branch_a_while_sibling_ticket_is_gated(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """kill -9 at step:a:started. Branch b's ticket sits behind an approval that is still open, so
    the kill cannot land during that call: recovery always completes, and the ticket happens once
    after the approval."""
    h = wf
    workflow_id = h.create_published_workflow(parallel_spec_branch_a_gated(h.agent_id))  # type: ignore[attr-defined]
    run_id, before = crash_run(h, workflow_id, crash_at="workflow_mid_step", event_key="step:a:started")
    before_keys = _keys(before)
    assert "step:a:started" in before_keys
    assert "step:b2:started" not in before_keys  # the gate has not released the ticket
    assert h.tickets(run_id) == [] and h.ticket_calls(run_id) == 0

    recover(h)
    waiting = h.wait_event(run_id, "step:hold:waiting")
    assert waiting.data["workflow_id"] == f"{run_id}:fan:hold" and waiting.data["prompt"] == "Release branch b?"
    approved = h.decide(run_id, "hold", comment="go")
    assert approved.status_code == 200, approved.text
    assert approved.json()["decision"] == "approved"

    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["a", "join"], tickets=1, calls=1,
        key_set=BRANCH_A_KEYS, branches=("fan:a", "fan:hold"), reconnect_from=before[-1].seq,
    )
    ks = _keys(facts["events"])
    fan_end = ks.index("step:fan:completed")
    for key in ks:
        if key.startswith(("step:a:", "step:hold:", "step:b2:")):
            assert ks.index("step:fan:started") < ks.index(key) < fan_end, ks
    assert ks.index("step:hold:completed") < ks.index("step:b2:started")
    assert fan_end < ks.index("step:join:started")
    assert ks.count("step:hold:decision") == 1
    decision = next(e for e in facts["events"] if e.event_key == "step:hold:decision")
    assert decision.type == "step_approved" and decision.data["decided_by"] == "alice"
    answer = json.loads(facts["run"]["output"])
    payload = json.loads(answer.removeprefix("Echo: "))
    ticket = facts["tickets"][0]
    assert payload == {"left": "Echo: left", "right": {"ticket_id": ticket.ticket_id, "created": True}}
    assert ticket.title == "Branch ticket" and ticket.body == "gated"
    assert h.decide(run_id, "hold").status_code == 409
    report(
        "W3 workflow_mid_step[step:a:started], sibling ticket gated: completed once after approval, "
        f"tickets=1 calls={facts['calls']}, no ticket in flight at the kill"
    )


# --------------------------------------------------------------------------- W4: kill while waiting for approval


@pytest.mark.parametrize("when", ["approve-after-restart", "approve-while-worker-down"])
def test_w4_kill_while_waiting_for_approval(wf: Harness, report: Callable[[str], None], when: str) -> None:
    """kill -9 right after step_waiting is recorded; approve over HTTP after the restart, or while no
    worker is running at all. Either way the decision is not lost and the run finishes once."""
    h = wf
    workflow_id = h.create_published_workflow(approval_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id, before = crash_run(h, workflow_id, crash_at="workflow_waiting", event_key="step:approve:waiting")
    assert _keys(before) == APPROVAL_KEYS[:7]
    waiting = before[-1].data
    assert waiting["workflow_id"] == run_id and waiting["prompt"] == "Ship it?" and waiting["deadline"]
    assert h.ticket_calls(run_id) == 1

    if when == "approve-after-restart":
        recover(h)
        time.sleep(2.0)  # the recovered run is waiting again (2 poll rounds), with no decision
        assert h.run_row(run_id)["status"] == "running"
        assert "step:approve:decision" not in _keys(h.events(run_id))
        approved = h.decide(run_id, "approve", comment="go")
    else:
        approved = h.decide(run_id, "approve", comment="go")
        decision = h.wait_event(run_id, "step:approve:decision", timeout=5)
        assert decision.type == "step_approved"
        time.sleep(1.0)  # nothing runs it while the worker is down
        assert h.run_row(run_id)["status"] == "running" and "step:approve:completed" not in _keys(h.events(run_id))
        recover(h)
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["decision"], body["decided_by"], body["comment"], body["step_id"]) == ("approved", "alice", "go", "approve")
    assert body["notified"] is True  # DBOS send needs only the system database, not a live worker

    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["ask", "done"], tickets=1, calls=1, keys=APPROVAL_KEYS,
        reconnect_from=before[-1].seq,
    )
    assert facts["run"]["output"] == '"Echo: Echo: printer on fire"'
    decision = next(e for e in facts["events"] if e.event_key == "step:approve:decision")
    assert decision.type == "step_approved" and decision.data["decided_by"] == "alice"
    out = next(e for e in facts["events"] if e.event_key == "step:approve:completed").data["output"]
    assert out == {"approved": True, "decided_by": "alice", "comment": "go"}
    # The decision cannot be made twice, nor reversed, once recorded.
    for verb in ("approve", "reject"):
        again = h.decide(run_id, "approve", verb, comment="again")
        assert again.status_code == 409, again.text
    assert len(h.events(run_id)) == len(facts["events"])
    report(f"W4 workflow_waiting, {when}: completed once, one decision (repeat 409), tickets=1, children=2")


def test_w4c_sigkill_while_a_parallel_branch_waits_then_approve_while_down(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """A healthy worker is SIGKILLed while a parallel branch's approval is really waiting (in its
    DBOS recv), the step is approved while no worker runs, then a fresh worker resumes the branch
    workflow (the decision's wake-up target) and the run finishes once."""
    h = wf
    spec = {
        "name": "Gate",
        "input_schema": Q_SCHEMA,
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["gate", "b"], "join": "done"},
            {"id": "gate", "type": "approval", "prompt": "Release?", "timeout_hours": 1, "next": "done"},
            {"id": "b", "type": "agent", "agent_id": h.agent_id, "input": {"prompt": {"$ref": "input.q"}},  # type: ignore[attr-defined]
             "next": "done"},
            {
                "id": "done", "type": "agent", "agent_id": h.agent_id,  # type: ignore[attr-defined]
                "input": {"gate": {"$ref": "steps.gate.output"}, "right": {"$ref": "steps.b.output"}},
            },
        ],
    }
    workflow_id = h.create_published_workflow(spec)
    first = h.start_worker("worker1", extra_env=WF_ENV)
    run_id = start_run(h, workflow_id)
    waiting = h.wait_event(run_id, "step:gate:waiting")
    h.wait_event(run_id, "step:b:completed")
    # The exact point: the branch workflow has recorded its step_waiting event AND checkpointed the
    # first decision check after it (eap.workflow_approval_decision), so it is in its wait loop.
    branch = f"{run_id}:fan:gate"
    wait_for(lambda: any(name == "eap.workflow_approval_decision" for _, name in h.dbos_steps(branch)),
             timeout=30, what="the branch's first approval decision check")
    assert "step:gate:decision" not in _keys(h.events(run_id))
    first.proc.kill()
    assert first.proc.wait(timeout=10) == -9
    assert waiting.data["workflow_id"] == f"{run_id}:fan:gate"
    before = h.events(run_id)
    assert h.run_row(run_id)["status"] == "running" and "end" not in _keys(before)

    approved = h.decide(run_id, "gate", comment="ok")
    assert approved.status_code == 200 and approved.json()["notified"] is True, approved.text
    recover(h)
    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["b", "done"], tickets=0, calls=0,
        key_set={"start", "step:fan:started", "step:gate:started", "step:gate:waiting", "step:gate:decision",
                 "step:gate:completed", "step:b:started", "step:b:completed", "step:fan:completed",
                 "step:done:started", "step:done:completed", "end"},
        branches=("fan:gate", "fan:b"), reconnect_from=before[-1].seq,
    )
    payload = json.loads(json.loads(facts["run"]["output"]).removeprefix("Echo: "))
    assert payload == {"gate": {"approved": True, "decided_by": "alice", "comment": "ok"},
                       "right": "Echo: printer on fire"}
    assert h.decide(run_id, "gate").status_code == 409
    report("W4c SIGKILL while a branch waits, approved while down: completed once via the branch workflow")


# One recorded hour. Long enough that the worker can be killed and a fresh one can be ready while
# the deadline is still ahead, short enough that approving just before it races the timeout.
_RACE_HOUR_S = 20.0
_RACE_ENV: dict[str, str | None] = {
    "EAP_APPROVAL_POLL_SECONDS": "0.25",
    "EAP_WORKFLOW_HOUR_SECONDS": str(_RACE_HOUR_S),
}
_RACE_APPROVED_KEYS = [
    "start",
    "step:hold:started", "step:hold:waiting", "step:hold:decision", "step:hold:completed",
    "step:done:started", "step:done:completed",
    "end",
]
_RACE_TIMEOUT_KEYS = [
    "start",
    "step:hold:started", "step:hold:waiting", "step:hold:decision", "step:hold:failed",
    "end",
]


def test_w4d_approval_timeout_races_approve_across_a_crash(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """Kill while an approval is waiting under a short hour. Restart, then approve within a second
    of the deadline the first worker recorded. Exactly one decision event is written, and the final
    status is the one that decision names: completed if the approval won, failed approval_timeout
    if the deadline won."""
    h = wf
    spec = {
        "name": "Deadline race",
        "input_schema": {"type": "object"},
        "start": "hold",
        "steps": [
            {"id": "hold", "type": "approval", "prompt": "Now?", "timeout_hours": 1, "next": "done"},
            {
                "id": "done", "type": "agent", "agent_id": h.agent_id,  # type: ignore[attr-defined]
                "input": {"prompt": "after"},
            },
        ],
    }
    workflow_id = h.create_published_workflow(spec)
    first = h.start_worker(
        "worker1-crashing", crash_at="workflow_waiting", event_key="step:hold:waiting", extra_env=_RACE_ENV
    )
    run_id = start_run(h, workflow_id, {})
    assert first.wait_exit() == 137, first.log()[-4000:]
    assert h.crash_marker.exists()
    waiting = next(e for e in h.events(run_id) if e.event_key == "step:hold:waiting")
    deadline = datetime.fromisoformat(waiting.data["deadline"])
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    deadline_ts = deadline.timestamp()
    ahead = deadline_ts - time.time()
    assert _RACE_HOUR_S - 8 < ahead < _RACE_HOUR_S + 2, (ahead, waiting.data["deadline"])
    assert h.run_row(run_id)["status"] == "running"
    assert _keys(h.events(run_id)).count("step:hold:decision") == 0

    second = h.start_worker("worker2-recovering", extra_env=_RACE_ENV)
    assert second.proc.poll() is None
    remaining = deadline_ts - time.time()
    assert remaining > 4, f"restart left {remaining:.1f}s before the recorded deadline"
    time.sleep(remaining - 0.5)  # approve in the last half-second, while the worker is polling
    # Still waiting right before the decision: a timeout recorded early would show here.
    assert h.run_row(run_id)["status"] == "running"
    assert "step:hold:decision" not in _keys(h.events(run_id)), "decided before the recorded deadline"
    sent_at = time.time()
    approved = h.decide(run_id, "hold", comment="near")
    skew = sent_at - deadline_ts
    assert -2.0 < skew < 2.0, f"approve was {skew:.2f}s from the deadline, not near it"

    if approved.status_code == 200:
        body = approved.json()
        assert (body["decision"], body["decided_by"], body["comment"]) == ("approved", "alice", "near")
        facts = assert_exactly_once(
            h, run_id, status="completed", agent_steps=["done"], tickets=0, calls=0,
            keys=_RACE_APPROVED_KEYS, reconnect_from=waiting.seq,
        )
        decision = next(e for e in facts["events"] if e.event_key == "step:hold:decision")
        assert decision.type == "step_approved" and decision.data["comment"] == "near"
        assert decision.data["decided_by"] == "alice"
        assert facts["run"]["output"] == '"Echo: after"'
        assert "step:hold:failed" not in _keys(facts["events"])
        winner = "approved"
    elif approved.status_code == 409:
        facts = assert_exactly_once(
            h, run_id, status="failed", agent_steps=[], tickets=0, calls=0,
            keys=_RACE_TIMEOUT_KEYS, reconnect_from=waiting.seq,
        )
        decision = next(e for e in facts["events"] if e.event_key == "step:hold:decision")
        assert decision.type == "step_timed_out"
        # The timeout was written at the recorded deadline: never before it, and within ~1 s after.
        decided_at = decision.ts.timestamp()
        assert deadline_ts - 0.1 <= decided_at <= deadline_ts + 1.0, (
            f"step_timed_out written {decided_at - deadline_ts:+.2f}s from the recorded deadline"
        )
        assert facts["events"][-1].data["error_code"] == "approval_timeout"
        assert facts["events"][-1].data["step_id"] == "hold"
        assert facts["run"]["error"] == "WorkflowStepError: the approval was not decided before its timeout"
        assert "step:done:started" not in _keys(facts["events"])
        winner = "timed_out"
    else:
        raise AssertionError(approved.text)
    assert _keys(facts["events"]).count("step:hold:decision") == 1
    assert _keys(facts["events"]).count("end") == 1
    assert h.decide(run_id, "hold", comment="again").status_code == 409
    assert len(h.events(run_id)) == len(facts["events"])
    report(
        f"W4d timeout races approve across a crash: one decision ({winner}), "
        f"status={facts['run']['status']}, approve {skew:+.2f}s from the recorded deadline"
    )


def test_w4e_approve_after_the_recorded_deadline_loses(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """Same crash and 20 s hour as W4d, but the approve is sent only after the recorded deadline,
    once the worker's poll has written step_timed_out. That late approve is 409. Exactly one
    decision event exists, its ts is inside the deadline window, and the run fails approval_timeout.
    W4d stays the near-deadline race; this is the timeout path every time."""
    h = wf
    spec = {
        "name": "Deadline passed",
        "input_schema": {"type": "object"},
        "start": "hold",
        "steps": [
            {"id": "hold", "type": "approval", "prompt": "Now?", "timeout_hours": 1, "next": "done"},
            {
                "id": "done", "type": "agent", "agent_id": h.agent_id,  # type: ignore[attr-defined]
                "input": {"prompt": "after"},
            },
        ],
    }
    workflow_id = h.create_published_workflow(spec)
    first = h.start_worker(
        "worker1-crashing", crash_at="workflow_waiting", event_key="step:hold:waiting", extra_env=_RACE_ENV
    )
    run_id = start_run(h, workflow_id, {})
    assert first.wait_exit() == 137, first.log()[-4000:]
    assert h.crash_marker.exists()
    waiting = next(e for e in h.events(run_id) if e.event_key == "step:hold:waiting")
    deadline = datetime.fromisoformat(waiting.data["deadline"])
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    deadline_ts = deadline.timestamp()
    ahead = deadline_ts - time.time()
    assert _RACE_HOUR_S - 8 < ahead < _RACE_HOUR_S + 2, (ahead, waiting.data["deadline"])
    assert h.run_row(run_id)["status"] == "running"
    assert _keys(h.events(run_id)).count("step:hold:decision") == 0

    second = h.start_worker("worker2-recovering", extra_env=_RACE_ENV)
    assert second.proc.poll() is None
    remaining = deadline_ts - time.time()
    assert remaining > 4, f"restart left {remaining:.1f}s before the recorded deadline"
    # Still waiting with the deadline ahead: a timeout recorded early would show here.
    time.sleep(remaining - 1.0)
    assert time.time() < deadline_ts
    assert h.run_row(run_id)["status"] == "running"
    assert "step:hold:decision" not in _keys(h.events(run_id)), "decided before the recorded deadline"

    # The 0.25 s poll writes step_timed_out on the first look after the deadline. Wait for that
    # event (inside the window W4d allows) and only then send the approve.
    decision = None
    while time.time() < deadline_ts + 1.15:
        if time.time() >= deadline_ts:
            found = [e for e in h.events(run_id) if e.event_key == "step:hold:decision"]
            if found:
                decision = found[0]
                break
        time.sleep(0.05)
    assert decision is not None, "worker did not record step:hold:decision after the deadline"
    assert _keys(h.events(run_id)).count("step:hold:decision") == 1
    assert decision.type == "step_timed_out"
    decided_at = decision.ts.timestamp()
    assert deadline_ts - 0.1 <= decided_at <= deadline_ts + 1.0, (
        f"step_timed_out written {decided_at - deadline_ts:+.2f}s from the recorded deadline"
    )
    sent_at = time.time()
    approved = h.decide(run_id, "hold", comment="late")
    skew = sent_at - deadline_ts
    assert skew > 0, f"approve was {skew:.2f}s from the deadline, not after it"
    assert approved.status_code == 409, approved.text

    facts = assert_exactly_once(
        h, run_id, status="failed", agent_steps=[], tickets=0, calls=0,
        keys=_RACE_TIMEOUT_KEYS, reconnect_from=waiting.seq,
    )
    recorded = next(e for e in facts["events"] if e.event_key == "step:hold:decision")
    assert recorded.type == "step_timed_out" and recorded.seq == decision.seq
    assert facts["events"][-1].data["error_code"] == "approval_timeout"
    assert facts["events"][-1].data["step_id"] == "hold"
    assert facts["run"]["error"] == "WorkflowStepError: the approval was not decided before its timeout"
    assert "step:done:started" not in _keys(facts["events"])
    assert _keys(facts["events"]).count("step:hold:decision") == 1
    assert _keys(facts["events"]).count("end") == 1
    assert h.decide(run_id, "hold", comment="again").status_code == 409
    assert len(h.events(run_id)) == len(facts["events"])
    report(
        f"W4e approve after the recorded deadline: 409, one step_timed_out "
        f"({decided_at - deadline_ts:+.2f}s), status=failed approval_timeout, approve {skew:+.2f}s"
    )


# --------------------------------------------------------------------------- W5: child row inserted, then v2


def test_w5_kill_after_child_run_insert_then_publish_v2(wf: Harness, report: Callable[[str], None]) -> None:
    """kill -9 after the child agent run row (naming v1) is inserted, before the step checkpoints;
    v2 is published over HTTP while the worker is down; the recovered child still runs v1."""
    h = wf
    spec = {
        "name": "One agent",
        "input_schema": {"type": "object"},
        "start": "ask",
        "steps": [{"id": "ask", "type": "agent", "agent_id": h.agent_id, "input": {"prompt": "hello"}}],  # type: ignore[attr-defined]
    }
    workflow_id = h.create_published_workflow(spec)
    run_id, before = crash_run(
        h, workflow_id, crash_at="after_child_run_insert", event_key="step:ask:child", input_value={}
    )
    assert _keys(before) == ["start", "step:ask:started"]
    children = h.child_runs(run_id)
    assert [(c[1], c[3]) for c in children] == [("ask", 1)], children
    assert h.publish_agent_version(h.agent_id, HISTORY_SPEC) == 2  # type: ignore[attr-defined]

    recover(h)
    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["ask"], tickets=0, calls=0,
        keys=["start", "step:ask:started", "step:ask:completed", "end"], reconnect_from=before[-1].seq,
    )
    assert facts["run"]["output"] == '"Echo: hello"'  # v1 (echo), not v2 (history: "Turn 1. ...")
    child_id = children[0][0]
    assert facts["children"][0][3] == 1
    child = h.client.get(f"/api/v1/runs/{child_id}", headers=h.headers).json()
    assert child["version"] == 1 and child["parent_run_id"] == run_id and child["parent_step_id"] == "ask"
    report("W5 after_child_run_insert + v2 published: child ran v1 once, output 'Echo: hello'")


# --------------------------------------------------------------------------- W6: the API process dies


def _crashing_api(h: Harness, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("crashing_workflow_api.py")), args[0], h.tenant, *args[1:]],
        cwd=REPO_ROOT, env=h.process_env(WF_ENV), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 137, result.stdout + result.stderr
    assert f"EAP_API_CRASH {args[0]} launcher reached" in result.stdout
    return result


def test_w6a_api_dies_inside_the_workflow_launcher_then_client_retries(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The API process dies in launch_workflow, after the runs row committed and before the enqueue;
    the client retries the same POST (same run_id) against the real API: one run, completed once."""
    h = wf
    worker = h.start_worker("worker", extra_env=WF_ENV)
    workflow_id = h.create_published_workflow(linear_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id = str(uuid.uuid4())
    h.run_ids.append(run_id)
    _crashing_api(h, "run", workflow_id, run_id, json.dumps(INPUT))

    # The orphan: a committed kind='workflow' row that no workflow picks up on its own.
    assert h.run_row(run_id)["status"] == "queued"
    time.sleep(2.0)  # 10 queue polls of a live worker
    assert h.workflows_with_prefix(run_id) == [] and h.events(run_id) == [] and h.ticket_calls(run_id) == 0
    assert h.run_row(run_id)["status"] == "queued"

    retry = h.post_workflow_run(workflow_id, run_id, INPUT)
    assert retry.status_code == 202 and retry.json()["run_id"] == run_id, retry.text
    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["ask", "done"], tickets=1, calls=1, keys=LINEAR_KEYS,
    )

    again = h.post_workflow_run(workflow_id, run_id, INPUT)  # after completion: attaches, starts nothing
    assert again.status_code == 202, again.text
    time.sleep(1.0)
    assert [w[2] for w in h.workflows_with_prefix(run_id)] == ["SUCCESS"]
    assert h.events(run_id) == facts["events"] and h.ticket_calls(run_id) == 1
    assert h.sql("SELECT count(*) FROM runs WHERE id = :r", r=run_id)[0][0] == 1
    other = h.post_workflow_run(workflow_id, run_id, {"q": "something else"})
    assert other.status_code == 409, other.text  # same run_id, different request
    assert worker.proc.poll() is None
    report("W6a API dies in launch_workflow: orphan stayed queued, same-run_id retry 202 -> completed once; "
           "extra retry no-op; different input 409")


def test_w6b_api_dies_after_the_decision_before_the_wake_up(wf: Harness, report: Callable[[str], None]) -> None:
    """The API process dies in notify_approval: the decision event committed, the DBOS wake-up was
    never sent. The waiting run still finds the decision (poll) and finishes once."""
    h = wf
    h.start_worker("worker", extra_env=WF_ENV)
    workflow_id = h.create_published_workflow(approval_spec(h.agent_id))  # type: ignore[attr-defined]
    run_id = start_run(h, workflow_id)
    h.wait_event(run_id, "step:approve:waiting")
    _crashing_api(h, "approve", run_id, "approve")
    decision = h.wait_event(run_id, "step:approve:decision", timeout=5)
    assert decision.type == "step_approved" and decision.data["comment"] == "lost wake-up"

    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["ask", "done"], tickets=1, calls=1, keys=APPROVAL_KEYS,
    )
    assert h.decide(run_id, "approve").status_code == 409
    out = next(e for e in facts["events"] if e.event_key == "step:approve:completed").data["output"]
    assert out == {"approved": True, "decided_by": "alice", "comment": "lost wake-up"}
    report("W6b API dies after the decision, before the wake-up: decision kept, picked up by poll, completed once")


# --------------------------------------------------------------------------- W7: kill during an MCP call inside a workflow agent step


_W7_KEYS = ["start", "step:ask:started", "step:ask:failed", "end"]


def test_w7_kill_during_mcp_tool_call_inside_a_workflow(
    harness: Harness, report: Callable[[str], None]
) -> None:
    """A workflow agent step runs an agent whose only tool is a side-effecting MCP call. kill -9
    after the server answered and before the side effect is recorded: the workflow and the child
    run each end needs_attention once, and the server is not called again."""
    from test_mcp_safety import LocalMcpServer, assert_no_secret, secrets_env

    h = harness
    env = secrets_env()
    sentinel = f"sntl{uuid.uuid4().hex}w7"
    request = json.dumps({"tool": "cancel_order", "args": {"order_id": "W7"}})
    with LocalMcpServer(required_auth=f"Bearer {sentinel}") as server:
        h.start_api(extra_env=env)
        created = h.client.post(
            "/api/v1/mcp-servers",
            json={"name": "orders", "url": server.url, "headers": {"Authorization": f"Bearer {sentinel}"}},
            headers=h.headers,
        )
        assert created.status_code == 201, created.text
        agent_id = h.create_published_agent({
            "name": "MCP agent",
            "instructions": "Use the MCP tool.",
            "model": {"provider": "test", "name": "mcp"},
            "tools": [],
            "mcp_tools": [{"server": "orders", "tool": "cancel_order"}],
        })
        workflow_id = h.create_published_workflow({
            "name": "MCP step",
            "input_schema": {"type": "object"},
            "start": "ask",
            "steps": [{
                "id": "ask", "type": "agent", "agent_id": agent_id,
                "input": {"prompt": request},
            }],
        })
        first = h.start_worker("worker1-crashing", crash_at="after_tool_call", extra_env=env)
        run_id = start_run(h, workflow_id, {})
        assert first.wait_exit() == 137, first.log()[-4000:]
        assert h.crash_marker.exists()
        assert "EAP_CRASH_AT=after_tool_call reached" in first.log()
        assert server.call_count("cancel_order") == 1
        assert server.tool_call_posts.count("cancel_order") == 1
        requests_before = len(server.seen_headers)
        assert h.side_effect_states() == ["reserved"]
        before = h.events(run_id)
        assert "end" not in _keys(before) and h.run_row(run_id)["status"] == "running"

        second = h.start_worker("worker2-recovering", extra_env=env)
        facts = assert_exactly_once(
            h, run_id, status="needs_attention", agent_steps=["ask"], tickets=0, calls=0,
            keys=_W7_KEYS, child_terminal="needs_attention", reconnect_from=before[-1].seq,
        )
        time.sleep(0.5)  # a re-send would POST tools/call again within ~250 ms
        assert server.call_count("cancel_order") == 1
        assert server.tool_call_posts.count("cancel_order") == 1
        assert len(server.seen_headers) == requests_before
        assert h.side_effect_states() == ["reserved"]

    failed = next(e for e in facts["events"] if e.event_key == "step:ask:failed").data
    assert failed["error_code"] == "side_effect_outcome_unknown" and failed["status"] == "needs_attention"
    assert facts["events"][-1].data["status"] == "needs_attention"
    assert str(facts["run"]["error"]).startswith("SideEffectOutcomeUnknown")
    child_id = facts["children"][0][0]
    child_events = h.events(child_id)
    assert child_events[-1].type == "run_failed"
    assert child_events[-1].data["status"] == "needs_attention"
    assert child_events[-1].data["error_code"] == "side_effect_outcome_unknown"
    assert sum(1 for e in child_events if e.type == "tool_call") == 1
    assert _keys(child_events).count("end") == 1
    for name, text_ in (("crashed worker log", first.log()), ("recovering worker log", second.log())):
        assert_no_secret(name, text_, [sentinel])
    report(
        "W7 after_tool_call inside a workflow agent step's MCP call: needs_attention once, "
        "child needs_attention once, server called once, no request after recovery"
    )
