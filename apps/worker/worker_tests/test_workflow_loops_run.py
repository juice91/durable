"""LP.7: `loop` steps (while / until, state and carry) on the in-process worker.

Each iteration is one `eap.workflow_iteration` child workflow taking references (D6); body steps
use scoped keys and ids (D4); body agents deny tool approvals (D8a).
"""

from __future__ import annotations

import base64
import json
import pickle
import uuid

import psycopg
import pytest
from sqlalchemy import text

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
ITERATION = ["eap.workflow_iteration", "DBOS.getResult", EVENT]  # per iteration, in the parent


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _ref(path):
    return {"$ref": path}


def _wf(*steps, **extra):
    return {"name": "Loops", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps), **extra}


def _loop(body, *, lid="lp", **kw):
    return {"id": lid, "type": "loop", "body": {"start": body[0]["id"], "steps": body}, **kw}


def _run(worker, client, tenant, spec, input_value):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def _events(worker, run_id):
    return {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}


def _echo_loop(agent_id, **kw):
    """Each iteration echoes the carried message: state.msg grows by one "Echo: " per iteration."""
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.lp.state.msg")}}
    kw.setdefault("until", {"op": "eq", "left": _ref("loop.lp.index"), "right": 2})
    return _loop([ask], max_iterations=5, state={"msg": _ref("input.q")}, carry={"msg": _ref("steps.ask.output")}, **kw)


def test_until_runs_three_iterations_with_carried_state(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id, run = _run(worker, client, tenant, _wf(_echo_loop(agent_id)), {"q": "hi"})
    assert run["status"] == "completed", run["error"]
    expected = {"iterations": 3, "exited": "condition", "last": "Echo: Echo: Echo: hi",
                "state": {"msg": "Echo: Echo: Echo: hi"}}
    assert json.loads(run["output"]) == expected
    rows = event_rows(worker.engine, run_id)
    assert [e[0] for e in rows] == list(range(len(rows)))  # gap-free seq
    keys = [k for _, k, _, _ in rows]
    per_iteration = lambda i: [  # noqa: E731
        f"loop:lp.{i}:started", f"loop:lp.{i}:step:ask:started", f"loop:lp.{i}:step:ask:completed",
        f"loop:lp.{i}:completed",
    ]
    assert keys == ["start", "step:lp:started", *per_iteration(0), *per_iteration(1), *per_iteration(2),
                    "step:lp:completed", "end"]
    events = _events(worker, run_id)
    assert events["loop:lp.1:started"] == (
        "loop_iteration_started", {"step_id": "lp", "scope": [], "index": 1, "state": {"msg": "Echo: hi"}})
    t, done = events["loop:lp.2:completed"]
    assert t == "loop_iteration_completed" and done["until"] == "true" and done["index"] == 2
    assert done["last_key"] == "loop:lp.2:step:ask:completed"
    _t, body = events["loop:lp.1:step:ask:started"]
    assert body["step_id"] == "ask" and body["scope"] == [{"loop": "lp", "index": 1}]
    assert body["input"] == {"p": "Echo: hi"}
    assert events["step:lp:completed"][1]["output"] == expected
    assert "warnings" not in events["step:lp:completed"][1]

    # Scoped child runs (D4): one per iteration, parent_step_id `{S}/{id}`.
    from eap_schemas.workflow_scope import make_scope, scoped_child_run_id

    children = worker.run_store.list_runs(tenant, parent_run_id=run_id)
    assert sorted(c["parent_step_id"] for c in children) == ["lp.0/ask", "lp.1/ask", "lp.2/ask"]
    for i in range(3):
        assert body_child(events, i) == scoped_child_run_id(str(run_id), make_scope([("lp", i)]), "ask")

    # Golden step lists: the parent records 3 operations per iteration; each iteration its body.
    schema = worker_settings.dbos_system_schema
    assert step_names(schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, *ITERATION * 3, EVENT, EVENT, "eap.finish_run"]
    assert step_names(schema, f"{run_id}:loop:lp.0") == [EVENT, BEGIN, *AGENT, EVENT]

    # References, not payloads (D6): an iteration's recorded inputs are its arguments only, and
    # what the parent records from it is reference-sized.
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        # DBOS 3.5 keeps a workflow's inputs in workflow_input (older rows: workflow_status.inputs).
        inputs = conn.execute(
            f'SELECT coalesce(i.inputs, s.inputs) FROM "{schema}".workflow_status s'
            f' LEFT JOIN "{schema}".workflow_input i USING (workflow_uuid) WHERE s.workflow_uuid = %s',
            (f"{run_id}:loop:lp.1",),
        ).fetchone()[0]
        results = conn.execute(
            f'SELECT output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s'
            " AND function_name = 'DBOS.getResult' ORDER BY function_id", (str(run_id),),
        ).fetchall()
    args = pickle.loads(base64.b64decode(inputs))["args"]
    assert list(args[3:7]) == [["lp"], [], 1, {"msg": "Echo: hi"}]
    assert "Triage" not in inputs and len(inputs) < 1024 + 2 * len(json.dumps(args[6]))
    for (output,) in results:
        value = pickle.loads(base64.b64decode(output))
        assert set(value) == {"status", "done", "state", "last_key", "until"}  # no body output
        assert len(output) < 1024


def body_child(events, i):
    return events[f"loop:lp.{i}:step:ask:started"][1]["child_run_id"]


def test_while_false_at_first_runs_no_iteration(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_loop([tool], max_iterations=3, **{"while": {"op": "eq", "left": _ref("input.go"), "right": True}}))
    run_id, run = _run(worker, client, tenant, spec, {"go": False})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == {"iterations": 0, "exited": "condition", "last": None, "state": {}}
    assert [k for _, k, _, _ in event_rows(worker.engine, run_id)] == [
        "start", "step:lp:started", "step:lp:completed", "end"]


def test_while_reads_the_carried_state(worker, client, tenant, defs):
    """`while` sees loop.<L>.state carried out of the previous iteration and stops on it."""
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_loop(
        [tool], max_iterations=5, state={"go": True}, carry={"go": False},
        **{"while": {"op": "eq", "left": _ref("loop.lp.state.go"), "right": True}},
    ))
    _run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["iterations"] == 1 and out["exited"] == "condition" and out["state"] == {"go": False}
    assert out["last"] is not None  # the clock tool's output in iteration 0


