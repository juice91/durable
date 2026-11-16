"""LP.9: workflow loop spans of the event-derived trace (pure: build_trace on recorded D4 events).

A `loop` span per loop or foreach step, a `loop_iteration` span per iteration under it, body step
spans per iteration and each body agent's child run under its body step; overlapping foreach
iterations, nesting, open iterations and the per-loop iteration span cap.
"""

from __future__ import annotations

from datetime import timedelta

from eap_schemas.workflow_scope import child_scope, scoped_child_run_id

from eap_run_store.trace import MAX_LOOP_ITERATION_SPANS, SPAN_KINDS, RunData, TraceInput, build_trace

from .loop_trace_fixtures import RUN, Log, body_agent, refine_loop, workflow_run


def _trace(run, events, children):
    return build_trace(TraceInput(root=RunData(run=run, events=events, requests={}), children=children))


def _by_id(trace):
    return {s["id"]: s for s in trace["spans"]}


def _kind(trace, kind):
    return [s for s in trace["spans"] if s["kind"] == kind]


def _parents_first(trace):
    seen: set[str] = set()
    for span in trace["spans"]:
        assert span["parent_id"] is None or span["parent_id"] in seen, span["id"]
        seen.add(span["id"])


def test_loop_kinds_are_declared():
    assert "loop" in SPAN_KINDS and "loop_iteration" in SPAN_KINDS


def test_loop_with_three_iterations_nests_body_spans_and_child_runs():
    run, events, children = refine_loop(3)
    trace = _trace(run, events, children)
    _parents_first(trace)
    spans = _by_id(trace)
    loop = spans[f"{RUN}:step:refine"]
    assert loop["kind"] == "loop" and loop["parent_id"] == f"run:{RUN}" and loop["status"] == "ok"
    attrs = loop["attributes"]
    assert attrs["step_type"] == "loop"
    assert (attrs["iterations_started"], attrs["iterations_completed"], attrs["iterations_failed"]) == (3, 3, 0)
    assert attrs["iterations_omitted"] == 0 and attrs["iterations"] == 3 and attrs["exited"] == "condition"
    iterations = _kind(trace, "loop_iteration")
    assert [s["id"] for s in iterations] == [f"{RUN}:loop:refine.{i}" for i in range(3)]
    for i, it in enumerate(iterations):
        assert it["parent_id"] == loop["id"] and it["status"] == "ok"
        assert it["attributes"]["index"] == i and it["attributes"]["scope"] == f"refine.{i}"
        assert it["input"] == {"attempt": i} and it["output"] == {"attempt": i + 1}
        assert it["attributes"]["last_key"] == f"loop:refine.{i}:step:check:completed"
        draft = spans[f"{RUN}:step:refine.{i}~draft"]
        check = spans[f"{RUN}:step:refine.{i}~check"]
        assert draft["parent_id"] == it["id"] and check["parent_id"] == it["id"]
        assert draft["kind"] == "workflow_step" and draft["attributes"]["scope"] == f"refine.{i}"
        assert check["output"] == {"ok": i == 2}
        child = spans[f"run:{scoped_child_run_id(RUN, child_scope((), 'refine', i), 'draft')}"]
        assert child["kind"] == "child_run" and child["parent_id"] == draft["id"]
        assert child["attributes"]["parent_step_id"] == f"refine.{i}/draft"
    assert iterations[2]["attributes"]["until"] == "true"
    # No body span leaks to the top level and none is duplicated across iterations.
    assert f"{RUN}:step:draft" not in spans
    assert len(_kind(trace, "child_run")) == 3
    assert trace["truncated"]["iterations"] == 0


