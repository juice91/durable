"""A graceful worker shutdown (SIGTERM) in the middle of a parallel step, whole-system.

`DBOS.destroy` at shutdown cancels the running workflow tasks with a plain asyncio CancelledError.
The parallel step (eap_worker.workflow_run `_parallel`) must let that propagate untouched: it may
cascade a cancel to its branch workflows only for a real cancel of the run
(DBOSWorkflowCancelledError). Cancelling the branches at shutdown would durably cancel them and
record `DBOS.cancelWorkflow` operations in the parent that the next worker's replay does not make,
failing the run with DBOSUnexpectedStepError.

The workflow fans out to three branches, awaited in declaration order: `gate` (an approval, still
waiting at the shutdown, so the parent is blocked awaiting it), `a` (an agent step) and `b1` (an
agent step) -> `b2` (tickets.create). The worker is stopped with SIGTERM while `gate` waits in its
DBOS recv; a fresh worker (same executor id) recovers the run; the gate is approved over HTTP. The
run completes once: every branch workflow once in SUCCESS, one child agent run per agent step, one
ticket, no cancel operation recorded in the parent, gap-free events, and SSE resume equal to the DB.
"""

from __future__ import annotations

import json
import signal
from collections.abc import Callable

from conftest import Harness, wait_for
from test_workflow_crash import Q_SCHEMA, WF_ENV, _keys, assert_exactly_once, recover, start_run, wf  # noqa: F401

KEYS = {
    "start", "step:fan:started",
    "step:gate:started", "step:gate:waiting", "step:gate:decision", "step:gate:completed",
    "step:a:started", "step:a:completed",
    "step:b1:started", "step:b1:completed", "step:b2:started", "step:b2:completed",
    "step:fan:completed", "step:done:started", "step:done:completed", "end",
}


def three_branches(agent_id: str) -> dict:
    return {
        "name": "Fan three",
        "input_schema": Q_SCHEMA,
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["gate", "a", "b1"], "join": "done"},
            {"id": "gate", "type": "approval", "prompt": "Release?", "timeout_hours": 1, "next": "done"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"prompt": "left"}, "next": "done"},
            {"id": "b1", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}, "next": "b2"},
            {"id": "b2", "type": "tool", "tool_id": "tickets.create",
             "input": {"title": "Branch ticket", "body": {"$ref": "steps.b1.output"}}, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": agent_id,
             "input": {"gate": {"$ref": "steps.gate.output"}, "left": {"$ref": "steps.a.output"},
                       "right": {"$ref": "steps.b2.output"}}},
        ],
    }


def test_sigterm_during_a_parallel_step_recovers_every_branch_once(wf: Harness, report: Callable[[str], None]) -> None:  # noqa: F811
    h = wf
    workflow_id = h.create_published_workflow(three_branches(h.agent_id))  # type: ignore[attr-defined]
    # A long approval poll: once `gate` blocks in DBOS.recv it stays there until the shutdown.
    first = h.start_worker("worker1", extra_env={**WF_ENV, "EAP_APPROVAL_POLL_SECONDS": "600"})
    run_id = start_run(h, workflow_id)
    h.wait_event(run_id, "step:gate:waiting")
    h.wait_event(run_id, "step:a:completed")
    h.wait_event(run_id, "step:b2:completed")
    branch = f"{run_id}:fan:gate"
    wait_for(lambda: [n for _, n in h.dbos_steps(branch)][-1:] == ["DBOS.sleep"], timeout=30,
             what="the gate branch to block in DBOS.recv")

    first.proc.send_signal(signal.SIGTERM)  # graceful: DBOS.destroy cancels the workflow tasks
    first.proc.wait(timeout=30)
    before = h.events(run_id)
    assert h.run_row(run_id)["status"] == "running" and "end" not in _keys(before)
    # Nothing was cancelled: the parent recorded no cancel operation, no branch is CANCELLED.
    assert not [n for _, n in h.dbos_steps(run_id) if "cancel" in n.lower()], h.dbos_steps(run_id)
    assert all(status != "CANCELLED" for _, _, status in h.workflows_with_prefix(f"{run_id}:")), \
        h.workflows_with_prefix(f"{run_id}:")

    recover(h)
    h.wait_event(run_id, "step:gate:waiting")
    approved = h.decide(run_id, "gate", comment="ok")
    assert approved.status_code == 200, approved.text
    facts = assert_exactly_once(
        h, run_id, status="completed", agent_steps=["a", "b1", "done"], tickets=1, calls=1, key_set=KEYS,
        branches=("fan:gate", "fan:a", "fan:b1"), reconnect_from=before[-1].seq,
    )
    assert not [n for _, n in h.dbos_steps(run_id) if "cancel" in n.lower()]
    payload = json.loads(json.loads(facts["run"]["output"]).removeprefix("Echo: "))
    assert payload["gate"] == {"approved": True, "decided_by": "alice", "comment": "ok"}
    assert payload["left"] == "Echo: left" and payload["right"]["created"] is True
    report("SIGTERM during a 3-branch parallel step: no branch cancelled; recovered and completed once, 1 ticket")