@pytest.mark.parametrize("on_max", ["fail", "exit"])
def test_max_iterations(worker, client, tenant, defs, on_max):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_loop([tool], max_iterations=2, on_max_iterations=on_max,
                     until={"op": "eq", "left": _ref("loop.lp.index"), "right": 99}))
    run_id, run = _run(worker, client, tenant, spec, {})
    events = _events(worker, run_id)
    assert "loop:lp.1:completed" in events and "loop:lp.2:started" not in events
    if on_max == "fail":
        assert run["status"] == "failed"
        assert run["error"] == "WorkflowStepError: the loop reached its max_iterations without meeting its condition"
        assert events["step:lp:failed"][1]["error_code"] == "loop_max_iterations"
        assert events["end"][1]["error_code"] == "loop_max_iterations" and events["end"][1]["step_id"] == "lp"
    else:
        assert run["status"] == "completed", run["error"]
        out = json.loads(run["output"])
        assert out["iterations"] == 2 and out["exited"] == "max_iterations" and out["last"] is not None


def test_state_over_the_cap_fails_loop_state_too_large(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_loop([tool], max_iterations=2, state={"s": _ref("input.big")},
                     until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {"big": "x" * (17 * 1024)})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["step:lp:failed"][1]["error_code"] == "loop_state_too_large"
    assert "loop:lp.0:started" not in events  # refused before the iteration started


def test_carry_over_the_state_cap_fails_the_iteration(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_STATE_BYTES", "64")
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_loop([tool], max_iterations=2, carry={"now": _ref("steps.x.output"), "pad": "y" * 80},
                     until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["loop:lp.0:failed"][1]["error_code"] == "loop_state_too_large"
    assert events["end"][1]["error_code"] == "loop_state_too_large"


def test_body_failure_fails_iteration_loop_and_run_with_the_body_code(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {"v": _ref("input.missing")}}
    spec = _wf(_loop([tool], max_iterations=2, until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys == ["start", "step:lp:started", "loop:lp.0:started", "loop:lp.0:step:x:started",
                    "loop:lp.0:step:x:failed", "loop:lp.0:failed", "step:lp:failed", "end"]
    events = _events(worker, run_id)
    for key in ("loop:lp.0:step:x:failed", "loop:lp.0:failed", "step:lp:failed", "end"):
        assert events[key][1]["error_code"] == "input_ref_missing", key
    assert events["loop:lp.0:failed"][1]["failed_step_id"] == "x"
    assert events["end"][1]["step_id"] == "lp"


def test_scoped_tool_keys_one_ledger_row_per_iteration(worker, client, tenant, defs):
    tool = {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}
    spec = _wf(_loop([tool], max_iterations=3, until={"op": "eq", "left": _ref("loop.lp.index"), "right": 1}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    with worker.engine.connect() as conn:
        keys = set(conn.execute(text("SELECT key FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).scalars())
    assert keys == {f"{run_id}:lp.0:mk", f"{run_id}:wf_lp.0~mk", f"{run_id}:lp.1:mk", f"{run_id}:wf_lp.1~mk"}
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 2


def test_nested_until_reads_outer_state_and_uses_nested_scopes(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id,
           "input": {"p": _ref("loop.outer.state.msg")}}
    inner = _loop([ask], lid="inner", max_iterations=3, until={"op": "eq", "left": _ref("loop.inner.index"), "right": 1})
    outer = _loop([{**inner, "next": None}], lid="outer", max_iterations=3, state={"msg": _ref("input.q")},
                  carry={"msg": _ref("steps.inner.output.last")},
                  until={"op": "eq", "left": _ref("loop.outer.index"), "right": 1})
    run_id, run = _run(worker, client, tenant, _wf(outer), {"q": "hi"})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["iterations"] == 2 and out["state"] == {"msg": "Echo: Echo: hi"}
    events = _events(worker, run_id)
    assert events["loop:outer.1/inner.0:step:ask:started"][1]["input"] == {"p": "Echo: hi"}
    assert events["loop:outer.1/inner.0:started"][1]["scope"] == [{"loop": "outer", "index": 1}]
    assert events["loop:outer.0:step:inner:completed"][1]["output"]["iterations"] == 2
    children = worker.run_store.list_runs(tenant, parent_run_id=run_id)
    assert sorted(c["parent_step_id"] for c in children) == [
        "outer.0/inner.0/ask", "outer.0/inner.1/ask", "outer.1/inner.0/ask", "outer.1/inner.1/ask"]
    schema = worker_settings.dbos_system_schema
    assert step_names(schema, f"{run_id}:loop:outer.0") == [EVENT, BEGIN, *ITERATION * 2, EVENT]


def test_unknown_conditions_record_warnings(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    branch = {"id": "b", "type": "branch", "default": "x",
              "cases": [{"when": {"not": {"op": "eq", "left": _ref("input.nope"), "right": 1}}, "next": "x"}]}
    loop = _loop([{**tool, "id": "y"}], max_iterations=2, on_max_iterations="exit",
                 until={"op": "eq", "left": _ref("loop.lp.state.nope"), "right": 1})
    spec = _wf(branch, {**tool, "next": "lp"}, loop)
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    events = _events(worker, run_id)
    assert events["step:b:completed"][1]["warnings"] == [{"code": "condition_unknown", "condition": "cases.0"}]
    assert events["step:b:completed"][1]["output"] == {"case": None, "next": "x"}  # UNKNOWN is not taken
    assert events["step:lp:completed"][1]["warnings"] == [
        {"code": "condition_unknown", "condition": "until", "index": 0}]
    assert events["loop:lp.0:completed"][1]["until"] == "unknown"
    assert json.loads(run["output"])["exited"] == "max_iterations"


def test_body_agent_denies_gated_tool_calls(worker, client, tenant, defs):
    """D8a: a body agent's gated call is denied at once; no inbox row; the run completes."""
    from worker_tests.test_step_layout_ac import APPROVAL_PROMPT, gated

    from worker_tests.conftest import ticket_spec

    agent_id = publish_agent(worker.engine, tenant, {**gated(ticket_spec(model="approval")), "spec_version": 1})
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": APPROVAL_PROMPT}}
    spec = _wf(_loop([ask], max_iterations=2, until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    child = worker.run_store.list_runs(tenant, parent_run_id=run_id)[0]
    child_events = [(k, t, d) for _, k, t, d in event_rows(worker.engine, child["id"])]
    results = [d for _k, t, d in child_events if t == "tool_result"]
    assert results and results[0]["status"] == "denied"
    denials = [d for _k, t, d in child_events if t == "tool_denied"]
    assert denials and denials[0]["reason"] == "loop_body" and denials[0]["decided_by"] == "system"
    assert "Tool approvals are not available inside loops" in json.loads(run["output"])["last"]
    with worker.engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM pending_approvals WHERE tenant_id = :t"), {"t": tenant}).scalar()
    assert rows == 0
    assert worker.tickets.call_count(tenant, f"{child['id']}:") == 0


def test_recorded_limits_without_loop_keys(worker, client, tenant, defs, monkeypatch):
    """Upgrade safety (D10): a run whose recorded `limits` predate the loop knobs runs a loop-free
    spec, a parallel (branch workflow) spec and a loop spec without KeyError."""
    import eap_worker.workflow_run as wr

    old = wr.read_limits

    def pre_lp7_limits():
        return {k: v for k, v in old().items() if k not in wr.WORKFLOW_LOOP_LIMIT_DEFAULTS}

    monkeypatch.setattr(wr, "read_limits", pre_lp7_limits)
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    plain = _wf(tool)
    parallel = _wf(
        {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "j"},
        {**tool, "id": "a", "next": "j"}, {**tool, "id": "b", "next": "j"}, {**tool, "id": "j"},
    )
    loop = _wf(_loop([tool], max_iterations=2, until={"op": "exists", "left": _ref("loop.lp.index")}))
    for spec in (plain, parallel, loop):
        _run_id, run = _run(worker, client, tenant, spec, {})
        assert run["status"] == "completed", run["error"]


def test_a_cancelled_run_is_never_started(worker, client, worker_settings, tenant, defs):
    """mark_running refuses a terminal run (D12): the workflow stops with no event and no step."""
    tool = {"id": "x", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}
    workflow_id, version_id = publish_workflow(worker.engine, tenant, _wf(tool))
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, workflow_id, 1, "{}", run_id)
    worker.run_store.cancel_run_tree(tenant, run_id)
    enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
                         input_value={}, app_version=APP_VERSION, run_id=run_id)
    handle = client.retrieve_workflow(str(run_id))
    assert handle.get_result()["error_code"] == "run_not_started"
    assert step_names(worker_settings.dbos_system_schema, run_id) == ["eap.start_workflow_run"]
    assert [k for _, k, _, _ in event_rows(worker.engine, run_id)] == ["end"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0
