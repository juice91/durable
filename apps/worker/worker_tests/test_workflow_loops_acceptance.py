"""LP.7 acceptance (tester): loop semantics, bodies, identifiers, replay, crash recovery.

Complements test_workflow_loops_run/golden/unit (kept as is). Worker subprocesses are stopped in finally.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from eap_schemas.workflow_scope import iteration_workflow_id, make_scope, scoped_child_run_id
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_conversation import step_names
from worker_tests.test_workflow_loops_run import _events, _loop, _ref, _run, _wf
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)

TOOL = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
MARK = "SECRET-MARK-9f3"


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _keys(worker, run_id):
    return [k for _, k, _, _ in event_rows(worker.engine, run_id)]


def test_until_is_evaluated_over_the_updated_state(worker, client, tenant, defs):
    """carry runs before until: state.n is 0 going in, 7 after carry, so until holds in iteration 0."""
    spec = _wf(_loop([TOOL], max_iterations=4, state={"n": 0}, carry={"n": _ref("input.k")},
                     until={"op": "eq", "left": _ref("loop.lp.state.n"), "right": 7}))
    run_id, run = _run(worker, client, tenant, spec, {"k": 7})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert out["iterations"] == 1 and out["exited"] == "condition" and out["state"] == {"n": 7}
    done = _events(worker, run_id)["loop:lp.0:completed"][1]
    assert done["until"] == "true" and done["state"] == {"n": 7}


def test_unknown_until_never_exits_and_fails_at_max(worker, client, tenant, defs):
    spec = _wf(_loop([TOOL], max_iterations=3, until={"op": "eq", "left": _ref("loop.lp.state.nope"), "right": 1}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    for i in range(3):
        assert events[f"loop:lp.{i}:completed"][1]["until"] == "unknown"
    assert "loop:lp.3:started" not in events
    assert events["end"][1]["error_code"] == "loop_max_iterations"


def test_unknown_while_stops_and_records_the_warning(worker, client, tenant, defs):
    spec = _wf(_loop([TOOL], max_iterations=3, **{"while": {"op": "eq", "left": _ref("loop.lp.state.nope"), "right": 1}}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"])["iterations"] == 0
    warnings = _events(worker, run_id)["step:lp:completed"][1]["warnings"]
    assert warnings[0]["code"] == "condition_unknown" and warnings[0]["condition"] == "while"


def test_exit_on_max_lets_the_next_step_branch_on_the_output(worker, client, tenant, defs):
    branch = {"id": "b", "type": "branch", "default": "no",
              "cases": [{"when": {"op": "eq", "left": _ref("steps.lp.output.exited"), "right": "max_iterations"},
                         "next": "yes"}]}
    loop = _loop([TOOL], max_iterations=2, on_max_iterations="exit", next="b",
                 until={"op": "eq", "left": _ref("loop.lp.index"), "right": 99})
    spec = _wf(loop, branch, {**TOOL, "id": "yes"}, {**TOOL, "id": "no"})
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    assert _events(worker, run_id)["step:b:completed"][1]["output"]["case"] == 0
    assert "step:yes:completed" in _events(worker, run_id) and "step:no:started" not in _events(worker, run_id)


def test_state_cap_breach_names_the_code_and_leaks_no_state_value(worker, client, tenant, defs):
    spec = _wf(_loop([TOOL], max_iterations=2, state={"s": _ref("input.big")},
                     until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {"big": MARK + "x" * (17 * 1024)})
    assert run["status"] == "failed" and "loop state size limit" in (run["error"] or "")
    events = _events(worker, run_id)
    assert events["end"][1]["error_code"] == "loop_state_too_large"
    blob = json.dumps([run["error"], events["step:lp:failed"], events["end"]])
    assert MARK not in blob


def test_carry_cap_breach_leaks_no_state_value(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_STATE_BYTES", "64")
    spec = _wf(_loop([TOOL], max_iterations=2, carry={"pad": MARK + "y" * 80},
                     until={"op": "exists", "left": _ref("loop.lp.index")}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["loop:lp.0:failed"][1]["error_code"] == "loop_state_too_large"
    assert MARK not in json.dumps([run["error"], events["loop:lp.0:failed"], events["step:lp:failed"], events["end"]])


def test_loop_items_error_messages_carry_no_item_values():
    from eap_worker.errors import classify
    from eap_worker.workflow_eval import LoopItemsError

    for exc in (LoopItemsError("loop_items_not_list", "each"), LoopItemsError("loop_items_exceeded", "each", 9, 3)):
        failure = classify(exc)
        assert MARK not in failure.message and failure.code == exc.code


def test_body_with_agent_tool_and_branch(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    body = [
        {"id": "b", "type": "branch", "default": "a",
         "cases": [{"when": {"op": "eq", "left": _ref("loop.lp.index"), "right": 1}, "next": "t"}]},
        {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "hi"}},
        {"id": "t", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}},
    ]
    spec = _wf(_loop(body, max_iterations=4, until={"op": "eq", "left": _ref("loop.lp.index"), "right": 2}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    keys = _keys(worker, run_id)
    assert "loop:lp.1:step:t:completed" in keys and "loop:lp.1:step:a:started" not in keys
    for i in (0, 2):
        assert f"loop:lp.{i}:step:a:completed" in keys and f"loop:lp.{i}:step:t:started" not in keys
    assert _events(worker, run_id)["loop:lp.1:step:b:completed"][1]["output"]["next"] == "t"
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    children = worker.run_store.list_runs(tenant, parent_run_id=run_id)
    assert sorted(c["parent_step_id"] for c in children) == ["lp.0/a", "lp.2/a"]


def test_body_reads_outputs_of_steps_before_the_loop(worker, client, tenant, defs):
    """D6/D3: the iteration loads `steps.pre.output` by an immutable event read, per iteration."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    pre = {"id": "pre", "type": "agent", "agent_id": agent_id, "input": {"p": "seed"}, "next": "lp"}
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id,
           "input": {"p": _ref("steps.pre.output")}}
    spec = _wf(pre, _loop([ask], max_iterations=3, until={"op": "eq", "left": _ref("loop.lp.index"), "right": 1}))
    run_id, run = _run(worker, client, tenant, spec, {})
    assert run["status"] == "completed", run["error"]
    events = _events(worker, run_id)
    for i in (0, 1):
        assert events[f"loop:lp.{i}:step:ask:started"][1]["input"] == {"p": "Echo: seed"}


