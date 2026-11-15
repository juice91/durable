"""LP.9 tester: loop trace edge cases beyond the implementer's tests (pure build_trace on D4 events)."""

from __future__ import annotations

from eap_schemas.workflow_scope import child_scope, scoped_child_run_id

from eap_run_store.trace import MAX_LOOP_ITERATION_SPANS, RunData, TraceInput, build_trace

from .loop_trace_fixtures import RUN, Log, body_agent, child_run, refine_loop, workflow_run

CAP = MAX_LOOP_ITERATION_SPANS


def _trace(run, events, children=()):
    return build_trace(TraceInput(root=RunData(run=run, events=events, requests={}), children=list(children)))


def _spans(trace, kind):
    return [s for s in trace["spans"] if s["kind"] == kind]


def _ids(trace):
    return {s["id"] for s in trace["spans"]}


def _check_tree(trace):
    seen = set()
    for s in trace["spans"]:
        assert s["parent_id"] is None or s["parent_id"] in seen, s["id"]
        seen.add(s["id"])


def _start(log):
    log.add("start", "run_started", {"kind": "workflow", "input": {}})


def test_cap_boundary_exactly_100_shows_all_and_101_drops_one():
    t = _trace(*_run(CAP))
    assert len(_spans(t, "loop_iteration")) == CAP and t["truncated"]["iterations"] == 0
    idx = [s["attributes"]["index"] for s in _spans(t, "loop_iteration")]
    assert idx == list(range(CAP))
    t = _trace(*_run(CAP + 1))
    assert len(_spans(t, "loop_iteration")) == CAP and t["truncated"]["iterations"] == 1
    assert t["limits"]["max_loop_iteration_spans"] == 100 == CAP
    assert f"{RUN}:loop:refine.{CAP}" not in _ids(t) and f"{RUN}:loop:refine.{CAP - 1}" in _ids(t)


def _run(n):
    run, events, children = refine_loop(n)
    return run, events, children


def test_cap_drops_nested_iterations_and_child_runs_of_omitted_iteration():
    log, children = Log(), []
    _start(log)
    log.step_started((), "outer", "foreach")
    for i in (0, CAP):  # one shown, one omitted
        o = child_scope((), "outer", i)
        log.iteration_started((), "outer", i, item=i, foreach=True)
        log.step_started(o, "inner", "loop")
        s = child_scope(o, "inner", 0)
        log.iteration_started(o, "inner", 0, state={})
        k = body_agent(log, s, "w", children)
        log.iteration_completed(o, "inner", 0, k, state={})
        done = log.step_completed(o, "inner", {"iterations": 1, "exited": "condition"})
        log.iteration_completed((), "outer", i, done["event_key"], foreach=True)
    log.step_completed((), "outer", [1, 2])
    log.add("end", "run_completed", {"output": None})
    t = _trace(workflow_run(), log.events, children)
    _check_tree(t)
    ids = _ids(t)
    assert f"{RUN}:loop:outer.0/inner.0" in ids
    assert not any(f"outer.{CAP}" in i for i in ids)
    assert len(_spans(t, "child_run")) == 1
    assert t["truncated"]["iterations"] == 1  # nested iterations of an omitted one are not counted again


def test_truncated_iterations_totals_across_loops():
    log, children = Log(), []
    _start(log)
    for name, extra in (("a", 3), ("b", 5)):
        log.step_started((), name, "foreach")
        for i in range(CAP + extra):
            log.iteration_started((), name, i, item=i, foreach=True)
            log.iteration_completed((), name, i, "k", foreach=True)
        log.step_completed((), name, [])
    log.add("end", "run_completed", {"output": None})
    t = _trace(workflow_run(), log.events, children)
    assert t["truncated"]["iterations"] == 8
    by = {s["name"]: s for s in _spans(t, "loop")}
    assert by["a"]["attributes"]["iterations_omitted"] == 3 and by["b"]["attributes"]["iterations_omitted"] == 5
    assert len(_spans(t, "loop_iteration")) == 2 * CAP


