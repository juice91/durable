"""AGUI.7 acceptance (tester): worker client tools (plan D10), in-process worker.

Adds to the implementer's test_client_tools.py: several client calls in one response; client tools
beside server, MCP and approval-gated tools; limits (tool-call counts include client calls);
structured output; the invalid-args error result; security (no schemas or results in logs or
events beyond the contract); memory (one post-turn index/observe, only when the turn completes).
The scripted model is `test:frontend_tool`; `multi` (below) is a test-only wrapper that adds more
client calls to its response."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from eap_worker import models as worker_models
from eap_worker.client_tools import ARGS_RETRY_HEADING, NOT_EXECUTED_MESSAGE
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.mcp_helpers import mcp_spec, register_mcp_server
from worker_tests.test_client_tools import SCHEMA, WEATHER, client_turn, events, frontend_spec, new_conversation
from worker_tests.test_conversation import step_names
from worker_tests.test_mcp_tools import _insert_agent_and_version
from worker_tests.test_tool_approval import TICKET_ARGS, decide, side_effects, wait_waiting

CLOCK = {
    "name": "get_time",
    "description": "Ask the browser for the local time.",
    "parameters": {
        "type": "object",
        "properties": {"zone": {"type": "string", "minLength": 1}},
        "required": ["zone"],
        "additionalProperties": False,
    },
}


@pytest.fixture
def multi(monkeypatch):
    """Prompt JSON `{"multi": [<client tool names>]}`: call all of them in one response, answer
    `tool said: a | b | ...` once all results are in (otherwise the stock frontend plan)."""
    original = worker_models._frontend_plan

    def plan(messages, info):
        request = worker_models._prompt_json(messages)
        names = request.get("multi")
        if not names:
            return original(messages, info)
        last = messages[-1]
        returns = [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []
        if returns:
            text = " | ".join(str(p.content) for p in returns)
            return worker_models._frontend_answer(f"tool said: {text}", info)
        by_name = {t.name: t for t in info.function_tools if t.kind == "external"}
        calls = [(n, worker_models.schema_instance(by_name[n].parameters_json_schema)) for n in names]
        return "tools", calls

    monkeypatch.setattr(worker_models, "_frontend_plan", plan)


def pending_ids(worker, run_id) -> list[str]:
    return events(worker, run_id)[-1][2]["pending_tool_call_ids"]


def answer(call_id: str, content: str = "ok", error: str | None = None) -> dict[str, Any]:
    return {"tool_call_id": call_id, "content": content, "error": error}


# --------------------------------------------------------------------------- many calls


def test_several_client_calls_in_one_response_pend_together_and_resume_together(worker, client, tenant, multi):
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    prompt = json.dumps({"multi": ["get_weather", "get_time"]})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER, CLOCK])
    run = wait_terminal(worker.run_store, tenant, first)
    assert run["status"] == "completed" and run["output"] is None, run
    evs = events(worker, first)
    calls = [d for _k, t, d in evs if t == "tool_call"]
    assert [c["tool_name"] for c in calls] == ["get_weather", "get_time"]
    assert not [1 for _k, t, _d in evs if t == "tool_result"]
    ids = pending_ids(worker, first)
    assert ids == [c["tool_call_id"] for c in calls] and len(set(ids)) == 2
    # Answer in the reverse order: the results are matched by id, not position.
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER, CLOCK],
                         results=[answer(ids[1], "noon"), answer(ids[0], "sunny")])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: sunny | noon", run
    results = [d for _k, t, d in events(worker, second) if t == "tool_result"]
    assert sorted(r["tool_call_id"] for r in results) == sorted(ids)
    assert {r["tool_call_id"]: r["content"] for r in results} == {ids[0]: "sunny", ids[1]: "noon"}


def test_one_answered_one_error_one_missing(worker, client, tenant, multi):
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    prompt = json.dumps({"multi": ["get_weather", "get_time", "get_weather"]})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER, CLOCK])
    ids = pending_ids(worker, first) if wait_terminal(worker.run_store, tenant, first) else []
    assert len(ids) == 3
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER, CLOCK],
                         results=[answer(ids[0], "sunny"), answer(ids[1], "", "denied by user")])
    assert wait_terminal(worker.run_store, tenant, second)["status"] == "completed"
    by_id = {d["tool_call_id"]: d for _k, t, d in events(worker, second) if t == "tool_result"}
    assert [(by_id[i]["status"], by_id[i]["source"]) for i in ids] == [("ok", "client"), ("error", "client"),
                                                                       ("error", "client")]
    assert by_id[ids[1]]["content"] == "denied by user" and by_id[ids[2]]["content"] == NOT_EXECUTED_MESSAGE


def test_a_server_tool_runs_in_the_pending_turn_and_is_not_repeated(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = {**ticket_spec(tools=("tickets.create",), model="frontend_tool")}
    prompt = json.dumps({"with": "tickets.create", "with_args": TICKET_ARGS})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    evs = events(worker, first)
    ticket = [d for _k, t, d in evs if t == "tool_result"]
    assert [(r["tool_name"], r["status"]) for r in ticket] == [("tickets__create", "ok")]
    assert worker.tickets.ticket_count(tenant, f"{first}:") == 1
    ids = pending_ids(worker, first)
    assert len(ids) == 1 and ids != [ticket[0]["tool_call_id"]]
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "hot")])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: hot", run
    assert worker.tickets.ticket_count(tenant, f"{second}:") == 0  # the continuation ran no server tool
    assert [t for _k, t, _d in events(worker, second) if t == "tool_call"] == []


def test_an_mcp_tool_runs_beside_the_client_call_once(worker, client, tenant, mcp_env, local_mcp_server):
    from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key

    os.environ[KEY_ENV], os.environ[OLD_KEYS_ENV] = generate_key(), ""
    register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    spec = mcp_spec(("orders", "lookup_order"), model="frontend_tool")
    agent_id = uuid.uuid4()
    with worker.engine.begin() as conn:
        _insert_agent_and_version(conn, agent_id, tenant, spec)
    conv = new_conversation(worker, tenant)
    prompt = json.dumps({"with": "mcp__orders__lookup_order", "with_args": {"order_id": "123"}})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    evs = events(worker, first)
    mcp = [d for _k, t, d in evs if t == "tool_result"]
    assert [(r["tool_name"], r["status"]) for r in mcp] == [("mcp__orders__lookup_order", "ok")]
    assert local_mcp_server.call_count("lookup_order") == 1
    ids = pending_ids(worker, first)
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "ok")])
    assert wait_terminal(worker.run_store, tenant, second)["output"] == "tool said: ok"
    assert local_mcp_server.call_count("lookup_order") == 1  # the replay reuses, never re-calls


def test_gated_call_then_client_call_full_cycle_runs_the_ticket_once(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = {**ticket_spec(tools=("tickets.create",), model="frontend_tool"), "approvals": {"tools": ["tickets.create"]}}
    prompt = json.dumps({"with": "tickets.create", "with_args": TICKET_ARGS})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    waiting = wait_waiting(worker, first)
    # While the approval waits the turn is running and has not pended: approval first (plan D10).
    assert worker.run_store.get_run(tenant, first)["status"] == "running"
    assert "pending_tool_call_ids" not in json.dumps([d for _k, _t, d in events(worker, first) if _t == "run_completed"])
    decide(worker, client, tenant, first, waiting[0]["tool_call_id"])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    ids = pending_ids(worker, first)
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "cold")])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: cold", run
    assert side_effects(worker, tenant) == [(f"{first}:{waiting[0]['tool_call_id']}", "completed")]
    assert worker.tickets.ticket_count(tenant, "") == 1


def test_a_denied_gated_call_does_not_end_the_client_call(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = {**ticket_spec(tools=("tickets.create",), model="frontend_tool"), "approvals": {"tools": ["tickets.create"]}}
    prompt = json.dumps({"with": "tickets.create", "with_args": TICKET_ARGS})
    first = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    waiting = wait_waiting(worker, first)
    decide(worker, client, tenant, first, waiting[0]["tool_call_id"], approve=False, comment="no")
    run = wait_terminal(worker.run_store, tenant, first)
    assert run["status"] == "completed" and run["output"] is None, run
    assert len(pending_ids(worker, first)) == 1
    assert side_effects(worker, tenant) == [] and worker.tickets.ticket_count(tenant, "") == 0


# --------------------------------------------------------------------------- limits and structured output


def test_client_calls_count_against_max_tool_calls(worker, client, tenant):
    spec = {**ticket_spec(tools=("tickets.create",), model="frontend_tool"), "limits": {"max_tool_calls": 1}}
    prompt = json.dumps({"with": "tickets.create", "with_args": TICKET_ARGS})  # server call + client call = 2
    conv = new_conversation(worker, tenant)
    over = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, over)
    assert run["status"] == "failed", run  # the second call (the client's) is over the cap of 1
    end = events(worker, over)[-1]
    assert end[1] == "run_failed" and end[2].get("limit") == "max_tool_calls", end
    exactly = {**spec, "limits": {"max_tool_calls": 2}}
    conv = new_conversation(worker, tenant)
    ok = client_turn(worker, client, tenant, conv["id"], prompt, exactly, tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, ok)
    assert run["status"] == "completed" and run["output"] is None, run


def test_client_calls_alone_hit_max_tool_calls_zero_headroom(worker, client, tenant, multi):
    spec = {**frontend_spec(), "limits": {"max_tool_calls": 1}}
    conv = new_conversation(worker, tenant)
    prompt = json.dumps({"multi": ["get_weather", "get_time"]})
    run_id = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER, CLOCK])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert events(worker, run_id)[-1][2].get("limit") == "max_tool_calls"


def test_structured_spec_with_a_client_tool_and_invalid_args_still_yields_a_valid_object(worker, client, tenant):
    spec = frontend_spec(output={"json_schema": SCHEMA})
    conv = new_conversation(worker, tenant)
    first = client_turn(worker, client, tenant, conv["id"], json.dumps({"bad_first": True}), spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    ids = pending_ids(worker, first)
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "fog")])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed", run
    assert events(worker, second)[-1][2]["output_json"] == {"answer": "tool said: fog"}
    assert json.loads(run["output"]) == {"answer": "tool said: fog"}


# --------------------------------------------------------------------------- validation and security


def test_invalid_args_error_result_names_paths_never_values_and_the_model_retries(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    prompt = json.dumps({"args": {"city": "", "secret_field": "hunter2-sentinel"}})
    run_id = client_turn(worker, client, tenant, conv["id"], prompt, frontend_spec(), tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run  # every attempt is invalid: 1 + 2 retries, then the code
    evs = events(worker, run_id)
    calls = [d for _k, t, d in evs if t == "tool_call"]
    results = [d for _k, t, d in evs if t == "tool_result"]
    # 1 attempt + 2 retries; each REJECTED attempt that gets a retry is closed with an error result,
    # the last one ends the run (run_failed) instead.
    assert len(calls) == 3 and [r["tool_call_id"] for r in results] == [c["tool_call_id"] for c in calls[:2]]
    assert all(r["status"] == "error" and r.get("source") != "client" for r in results)
    assert all(ARGS_RETRY_HEADING in str(r["content"]) for r in results)
    assert "hunter2-sentinel" not in json.dumps(results) and "secret_field" not in json.dumps(results)
    assert evs[-1][2]["error_code"] == "client_tool_args_invalid"
    assert worker.run_store.get_run(tenant, run_id)["output"] is None


def test_a_non_object_args_value_is_a_retry_not_a_crash(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    run_id = client_turn(worker, client, tenant, conv["id"], json.dumps({"args": {"city": ["x"]}}),
                         frontend_spec(), tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert events(worker, run_id)[-1][2]["error_code"] == "client_tool_args_invalid"


def test_schemas_and_results_never_reach_logs_or_foreign_events(worker, client, tenant, caplog):
    schema_marker, result_marker = "SCHEMA-SENTINEL-7f3a", "RESULT-SENTINEL-91bc"
    decl = {**WEATHER, "description": schema_marker + " lookup",
            "parameters": {**WEATHER["parameters"], "description": schema_marker}}
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    with caplog.at_level(logging.DEBUG):
        first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[decl])
        wait_terminal(worker.run_store, tenant, first)
        ids = pending_ids(worker, first)
        second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[decl],
                             results=[answer(ids[0], result_marker)])
        assert wait_terminal(worker.run_store, tenant, second)["status"] == "completed"
    assert schema_marker not in caplog.text and result_marker not in caplog.text
    everything = json.dumps([e for rid in (first, second) for e in event_rows(worker.engine, rid)], default=str)
    assert schema_marker not in everything  # the declaration is stored on the run row, never in events
    assert result_marker in everything  # only as the contract's tool_result / model events


def test_a_maximum_size_client_result_is_continued_whole(worker, client, tenant):
    """The size cap is the API's (AGUI.1 ClientToolResult, tested in the e2e file); a result at the
    cap reaches the model and the event untouched."""
    from eap_schemas.client_tools import MAX_CLIENT_TOOL_RESULT_BYTES

    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    wait_terminal(worker.run_store, tenant, first)
    ids = pending_ids(worker, first)
    big = "x" * MAX_CLIENT_TOOL_RESULT_BYTES
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], big)])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: " + big


# --------------------------------------------------------------------------- memory (coordinator decision)
# Index and observe memory ONCE, when the turn really completes (after the client results and the
# final answer); never at the pending end.


def _memory_turns(worker, client, tenant, spec, resource="user-1"):
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="m", resource_id=resource)
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    wait_terminal(worker.run_store, tenant, first)
    ids = pending_ids(worker, first)
    return conv, first, ids


def test_semantic_recall_indexes_once_only_after_the_continuing_turn(worker, client, worker_settings, tenant):
    from worker_tests.test_observational_memory import wait_workflow
    from worker_tests.test_semantic_recall import embeddings, recall_spec

    schema = worker_settings.dbos_system_schema
    spec = recall_spec(model="frontend_tool")
    conv, first, ids = _memory_turns(worker, client, tenant, spec)
    wait_workflow(schema, first)
    assert embeddings(worker.engine, conv["id"]) == []  # nothing indexed at the pending end
    assert step_names(schema, first).count("eap.memory_index") == 0
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "fog")])
    assert wait_terminal(worker.run_store, tenant, second)["output"] == "tool said: fog"
    wait_workflow(schema, second)
    assert step_names(schema, second).count("eap.memory_index") == 1
    rows = embeddings(worker.engine, conv["id"])
    assert any(r["role"] == "assistant" and "tool said: fog" in r["text"] for r in rows), rows
    assert len({(r["turn"], r["role"]) for r in rows}) == len(rows)  # one row per turn and role


def test_the_logical_turn_indexes_its_original_question(worker, client, worker_settings, tenant):
    """Decision: the turn indexed when it completes is the whole logical turn: the question that
    started it ("weather?"), the client results and the answer, not just the continuation's input."""
    from worker_tests.test_observational_memory import wait_workflow
    from worker_tests.test_semantic_recall import embeddings, recall_spec

    spec = recall_spec(model="frontend_tool")
    conv, first, ids = _memory_turns(worker, client, tenant, spec)
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "fog")])
    wait_terminal(worker.run_store, tenant, second)
    wait_workflow(worker_settings.dbos_system_schema, second)
    users = [r["text"] for r in embeddings(worker.engine, conv["id"]) if r["role"] == "user"]
    assert any("weather?" in t for t in users), users


def test_observational_memory_observes_once_only_after_the_continuing_turn(worker, client, worker_settings, tenant):
    from worker_tests.test_observational_memory import obs_spec, wait_workflow

    schema = worker_settings.dbos_system_schema
    spec = obs_spec(model="frontend_tool")
    conv, first, ids = _memory_turns(worker, client, tenant, spec)
    wait_workflow(schema, first)
    assert "eap.memory_observe_run" not in step_names(schema, first)
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER], results=[answer(ids[0], "fog")])
    assert wait_terminal(worker.run_store, tenant, second)["output"] == "tool said: fog"
    wait_workflow(schema, second)
    assert step_names(schema, second).count("eap.memory_observe_run") == 1