def test_identifiers_follow_the_scope_helpers_and_a_duplicate_enqueue_adds_nothing(
    worker, client, worker_settings, tenant, defs
):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": "hi"}}
    spec = _wf(_loop([ask], max_iterations=3, until={"op": "eq", "left": _ref("loop.lp.index"), "right": 2}))
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                  version_id=version_id, input_value={}, app_version=APP_VERSION)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    schema = worker_settings.dbos_system_schema
    ids = {iteration_workflow_id(str(run_id), make_scope([("lp", i)])) for i in range(3)}
    assert ids == {f"{run_id}:loop:lp.{i}" for i in range(3)}
    for wid in ids:
        assert step_names(schema, wid)  # each iteration workflow ran
    children = sorted(c["id"] for c in worker.run_store.list_runs(tenant, parent_run_id=run_id))
    assert children == sorted(scoped_child_run_id(str(run_id), make_scope([("lp", i)]), "ask") for i in range(3))
    before = (event_rows(worker.engine, run_id), children)
    # A duplicate enqueue of the same workflow id: DBOS dedups it, so the finished run starts
    # nothing again. (This is not a recovery replay; the crash tests below cover that.)
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run",
                    "workflow_id": str(run_id), "app_version": APP_VERSION}, str(run_id), tenant, version_id, "{}")
    time.sleep(1.0)
    after = (event_rows(worker.engine, run_id),
             sorted(c["id"] for c in worker.run_store.list_runs(tenant, parent_run_id=run_id)))
    assert after == before


# ---------------------------------------------------------------------------- crash recovery
def _crash_loop_spec():
    tool = {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}
    return _wf(_loop([tool], max_iterations=4, until={"op": "eq", "left": _ref("loop.lp.index"), "right": 2}))


def _expected_keys():
    per = lambda i: [f"loop:lp.{i}:started", f"loop:lp.{i}:step:mk:started",  # noqa: E731
                     f"loop:lp.{i}:step:mk:completed", f"loop:lp.{i}:completed"]
    return ["start", "step:lp:started", *per(0), *per(1), *per(2), "step:lp:completed", "end"]