def test_same_nested_index_in_two_outer_iterations_is_capped_independently():
    log = Log()
    _start(log)
    log.step_started((), "o", "foreach")
    for i in range(2):
        o = child_scope((), "o", i)
        log.iteration_started((), "o", i, item=i, foreach=True)
        log.step_started(o, "n", "foreach")
        for j in range(CAP + 2):
            log.iteration_started(o, "n", j, item=j, foreach=True)
            log.iteration_completed(o, "n", j, "k", foreach=True)
        log.step_completed(o, "n", [])
        log.iteration_completed((), "o", i, "k", foreach=True)
    t = _trace(workflow_run(), log.events)
    _check_tree(t)
    assert len(_spans(t, "loop_iteration")) == 2 + 2 * CAP
    assert t["truncated"]["iterations"] == 4


def test_loop_inside_foreach_and_foreach_inside_loop_ids():
    log, children = Log(), []
    _start(log)
    log.step_started((), "outer", "loop")
    o = child_scope((), "outer", 1)
    log.iteration_started((), "outer", 0, state={})
    log.iteration_completed((), "outer", 0, "k", state={})
    log.iteration_started((), "outer", 1, state={})
    log.step_started(o, "inner", "foreach")
    log.iteration_started(o, "inner", 0, item="x", foreach=True)
    k = body_agent(log, child_scope(o, "inner", 0), "w", children)
    log.iteration_completed(o, "inner", 0, k, foreach=True)
    log.step_completed(o, "inner", ["a"])
    log.iteration_completed((), "outer", 1, "k", state={})
    t = _trace(workflow_run(), log.events, children)
    _check_tree(t)
    sp = {s["id"]: s for s in t["spans"]}
    it = sp[f"{RUN}:loop:outer.1/inner.0"]
    assert it["name"] == "inner #0" and it["input"] == "x"
    assert it["parent_id"] == f"{RUN}:step:outer.1~inner"
    assert sp[f"{RUN}:step:outer.1~inner"]["kind"] == "loop"
    assert sp[f"{RUN}:step:outer.1~inner"]["attributes"]["step_type"] == "foreach"
    assert sp[f"{RUN}:step:outer.1/inner.0~w".replace("/", "~")]["parent_id"] == it["id"]


def test_attributes_for_failed_loop_iteration_and_foreach_input():
    log = Log()
    _start(log)
    log.step_started((), "each", "foreach")
    log.iteration_started((), "each", 0, item={"k": 1}, foreach=True)
    log.iteration_failed((), "each", 0, "agent_failed", "triage", error="boom")
    log.step_started((), "lp", "loop")
    log.iteration_started((), "lp", 0, state={"s": 1})
    log.iteration_completed((), "lp", 0, "kk", state={"s": 2}, until="false")
    log.iteration_started((), "lp", 1, state={"s": 2})
    t = _trace(workflow_run("running", finished_at=None), log.events)
    sp = {s["id"]: s for s in t["spans"]}
    f = sp[f"{RUN}:loop:each.0"]
    assert f["status"] == "error" and f["input"] == {"k": 1} and f["output"] is None
    a = f["attributes"]
    assert (a["error_code"], a["failed_step_id"], a["error"], a["iteration_status"]) == (
        "agent_failed", "triage", "boom", "failed")
    c = sp[f"{RUN}:loop:lp.0"]
    assert c["attributes"]["iteration_status"] == "completed" and c["attributes"]["until"] == "false"
    assert c["attributes"]["last_key"] == "kk" and c["input"] == {"s": 1} and c["output"] == {"s": 2}
    r = sp[f"{RUN}:loop:lp.1"]
    assert r["status"] == "running" and r["end"] is None and "iteration_status" not in r["attributes"]
    la = sp[f"{RUN}:step:lp"]["attributes"]
    assert (la["iterations_started"], la["iterations_completed"], la["iterations_failed"]) == (2, 1, 0)
    assert sp[f"{RUN}:step:each"]["attributes"]["iterations_failed"] == 1
