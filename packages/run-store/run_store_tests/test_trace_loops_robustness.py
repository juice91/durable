"""LP.9 tester: malformed / out-of-order scoped events never crash build_trace or invent spans."""

from __future__ import annotations

import copy

from eap_schemas.workflow_scope import child_scope

from eap_run_store.trace import RunData, TraceInput, build_trace

from .loop_trace_fixtures import RUN, Log, refine_loop, workflow_run


def _trace(events, run=None, children=()):
    run = run or workflow_run()
    return build_trace(TraceInput(root=RunData(run=run, events=events, requests={}), children=list(children)))


def _kinds(t, kind):
    return [s for s in t["spans"] if s["kind"] == kind]


def _check_no_orphans(t, root_children):
    """Every span's parent exists and comes first; the run root holds only `root_children`."""
    seen = set()
    for s in t["spans"]:
        assert s["parent_id"] is None or s["parent_id"] in seen, s["id"]
        seen.add(s["id"])
    root = t["spans"][0]["id"]
    assert {s["id"] for s in t["spans"] if s["parent_id"] == root} == set(root_children)


def _base():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    return log


def test_iteration_completed_without_started_invents_no_span():
    log = _base()
    log.step_started((), "lp", "loop")
    log.iteration_completed((), "lp", 0, "k", state={})
    t = _trace(log.events)
    assert _kinds(t, "loop_iteration") == []
    assert _kinds(t, "loop")[0]["attributes"]["iterations_started"] == 0
    _check_no_orphans(t, {f"{RUN}:step:lp"})


def test_iteration_failed_without_started_invents_no_span():
    log = _base()
    log.step_started((), "lp", "loop")
    log.iteration_failed((), "lp", 0, "x", "s")
    t = _trace(log.events)
    assert _kinds(t, "loop_iteration") == []
    assert _kinds(t, "loop")[0]["attributes"]["iterations_failed"] == 1
    _check_no_orphans(t, {f"{RUN}:step:lp"})


def test_body_step_event_without_iteration_does_not_crash():
    log = _base()
    log.step_started((), "lp", "loop")
    s = child_scope((), "lp", 0)
    log.step_started(s, "b", "tool", tool_id="t")
    log.step_completed(s, "b", 1)
    t = _trace(log.events)
    assert len(_kinds(t, "loop_iteration")) == 0
    # The body step's iteration span never appears: the step is dropped, not re-hung.
    assert f"{RUN}:step:lp.0~b" not in {x["id"] for x in t["spans"]}
    _check_no_orphans(t, {f"{RUN}:step:lp"})


def test_unknown_or_malformed_scope_keys_are_ignored():
    log = _base()
    for key in ("loop:nope:started", "loop::started", "loop:a.x:started", "loop:a.-1:started",
                "loop:a.0:bogus", "loop:a.0", "loop:a.0:step:b", "loop:a.0:step:b:started:extra", "loop"):
        log.add(key, "loop_iteration_started", {"step_id": "a", "index": 0, "state": {}})
        log.add(key + "z", "step_started", {"step_id": "b", "step_type": "tool"})
    t = _trace(log.events)
    assert _kinds(t, "loop_iteration") == []
    # Only "loopz" (not a `loop:` key: a top-level step key) makes a span; no malformed scope does.
    assert [s["id"] for s in t["spans"]] == [f"run:{RUN}", f"{RUN}:step:b"]


def test_duplicate_iteration_start_event_yields_one_span():
    log = _base()
    log.step_started((), "lp", "loop")
    log.iteration_started((), "lp", 0, state={})
    log.iteration_started((), "lp", 0, state={})
    log.iteration_completed((), "lp", 0, "k", state={})
    t = _trace(log.events)
    ids = [s["id"] for s in t["spans"]]
    assert len(ids) == len(set(ids))
    assert len(_kinds(t, "loop_iteration")) == 1
    assert {s["id"] for s in _kinds(t, "loop_iteration")} == {f"{RUN}:loop:lp.0"}


def test_missing_scope_and_non_string_step_ids_do_not_crash():
    log = _base()
    log.add("loop:lp.0:started", "loop_iteration_started", {})
    log.add("loop:lp.0:step:b:started", "step_started", {"step_id": 5, "step_type": "tool"})
    log.add("loop:lp.0:step:b:completed", "step_completed", {})
    log.add("step:x:started", "step_started", {"step_id": None})
    t = _trace(log.events)
    # No loop step span: the iteration and its body have no parent to hang under (step:x is a
    # top-level step read by its key).
    assert [s["id"] for s in t["spans"]] == [f"run:{RUN}", f"{RUN}:step:x"]


def test_iteration_before_loop_step_and_reordered_events_still_build():
    run, events, children = refine_loop(2)
    shuffled = list(reversed(events))
    t = build_trace(TraceInput(root=RunData(run=run, events=shuffled, requests={}), children=children))
    assert t["spans"][0]["kind"] == "run"
    _check_no_orphans(t, {f"{RUN}:step:refine"})
    assert {s["id"] for s in _kinds(t, "loop_iteration")} == {f"{RUN}:loop:refine.0", f"{RUN}:loop:refine.1"}
    by = {s["id"]: s for s in t["spans"]}
    for i in range(2):
        for step in ("draft", "check"):
            assert by[f"{RUN}:step:refine.{i}~{step}"]["parent_id"] == f"{RUN}:loop:refine.{i}"
    assert len(_kinds(t, "child_run")) == 2
    assert all(by[c["parent_id"]]["kind"] == "workflow_step" for c in _kinds(t, "child_run"))


def test_running_loop_with_open_iteration_has_no_end_and_run_not_ended():
    run, events, children = refine_loop(1, status="running")
    log = Log(events[-1]["ts"])
    log.events = list(events)
    s = child_scope((), "refine", 1)
    log.iteration_started((), "refine", 1, state={})
    log.step_started(s, "draft", "agent")
    t = _trace(log.events, run, children)
    loop = next(x for x in t["spans"] if x["id"] == f"{RUN}:step:refine")
    it = next(x for x in t["spans"] if x["id"] == f"{RUN}:loop:refine.1")
    assert loop["status"] == "running" and loop["end"] is None
    assert it["status"] == "running" and it["end"] is None
    assert t["truncated"]["iterations"] == 0


def test_build_trace_does_not_mutate_events():
    run, events, children = refine_loop(2)
    before = copy.deepcopy(events)
    _trace(events, run, children)
    assert events == before
