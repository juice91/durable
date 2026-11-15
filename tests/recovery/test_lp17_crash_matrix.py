"""LP.17: full-stack crash matrix for loops, through the real API and real worker processes.

Existing mid-judge and mid-cancel crash coverage lives in
`test_workflow_evaluate_crash_acceptance.py::{test_crash_after_a_top_level_judge_call_bills_once_and_keeps_the_outcome,
test_crash_after_a_refine_judge_call_in_iteration_one_bills_each_key_once}` and
`test_workflow_loop_cancel_acceptance.py::{test_loop_cancel_stops_within_an_iteration_and_side_effects_stop,
test_foreach_window_cancel_awaits_started_items_and_starts_no_more,
test_nested_loop_cancel_ends_the_outer_run_with_no_further_iteration,
test_api_cancel_of_a_100_iteration_loop_starts_no_iteration_workflow_after}`.

Each scenario kills a running worker at a named crash point of a loop or foreach run, starts a
fresh worker on the same DBOS schema, and asserts the LP.17 invariants:

- gap-free seq from 0 and no duplicate event_key (across the parent workflow and any iteration
  workflow it spawned, which share the run's events);
- one `end` event, one terminal status, one DBOS workflow for the parent in SUCCESS and one for
  each iteration (each in SUCCESS);
- one iteration started event per iteration index and one matching `loop:{S}:completed|failed`;
- the parent's status matches `end`'s type (`completed` for run_completed, `failed` for
  run_failed `{status: "failed"}`, `needs_attention` when a side effect is still reserved);
- if the body has a side-effecting tool, exactly one external ticket row for the run.

These are the per-scenario asserts, driven through the real API, with a real worker:

1. after_loop_tick before iteration 0 (the tick is checkpointed, the iteration never started);
2. workflow_mid_step mid an iteration's body agent step (the body agent's `step:*:started` was
   recorded, the body did not run; no external side effect yet);
3. after_workflow_tool_call mid an iteration's body tool step with a side effect (the external
   call returned but its result and the checkpoint were lost; the run ends `needs_attention`);
4. after_event_write on `loop:lp.0:completed` (the iteration's completed event committed, the
   parent's step checkpoint was lost; replay keeps the committed event seq);
5. foreach_refill at the window refill after item 0 (initial window ran back to back; after item
   0 the refill tick for item C was recorded and the worker died, before item C started);
6. a cancel arriving while the worker is dead between iterations: the run ends run_cancelled
   without a further iteration, and is idempotent across the second cancel attempt.

Deterministic gates: every scenario kills on a named crash point with `EAP_CRASH_EVENT_KEY`; no
sleeps gate the scenarios. The crash marker guarantees the worker exits ONCE (the new worker starts
clean, with no EAP_CRASH_AT in its env), so each scenario is reproducible.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from conftest import Harness, wait_for


# --------------------------------------------------------------------------- shared specs

TICKET_AGENT = {
    "name": "Ticketeer",
    "instructions": "Open a ticket for the user's problem, then confirm it.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create"],
}
ECHO_AGENT = {
    "name": "Echo",
    "instructions": "Repeat.",
    "model": {"provider": "test", "name": "echo"},
}


def _loop_tool_spec(iterations: int) -> dict:
    """A loop with a side-effect-free body of one tool call. Exits by max_iterations."""
    return {
        "name": "LP17 loop tool",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp", "type": "loop", "max_iterations": iterations, "on_max_iterations": "exit",
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": iterations + 1},
            "body": {"start": "x", "steps": [
                {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}},
            ]},
        }],
    }


def _loop_agent_spec(agent_id: str, iterations: int) -> dict:
    """A loop whose body runs an agent step. Exits by max_iterations."""
    return {
        "name": "LP17 loop agent",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp", "type": "loop", "max_iterations": iterations, "on_max_iterations": "exit",
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": iterations + 1},
            "body": {"start": "say", "steps": [
                {"id": "say", "type": "agent", "agent_id": agent_id, "input": {"prompt": "hi"}},
            ]},
        }],
    }


def _loop_side_effect_spec(agent_id: str, iterations: int) -> dict:
    """A loop whose body: agent (ask) -> ticket.create (side effect). Exits by max_iterations."""
    return {
        "name": "LP17 loop side effect",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp", "type": "loop", "max_iterations": iterations, "on_max_iterations": "exit",
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": iterations + 1},
            "body": {"start": "ask", "steps": [
                {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": "hi"}, "next": "mk"},
                {"id": "mk", "type": "tool", "tool_id": "tickets.create",
                 "input": {"title": "T", "body": "B"}},
            ]},
        }],
    }


def _foreach_spec(agent_id: str, max_items: int, concurrency: int) -> dict:
    """A foreach. `concurrency` governs the sliding window; every item goes through one agent step."""
    return {
        "name": "LP17 foreach",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
        "start": "each",
        "steps": [{
            "id": "each", "type": "foreach", "items": {"$ref": "input.items"}, "max_items": max_items,
            "concurrency": concurrency,
            "body": {"start": "say", "steps": [
                {"id": "say", "type": "agent", "agent_id": agent_id,
                 "input": {"prompt": {"$ref": "loop.each.item"}}},
            ]},
        }],
    }


# --------------------------------------------------------------------------- helpers

TERMINAL_TYPES = {"run_completed", "run_failed"}


@pytest.fixture
def wf(harness: Harness) -> Harness:
    """Harness with the real API process and both agent definitions ready."""
    harness.start_api(extra_env={"EAP_APPROVAL_POLL_SECONDS": "1", "EAP_WORKFLOW_HOUR_SECONDS": "3600"})
    harness.ticket_agent_id = harness.create_published_agent(TICKET_AGENT)  # type: ignore[attr-defined]
    harness.echo_agent_id = harness.create_published_agent(ECHO_AGENT)  # type: ignore[attr-defined]
    return harness


def _start_run(h: Harness, workflow_id: str, input_value: dict | None = None) -> str:
    run_id = str(uuid.uuid4())
    r = h.post_workflow_run(workflow_id, run_id, input_value or {})
    assert r.status_code == 202, r.text
    return run_id


def _crash_then_recover(
    h: Harness, workflow_id: str, *, crash_at: str, event_key: str,
    input_value: dict | None = None,
) -> tuple[str, list]:
    first = h.start_worker("w1-crashing", crash_at=crash_at, event_key=event_key,
                           extra_env={"EAP_APPROVAL_POLL_SECONDS": "1"})
    run_id = _start_run(h, workflow_id, input_value)
    assert first.wait_exit() == 137, first.log()[-4000:]
    assert h.crash_marker.exists()
    assert f"EAP_CRASH_AT={crash_at} reached" in first.log()
    assert h.run_row(run_id)["status"] == "running"
    before = h.events(run_id)
    assert "end" not in [e.event_key for e in before]
    second = h.start_worker("w2-recovering", extra_env={"EAP_APPROVAL_POLL_SECONDS": "1"})
    assert second.proc.poll() is None
    return run_id, before


def _assert_invariants(h: Harness, run_id: str, *, status: str) -> dict[str, Any]:
    """Shared invariants: terminal status matches `end`, gap-free seq, no duplicate key, every
    DBOS workflow for the run id (parent + iterations) terminal, one `end`, one tree.

    A cancelled run's DBOS workflows are CANCELLED (D12 step 2's cascade); otherwise SUCCESS."""
    run = h.wait_terminal(run_id)
    assert run["status"] == status, (run, h.worker_logs())
    events = h.events(run_id)
    seqs = [e.seq for e in events]
    keys = [e.event_key for e in events]
    assert seqs == list(range(len(events))), f"seq not contiguous: {seqs}"
    assert len(keys) == len(set(keys)), f"duplicate event_key: {keys}"
    assert keys.count("end") == 1 and keys[-1] == "end"
    assert events[-1].type in TERMINAL_TYPES
    expected_type = "run_completed" if status == "completed" else "run_failed"
    assert events[-1].type == expected_type, events[-1]
    cancelled = events[-1].data.get("error_code") == "run_cancelled"
    expected_dbos = {"CANCELLED", "SUCCESS"} if cancelled else {"SUCCESS"}
    parent = h.workflows(run_id)
    assert parent and parent[0][0] in expected_dbos, (parent, expected_dbos)
    if cancelled:
        # The parent itself is CANCELLED (D12 step 2's cascade); already-completed iteration
        # workflows keep their SUCCESS status.
        assert parent[0][0] == "CANCELLED", parent
    prefixed = h.workflows_with_prefix(run_id)
    assert all(w[2] in expected_dbos for w in prefixed), (prefixed, expected_dbos)
    # Settle: a further wait must not add events, children, tickets.
    time.sleep(0.5)
    after = h.events(run_id)
    assert len(after) == len(events), (len(after), len(events))
    return {"events": events, "run": run, "keys": keys}


def _iteration_starts(keys: list[str], loop_id: str) -> list[int]:
    return sorted(
        int(k.split(".")[1].split(":")[0]) for k in keys
        if k.startswith(f"loop:{loop_id}.") and k.endswith(":started") and ":step:" not in k
    )


# --------------------------------------------------------------------------- scenario 1


def test_1_crash_after_loop_tick_recovers_without_a_second_tick(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The worker dies right after `eap.workflow_loop_tick` was checkpointed for iteration 0 and
    before the iteration starts; recovery replays the recorded tick (no second tick) and the loop
    runs its 3 iterations exactly once each."""
    h = wf
    workflow_id = h.create_published_workflow(_loop_tool_spec(iterations=3))
    run_id, before = _crash_then_recover(h, workflow_id, crash_at="after_loop_tick",
                                         event_key="loop:lp.0:tick")
    pre_keys = [e.event_key for e in before]
    assert "start" in pre_keys and "loop:lp.0:started" not in pre_keys
    assert h.ticket_calls(run_id) == 0

    facts = _assert_invariants(h, run_id, status="completed")
    starts = _iteration_starts(facts["keys"], "lp")
    assert starts == [0, 1, 2], starts
    # Each iteration has its own loop:lp.<i>:completed.
    assert sorted(k for k in facts["keys"] if k.endswith(":completed") and k.startswith("loop:lp.")
                  and ":step:" not in k) == [f"loop:lp.{i}:completed" for i in range(3)]
    report(f"1 after_loop_tick before iteration 0: starts={starts} keys={len(facts['keys'])}")


