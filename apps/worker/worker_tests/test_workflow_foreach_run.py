"""LP.10: `foreach` steps on the in-process worker.

A sliding window in item order (D7), `on_item_error` fail or collect, the cancelled-child rule,
the ordered output assembled from references (D6), the loop-output cap, and nested loops.
"""

from __future__ import annotations

import base64
import json
import pickle

import psycopg
import pytest

from worker_tests.cancel_acceptance_helpers import Gate, gated_echo_model
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)

EVENT = "eap.append_event"
BEGIN = "eap.workflow_step_begin"
AGENT = ["eap.workflow_resolve_agent", "eap.agent_run", "DBOS.getResult"]
TICK = "eap.workflow_loop_tick"  # LP.11: once before the initial window, then before each refill
START = "eap.workflow_iteration"
AWAIT = ["DBOS.getResult", EVENT]  # an item's result and its loop:{S}:completed|failed


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _ref(path):
    return {"$ref": path}


def _wf(*steps, **extra):
    return {"name": "Foreach", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps), **extra}


def _foreach(body, *, sid="each", items="input.items", max_items=5, **kw):
    return {"id": sid, "type": "foreach", "items": _ref(items), "max_items": max_items,
            "body": {"start": body[0]["id"], "steps": body}, **kw}


def _ask(agent_id, ref="loop.each.item", sid="ask"):
    return {"id": sid, "type": "agent", "agent_id": agent_id, "input": {"p": _ref(ref)}}


def _run(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def _events(worker, run_id):
    return {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}


def test_window_output_in_item_order_and_parent_layout(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=2))
    run_id, run = _run(worker, client, tenant, spec, {"items": ["a", "b", "c"]})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == ["Echo: a", "Echo: b", "Echo: c"]
    events = _events(worker, run_id)
    assert events["loop:each.1:started"] == (
        "loop_iteration_started", {"step_id": "each", "scope": [], "index": 1, "item": "b"})
    assert events["loop:each.2:completed"] == (
        "loop_iteration_completed",
        {"step_id": "each", "scope": [], "index": 2, "last_key": "loop:each.2:step:ask:completed"})
    assert events["step:each:completed"][1]["output"] == ["Echo: a", "Echo: b", "Echo: c"]
    rows = event_rows(worker.engine, run_id)
    assert [e[0] for e in rows] == list(range(len(rows)))  # gap-free seq
    # Sliding window, c = 2: start 0 and 1; await 0, start 2; await 1; await 2.
    schema = worker_settings.dbos_system_schema
    assert step_names(schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, TICK, START, START, *AWAIT, TICK, START, *AWAIT, *AWAIT, EVENT,
        EVENT, "eap.finish_run"]
    assert step_names(schema, f"{run_id}:loop:each.0") == [EVENT, BEGIN, *AGENT, EVENT]
    children = worker.run_store.list_runs(tenant, parent_run_id=run_id)
    assert sorted(c["parent_step_id"] for c in children) == ["each.0/ask", "each.1/ask", "each.2/ask"]

    # References (D6): the iteration's arguments carry no item (state is None) and no spec; each
    # recorded result is {status, last_key}.
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        inputs = conn.execute(
            f'SELECT coalesce(i.inputs, s.inputs) FROM "{schema}".workflow_status s'
            f' LEFT JOIN "{schema}".workflow_input i USING (workflow_uuid) WHERE s.workflow_uuid = %s',
            (f"{run_id}:loop:each.1",),
        ).fetchone()[0]
        results = conn.execute(
            f'SELECT output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s'
            " AND function_name = 'DBOS.getResult' ORDER BY function_id", (str(run_id),),
        ).fetchall()
    args = pickle.loads(base64.b64decode(inputs))["args"]
    assert len(args) == 9 and list(args[3:7]) == [["each"], [], 1, None]
    assert "'b'" not in repr(args) and agent_id not in repr(args)
    assert len(results) == 3
    for (output,) in results:
        value = pickle.loads(base64.b64decode(output))
        assert set(value) == {"status", "last_key"} and len(output) < 1024


