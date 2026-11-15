"""LP.11 acceptance (tester): the cooperative cancel (a `cancel_requested` event, no API terminal
write) of loops, foreach windows and nested loops, and the API cancel of a 100-iteration loop."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from worker_tests.cancel_acceptance_helpers import assert_cancelled_tree, snapshot, stays, tree_ids
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_workflow_loop_tick import _ask, _cancel_requested, _events, _foreach, _iteration_starts, _loop, _ref, _start, _wf, defs  # noqa: F401
from worker_tests.workflow_run_helpers import agent_spec, publish_agent, publish_workflow, wait_event


@pytest.fixture
def gate(monkeypatch, worker, tenant):
    """`test:echo`, made deterministic: it counts requests (`calls`). On request `cancel_at`
    (1-based) it writes the run's `cancel_requested` event (the cooperative cancel) BEFORE it
    answers; on request `hold_at` it sets `entered` and waits for `release` (at most 30 s) before
    it answers. So a cancel lands inside a known iteration, never "after some sleep". Set
    `run_id` through `_start(before=gate.bind)`."""
    state: dict = {"calls": 0, "run_id": None, "cancel_at": None, "hold_at": None,
                   "entered": threading.Event(), "release": threading.Event()}
    write = _cancel_requested(worker, tenant)

    def build() -> FunctionModel:
        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            state["calls"] += 1
            if state["calls"] == state["cancel_at"]:
                await asyncio.to_thread(write, state["run_id"])
            if state["calls"] == state["hold_at"]:
                state["entered"].set()
                await asyncio.to_thread(state["release"].wait, 30)
            yield "Echo: gated"

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(parts=[TextPart("Echo: gated")])

        return FunctionModel(respond, stream_function=stream, model_name="scripted-echo")

    monkeypatch.setattr("eap_worker.models.scripted_echo_model", build)
    state["bind"] = lambda run_id: state.update(run_id=run_id)
    yield state
    state["release"].set()


def _body(agent_id):
    ticket = {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}
    return [{**_ask(agent_id), "next": "mk"}, ticket]


def _assert_cancelled(worker, run_id, step_id):
    events = event_rows(worker.engine, run_id)
    keys = [e[1] for e in events]
    assert keys.count("end") == 1 and keys[-1] == "end"
    by = {e[1]: e[3] for e in events}
    assert by[f"step:{step_id}:failed"]["error_code"] == "run_cancelled"
    assert by["end"]["error_code"] == "run_cancelled" and f"step:{step_id}:completed" not in by
    return events


def _iteration_workflows(worker_settings, run_id, loop_id="lp"):
    """The DBOS iteration workflows of the run's loop `loop_id`: {workflow id: created_at (ms)}."""
    schema = worker_settings.dbos_system_schema
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        rows = conn.execute(
            f'SELECT workflow_uuid, created_at FROM "{schema}".workflow_status WHERE workflow_uuid LIKE %s',
            (f"{run_id}:loop:{loop_id}.%",),
        ).fetchall()
    return {r[0]: r[1] for r in rows if ":" not in r[0][len(f"{run_id}:loop:"):]}