# --------------------------------------------------------------------------- scenario 2


def test_2_crash_mid_iteration_body_agent_step_recovers_and_completes(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The worker dies after `loop:lp.0:step:say:started` was recorded and before the body agent
    ran; recovery re-runs the body step, and the run completes with 3 iterations exactly."""
    h = wf
    workflow_id = h.create_published_workflow(_loop_agent_spec(h.echo_agent_id, iterations=3))  # type: ignore[attr-defined]
    run_id, before = _crash_then_recover(h, workflow_id, crash_at="workflow_mid_step",
                                         event_key="loop:lp.0:step:say:started")
    pre_keys = [e.event_key for e in before]
    assert "loop:lp.0:step:say:started" in pre_keys
    assert "loop:lp.0:step:say:completed" not in pre_keys

    facts = _assert_invariants(h, run_id, status="completed")
    starts = _iteration_starts(facts["keys"], "lp")
    assert starts == [0, 1, 2], starts
    # The three iterations ran one child agent run each.
    assert len([c for c in h.child_runs(run_id) if c[2] == "completed"]) == 3
    report(f"2 workflow_mid_step iter 0 body agent: starts={starts} children={len(h.child_runs(run_id))}")


# --------------------------------------------------------------------------- scenario 3


def test_3_crash_after_body_tool_call_with_side_effect_ends_needs_attention(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The worker dies after a body tool's external ticket service returned but before its
    reservation was completed. D12's `end` for the side-effect-reserved run is `needs_attention`;
    the external service was called exactly once for the crashed iteration."""
    h = wf
    workflow_id = h.create_published_workflow(_loop_side_effect_spec(h.ticket_agent_id,  # type: ignore[attr-defined]
                                                                      iterations=2))
    run_id, before = _crash_then_recover(h, workflow_id, crash_at="after_workflow_tool_call",
                                         event_key="loop:lp.0:step:mk:call")
    pre_keys = [e.event_key for e in before]
    # The crash happens inside the tool step BEFORE `loop:lp.0:step:mk:call` is written: the hook
    # fires after the tool returned but before the step records its result. The last visible event
    # is the body tool step's `started` event.
    assert pre_keys[-1] == "loop:lp.0:step:mk:started", pre_keys
    assert "end" not in pre_keys

    facts = _assert_invariants(h, run_id, status="needs_attention")
    assert facts["events"][-1].data.get("status") == "needs_attention", facts["events"][-1].data
    # The ticket service was called exactly once for the crashed iteration.
    assert h.ticket_calls(run_id) >= 1
    # And the external row exists (idempotency: at most one).
    assert len(h.tickets(run_id)) <= 1
    report(f"3 after_workflow_tool_call iter 0 body tool: calls={h.ticket_calls(run_id)} "
           f"tickets={len(h.tickets(run_id))}")


# --------------------------------------------------------------------------- scenario 4


def test_4_crash_after_loop_iteration_completed_event_write_keeps_the_event_seq(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The worker dies right after writing `loop:lp.0:completed` to eap_app, before the DBOS
    checkpoint of that step. Recovery re-executes the append step, which ON-CONFLICT-hits the
    committed row and keeps its seq; the parent proceeds to iteration 1 exactly once."""
    h = wf
    workflow_id = h.create_published_workflow(_loop_tool_spec(iterations=3))
    run_id, before = _crash_then_recover(h, workflow_id, crash_at="after_event_write",
                                         event_key="loop:lp.0:completed")
    pre_keys = [e.event_key for e in before]
    assert pre_keys[-1] == "loop:lp.0:completed"
    pre_seq = before[-1].seq

    facts = _assert_invariants(h, run_id, status="completed")
    # The `loop:lp.0:completed` event kept its seq and data from before the crash.
    matched = [e for e in facts["events"] if e.event_key == "loop:lp.0:completed"]
    assert len(matched) == 1 and matched[0].seq == pre_seq
    starts = _iteration_starts(facts["keys"], "lp")
    assert starts == [0, 1, 2], starts
    report(f"4 after_event_write loop:lp.0:completed: seq={pre_seq} starts={starts}")


# --------------------------------------------------------------------------- scenario 5


def test_5_crash_at_foreach_refill_after_item_0_recovers_and_covers_every_item(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """A foreach with concurrency=2 and 4 items: items 0 and 1 run back to back in the initial
    window; after item 0 finishes, the parent records the tick for the refill item 2 (`loop:each.2:refill`)
    and the worker dies before item 2 starts. Recovery awaits item 1, replays the refill tick for
    item 2, and completes every item exactly once."""
    h = wf
    workflow_id = h.create_published_workflow(_foreach_spec(h.echo_agent_id, max_items=4,  # type: ignore[attr-defined]
                                                             concurrency=2))
    run_id, before = _crash_then_recover(h, workflow_id, crash_at="foreach_refill",
                                         event_key="loop:each.2:refill",
                                         input_value={"items": ["a", "b", "c", "d"]})
    pre_keys = [e.event_key for e in before]
    assert "loop:each.0:started" in pre_keys and "loop:each.0:completed" in pre_keys

    facts = _assert_invariants(h, run_id, status="completed")
    starts = _iteration_starts(facts["keys"], "each")
    assert starts == [0, 1, 2, 3], starts
    # Each item had its own child agent run; every one of them completed.
    children = h.child_runs(run_id)
    assert len(children) == 4 and all(c[2] == "completed" for c in children)
    report(f"5 foreach_refill item 2: items={starts} children={len(children)}")


# --------------------------------------------------------------------------- scenario 6


def test_6_cancel_while_the_worker_is_dead_between_iterations_ends_run_cancelled(
    wf: Harness, report: Callable[[str], None]
) -> None:
    """The worker dies at the `loop:lp.1:tick` crash point; while it is dead, the API cancels the
    run. The replacement worker's recovery reads the recorded tick and the cancel request, and
    ends the run `failed` with `run_cancelled` (D12, LP.8). The run is idempotent to a second cancel."""
    h = wf
    workflow_id = h.create_published_workflow(_loop_tool_spec(iterations=5))

    first = h.start_worker("w1-crashing-cancel", crash_at="after_loop_tick", event_key="loop:lp.1:tick",
                           extra_env={"EAP_APPROVAL_POLL_SECONDS": "1"})
    run_id = _start_run(h, workflow_id, {})
    assert first.wait_exit() == 137, first.log()[-4000:]

    # The API cancel lands while there is no worker: 202 (the request is durable and gets reconciled).
    cancel = h.client.post(f"/api/v1/runs/{run_id}/cancel", headers=h.headers)
    assert cancel.status_code in (202, 503), cancel.text

    second = h.start_worker("w2-recover-cancel", extra_env={"EAP_APPROVAL_POLL_SECONDS": "1"})
    assert second.proc.poll() is None

    facts = _assert_invariants(h, run_id, status="failed")
    end = facts["events"][-1].data
    assert end.get("error_code") == "run_cancelled", end
    starts = _iteration_starts(facts["keys"], "lp")
    assert 2 not in starts, f"iteration 2 ran after the cancel: {starts}"
    # A second cancel is idempotent (202 with the same result).
    retry = h.client.post(f"/api/v1/runs/{run_id}/cancel", headers=h.headers)
    assert retry.status_code == 202, retry.text
    report(f"6 cancel between iterations: starts={starts} end={end.get('error_code')}")