def test_concurrency_one_keeps_the_loop_layout(worker, client, worker_settings, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    run_id, run = _run(worker, client, tenant, _wf(_foreach([tool])), {"items": [1, 2]})
    assert run["status"] == "completed", run["error"]
    assert len(json.loads(run["output"])) == 2
    assert step_names(worker_settings.dbos_system_schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, TICK, START, *AWAIT, TICK, START, *AWAIT, EVENT, EVENT, "eap.finish_run"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys == ["start", "step:each:started",
                    "loop:each.0:started", "loop:each.0:step:x:started", "loop:each.0:step:x:completed",
                    "loop:each.0:completed",
                    "loop:each.1:started", "loop:each.1:step:x:started", "loop:each.1:step:x:completed",
                    "loop:each.1:completed", "step:each:completed", "end"]


ITEMS = [{"v": "a"}, {}, {"v": "c"}]  # item 1 has no `v`: its body step fails input_ref_missing


def test_collect_records_error_entries(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id, "loop.each.item.v")], concurrency=2, on_item_error="collect"))
    run_id, run = _run(worker, client, tenant, spec, {"items": ITEMS})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out[0] == "Echo: a" and out[2] == "Echo: c"
    assert set(out[1]) == {"error_code", "error"} and out[1]["error_code"] == "input_ref_missing"
    events = _events(worker, run_id)
    assert events["loop:each.1:failed"][1]["error_code"] == "input_ref_missing"
    assert events["loop:each.1:failed"][1]["failed_step_id"] == "ask"


@pytest.mark.parametrize("concurrency, started", [(1, 2), (2, 3)])
def test_fail_stops_new_starts_and_awaits_the_started(worker, client, worker_settings, tenant, defs,
                                                       concurrency, started):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    items = [*ITEMS, {"v": "d"}]
    spec = _wf(_foreach([_ask(agent_id, "loop.each.item.v")], concurrency=concurrency))
    run_id, run = _run(worker, client, tenant, spec, {"items": items})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    for key in ("loop:each.1:failed", "step:each:failed", "end"):
        assert events[key][1]["error_code"] == "input_ref_missing", key
    assert events["end"][1]["step_id"] == "each"
    started_keys = {k for k in events if k.startswith("loop:each.") and k.endswith(":started") and ":step:" not in k}
    assert started_keys == {f"loop:each.{i}:started" for i in range(started)}
    for i in range(started):  # every started item was awaited and recorded
        assert f"loop:each.{i}:completed" in events or f"loop:each.{i}:failed" in events
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert names.count(START) == started and names.count("DBOS.getResult") == started


def test_empty_list_runs_no_iteration(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    run_id, run = _run(worker, client, tenant, _wf(_foreach([tool])), {"items": []})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == []
    assert [k for _, k, _, _ in event_rows(worker.engine, run_id)] == [
        "start", "step:each:started", "step:each:completed", "end"]


@pytest.mark.parametrize("items, code", [
    ([1, 2, 3], "loop_items_exceeded"), ("abc", "loop_items_not_list"),
])
def test_bad_items_fail_the_step(worker, client, tenant, defs, items, code):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    run_id, run = _run(worker, client, tenant, _wf(_foreach([tool], max_items=2)), {"items": items})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["step:each:failed"][1]["error_code"] == code
    assert events["end"][1]["error_code"] == code
    assert not any(k.startswith("loop:") for k in events)


def test_missing_items_ref_fails_input_ref_missing(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    run_id, run = _run(worker, client, tenant, _wf(_foreach([tool])), {})
    assert run["status"] == "failed"
    assert _events(worker, run_id)["step:each:failed"][1]["error_code"] == "input_ref_missing"


def test_output_over_the_loop_output_cap(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES", "30")
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)]))
    run_id, run = _run(worker, client, tenant, spec, {"items": ["aaaaaaaaaa", "bbbbbbbbbb"]})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert "loop:each.1:completed" in events
    assert events["step:each:failed"][1]["error_code"] == "step_output_too_large"