def _crash_and_recover(worker, tenant, crash_schema, tmp_path, crash_env):
    spec = _crash_loop_spec()
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"lp-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, **crash_env), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={}, app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert "end" not in _keys(worker, run_id)
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        time.sleep(0.5)  # a second look: recovery must add nothing after the run is terminal
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))  # gap-free seq
        assert [e[1] for e in events] == _expected_keys()  # no duplicate event, one end
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 3  # exactly one execution per iteration
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 3
        with worker.engine.connect() as conn:
            n = conn.execute(text("SELECT count(*) FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).scalar()
        assert n == 6  # two reservations per iteration (guard + tool call id), none duplicated
        assert json.loads(run["output"])["iterations"] == 3
        return run_id
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_crash_mid_iteration_before_the_tool_runs(worker, tenant, defs, crash_schema, tmp_path):
    _crash_and_recover(worker, tenant, crash_schema, tmp_path,
                       {"EAP_CRASH_AT": "workflow_mid_step", "EAP_CRASH_EVENT_KEY": "loop:lp.1:step:mk:started"})


def test_crash_inside_an_iteration_after_its_started_event(worker, tenant, defs, crash_schema, tmp_path):
    """Killed in the iteration workflow right after `loop:{S}:started`, before its first body step."""
    _crash_and_recover(worker, tenant, crash_schema, tmp_path,
                       {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:lp.1:started"})


def test_crash_after_a_body_tool_call_under_its_scoped_guard(worker, tenant, defs, crash_schema, tmp_path):
    """Killed after the body's side-effecting call and before its result was stored. The scoped
    guard `{run_id}:lp.1:mk` stays `reserved`, so recovery never calls the tool again. As for a
    top-level tool step, the outcome is unknown: the body step, the iteration, the loop step and the
    run all end `needs_attention` with side_effect_outcome_unknown."""
    spec = _crash_loop_spec()
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"lp-{uuid.uuid4().hex[:8]}"
    env = {"EAP_CRASH_AT": "after_workflow_tool_call", "EAP_CRASH_EVENT_KEY": "loop:lp.1:step:mk:call"}
    first = WorkerProcess(_env(crash_schema, executor_id, **env), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    dbos_client: DBOSClient | None = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={}, app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 2  # iterations 0 and 1
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "needs_attention", (run["error"], (tmp_path / "w2.log").read_text())
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 2  # never called again
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        keys = [e[1] for e in events]
        assert keys[-6:] == ["loop:lp.1:started", "loop:lp.1:step:mk:started", "loop:lp.1:step:mk:failed",
                             "loop:lp.1:failed", "step:lp:failed", "end"]
        by_key = {e[1]: e[3] for e in events}
        for key in ("loop:lp.1:step:mk:failed", "loop:lp.1:failed", "step:lp:failed", "end"):
            assert by_key[key]["error_code"] == "side_effect_outcome_unknown", key
            assert by_key[key]["status"] == "needs_attention", key
        with worker.engine.connect() as conn:
            guard = conn.execute(text("SELECT state FROM side_effects WHERE tenant_id = :t AND key = :k"),
                                 {"t": tenant, "k": f"{run_id}:lp.1:mk"}).scalar()
        assert guard == "reserved"
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def test_crash_between_iterations(worker, tenant, defs, crash_schema, tmp_path):
    _crash_and_recover(worker, tenant, crash_schema, tmp_path,
                       {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:lp.0:completed"})


def test_crash_after_the_last_iteration_before_the_step_completes(worker, tenant, defs, crash_schema, tmp_path):
    _crash_and_recover(worker, tenant, crash_schema, tmp_path,
                       {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:lp.2:completed"})


# ---------------------------------------------------------------------------- through the API
def test_three_iteration_until_loop_runs_end_to_end_through_the_api(
    worker, worker_settings, tenant, defs, monkeypatch
):
    """LP.7 acceptance: publish and start over HTTP (no preview flag since LP.11); the worker runs
    3 iterations."""
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW", raising=False)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.lp.state.msg")}}
    loop = _loop([ask], max_iterations=5, state={"msg": _ref("input.q")}, carry={"msg": _ref("steps.ask.output")},
                 until={"op": "eq", "left": _ref("loop.lp.index"), "right": 2})
    headers = auth_header(tenant)
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        created = api.post("/api/v1/workflows", json={"spec": _wf(loop)}, headers=headers)
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        published = api.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers)
        assert published.status_code == 201, published.text
        started = api.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=headers)
        assert started.status_code == 202, started.text
        run_id = uuid.UUID(started.json()["run_id"])
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", run["error"]
        out = json.loads(run["output"])
        assert out["iterations"] == 3 and out["exited"] == "condition" and out["last"] == "Echo: Echo: Echo: hi"
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        assert [e[1] for e in events].count("end") == 1
        listed = api.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        assert listed.status_code == 200, listed.text
