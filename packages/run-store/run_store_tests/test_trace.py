"""P0.7: the event-derived trace (eap_run_store.trace) on seeded runs, events and usage rows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from eap_run_store import trace as trace_mod
from eap_run_store.trace import TraceReader, step_parents

T0 = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(
            text("UPDATE runs SET parent_run_id = NULL, parent_step_id = NULL WHERE tenant_id = :t"), {"t": tenant_id}
        )
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _usage(key: str, inp: int, out: int, cost: str | None, model: str = "test:ticket") -> dict:
    return {
        "request_key": key, "model_id": model, "provider_model_name": "scripted", "input_tokens": inp,
        "output_tokens": out, "cache_read_tokens": 1, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": None if cost is None else "override",
    }


def _seed(engine, event_log, tenant, run_id, events, usage=None) -> None:
    """Append (key, type, data) events, then pin their ts to T0 + seq seconds."""
    usage = usage or {}
    for key, type_, data in events:
        event_log.append(tenant, UUID(str(run_id)), key, type_, data, usage.get(key))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = :t0 + make_interval(secs => seq) WHERE run_id = :r"),
            {"t0": T0, "r": str(run_id)},
        )


def _finish(engine, run_store, tenant, run_id, status="completed", output="done", seconds=100) -> None:
    run_store.mark_running(tenant, UUID(str(run_id)))
    run_store.set_status(tenant, UUID(str(run_id)), status, output=output if status == "completed" else None,
                         error=None if status == "completed" else "Boom: failed")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET started_at = :s, finished_at = :f WHERE id = :r"),
            {"s": T0, "f": T0 + timedelta(seconds=seconds), "r": str(run_id)},
        )


def _model_event(n: int, text_: str, calls: list[tuple[str, str]] = ()) -> dict:
    parts = [{"part_kind": "text", "content": text_}] if text_ else []
    parts += [{"part_kind": "tool-call", "tool_name": name, "tool_call_id": cid, "args": {"q": n}} for cid, name in calls]
    return {"model_request": n, "text": text_, "message": {"kind": "response", "parts": parts}}


AGENT_EVENTS = [
    ("start", "run_started", {"agent_id": "a", "version": 1, "input": "printer is on fire"}),
    ("model:0:final", "model_message", _model_event(0, "", [("c1", "tickets__create")])),
    ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "tickets__create", "args": {"q": 0}}),
    ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "tickets__create", "status": "ok",
                                       "content": {"id": "TCK-1"}}),
    ("model:1:final", "model_message", _model_event(1, "Created TCK-1.")),
    ("end", "run_completed", {"output": "Created TCK-1."}),
]


def _by_id(trace):
    return {s["id"]: s for s in trace["spans"]}


def test_agent_run_with_tools(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "printer is on fire", run_id)
    _seed(engine, event_log, tenant, run_id, AGENT_EVENTS,
          {"model:0:final": _usage("model:0", 10, 5, "0.001"), "model:1:final": _usage("model:1", 20, 7, "0.002")})
    _finish(engine, run_store, tenant, run_id)

    trace = TraceReader(engine).read(tenant, run_id)
    spans = _by_id(trace)
    rid = str(run_id)
    assert [s["kind"] for s in trace["spans"]] == ["run", "model_request", "tool_call", "model_request"]
    root = spans[f"run:{rid}"]
    assert root["parent_id"] is None and root["status"] == "ok"
    assert root["start"] == T0.isoformat() and root["end"] == (T0 + timedelta(seconds=100)).isoformat()
    assert root["input"] == "printer is on fire" and root["output"] == "Created TCK-1."

    m0, tool, m1 = spans[f"{rid}:model:0"], spans[f"{rid}:tool:c1"], spans[f"{rid}:model:1"]
    assert {m0["parent_id"], tool["parent_id"], m1["parent_id"]} == {f"run:{rid}"}
    # model n spans from the previous durable event to model:{n}:final; tool call -> result.
    assert (m0["start"], m0["end"]) == ((T0).isoformat(), (T0 + timedelta(seconds=1)).isoformat())
    assert (tool["start"], tool["end"]) == ((T0 + timedelta(seconds=2)).isoformat(), (T0 + timedelta(seconds=3)).isoformat())
    assert (m1["start"], m1["end"]) == ((T0 + timedelta(seconds=3)).isoformat(), (T0 + timedelta(seconds=4)).isoformat())
    assert m0["attributes"]["model_id"] == "test:ticket" and m0["attributes"]["cost_usd"] == "0.0010000000"
    assert m0["attributes"]["usage"] == {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 1,
                                         "cache_write_tokens": 0, "usage_measured": True}
    assert m0["input"] == "printer is on fire"
    assert m0["output"]["tool_calls"] == [{"tool_call_id": "c1", "tool_name": "tickets__create", "args": {"q": 0}}]
    assert m1["input"]["tool_results"][0]["content"] == {"id": "TCK-1"}
    assert m1["output"]["text"] == "Created TCK-1."
    assert tool["status"] == "ok" and tool["input"] == {"q": 0} and tool["output"] == {"id": "TCK-1"}
    assert tool["attributes"]["model_request"] == 0

    totals = trace["totals"]
    assert totals["model_requests"] == 2 and totals["tool_calls"] == 1
    assert (totals["input_tokens"], totals["output_tokens"], totals["cost_usd"]) == (30, 12, "0.0030000000")
    assert totals["duration_ms"] == 100_000
    assert trace["truncated"] == {"spans": False, "events": False, "child_runs": False, "payload_fields": 0}


def test_other_tenant_and_unknown_run_are_none(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    reader = TraceReader(engine)
    assert reader.read(tenant, run_id) is not None
    assert reader.read(f"{tenant}_other", run_id) is None
    assert reader.read(tenant, uuid4()) is None


def test_running_run_and_unrecorded_usage(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    run_store.mark_running(tenant, run_id)
    # Recorded before usage existed: no row, no usage fields in the event.
    _seed(engine, event_log, tenant, run_id, AGENT_EVENTS[:3])
    trace = TraceReader(engine).read(tenant, run_id)
    spans = _by_id(trace)
    rid = str(run_id)
    assert spans[f"run:{rid}"]["status"] == "running" and spans[f"run:{rid}"]["end"] is None
    assert spans[f"{rid}:model:0"]["attributes"]["usage"] is None
    assert spans[f"{rid}:model:0"]["attributes"]["cost_usd"] is None
    tool = spans[f"{rid}:tool:c1"]
    assert tool["status"] == "running" and tool["end"] is None
    totals = trace["totals"]
    assert totals["input_tokens"] is None and totals["cost_usd"] is None and totals["unrecorded_requests"] == 1


def test_failed_run_unpriced_request_and_incomplete_tool(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    _seed(engine, event_log, tenant, run_id,
          AGENT_EVENTS[:3] + [("end", "run_failed", {"status": "failed", "error": "Boom: x", "error_code": "tool_failed"})],
          {"model:0:final": _usage("model:0", 3, 4, None)})
    _finish(engine, run_store, tenant, run_id, status="failed")
    trace = TraceReader(engine).read(tenant, run_id)
    spans = _by_id(trace)
    rid = str(run_id)
    assert spans[f"run:{rid}"]["status"] == "error"
    assert spans[f"run:{rid}"]["attributes"]["error_code"] == "tool_failed"
    assert spans[f"{rid}:tool:c1"]["status"] == "incomplete"
    assert trace["totals"]["input_tokens"] == 3 and trace["totals"]["cost_usd"] is None
    assert trace["totals"]["unpriced_requests"] == 1


def test_tool_run(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_tool_run(tenant, "clock.now", "{}", run_id)
    call = {"tool_call_id": "tool-run", "tool_name": "clock_now", "tool_id": "clock.now"}
    _seed(engine, event_log, tenant, run_id, [
        ("start", "run_started", {"kind": "tool", "tool_id": "clock.now", "input": {}}),
        ("tool:tool-run:call", "tool_call", {**call, "args": {}}),
        ("tool:tool-run:result", "tool_result", {**call, "status": "ok", "content": "12:00"}),
        ("end", "run_completed", {"output": '"12:00"', "result": "12:00"}),
    ])
    _finish(engine, run_store, tenant, run_id, output='"12:00"')
    trace = TraceReader(engine).read(tenant, run_id)
    assert [s["kind"] for s in trace["spans"]] == ["run", "tool_call"]
    assert trace["spans"][0]["output"] == "12:00" and trace["spans"][0]["input"] == {}
    assert trace["spans"][1]["attributes"]["model_request"] is None
    assert trace["totals"]["model_requests"] == 0 and trace["totals"]["cost_usd"] == "0"


SPEC = {
    "name": "w",
    "start": "ask",
    "steps": [
        {"id": "ask", "type": "agent", "next": "fan"},
        {"id": "fan", "type": "parallel", "branches": ["b1", "b2"], "join": "approve"},
        {"id": "b1", "type": "tool", "next": "b1x"},
        {"id": "b1x", "type": "branch", "cases": [{"next": "inner"}], "default": "approve"},
        {"id": "inner", "type": "parallel", "branches": ["i1", "i2"], "join": "approve"},
        {"id": "i1", "type": "tool"},
        {"id": "i2", "type": "tool"},
        {"id": "b2", "type": "agent"},
        {"id": "approve", "type": "approval", "next": "done"},
        {"id": "done", "type": "tool"},
    ],
}


def test_step_parents_nests_branches_innermost():
    assert step_parents(SPEC) == {"b1": "fan", "b1x": "fan", "inner": "fan", "b2": "fan", "i1": "inner", "i2": "inner"}
    assert step_parents(None) == {} and step_parents({"steps": "junk"}) == {}


def test_workflow_run_with_child_runs_and_branches(engine, run_store, event_log, tenant):
    wf = uuid4()
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, '{"q":"x"}', run_id)
    rid = str(run_id)
    child = uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 3, "x", child, run_id, "ask")
    other_child = uuid4()  # b2's child run
    run_store.create_child_agent_run(tenant, uuid4(), 3, "y", other_child, run_id, "b2")

    def step(sid, stype, **extra):
        return (f"step:{sid}:started", "step_started", {"step_id": sid, "step_type": stype, **extra})

    _seed(engine, event_log, tenant, run_id, [
        ("start", "run_started", {"kind": "workflow", "input": {"q": "x"}}),
        step("ask", "agent", input={"p": "x"}, child_run_id=str(child)),
        ("step:ask:completed", "step_completed", {"step_id": "ask", "output": "answer"}),
        step("fan", "parallel"),
        step("b1", "tool", input={}, tool_id="clock.now"),
        step("b2", "agent", input={}, child_run_id=str(other_child)),
        ("step:b1:completed", "step_completed", {"step_id": "b1", "output": "12:00"}),
        ("step:b2:completed", "step_completed", {"step_id": "b2", "output": "ok"}),
        ("step:fan:completed", "step_completed", {"step_id": "fan", "output": None}),
        step("approve", "approval"),
        ("step:approve:waiting", "step_waiting", {"step_id": "approve", "prompt": "OK?", "timeout_hours": 1,
                                                  "deadline": "2026-11-13T13:00:00+00:00"}),
    ])
    _seed(engine, event_log, tenant, child, AGENT_EVENTS, {"model:0:final": _usage("model:0", 10, 5, "0.001"),
                                                           "model:1:final": _usage("model:1", 20, 7, "0.002")})
    _finish(engine, run_store, tenant, child)
    run_store.mark_running(tenant, run_id)

    trace = TraceReader(engine).read(tenant, run_id, workflow_spec=lambda w, v: SPEC)
    spans = _by_id(trace)
    root = f"run:{rid}"
    assert spans[f"{rid}:step:ask"]["parent_id"] == root
    assert spans[f"{rid}:step:fan"]["parent_id"] == root
    assert spans[f"{rid}:step:b1"]["parent_id"] == f"{rid}:step:fan"
    assert spans[f"{rid}:step:b2"]["parent_id"] == f"{rid}:step:fan"
    assert spans[f"run:{child}"]["kind"] == "child_run" and spans[f"run:{child}"]["parent_id"] == f"{rid}:step:ask"
    assert spans[f"run:{other_child}"]["parent_id"] == f"{rid}:step:b2"
    assert spans[f"{child}:model:0"]["parent_id"] == f"run:{child}"
    assert spans[f"{child}:model:1"]["attributes"]["cost_usd"] == "0.0020000000"
    approve = spans[f"{rid}:step:approve"]
    assert approve["status"] == "waiting" and approve["attributes"]["approval"]["prompt"] == "OK?"
    assert spans[root]["status"] == "running" and spans[root]["input"] == {"q": "x"}
    # Parents always precede their children.
    seen = set()
    for s in trace["spans"]:
        assert s["parent_id"] is None or s["parent_id"] in seen
        seen.add(s["id"])
    assert trace["totals"]["runs"] == 3 and trace["totals"]["cost_usd"] == "0.0030000000"

    # Without the spec, branch steps hang off the run.
    flat = _by_id(TraceReader(engine).read(tenant, run_id))
    assert flat[f"{rid}:step:b1"]["parent_id"] == root

    # The child's own trace: a root run span with its parent link in attributes.
    own = TraceReader(engine).read(tenant, child)
    assert own["spans"][0]["kind"] == "run" and own["spans"][0]["attributes"]["parent_run_id"] == rid
    assert TraceReader(engine).read(f"{tenant}_other", child) is None


def test_payload_truncation_and_span_cap(engine, run_store, event_log, tenant, monkeypatch):
    run_id = uuid4()
    big = "x" * (trace_mod.FIELD_MAX_BYTES + 10)
    run_store.create_run(tenant, uuid4(), 1, big, run_id)
    events = [("start", "run_started", {"input": big})]
    for i in range(5):
        events.append((f"tool:c{i}:call", "tool_call", {"tool_call_id": f"c{i}", "tool_name": "t", "args": {}}))
    _seed(engine, event_log, tenant, run_id, events)
    trace = TraceReader(engine).read(tenant, run_id)
    root = trace["spans"][0]
    assert root["truncated"] == {"input": len(big)}
    assert root["input"] == big[: trace_mod.FIELD_MAX_BYTES]
    assert trace["truncated"]["payload_fields"] == 1

    monkeypatch.setattr(trace_mod, "MAX_SPANS", 3)
    capped = TraceReader(engine).read(tenant, run_id)
    assert len(capped["spans"]) == 3 and capped["truncated"]["spans"] is True


def test_trace_event_budget_stops_loading_children(engine, run_store, event_log, tenant, monkeypatch):
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    first, second = uuid4(), uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, "a", first, run_id, "s1")
    run_store.create_child_agent_run(tenant, uuid4(), 1, "b", second, run_id, "s2")
    _seed(engine, event_log, tenant, run_id, [
        ("start", "run_started", {"kind": "workflow", "input": {}}),
        ("step:s1:started", "step_started", {"step_id": "s1", "step_type": "agent", "child_run_id": str(first)}),
        ("step:s2:started", "step_started", {"step_id": "s2", "step_type": "agent", "child_run_id": str(second)}),
    ])
    _seed(engine, event_log, tenant, first, AGENT_EVENTS[:4])
    _seed(engine, event_log, tenant, second, AGENT_EVENTS[:2])

    monkeypatch.setattr(trace_mod, "MAX_TRACE_EVENTS", 5)
    loaded = TraceReader(engine).load(tenant, run_id)
    # Root: 3 events (budget 2 left); first child cut to 2 of 4; second child never loaded.
    assert len(loaded.root.events) == 3 and not loaded.root.events_truncated
    assert [len(c.events) for c in loaded.children] == [2] and loaded.children[0].events_truncated
    assert loaded.children_truncated

    trace = TraceReader(engine).read(tenant, run_id)
    assert trace["truncated"]["events"] is True and trace["truncated"]["child_runs"] is True
    ids = {s["id"] for s in trace["spans"]}
    assert f"run:{first}" in ids and f"run:{second}" not in ids
    assert trace["totals"]["runs"] == 2 and trace["limits"]["max_trace_events"] == 5

    # Within budget: everything is loaded, nothing is flagged.
    monkeypatch.setattr(trace_mod, "MAX_TRACE_EVENTS", 100)
    full = TraceReader(engine).read(tenant, run_id)
    assert full["truncated"]["events"] is False and full["truncated"]["child_runs"] is False
    assert full["totals"]["runs"] == 3


def test_statement_timeout_raises_trace_query_timeout(engine, run_store, tenant):
    import pickle

    from eap_run_store.trace import TraceQueryTimeout

    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    # Lock waits count toward statement_timeout: hold an ACCESS EXCLUSIVE lock on runs meanwhile.
    with engine.connect() as locker:
        tx = locker.begin()
        locker.execute(text("LOCK TABLE runs IN ACCESS EXCLUSIVE MODE"))
        try:
            with pytest.raises(TraceQueryTimeout) as caught:
                TraceReader(engine, statement_timeout_ms=200).read(tenant, run_id)
        finally:
            tx.rollback()
    assert caught.value.timeout_ms == 200 and "200 ms" in str(caught.value)
    again = pickle.loads(pickle.dumps(caught.value))
    assert isinstance(again, TraceQueryTimeout) and again.timeout_ms == 200
    # The lock is gone: the same read succeeds.
    assert TraceReader(engine, statement_timeout_ms=200).read(tenant, run_id) is not None


def test_processor_spans_and_moderation_usage(engine, run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "my email is a@b.c", run_id)
    pii = {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "redact",
           "match_counts": {"email": 1}, "text": "SECRET a@b.c"}
    mid = {"phase": "output", "index": 0, "processor_type": "max_length", "action": "pass", "match_counts": {}}
    mod = {"phase": "output", "index": 1, "processor_type": "moderation", "action": "block",
           "match_counts": {}, "categories": ["violence"], "model_id": "test:moderation",
           "usage": {"input_tokens": 7, "output_tokens": 3, "measured": True}, "cost_usd": "0.0005"}
    _seed(engine, event_log, tenant, run_id, [
        ("start", "run_started", {"input": "my email is [email]"}),
        ("processor:input:0", "processor_result", pii),
        # The worker's order: a response's output processor events come BEFORE its model:{n}:final.
        ("processor:output:0:model:0", "processor_result", mid),
        ("model:0:final", "model_message", _model_event(0, "", [("c1", "t")])),
        ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "t", "args": {}}),
        ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "t", "status": "ok", "content": 1}),
        ("processor:output:0:model:1", "processor_result", {**mid, "index": 0}),
        ("processor:output:1:model:1", "processor_result", mod),
        ("model:1:final", "model_message", _model_event(1, "FLAG_ME")),
        ("end", "run_failed", {"status": "failed", "error": "OutputBlocked: x", "error_code": "output_blocked"}),
    ], {"model:0:final": _usage("model:0", 10, 5, "0.001"), "model:1:final": _usage("model:1", 20, 7, "0.002"),
        "processor:output:1:model:1": _usage("moderation:output:1:model:1", 7, 3, "0.0005", model="test:moderation")})
    _finish(engine, run_store, tenant, run_id, status="failed")

    trace = TraceReader(engine).read(tenant, run_id)
    spans = _by_id(trace)
    rid = str(run_id)
    p_in = spans[f"{rid}:processor:input:0"]
    assert p_in["kind"] == "processor" and p_in["name"] == "pii_redact" and p_in["status"] == "ok"
    assert p_in["parent_id"] == f"run:{rid}" and p_in["start"] == p_in["end"]
    assert p_in["attributes"] == {"processor_key": "processor:input:0", "phase": "input", "index": 0,
                                  "processor_type": "pii_redact", "action": "redact", "match_counts": {"email": 1}}
    assert p_in["input"] is None and p_in["output"] is None
    assert "SECRET" not in str(trace)
    p_mid = spans[f"{rid}:processor:output:0:model:0"]
    assert p_mid["name"] == "max_length" and p_mid["attributes"]["model_request"] == 0
    assert "usage" not in p_mid["attributes"]
    p_mod = spans[f"{rid}:processor:output:1:model:1"]
    assert p_mod["name"] == "moderation" and p_mod["status"] == "error"
    attrs = p_mod["attributes"]
    assert attrs["categories"] == ["violence"] and attrs["request_key"] == "moderation:output:1:model:1"
    assert attrs["model_request"] == 1
    # Matched to the usage row (the event carries no price_source; only the row does).
    assert attrs["price_source"] == "override" and attrs["provider_model_name"] == "scripted"
    assert attrs["model_id"] == "test:moderation" and attrs["cost_usd"] == "0.0005000000"
    assert attrs["usage"]["input_tokens"] == 7 and attrs["usage"]["output_tokens"] == 3
    totals = trace["totals"]
    assert totals["model_requests"] == 3 and totals["moderation_requests"] == 1
    assert (totals["input_tokens"], totals["output_tokens"], totals["cost_usd"]) == (37, 15, "0.0035000000")


def test_model_span_covers_the_time_up_to_its_output_processor_events(engine, run_store, event_log, tenant):
    """Output processor events sit between a response and its model:{n}:final; the request's span
    starts at the previous non-output-processor event, not at the last processor check."""
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    check = {"phase": "output", "index": 0, "processor_type": "pii_redact", "action": "allow", "match_counts": {}}
    _seed(engine, event_log, tenant, run_id, [
        ("start", "run_started", {"input": "hi"}),                                          # T0 + 0 s
        ("processor:output:0:model:0", "processor_result", check),                          # T0 + 1 s
        ("processor:output:1:model:0", "processor_result", {**check, "index": 1}),          # T0 + 2 s
        ("model:0:final", "model_message", _model_event(0, "Echo: hi")),                    # T0 + 3 s
        ("end", "run_completed", {"output": "Echo: hi"}),
    ], {"model:0:final": _usage("model:0", 5, 3, "0.001")})
    _finish(engine, run_store, tenant, run_id)
    spans = _by_id(TraceReader(engine).read(tenant, run_id))
    rid = str(run_id)
    model = spans[f"{rid}:model:0"]
    first_check = spans[f"{rid}:processor:output:0:model:0"]

    def at(value) -> datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)

    # From `start` (T0 + 0 s), not from the last check (T0 + 2 s), to model:0:final (T0 + 3 s).
    assert at(model["start"]) == T0 and at(model["end"]) == T0 + timedelta(seconds=3)
    assert at(model["start"]) < at(first_check["start"]) == T0 + timedelta(seconds=1) <= at(model["end"])
