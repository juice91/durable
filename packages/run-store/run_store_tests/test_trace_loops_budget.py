"""LP.9 review fixes: the loop span cap bounds what a trace loads and keeps (MAX_LOOP_TRACE_EVENTS),
so a 100x100 nested loop keeps its run's terminal events, the body child runs of the iterations it
shows and correct counts, at a bounded cost; out-of-order and malformed scoped events never make
spans under the run root."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from eap_schemas.workflow_scope import (
    child_scope,
    iteration_event_key,
    scope_to_data,
    scoped_event_key,
    scoped_parent_step_id,
)

from eap_run_store import trace as trace_mod
from eap_run_store.trace import (
    MAX_LOOP_ITERATION_SPANS,
    MAX_LOOP_TRACE_EVENTS,
    MAX_SPANS,
    RunData,
    TraceInput,
    TraceReader,
    build_trace,
)

from .loop_trace_fixtures import RUN, Log, body_agent, workflow_run

N = 100


def _check_no_orphans(trace, root_children: set[str]):
    """Every span's parent exists and comes first; the run root holds only `root_children`."""
    seen: set[str] = set()
    root = trace["spans"][0]["id"]
    for s in trace["spans"]:
        assert s["parent_id"] is None or s["parent_id"] in seen, s["id"]
        seen.add(s["id"])
    assert {s["id"] for s in trace["spans"] if s["parent_id"] == root} == root_children


def _nested(n_outer: int, n_inner: int):
    """foreach `outer` (n_outer items) -> loop `inner` (n_inner iterations) -> agent `w` (a child
    run each), then a top-level tool step `after`, then the run ends."""
    log, children = Log(), []
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "outer", "foreach")
    for i in range(n_outer):
        o = child_scope((), "outer", i)
        log.iteration_started((), "outer", i, item=i, foreach=True)
        log.step_started(o, "inner", "loop")
        for j in range(n_inner):
            s = child_scope(o, "inner", j)
            log.iteration_started(o, "inner", j, state={"j": j})
            k = body_agent(log, s, "w", children)
            log.iteration_completed(o, "inner", j, k, state={"j": j + 1}, until="false")
        done = log.step_completed(o, "inner", {"iterations": n_inner, "exited": "max_iterations"})
        log.iteration_completed((), "outer", i, done["event_key"], foreach=True)
    log.step_completed((), "outer", list(range(n_outer)))
    log.step_started((), "after", "tool", tool_id="t")
    log.step_completed((), "after", "ok")
    log.add("end", "run_completed", {"output": "ok"})
    return log.events, children


def _spans(t, kind):
    return [s for s in t["spans"] if s["kind"] == kind]


def test_100x100_nested_loop_keeps_terminal_events_child_runs_and_counts():
    events, children = _nested(N, N)
    assert len(events) > 40_000 and len(children) == N * N
    began = time.perf_counter()
    t = build_trace(TraceInput(root=RunData(run=workflow_run(), events=events, requests={}), children=children))
    assert time.perf_counter() - began < 10

    by = {s["id"]: s for s in t["spans"]}
    _check_no_orphans(t, {f"{RUN}:step:outer", f"{RUN}:step:after"})
    assert len(t["spans"]) <= MAX_SPANS and t["truncated"]["spans"] is False

    # The run's terminal events and the steps after the loop are all there.
    assert by[f"run:{RUN}"]["status"] == "ok" and by[f"run:{RUN}"]["output"] == "ok"
    assert by[f"{RUN}:step:after"]["status"] == "ok"
    outer = by[f"{RUN}:step:outer"]
    assert outer["status"] == "ok" and outer["end"] is not None

    # Counts are over every iteration, shown or not.
    a = outer["attributes"]
    shown_outer = [s for s in _spans(t, "loop_iteration") if s["attributes"]["step_id"] == "outer"]
    assert a["iterations_started"] == a["iterations_completed"] == N and a["iterations_failed"] == 0
    assert 0 < len(shown_outer) < N
    assert a["iterations_omitted"] == N - len(shown_outer)
    expected_omitted = a["iterations_omitted"]
    for it in shown_outer:
        inner = by[f"{RUN}:step:{it['attributes']['scope']}~inner"]
        ia = inner["attributes"]
        assert ia["iterations_started"] == ia["iterations_completed"] == N
        shown_inner = [s for s in _spans(t, "loop_iteration") if s["parent_id"] == inner["id"]]
        assert len(shown_inner) <= MAX_LOOP_ITERATION_SPANS
        assert ia["iterations_omitted"] == N - len(shown_inner)
        expected_omitted += ia["iterations_omitted"]
        # Every shown iteration's body agent step holds its child run.
        for s in shown_inner:
            step = by[f"{RUN}:step:{s['attributes']['scope'].replace('/', '~')}~w"]
            assert step["parent_id"] == s["id"] and step["status"] == "ok"
            assert [c["kind"] for c in t["spans"] if c["parent_id"] == step["id"]] == ["child_run"]
    assert t["truncated"]["iterations"] == expected_omitted > 0
    assert len(_spans(t, "child_run")) == sum(1 for s in _spans(t, "loop_iteration") if s["attributes"]["step_id"] == "inner")
    assert t["limits"]["max_loop_trace_events"] == MAX_LOOP_TRACE_EVENTS