def test_overlapping_foreach_iterations_are_siblings_with_their_own_bodies():
    log = Log()
    children = []
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "each", "foreach")
    s = [child_scope((), "each", i) for i in range(3)]
    # concurrency 2: items 0 and 1 start, interleave; item 1 fails (collect); item 2 refills.
    log.iteration_started((), "each", 0, item={"n": 0}, foreach=True)
    log.iteration_started((), "each", 1, item={"n": 1}, foreach=True)
    log.step_started(s[1], "triage", "agent", child_run_id=scoped_child_run_id(RUN, s[1], "triage"))
    k0 = body_agent(log, s[0], "triage", children)
    log.step_failed(s[1], "triage", "agent_failed")
    log.iteration_completed((), "each", 0, k0, foreach=True)
    log.iteration_failed((), "each", 1, "agent_failed", "triage")
    log.iteration_started((), "each", 2, item={"n": 2}, foreach=True)
    k2 = body_agent(log, s[2], "triage", children)
    log.iteration_completed((), "each", 2, k2, foreach=True)
    log.step_completed((), "each", ["a", {"error_code": "agent_failed", "error": "the step failed"}, "a"])
    log.add("end", "run_completed", {"output": []})
    trace = _trace(workflow_run(), log.events, children)
    _parents_first(trace)
    spans = _by_id(trace)
    loop = spans[f"{RUN}:step:each"]
    assert loop["kind"] == "loop" and loop["attributes"]["step_type"] == "foreach"
    assert (loop["attributes"]["iterations_completed"], loop["attributes"]["iterations_failed"]) == (2, 1)
    its = [spans[f"{RUN}:loop:each.{i}"] for i in range(3)]
    assert all(it["parent_id"] == loop["id"] for it in its)
    assert [it["status"] for it in its] == ["ok", "error", "ok"]
    assert its[1]["attributes"]["error_code"] == "agent_failed"
    assert its[1]["attributes"]["failed_step_id"] == "triage"
    assert [it["input"] for it in its] == [{"n": 0}, {"n": 1}, {"n": 2}]
    # Overlap: iteration 1 started before iteration 0 ended.
    assert its[1]["start"] < its[0]["end"]
    for i in range(3):
        assert spans[f"{RUN}:step:each.{i}~triage"]["parent_id"] == its[i]["id"]
    assert spans[f"{RUN}:step:each.1~triage"]["status"] == "error"
    assert spans[f"{RUN}:step:each.1~triage"]["attributes"]["error_code"] == "agent_failed"
    for i in (0, 2):
        child = spans[f"run:{scoped_child_run_id(RUN, s[i], 'triage')}"]
        assert child["parent_id"] == f"{RUN}:step:each.{i}~triage"


def test_nested_loops_nest_per_iteration():
    log = Log()
    children = []
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "outer", "foreach")
    for i in range(2):
        o = child_scope((), "outer", i)
        log.iteration_started((), "outer", i, item=i, foreach=True)
        log.step_started(o, "inner", "loop")
        last = None
        for j in range(2):
            inner = child_scope(o, "inner", j)
            log.iteration_started(o, "inner", j, state={"j": j})
            last = body_agent(log, inner, "work", children)
            log.iteration_completed(o, "inner", j, last, state={"j": j + 1})
        inner_done = log.step_completed(o, "inner", {"iterations": 2, "exited": "max_iterations", "last": "a", "state": {}})
        log.iteration_completed((), "outer", i, inner_done["event_key"], foreach=True)
    log.step_completed((), "outer", [{}, {}])
    log.add("end", "run_completed", {"output": []})
    trace = _trace(workflow_run(), log.events, children)
    _parents_first(trace)
    spans = _by_id(trace)
    for i in range(2):
        inner_loop = spans[f"{RUN}:step:outer.{i}~inner"]
        assert inner_loop["kind"] == "loop" and inner_loop["parent_id"] == f"{RUN}:loop:outer.{i}"
        assert inner_loop["attributes"]["iterations_completed"] == 2
        assert inner_loop["attributes"]["exited"] == "max_iterations"
        for j in range(2):
            it = spans[f"{RUN}:loop:outer.{i}/inner.{j}"]
            assert it["parent_id"] == inner_loop["id"] and it["attributes"]["scope"] == f"outer.{i}/inner.{j}"
            work = spans[f"{RUN}:step:outer.{i}~inner.{j}~work"]
            assert work["parent_id"] == it["id"]
            sc = child_scope(child_scope((), "outer", i), "inner", j)
            child = spans[f"run:{scoped_child_run_id(RUN, sc, 'work')}"]
            assert child["parent_id"] == work["id"]
            assert child["attributes"]["parent_step_id"] == f"outer.{i}/inner.{j}/work"
    assert len(_kind(trace, "loop_iteration")) == 6
    assert len(_kind(trace, "child_run")) == 4


