"""Agents as tools (eap_worker.agent_tools): a supervisor agent calls published agents as child runs.

In-process worker. Parents use the scripted `test:supervisor` model (one tool call per response, then an
answer that lists every tool result); children answer `Echo: <input>` (`test:echo`) or run their own spec.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_schemas import AgentSpec
from eap_schemas.agent_tools import agent_tool_child_run_id
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal
from worker_tests.test_tool_approval import TICKET_ARGS, gated_spec
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def supervisor_spec(*refs: dict[str, Any], name: str = "Boss") -> dict[str, Any]:
    spec = agent_spec("supervisor", name=name)
    spec["agent_tools"] = list(refs)
    AgentSpec.model_validate(spec)  # the spec is a valid AgentSpec
    return spec


def ask(*calls: tuple[str, str]) -> str:
    return json.dumps({"calls": [{"tool": tool, "input": text_} for tool, text_ in calls]})


def run_parent(worker, client, tenant, spec: dict[str, Any], input_text: str, *, agent_id: str | None = None,
               timeout: float = 90.0) -> dict[str, Any]:
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text, app_version=APP_VERSION,
        agent_id=uuid.UUID(agent_id) if agent_id else None,
    )
    try:
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=timeout)
    except AssertionError:  # show where it stood
        raise AssertionError(
            f"run did not finish: {worker.run_store.get_run(tenant, run_id)} events={event_types(worker.engine, run_id)}"
            f" children={children(worker.engine, run_id)}"
        ) from None
    run["_id"] = run_id
    return run


def children(engine, run_id) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, agent_id, agent_version, parent_run_id, parent_step_id, status, input, output, error,"
                 " request_context, conversation_id FROM runs WHERE parent_run_id = :r ORDER BY created_at"),
            {"r": str(run_id)},
        ).mappings()
        return [dict(r) for r in rows]


def event_types(engine, run_id) -> list[tuple[str, str]]:
    return [(k, t) for _s, k, t, _d in event_rows(engine, run_id)]


def test_a_parent_calls_a_child_agent_and_gets_its_output(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, agent_spec("echo", name="Echo Helper"))
    spec = supervisor_spec({"agent_id": child_id})
    run = run_parent(worker, client, tenant, spec, ask(("echo_helper", "ping")))
    assert run["status"] == "completed", run["error"]
    kids = children(worker.engine, run["_id"])
    assert len(kids) == 1
    kid = kids[0]
    call_id = kid["parent_step_id"]
    assert kid["id"] == agent_tool_child_run_id(str(run["_id"]), call_id)
    assert kid["agent_id"] == child_id and kid["agent_version"] == 1
    assert kid["status"] == "completed" and kid["output"] == "Echo: ping"
    assert run["output"] == f'Supervisor: ok: {{"output": "Echo: ping", "run_id": "{kid["id"]}"}}'
    events = {k: (t, d) for _s, k, t, d in event_rows(worker.engine, run["_id"])}
    started = events[f"agent_tool:{call_id}:started"]
    assert started[0] == "agent_tool_started"
    assert started[1]["child_run_id"] == kid["id"] and started[1]["version"] == 1 and started[1]["depth"] == 1
    completed = events[f"agent_tool:{call_id}:completed"]
    assert completed[0] == "agent_tool_completed" and completed[1]["status"] == "completed"
    assert completed[1]["child_run_id"] == kid["id"]
    # The child has its own events (its own run_started / run_completed).
    assert ("end", "run_completed") in event_types(worker.engine, kid["id"])


def test_the_tool_is_offered_with_the_override_name_and_description(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, agent_spec("echo", name="Echo Helper"))
    spec = supervisor_spec({"agent_id": child_id, "name": "ask_echo", "description": "Ask the echo desk."})
    spec["model"]["name"] = "mcp"  # `Tools offered: ...` in its answer
    run = run_parent(worker, client, tenant, spec, json.dumps({"tool": "ask_echo", "args": {"input": "hi"}}))
    assert run["status"] == "completed", run["error"]
    assert "Tools offered: ask_echo." in run["output"]
    assert '"output": "Echo: hi"' in run["output"]


def test_the_child_runs_its_own_spec_and_nothing_of_the_callers_context(worker, client, tenant, defs):
    child = agent_spec("ticket", tools=("tickets.create",), name="Ticketer")
    child_id = publish_agent(worker.engine, tenant, child)
    spec = supervisor_spec({"agent_id": child_id, "name": "ticketer"})
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=spec, input_text=ask(("ticketer", "my disk is full")),
        app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    (kid,) = children(worker.engine, run_id)
    assert kid["status"] == "completed"
    # The child used ITS tool (a ticket under its own run id), the caller has none.
    assert worker.tickets.ticket_count(tenant, f"{kid['id']}:") == 1
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 0
    # Only the input string crossed: the child's row has no context, conversation or model override.
    assert kid["input"] == "my disk is full"
    assert not kid["request_context"] and kid["conversation_id"] is None


def test_a_pinned_version_runs_that_version_and_latest_runs_the_newest(worker, client, tenant, defs):
    first = agent_spec("echo", name="Versioned")
    second = {**agent_spec("echo", name="Versioned"), "instructions": "Newer."}
    agent_id = publish_agent(worker.engine, tenant, first)
    publish_agent(worker.engine, tenant, second, agent_id=agent_id, version=2)
    pinned = run_parent(worker, client, tenant, supervisor_spec({"agent_id": agent_id, "version": 1}),
                        ask(("versioned", "a")))
    latest = run_parent(worker, client, tenant, supervisor_spec({"agent_id": agent_id}), ask(("versioned", "b")))
    assert children(worker.engine, pinned["_id"])[0]["agent_version"] == 1
    assert children(worker.engine, latest["_id"])[0]["agent_version"] == 2


def test_max_calls_per_run_refuses_the_extra_call_without_failing_the_run(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, agent_spec("echo", name="Echo Helper"))
    spec = supervisor_spec({"agent_id": child_id, "name": "echo", "max_calls_per_run": 2})
    run = run_parent(worker, client, tenant, spec, ask(("echo", "1"), ("echo", "2"), ("echo", "3")))
    assert run["status"] == "completed", run["error"]
    assert len(children(worker.engine, run["_id"])) == 2
    assert "error: [agent_tool_max_calls]" in run["output"]
    assert run["output"].count("ok: ") == 2
    completed = [d for _s, k, t, d in event_rows(worker.engine, run["_id"]) if t == "agent_tool_completed"]
    assert [c["status"] for c in completed] == ["completed", "completed", "refused"]
    assert completed[2]["error_code"] == "agent_tool_max_calls"


def test_the_depth_cap_refuses_a_chain_that_is_too_deep(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_AGENT_TOOL_MAX_DEPTH", "1")
    leaf = publish_agent(worker.engine, tenant, agent_spec("echo", name="Leaf"))
    middle = publish_agent(
        worker.engine, tenant, supervisor_spec({"agent_id": leaf, "name": "leaf"}, name="Middle")
    )
    top = supervisor_spec({"agent_id": middle, "name": "middle"})
    run = run_parent(worker, client, tenant, top, ask(("middle", ask(("leaf", "deep")))))
    assert run["status"] == "completed", run["error"]
    (kid,) = children(worker.engine, run["_id"])
    assert kid["status"] == "completed"  # the middle agent ran (level 1) ...
    assert "error: [agent_tool_depth_exceeded]" in kid["output"]  # ... and its own call (level 2) was refused
    assert children(worker.engine, kid["id"]) == []


def test_a_chain_within_the_depth_cap_runs_to_the_leaf(worker, client, tenant, defs, monkeypatch):
    monkeypatch.setenv("EAP_AGENT_TOOL_MAX_DEPTH", "2")
    leaf = publish_agent(worker.engine, tenant, agent_spec("echo", name="Leaf"))
    middle = publish_agent(
        worker.engine, tenant, supervisor_spec({"agent_id": leaf, "name": "leaf"}, name="Middle")
    )
    run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": middle, "name": "middle"}),
                     ask(("middle", ask(("leaf", "deep")))))
    (mid,) = children(worker.engine, run["_id"])
    (leaf_run,) = children(worker.engine, mid["id"])
    assert leaf_run["output"] == "Echo: deep" and leaf_run["status"] == "completed"
    started = [d for _s, _k, t, d in event_rows(worker.engine, mid["id"]) if t == "agent_tool_started"]
    assert started[0]["depth"] == 2


def test_a_timeout_cancels_the_child_and_returns_a_readable_error(worker, client, tenant, defs):
    # The child waits for an approval nobody gives; the caller's timeout ends the wait.
    child = {**gated_spec(), "name": "Slow"}
    child_id = publish_agent(worker.engine, tenant, child)
    spec = supervisor_spec({"agent_id": child_id, "name": "slow", "timeout_seconds": 2})
    prompt = json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})
    run = run_parent(worker, client, tenant, spec, ask(("slow", prompt)), timeout=120)
    assert run["status"] == "completed", run["error"]
    assert "error: [agent_tool_timeout]" in run["output"]
    (kid,) = children(worker.engine, run["_id"])
    assert kid["status"] == "failed"
    end = [d for _s, k, _t, d in event_rows(worker.engine, kid["id"]) if k == "end"]
    assert end and end[0]["error_code"] == "run_cancelled"
    completed = [d for _s, _k, t, d in event_rows(worker.engine, run["_id"]) if t == "agent_tool_completed"]
    assert completed[0]["status"] == "timeout" and completed[0]["child_run_id"] == kid["id"]
    assert worker.tickets.ticket_count(tenant, f"{kid['id']}:") == 0  # the gated tool never ran


def test_a_failing_child_is_a_tool_error_not_a_run_failure(worker, client, tenant, defs):
    broken = {**agent_spec("fail-400", name="Broken")}
    child_id = publish_agent(worker.engine, tenant, broken)
    run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": child_id, "name": "broken"}),
                     ask(("broken", "go")))
    assert run["status"] == "completed", run["error"]
    assert "error: [agent_tool_failed]" in run["output"]
    (kid,) = children(worker.engine, run["_id"])
    assert kid["status"] == "failed"


def test_an_archived_target_is_refused_at_call_time(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, agent_spec("echo", name="Echo Helper"))
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :i"), {"i": child_id})
    run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": child_id, "name": "echo"}),
                     ask(("echo", "x")))
    assert run["status"] == "completed", run["error"]
    assert "error: [agent_tool_agent_archived]" in run["output"]
    assert children(worker.engine, run["_id"]) == []


def test_another_tenants_agent_is_never_called(worker, client, tenant, defs):
    foreign = publish_agent(worker.engine, f"{tenant}-other", agent_spec("echo", name="Foreign"))
    try:
        run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": foreign, "name": "foreign"}),
                         ask(("foreign", "x")))
        assert run["status"] == "completed", run["error"]
        assert "error: [agent_tool_unknown_agent]" in run["output"]
        assert children(worker.engine, run["_id"]) == []
        with worker.engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM runs WHERE agent_id = :a"), {"a": foreign}
            ).scalar_one() == 0
    finally:
        cleanup_definitions(worker.engine, f"{tenant}-other")


def test_a_long_child_output_is_cut_with_a_marker(worker, client, tenant, defs):
    from eap_worker.agent_tools import MAX_OUTPUT_BYTES

    child_id = publish_agent(worker.engine, tenant, agent_spec("echo", name="Echo Helper"))
    big = "x" * (MAX_OUTPUT_BYTES + 500)
    run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": child_id, "name": "echo"}),
                     ask(("echo", big)))
    assert run["status"] == "completed", run["error"]
    assert "[output truncated: " in run["output"]
    completed = [d for _s, _k, t, d in event_rows(worker.engine, run["_id"]) if t == "agent_tool_completed"]
    assert completed[0]["truncated"] is True


def test_the_child_spans_nest_in_the_parents_trace_and_cost_is_summed(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, agent_spec("priced", name="Priced Helper"))
    spec = supervisor_spec({"agent_id": child_id, "name": "priced_helper"})
    run = run_parent(worker, client, tenant, spec, ask(("priced_helper", "hello")))
    assert run["status"] == "completed", run["error"]
    from eap_run_store.trace import TraceReader

    trace = TraceReader(worker.engine).read(tenant, run["_id"])
    spans = trace["spans"]
    (kid,) = children(worker.engine, run["_id"])
    tool_span = next(s for s in spans if s["kind"] == "tool_call" and s["attributes"].get("tool_call_id") == kid["parent_step_id"])
    child_span = next(s for s in spans if s["id"] == f"run:{kid['id']}")
    assert child_span["kind"] == "child_run" and child_span["parent_id"] == tool_span["id"]
    assert tool_span["attributes"]["child_run_id"] == kid["id"]
    # The child's usage stays on the child run; the trace total includes it.
    assert trace["totals"]["input_tokens"] >= 1000
    assert trace["totals"]["runs"] == 2


POST_SCHEMA = {
    "type": "object",
    "properties": {"post": {"type": "string"}, "alt_text": {"type": "string"}, "refusal": {"type": "boolean"}},
    "required": ["post", "alt_text", "refusal"],
    "additionalProperties": False,
}


def structured_child(name: str = "Poster") -> dict[str, Any]:
    spec = agent_spec("structured", name=name)
    spec["output"] = {"json_schema": POST_SCHEMA}
    return spec


def test_a_structured_child_output_reaches_the_parent_as_a_json_object(worker, client, tenant, defs):
    child_id = publish_agent(worker.engine, tenant, structured_child())
    wanted = {"post": "It's \u00fcber \"quoted\"", "alt_text": "a cat", "refusal": False}
    spec = supervisor_spec({"agent_id": child_id, "name": "poster"})
    run = run_parent(worker, client, tenant, spec, ask(("poster", json.dumps({"output": wanted}))))
    assert run["status"] == "completed", run["error"]
    (kid,) = children(worker.engine, run["_id"])
    assert kid["status"] == "completed"
    # The tool result holds the object itself (real JSON: double quotes, false), not a Python repr or JSON text.
    assert run["output"] == "Supervisor: ok: " + json.dumps({"output": wanted, "run_id": kid["id"]}, sort_keys=True)
    assert "True" not in run["output"] and "False" not in run["output"] and "None" not in run["output"]
    (call,) = [d for _s, _k, t, d in event_rows(worker.engine, run["_id"]) if t == "tool_result"]
    assert call["content"] == {"output": wanted, "run_id": kid["id"]}


def test_a_large_structured_child_output_is_cut_as_json_text_with_the_marker(worker, client, tenant, defs):
    from eap_worker.agent_tools import MAX_OUTPUT_BYTES

    child_id = publish_agent(worker.engine, tenant, structured_child())
    big = {"post": "x" * (MAX_OUTPUT_BYTES + 500), "alt_text": "a", "refusal": False}
    run = run_parent(worker, client, tenant, supervisor_spec({"agent_id": child_id, "name": "poster"}),
                     ask(("poster", json.dumps({"output": big}))))
    assert run["status"] == "completed", run["error"]
    (call,) = [d for _s, _k, t, d in event_rows(worker.engine, run["_id"]) if t == "tool_result"]
    text_ = call["content"]["output"]
    assert isinstance(text_, str) and text_.startswith("{") and '"post": "xxx' in text_  # JSON text, double quotes
    assert "[output truncated: " in text_
    completed = [d for _s, _k, t, d in event_rows(worker.engine, run["_id"]) if t == "agent_tool_completed"]
    assert completed[0]["truncated"] is True


def test_bounded_output_keeps_small_objects_and_cuts_text():
    from eap_worker.agent_tools import MAX_OUTPUT_BYTES, bounded_output

    assert bounded_output({"a": [1, None, True]}) == ({"a": [1, None, True]}, False)
    assert bounded_output("plain") == ("plain", False)
    assert bounded_output(None) == ("", False)
    text_, cut = bounded_output({"k": "é" * MAX_OUTPUT_BYTES})
    assert cut and isinstance(text_, str) and text_.startswith('{"k": "é') and "[output truncated: " in text_