def test_loop_cancel_stops_within_an_iteration_and_side_effects_stop(worker, client, tenant, defs, gate):
    """The cancel lands during iteration 2's agent step. That iteration runs to its end (its
    ticket is created: the tick runs between iterations, see the workflow_run docstring); the
    tick before iteration 3 stops the run. Exactly 3 iterations and 3 tickets."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    gate["cancel_at"] = 3
    run_id = _start(worker, client, tenant, _wf(_loop(_body(agent_id), max_iterations=50)), {}, before=gate["bind"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = _assert_cancelled(worker, run_id, "lp")
    assert _iteration_starts({e[1]: 1 for e in events}) == [0, 1, 2]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 3
    assert gate["calls"] == 3


def test_foreach_window_cancel_awaits_started_items_and_starts_no_more(worker, client, tenant, defs, gate):
    """concurrency 2, 8 items: the cancel lands during the first model request of the initial
    window, so the refill tick after item 0 stops the foreach. Items 0 and 1 started and were
    awaited; no other item started; the run is cancelled (never a collected entry)."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.each.item")}}
    spec = _wf(_foreach([ask], concurrency=2, max_items=8, on_item_error="collect"))
    gate["cancel_at"] = 1
    run_id = _start(worker, client, tenant, spec, {"items": list("abcdefgh")}, before=gate["bind"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = _assert_cancelled(worker, run_id, "each")  # run_cancelled is never collected as an item entry
    keys = [e[1] for e in events]
    started = [k for k in keys if k.startswith("loop:each.") and k.endswith(":started") and ":step:" not in k]
    assert started == ["loop:each.0:started", "loop:each.1:started"]
    for key in started:
        assert key.replace(":started", ":completed") in keys  # started items were awaited
    assert gate["calls"] == 2


def test_nested_loop_cancel_ends_the_outer_run_with_no_further_iteration(worker, client, tenant, defs, gate):
    """The cancel lands during the inner loop's iteration 2: the inner tick before in.3 stops the
    inner loop, which fails outer iteration 0, and the outer loop starts no iteration 1."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    inner = _loop(_body(agent_id), lid="in", max_iterations=30)
    gate["cancel_at"] = 3
    run_id = _start(worker, client, tenant, _wf(_loop([inner], lid="out", max_iterations=5)), {}, before=gate["bind"])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
    assert run["status"] == "failed"
    events = _assert_cancelled(worker, run_id, "out")
    keys = [e[1] for e in events]
    assert "loop:out.1:started" not in keys
    inner_starts = [k for k in keys if k.startswith("loop:out.0/in.") and k.endswith(":started") and ":step:" not in k]
    assert sorted(inner_starts) == [f"loop:out.0/in.{i}:started" for i in range(3)], inner_starts
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 3


def test_api_cancel_of_a_100_iteration_loop_starts_no_iteration_workflow_after(
        worker, client, worker_settings, tenant, defs, gate):
    """The API cancel lands while iteration 2's agent request is held. DBOS's workflow_status is
    the truth for "no iteration workflow is started after the cancel" (events after `end` are
    dropped by the store, so the event log alone cannot show it): exactly lp.0..lp.2 exist, all
    created before the cancel."""
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf, _ = publish_workflow(worker.engine, tenant, _wf(_loop(_body(agent_id), max_iterations=100)))
    run_id = str(uuid.uuid4())
    gate["hold_at"] = 3
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        assert api.post(f"/api/v1/workflows/{wf}/runs", headers=auth_header(tenant),
                        json={"input": {}, "run_id": run_id}).status_code == 202
        assert gate["entered"].wait(60), "iteration 2's agent request never started"
        cancelled_at_ms = time.time() * 1000
        r = api.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_header(tenant, "alice"))
        assert r.status_code == 202 and r.json()["error_code"] == "run_cancelled", r.text
        gate["release"].set()
        ids = tree_ids(worker.engine, run_id)
        assert_cancelled_tree(worker.engine, worker.run_store, tenant, [run_id])
        for rid in ids[1:]:  # a child that finished before the cancel stays completed; the rest are cancelled
            if worker.run_store.get_run(tenant, uuid.UUID(rid))["status"] != "completed":
                assert_cancelled_tree(worker.engine, worker.run_store, tenant, [rid])
        before = snapshot(worker.engine, [run_id])
        assert stays(lambda: snapshot(worker.engine, [run_id]) == before, 3)
        iterations = _iteration_workflows(worker_settings, run_id)
        assert sorted(iterations) == [f"{run_id}:loop:lp.{i}" for i in range(3)], iterations
        assert all(created <= cancelled_at_ms for created in iterations.values()), (iterations, cancelled_at_ms)
        assert gate["calls"] == 3  # no agent request after the held one