def test_a_small_nested_loop_is_shown_whole():
    events, children = _nested(3, 4)
    t = build_trace(TraceInput(root=RunData(run=workflow_run(), events=events, requests={}), children=children))
    _check_no_orphans(t, {f"{RUN}:step:outer", f"{RUN}:step:after"})
    assert len(_spans(t, "loop_iteration")) == 3 + 3 * 4 and len(_spans(t, "child_run")) == 12
    assert t["truncated"]["iterations"] == 0


def test_body_event_before_its_started_past_the_cap_is_dropped():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "each", "foreach")
    for i in (3, MAX_LOOP_ITERATION_SPANS + 50):
        s = child_scope((), "each", i)
        # The body step comes before the iteration's own started event.
        log.step_started(s, "triage", "tool", tool_id="t")
        log.step_completed(s, "triage", "x")
        log.iteration_started((), "each", i, item=i, foreach=True)
        log.iteration_completed((), "each", i, "k", foreach=True)
    log.step_completed((), "each", [])
    t = build_trace(TraceInput(root=RunData(run=workflow_run(), events=log.events, requests={})))
    by = {s["id"]: s for s in t["spans"]}
    _check_no_orphans(t, {f"{RUN}:step:each"})
    assert f"{RUN}:step:each.{MAX_LOOP_ITERATION_SPANS + 50}~triage" not in by
    assert by[f"{RUN}:step:each.3~triage"]["parent_id"] == f"{RUN}:loop:each.3"
    assert by[f"{RUN}:loop:each.3"]["parent_id"] == f"{RUN}:step:each"
    assert t["truncated"]["iterations"] == 1
    assert by[f"{RUN}:step:each"]["attributes"]["iterations_omitted"] == 1


def test_fully_reversed_log_nests_every_scoped_span_under_its_parent():
    events, children = _nested(2, 2)
    t = build_trace(TraceInput(
        root=RunData(run=workflow_run(), events=list(reversed(events)), requests={}), children=children,
    ))
    by = {s["id"]: s for s in t["spans"]}
    _check_no_orphans(t, {f"{RUN}:step:outer", f"{RUN}:step:after"})
    assert by[f"{RUN}:loop:outer.1/inner.0"]["parent_id"] == f"{RUN}:step:outer.1~inner"
    assert by[f"{RUN}:step:outer.1~inner.0~w"]["parent_id"] == f"{RUN}:loop:outer.1/inner.0"
    assert len(_spans(t, "loop_iteration")) == 2 + 4 and len(_spans(t, "child_run")) == 4


def test_malformed_scope_keys_make_no_top_level_span():
    log = Log()
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.add("loop:BAD:step:triage:started", "step_started", {"step_id": "triage", "step_type": "tool"})
    log.add("loop:BAD:step:triage:completed", "step_completed", {"step_id": "triage", "output": 1})
    log.add("loop::step:triage:started", "step_started", {"step_id": "triage", "step_type": "tool"})
    log.add("loop:a.0:step:triage", "step_started", {"step_id": "triage", "step_type": "tool"})
    log.add("loop:a.01:started", "loop_iteration_started", {"step_id": "a", "index": 1})
    log.add("step:triage:completed", "step_completed", {"step_id": "triage", "output": 2})
    t = build_trace(TraceInput(root=RunData(run=workflow_run(), events=log.events, requests={})))
    assert [s["id"] for s in t["spans"]] == [f"run:{RUN}"]


def test_scoped_child_run_without_its_step_is_dropped():
    events, children = _nested(1, 1)
    events = [e for e in events if not e["event_key"].startswith("loop:outer.0/inner.0:step:w:")]
    t = build_trace(TraceInput(root=RunData(run=workflow_run(), events=events, requests={}), children=children))
    _check_no_orphans(t, {f"{RUN}:step:outer", f"{RUN}:step:after"})
    assert _spans(t, "child_run") == []


# ---------------------------------------------------------------------------- the reader (Postgres)

T0 = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(
            text("UPDATE runs SET parent_run_id = NULL, parent_step_id = NULL WHERE tenant_id = :t"), {"t": tenant_id}
        )
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _seed(engine, event_log, tenant, run_id, events) -> None:
    for key, type_, data in events:
        event_log.append(tenant, UUID(str(run_id)), key, type_, data, None)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = :t0 + make_interval(secs => seq) WHERE run_id = :r"),
            {"t0": T0, "r": str(run_id)},
        )