def test_nested_foreach_in_until_uses_unique_scoped_keys(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    each = _foreach([_ask(agent_id)], concurrency=2)
    outer = {"id": "outer", "type": "loop", "max_iterations": 3,
             "until": {"op": "eq", "left": _ref("loop.outer.index"), "right": 1},
             "body": {"start": "each", "steps": [each]}}
    run_id, run = _run(worker, client, tenant, _wf(outer), {"items": ["x", "y"]})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["iterations"] == 2 and out["last"] == ["Echo: x", "Echo: y"]
    events = _events(worker, run_id)
    for i in range(2):
        for j in range(2):
            key = f"loop:outer.{i}/each.{j}:step:ask:completed"
            assert events[key][1]["scope"] == [{"loop": "outer", "index": i}, {"loop": "each", "index": j}]
    assert events["loop:outer.1/each.0:started"][1]["item"] == "x"
    children = worker.run_store.list_runs(tenant, parent_run_id=run_id)
    assert sorted(c["parent_step_id"] for c in children) == [
        "outer.0/each.0/ask", "outer.0/each.1/ask", "outer.1/each.0/ask", "outer.1/each.1/ask"]
    schema = worker_settings.dbos_system_schema
    assert step_names(schema, f"{run_id}:loop:outer.0") == [
        EVENT, BEGIN, TICK, START, START, *AWAIT, *AWAIT, EVENT]


def test_nested_foreach_reads_items_from_the_outer_item_and_a_step(worker, client, tenant, defs):
    """Items from `loop.<outer>.item` (the outer started event) and, for the outer foreach, from a
    step's output (its completed event), both read again inside the iteration (D6)."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    first = {"id": "first", "type": "agent", "agent_id": agent_id, "input": {"p": "go"}, "next": "outer"}
    inner = _foreach([_ask(agent_id, "loop.inner.item")], sid="inner", items="loop.outer.item.members",
                     concurrency=2)
    outer = _foreach([inner], sid="outer", items="input.groups", concurrency=2)
    groups = [{"members": ["a", "b"]}, {"members": ["c"]}]
    _run_id, run = _run(worker, client, tenant, _wf(first, outer), {"groups": groups})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == [["Echo: a", "Echo: b"], ["Echo: c"]]


def test_foreach_items_from_a_step_output(worker, client, tenant, defs):
    """The items list is the output of an earlier foreach (its completed event)."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    one = {**_foreach([_ask(agent_id, "loop.one.item", sid="a1")], sid="one"), "next": "two"}
    two = _foreach([_ask(agent_id, "loop.two.item", sid="a2")], sid="two", items="steps.one.output", concurrency=2)
    run_id, run = _run(worker, client, tenant, _wf(one, two), {"items": ["p", "q"]})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == ["Echo: Echo: p", "Echo: Echo: q"]
    assert _events(worker, run_id)["loop:two.0:started"][1]["item"] == "Echo: p"


@pytest.fixture
def gate(monkeypatch):
    g = Gate()
    monkeypatch.setattr("eap_worker.models.scripted_echo_model", gated_echo_model(g))
    yield g
    g.release.set()


def test_a_cancelled_child_under_collect_is_run_cancelled_not_an_entry(worker, client, tenant, defs, gate):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], on_item_error="collect"))
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={"items": ["a", "b"]}, app_version=APP_VERSION,
    )
    assert gate.entered.wait(60)
    client.cancel_workflow(f"{run_id}:loop:each.0")  # only the iteration, not the run
    gate.release.set()  # the parked model returns; the iteration stops at its next operation
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["loop:each.0:failed"][1]["error_code"] == "run_cancelled"
    assert events["step:each:failed"][1]["error_code"] == "run_cancelled"
    assert events["end"][1]["error_code"] == "run_cancelled"
    assert "loop:each.1:started" not in events and "step:each:completed" not in events
