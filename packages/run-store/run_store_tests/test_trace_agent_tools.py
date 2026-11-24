"""Agents as tools in the trace: the child runs of an agent run nest under the tool call that started
them, a child's own children nest further, and the totals sum every descendant."""

from __future__ import annotations

from uuid import UUID, uuid4

from eap_run_store import trace as trace_mod
from eap_run_store.trace import TraceReader

from .test_trace import AGENT_EVENTS, _by_id, _finish, _model_event, _seed, _usage, tenant  # noqa: F401 - fixture


def _caller_events(call_id: str, child: UUID, *, tool: str = "ask_helper", started: bool = True) -> list:
    events = [
        ("start", "run_started", {"agent_id": "a", "version": 1, "input": "go"}),
        ("model:0:final", "model_message", _model_event(0, "", [(call_id, tool)])),
        (f"tool:{call_id}:call", "tool_call", {"tool_call_id": call_id, "tool_name": tool, "args": {"input": "x"}}),
    ]
    if started:
        events.append((f"agent_tool:{call_id}:started", "agent_tool_started", {
            "tool_call_id": call_id, "tool_name": tool, "agent_id": "b", "version": 2, "child_run_id": str(child),
            "depth": 1, "timeout_seconds": 300,
        }))
    events += [
        (f"agent_tool:{call_id}:completed", "agent_tool_completed", {
            "tool_call_id": call_id, "tool_name": tool, "child_run_id": str(child), "status": "completed",
        }),
        (f"tool:{call_id}:result", "tool_result", {"tool_call_id": call_id, "tool_name": tool, "status": "ok",
                                                    "content": {"output": "Done", "run_id": str(child)}}),
        ("model:1:final", "model_message", _model_event(1, "All done.")),
        ("end", "run_completed", {"output": "All done."}),
    ]
    return events


def test_a_child_run_nests_under_its_tool_call_with_the_link_attributes(engine, run_store, event_log, tenant):
    parent, child = uuid4(), uuid4()
    run_store.create_run(tenant, uuid4(), 1, "go", parent)
    run_store.create_child_agent_run(tenant, uuid4(), 2, "x", child, parent, "call_1")
    _seed(engine, event_log, tenant, parent, _caller_events("call_1", child),
          {"model:0:final": _usage("model:0", 10, 5, "0.001"), "model:1:final": _usage("model:1", 20, 5, "0.002")})
    _seed(engine, event_log, tenant, child, AGENT_EVENTS, {"model:0:final": _usage("model:0", 100, 50, "0.01")})
    _finish(engine, run_store, tenant, parent)
    _finish(engine, run_store, tenant, child)

    trace = TraceReader(engine).read(tenant, parent)
    spans = _by_id(trace)
    tool = spans[f"{parent}:tool:call_1"]
    assert tool["kind"] == "tool_call"
    assert tool["attributes"]["agent_tool"] is True and tool["attributes"]["child_run_id"] == str(child)
    assert tool["attributes"]["agent_tool_version"] == 2 and tool["attributes"]["agent_tool_depth"] == 1
    assert tool["attributes"]["agent_tool_status"] == "completed"
    child_span = spans[f"run:{child}"]
    assert child_span["kind"] == "child_run" and child_span["parent_id"] == tool["id"]
    # the child's own spans hang under it
    assert any(s["parent_id"] == child_span["id"] and s["kind"] == "model_request" for s in trace["spans"])
    # totals include the child's usage; the run row's own usage does not
    assert trace["totals"]["runs"] == 2
    assert trace["totals"]["input_tokens"] == 10 + 20 + 100


def test_grandchildren_are_read_through_agent_runs(engine, run_store, event_log, tenant):
    top, mid, leaf = uuid4(), uuid4(), uuid4()
    run_store.create_run(tenant, uuid4(), 1, "go", top)
    run_store.create_child_agent_run(tenant, uuid4(), 1, "x", mid, top, "call_a")
    run_store.create_child_agent_run(tenant, uuid4(), 1, "y", leaf, mid, "call_b")
    _seed(engine, event_log, tenant, top, _caller_events("call_a", mid))
    _seed(engine, event_log, tenant, mid, _caller_events("call_b", leaf, tool="ask_leaf"))
    _seed(engine, event_log, tenant, leaf, AGENT_EVENTS, {"model:0:final": _usage("model:0", 7, 3, "0.001")})
    for run_id in (top, mid, leaf):
        _finish(engine, run_store, tenant, run_id)

    trace = TraceReader(engine).read(tenant, top)
    spans = _by_id(trace)
    assert spans[f"run:{mid}"]["parent_id"] == f"{top}:tool:call_a"
    assert spans[f"run:{leaf}"]["parent_id"] == f"{mid}:tool:call_b"
    assert trace["totals"]["runs"] == 3 and trace["totals"]["input_tokens"] == 7
    # reading the middle run alone shows its own child
    middle = TraceReader(engine).read(tenant, mid)
    assert _by_id(middle)[f"run:{leaf}"]["parent_id"] == f"{mid}:tool:call_b"


def test_a_refused_call_has_attributes_and_no_child(engine, run_store, event_log, tenant):
    parent = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "go", parent)
    events = _caller_events("call_1", parent, started=False)
    events[3] = ("agent_tool:call_1:completed", "agent_tool_completed", {
        "tool_call_id": "call_1", "tool_name": "ask_helper", "status": "refused", "error_code": "agent_tool_max_calls",
    })
    _seed(engine, event_log, tenant, parent, events)
    _finish(engine, run_store, tenant, parent)
    tool = _by_id(TraceReader(engine).read(tenant, parent))[f"{parent}:tool:call_1"]
    assert tool["attributes"]["agent_tool"] is True
    assert tool["attributes"]["agent_tool_status"] == "refused"
    assert tool["attributes"]["agent_tool_error_code"] == "agent_tool_max_calls"
    assert "child_run_id" not in tool["attributes"]


def test_the_level_and_child_budgets_bound_the_walk(engine, run_store, event_log, tenant, monkeypatch):
    chain = [uuid4() for _ in range(4)]
    run_store.create_run(tenant, uuid4(), 1, "go", chain[0])
    for i in range(1, 4):
        run_store.create_child_agent_run(tenant, uuid4(), 1, "x", chain[i], chain[i - 1], f"call_{i}")
    for i in range(4):
        _seed(engine, event_log, tenant, chain[i], AGENT_EVENTS[:2])
    monkeypatch.setattr(trace_mod, "MAX_AGENT_TOOL_LEVELS", 1)
    loaded = TraceReader(engine).load(tenant, chain[0])
    assert len(loaded.children) == 2 and loaded.children_truncated  # chain[1] and chain[2]; chain[3] is past the levels
    monkeypatch.setattr(trace_mod, "MAX_AGENT_TOOL_LEVELS", 8)
    monkeypatch.setattr(trace_mod, "MAX_CHILD_RUNS", 2)
    loaded = TraceReader(engine).load(tenant, chain[0])
    assert len(loaded.children) == 2 and loaded.children_truncated