def _seed_nested(engine, run_store, event_log, tenant, n_outer, n_inner):
    """The `_nested` shape in Postgres, with a child agent run per inner iteration."""
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    events = [("start", "run_started", {"kind": "workflow", "input": {}}),
              ("step:outer:started", "step_started", {"step_id": "outer", "step_type": "foreach"})]
    children: dict[str, UUID] = {}
    for i in range(n_outer):
        o = child_scope((), "outer", i)
        events.append((iteration_event_key(o, "started"), "loop_iteration_started",
                       {"step_id": "outer", "scope": [], "index": i, "item": {"secret": f"item-{i}"}}))
        events.append((scoped_event_key(o, "inner", "started"), "step_started",
                       {"step_id": "inner", "step_type": "loop", "scope": scope_to_data(o)}))
        for j in range(n_inner):
            s = child_scope(o, "inner", j)
            child = uuid4()
            run_store.create_child_agent_run(tenant, uuid4(), 1, "q", child, run_id, scoped_parent_step_id(s, "w"))
            children[scoped_parent_step_id(s, "w")] = child
            events += [
                (iteration_event_key(s, "started"), "loop_iteration_started",
                 {"step_id": "inner", "scope": scope_to_data(o), "index": j, "state": {"j": j}}),
                (scoped_event_key(s, "w", "started"), "step_started",
                 {"step_id": "w", "step_type": "agent", "scope": scope_to_data(s), "child_run_id": str(child)}),
                (scoped_event_key(s, "w", "completed"), "step_completed",
                 {"step_id": "w", "scope": scope_to_data(s), "output": "a"}),
                (iteration_event_key(s, "completed"), "loop_iteration_completed",
                 {"step_id": "inner", "scope": scope_to_data(o), "index": j, "state": {"j": j + 1}}),
            ]
        events += [
            (scoped_event_key(o, "inner", "completed"), "step_completed",
             {"step_id": "inner", "scope": scope_to_data(o), "output": {"iterations": n_inner}}),
            (iteration_event_key(o, "completed"), "loop_iteration_completed",
             {"step_id": "outer", "scope": [], "index": i}),
        ]
    events += [("step:outer:completed", "step_completed", {"step_id": "outer", "output": []}),
               ("end", "run_completed", {"output": "ok"})]
    _seed(engine, event_log, tenant, run_id, events)
    return run_id, children, len(events)


def test_reader_keeps_the_end_event_and_loads_only_shown_iterations_child_runs(
    engine, run_store, event_log, tenant, monkeypatch
):
    run_id, children, n_events = _seed_nested(engine, run_store, event_log, tenant, 6, 6)
    rid = str(run_id)
    # A loop budget small enough that this run is cut the way a 100x100 one is by the defaults.
    monkeypatch.setattr(trace_mod, "MAX_LOOP_TRACE_EVENTS", 40)
    monkeypatch.setattr(trace_mod, "MAX_TRACE_EVENTS", 90)

    loaded = TraceReader(engine).load(tenant, run_id)
    keys = [e["event_key"] for e in loaded.root.events]
    assert "end" in keys and "step:outer:completed" in keys
    assert not loaded.root.events_truncated
    assert len(loaded.root.events) < n_events and len(loaded.root.events) <= 40 + 10
    # Payloads of kept loop events are fetched; left-out ones are never loaded.
    first = next(e for e in loaded.root.events if e["event_key"] == "loop:outer.0:started")
    assert first["data"]["item"] == {"secret": "item-0"}
    assert all(e["data"] is not None for e in loaded.root.events)
    shown_steps = {e["event_key"].removeprefix("loop:").removesuffix(":step:w:started").replace(":", "")
                   for e in loaded.root.events if e["event_key"].endswith(":step:w:started")}
    assert {c.run["parent_step_id"] for c in loaded.children} == {f"{s}/w" for s in shown_steps}
    assert 0 < len(loaded.children) < len(children) and not loaded.children_truncated

    t = TraceReader(engine).read(tenant, run_id)
    by = {s["id"]: s for s in t["spans"]}
    _check_no_orphans(t, {f"{rid}:step:outer"})
    assert by[f"run:{rid}"]["output"] == "ok"
    outer = by[f"{rid}:step:outer"]
    assert outer["status"] == "ok"
    assert outer["attributes"]["iterations_started"] == outer["attributes"]["iterations_completed"] == 6
    shown_iterations = _spans(t, "loop_iteration")
    omitted = outer["attributes"]["iterations_omitted"] + sum(
        by[f"{rid}:step:{s['attributes']['scope']}~inner"]["attributes"]["iterations_omitted"]
        for s in shown_iterations if s["attributes"]["step_id"] == "outer"
    )
    assert t["truncated"]["iterations"] == omitted > 0
    assert t["truncated"]["events"] is False and t["truncated"]["child_runs"] is False
    assert len(_spans(t, "child_run")) == len(loaded.children)


def test_reader_scan_cap_bounds_the_rows_read(engine, run_store, event_log, tenant, monkeypatch):
    run_id, _, n_events = _seed_nested(engine, run_store, event_log, tenant, 2, 3)
    monkeypatch.setattr(trace_mod, "MAX_SCANNED_EVENTS_PER_RUN", 10)
    loaded = TraceReader(engine).load(tenant, run_id)
    assert loaded.root.events_truncated and len(loaded.root.events) <= 10 < n_events