def test_open_iteration_is_running_then_incomplete_once_the_run_ended():
    run, events, children = refine_loop(2, status="running")
    log = Log(events[-1]["ts"])
    log.events = list(events)
    scope = child_scope((), "refine", 2)
    log.iteration_started((), "refine", 2, state={"attempt": 2})
    log.step_started(scope, "draft", "agent")
    trace = _trace(run, log.events, children)
    spans = _by_id(trace)
    assert spans[f"{RUN}:step:refine"]["status"] == "running"
    assert spans[f"{RUN}:loop:refine.2"]["status"] == "running"
    assert spans[f"{RUN}:loop:refine.2"]["end"] is None
    assert spans[f"{RUN}:step:refine.2~draft"]["status"] == "running"
    ended = _trace({**run, "status": "failed", "display_status": "failed", "finished_at": log.now}, log.events, children)
    spans = _by_id(ended)
    assert spans[f"{RUN}:loop:refine.2"]["status"] == "incomplete"
    assert spans[f"{RUN}:step:refine.2~draft"]["status"] == "incomplete"


def test_iterations_past_the_cap_are_counted_not_shown():
    total = MAX_LOOP_ITERATION_SPANS + 20
    run, events, children = refine_loop(total)
    trace = _trace(run, events, children)
    _parents_first(trace)
    its = _kind(trace, "loop_iteration")
    assert len(its) == MAX_LOOP_ITERATION_SPANS
    assert its[-1]["attributes"]["index"] == MAX_LOOP_ITERATION_SPANS - 1
    loop = _by_id(trace)[f"{RUN}:step:refine"]
    assert loop["attributes"]["iterations_started"] == total
    assert loop["attributes"]["iterations_omitted"] == 20
    assert trace["truncated"]["iterations"] == 20
    assert trace["limits"]["max_loop_iteration_spans"] == MAX_LOOP_ITERATION_SPANS
    # Body spans and child runs of the omitted iterations appear nowhere (not even under the run).
    assert not any(s["attributes"].get("scope", "").startswith(f"refine.{MAX_LOOP_ITERATION_SPANS}") for s in trace["spans"]
                   if s["kind"] == "workflow_step")
    assert len(_kind(trace, "child_run")) == MAX_LOOP_ITERATION_SPANS
    assert sum(1 for s in trace["spans"] if s["kind"] == "workflow_step") == 2 * MAX_LOOP_ITERATION_SPANS
    assert trace["totals"]["runs"] == 1 + total  # totals still count every child run loaded


def test_loop_free_workflow_trace_is_unchanged():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "a", "tool", tool_id="t", input={"x": 1})
    log.step_completed((), "a", {"y": 2})
    log.add("end", "run_completed", {"output": {"y": 2}})
    trace = _trace(workflow_run(), log.events, [])
    [step] = [s for s in trace["spans"] if s["kind"] != "run"]
    assert step["id"] == f"{RUN}:step:a" and step["kind"] == "workflow_step"
    assert step["attributes"] == {"step_id": "a", "step_type": "tool", "tool_id": "t"}
    assert step["start"] == (log.start + timedelta(seconds=2)).isoformat()
    assert trace["truncated"]["iterations"] == 0
