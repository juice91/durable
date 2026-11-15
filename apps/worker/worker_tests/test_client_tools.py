"""AGUI.7 implementer tests: frontend (client) tools in the worker (plan D10).

A turn that declares client tools (`runs.client_tools`) and whose model calls one ends `completed`
with no output and `run_completed.pending_tool_call_ids`; the next turn of the conversation carries
the client's results (`runs.client_tool_results`), writes `tool:{id}:result` events with
`source: "client"` before its first model request, and continues the pending response. The model is
`test:frontend_tool` (eap_worker.models)."""

from __future__ import annotations

import json
import pickle
import uuid
from typing import Any

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart

from eap_worker.client_tools import (
    ARGS_RETRY_HEADING,
    NOT_EXECUTED_MESSAGE,
    ClientArgsRetry,
    ClientTool,
    ClientToolArgsInvalid,
    check_client_args,
    client_resume,
    client_tools_from_link,
    dangling_tail,
    resumable,
)
from eap_worker.errors import classify
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture
from worker_tests.test_tool_approval import TICKET_ARGS, decide, wait_waiting

WEATHER = {
    "name": "get_weather",
    "description": "Ask the browser for the weather.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "minLength": 1}},
        "required": ["city"],
        "additionalProperties": False,
    },
}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def frontend_spec(**extra: Any) -> dict[str, Any]:
    return {**ticket_spec(tools=(), model="frontend_tool"), **extra}


def client_turn(worker, client, tenant, conv_id, input_text, spec, *, tools=None, results=None) -> uuid.UUID:
    """What the AG-UI route does: the numbered turn with its client columns, then the hand-off."""
    store = worker.run_store
    conv = store.get_conversation(tenant, conv_id)
    run_id, created = store.create_turn(
        tenant, conv_id, 1, input_text, uuid.uuid4(), client_tools=tools, client_tool_results=results
    )
    assert created
    enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text=input_text, app_version=APP_VERSION,
                run_id=run_id, agent_id=uuid.UUID(conv["agent_id"]), create_row=False)
    return run_id


def events(worker, run_id) -> list[tuple[str, str, dict]]:
    return [(key, type_, data) for _seq, key, type_, data in event_rows(worker.engine, run_id)]


def new_conversation(worker, tenant) -> dict:
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="client tools")
    return conv


# --------------------------------------------------------------------------- unit


def test_resume_answers_errors_and_unanswered_calls():
    link = {
        "client_tool_pending": [
            {"tool_call_id": "a", "tool_name": "get_weather"},
            {"tool_call_id": "b", "tool_name": "get_weather"},
            {"tool_call_id": "c", "tool_name": "other"},
        ],
        "client_tool_results": [
            {"tool_call_id": "a", "content": "sunny", "error": None},
            {"tool_call_id": "b", "content": "", "error": "geolocation denied"},
            {"tool_call_id": "zz", "content": "ignored"},
        ],
    }
    resume = client_resume(link)
    assert resume is not None
    assert resume.results.calls["a"] == "sunny"
    assert isinstance(resume.results.calls["b"], ModelRetry) and resume.results.calls["b"].message == "geolocation denied"
    assert resume.results.calls["c"].message == NOT_EXECUTED_MESSAGE
    assert "zz" not in resume.results.calls
    assert [(e["tool_call_id"], e["status"], e["content"], e["source"]) for e in resume.events] == [
        ("a", "ok", "sunny", "client"),
        ("b", "error", "geolocation denied", "client"),
        ("c", "error", NOT_EXECUTED_MESSAGE, "client"),
    ]
    assert client_resume({}) is None and client_resume({"client_tool_pending": []}) is None


def test_old_checkpoints_have_no_client_tools():
    assert client_tools_from_link({}) is None
    assert client_tools_from_link({"client_tools": None}) is None
    tools = client_tools_from_link({"client_tools": [WEATHER]})
    assert tools is not None and tools.names == {"get_weather"}


def test_a_tampered_declaration_fails_closed():
    with pytest.raises(Exception):
        client_tools_from_link({"client_tools": [{**WEATHER, "name": "retrieve"}]})


def test_argument_check_names_paths_never_values():
    tool = ClientTool(WEATHER["name"], "", WEATHER["parameters"])
    check_client_args(tool, {"city": "Lisbon"})
    with pytest.raises(ClientArgsRetry) as info:
        check_client_args(tool, {"city": "", "secret": "hunter2"})
    message = info.value.message
    assert message.startswith(ARGS_RETRY_HEADING)
    assert "hunter2" not in message and "secret" not in message and "$.city" in message


def test_resumable_and_dangling_tail():
    call = ToolCallPart("get_weather", {"city": "x"}, tool_call_id="c1")
    history = [ModelRequest(parts=[UserPromptPart("hi")]), ModelResponse(parts=[call])]
    assert resumable(history, ["c1"]) and dangling_tail(history)
    assert not resumable(history, ["c2"]) and not resumable([], ["c1"]) and not resumable(history, [])
    answered = [*history, ModelRequest(parts=[ToolReturnPart("get_weather", "ok", tool_call_id="c1")])]
    assert not resumable(answered, ["c1"]) and not dangling_tail(answered)
    assert not dangling_tail(None)


def test_args_invalid_is_pickle_safe_and_classified():
    err = pickle.loads(pickle.dumps(ClientToolArgsInvalid(2)))
    assert err.max_retries == 2 and "2 retries" in str(err)
    failure = classify(err)
    assert failure.code == "client_tool_args_invalid" and failure.status == "failed"


# --------------------------------------------------------------------------- the worker


def test_a_client_call_ends_the_turn_and_the_next_turn_continues_it(worker, client, worker_settings, tenant):
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, first)
    assert run["status"] == "completed" and run["output"] is None, run
    evs = events(worker, first)
    assert [k for k, _t, _d in evs][:2] == ["start", "model:0:final"]
    call_key = next(k for k, t, _d in evs if t == "tool_call")
    call_id = call_key.split(":")[1]
    assert not [k for k, t, _d in evs if t == "tool_result"]  # never executed, no result
    end = evs[-1]
    assert end[1] == "run_completed" and end[2] == {"output": None, "pending_tool_call_ids": [call_id]}
    call = next(d for _k, t, d in evs if t == "tool_call")
    assert call["tool_name"] == "get_weather" and call["args"] == {"city": "x"}
    # No new step: the same shape as a tool turn up to the model's answer.
    steps = step_names(worker_settings.dbos_system_schema, first)
    assert "eap-agent__dynamic_toolset__eap-spec-tools.call_tool" not in steps
    assert not [s for s in steps if "client" in s]
    from eap_run_store import AgUiStore

    assert [p.tool_call_id for p in AgUiStore(worker.engine).pending_client_tool_calls(tenant, conv["id"])] == [call_id]

    second = client_turn(
        worker, client, tenant, conv["id"], "", spec, tools=[WEATHER],
        results=[{"tool_call_id": call_id, "content": "sunny", "error": None}],
    )
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: sunny", run
    evs = events(worker, second)
    assert [k for k, _t, _d in evs] == ["start", f"tool:{call_id}:result", "model:0:final", "end"]
    assert evs[1][2] == {"tool_call_id": call_id, "tool_name": "get_weather", "status": "ok", "content": "sunny",
                         "source": "client"}
    assert evs[-1][2] == {"output": "tool said: sunny"}
    # The next turn after the answered one has nothing pending.
    assert AgUiStore(worker.engine).pending_client_tool_calls(tenant, conv["id"]) == []

    third = client_turn(worker, client, tenant, conv["id"], "thanks", spec)
    run = wait_terminal(worker.run_store, tenant, third)
    assert run["status"] == "completed", run


def test_a_rest_turn_after_a_pending_turn_gets_the_not_executed_error(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    second = client_turn(worker, client, tenant, conv["id"], "never mind", spec)  # no client columns
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == f"tool said: error: {NOT_EXECUTED_MESSAGE}", run
    result = next(d for _k, t, d in events(worker, second) if t == "tool_result")
    assert result["status"] == "error" and result["source"] == "client"


def test_a_turn_after_a_failed_continuation_repairs_the_dangling_call(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    first = client_turn(worker, client, tenant, conv["id"], "weather?", frontend_spec(), tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    broken = client_turn(worker, client, tenant, conv["id"], "x", ticket_spec(tools=("nope.missing",), model="echo"))
    assert wait_terminal(worker.run_store, tenant, broken)["status"] == "failed"
    third = client_turn(worker, client, tenant, conv["id"], "hello again", ticket_spec(tools=(), model="echo"))
    run = wait_terminal(worker.run_store, tenant, third)
    assert run["status"] == "completed", run
    assert not [d for _k, t, d in events(worker, third) if t == "tool_result"]


def test_invalid_args_retry_then_pend(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    run_id = client_turn(worker, client, tenant, conv["id"], json.dumps({"bad_first": True}), frontend_spec(),
                         tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and run["output"] is None, run
    evs = events(worker, run_id)
    calls = [d for _k, t, d in evs if t == "tool_call"]
    results = [d for _k, t, d in evs if t == "tool_result"]
    assert len(calls) == 2
    # The rejected attempt is closed (an error result the model read); the valid one is pending.
    assert [r["tool_call_id"] for r in results] == [calls[0]["tool_call_id"]] and results[0]["status"] == "error"
    assert evs[-1][2]["pending_tool_call_ids"] == [calls[1]["tool_call_id"]]


def test_the_retry_limit_fails_the_run(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    run_id = client_turn(worker, client, tenant, conv["id"], json.dumps({"args": {"town": "x"}}), frontend_spec(),
                         tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    failed = events(worker, run_id)[-1]
    assert failed[1] == "run_failed" and failed[2]["error_code"] == "client_tool_args_invalid"
    assert "town" not in json.dumps(failed[2])


def test_structured_spec_pends_then_answers_a_valid_object(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = frontend_spec(output={"json_schema": SCHEMA})
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, first)
    assert run["status"] == "completed" and run["output"] is None, run
    end = events(worker, first)[-1][2]
    assert "output_json" not in end and len(end["pending_tool_call_ids"]) == 1
    call_id = end["pending_tool_call_ids"][0]
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER],
                         results=[{"tool_call_id": call_id, "content": "rain"}])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed", run
    end = events(worker, second)[-1][2]
    assert end["output_json"] == {"answer": "tool said: rain"}


def test_gated_server_call_waits_first_then_the_turn_pends(worker, client, tenant):
    conv = new_conversation(worker, tenant)
    spec = {**ticket_spec(tools=("tickets.create",), model="frontend_tool"), "approvals": {"tools": ["tickets.create"]}}
    prompt = json.dumps({"with": "tickets.create", "with_args": TICKET_ARGS})
    run_id = client_turn(worker, client, tenant, conv["id"], prompt, spec, tools=[WEATHER])
    waiting = wait_waiting(worker, run_id)
    assert waiting[0]["batch"] == [waiting[0]["tool_call_id"]]  # only the gated call waits
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    decide(worker, client, tenant, run_id, waiting[0]["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and run["output"] is None, run
    evs = events(worker, run_id)
    ticket_result = next(d for k, t, d in evs if t == "tool_result")
    assert ticket_result["tool_name"] == "tickets__create" and ticket_result["status"] == "ok"
    client_call = next(d for _k, t, d in evs if t == "tool_call" and d["tool_name"] == "get_weather")
    assert evs[-1][2]["pending_tool_call_ids"] == [client_call["tool_call_id"]]


def test_a_memory_turn_that_pends_runs_no_post_turn_step(worker, client, worker_settings, tenant):
    from worker_tests.test_observational_memory import wait_workflow
    from worker_tests.test_semantic_recall import recall_spec

    schema = worker_settings.dbos_system_schema
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="m", resource_id="user-1")
    spec = recall_spec(model="frontend_tool")
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    wait_workflow(schema, first)
    assert "eap.memory_index" not in step_names(schema, first)
    call_id = events(worker, first)[-1][2]["pending_tool_call_ids"][0]
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER],
                         results=[{"tool_call_id": call_id, "content": "fog"}])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed" and run["output"] == "tool said: fog", run
    wait_workflow(schema, second)
    assert step_names(schema, second).count("eap.memory_index") == 1


def test_client_calls_project_onto_the_tool_call_limit():
    from pydantic_ai.exceptions import UsageLimitExceeded
    from pydantic_ai.usage import RunUsage, UsageLimits

    from eap_worker.client_tools import check_client_tool_calls
    from eap_worker.errors import describe_usage_limit
    from eap_schemas.limits import UsageLimitsSpec as LimitsSpec

    usage = RunUsage(tool_calls=1)
    check_client_tool_calls(UsageLimits(tool_calls_limit=2), usage, 1)  # exactly at the cap
    check_client_tool_calls(None, usage, 5)
    check_client_tool_calls(UsageLimits(), usage, 5)  # no tool-call cap
    with pytest.raises(UsageLimitExceeded) as info:
        check_client_tool_calls(UsageLimits(tool_calls_limit=1), usage, 1)
    assert usage.tool_calls == 1  # the live usage is never changed
    describe_usage_limit(info.value, LimitsSpec(max_tool_calls=1), usage)
    assert (info.value.limit, info.value.value) == ("max_tool_calls", 1)


def test_logical_turn_texts_cover_the_whole_turn():
    from eap_worker.client_tools import ClientResume, logical_turn_texts

    chain = [
        {"input": "weather?", "calls": [{"tool_call_id": "a", "tool_name": "get_weather", "args": {"city": "x"}}],
         "results": {"a": "sunny"}},
        {"input": "", "calls": [{"tool_call_id": "b", "tool_name": "get_time", "args": {}}], "results": {}},
    ]
    resume = ClientResume(results=None, events=({"tool_call_id": "b", "content": "noon"},))  # type: ignore[arg-type]
    user, tools, answer = logical_turn_texts(chain, resume, "and?", "It is sunny at noon.")
    assert user == "weather?\n\nand?"
    assert tools == 'Tool get_weather({"city": "x"}) -> sunny\nTool get_time({}) -> noon'
    assert answer == "It is sunny at noon."
    assert logical_turn_texts([], None, "", None) == (None, None, None)


# --------------------------------------------------------------------------- review fixes


def _anyof_chain(n: int) -> dict[str, Any]:
    """The reviewer's schema: each $defs member anyOf-s two refs to the next (2^n validation work)."""
    defs: dict[str, Any] = {f"a{i}": {"anyOf": [{"$ref": f"#/$defs/a{i + 1}"}, {"$ref": f"#/$defs/a{i + 1}"}]}
                            for i in range(n)}
    defs[f"a{n}"] = {"type": "string"}
    return {"type": "object", "properties": {"x": {"$ref": "#/$defs/a0"}}, "$defs": defs}


@pytest.mark.parametrize("n", [22, 40])
def test_an_exponential_ref_chain_is_refused_at_declaration_fast(n):
    import time

    from pydantic import ValidationError

    from eap_schemas.client_tools import ClientToolDecl

    ClientToolDecl(name="warm", parameters={"type": "object"})  # jsonschema import outside the timing
    started = time.perf_counter()
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="chain", parameters=_anyof_chain(n))
    assert time.perf_counter() - started < 0.1
    assert info.value.errors()[0]["type"] == "client_tool_schema_too_complex"
    with pytest.raises(ValidationError):  # a tampered stored row is refused the same way in the worker
        client_tools_from_link({"client_tools": [{"name": "chain", "parameters": _anyof_chain(n)}]})


@pytest.mark.parametrize("defs", [
    {"a": {"$ref": "#/$defs/a"}},
    {"a": {"anyOf": [{"$ref": "#/$defs/b"}, {"type": "string"}]}, "b": {"properties": {"y": {"$ref": "#/$defs/a"}}}},
])
def test_a_ref_cycle_is_refused_not_a_recursion_error(defs):
    from pydantic import ValidationError

    from eap_schemas.client_tools import ClientToolDecl

    schema = {"type": "object", "properties": {"x": {"$ref": "#/$defs/a"}}, "$defs": defs}
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="cyclic", parameters=schema)
    assert info.value.errors()[0]["type"] == "client_tool_schema_too_complex"


def test_a_shared_def_used_often_is_counted_once_per_use_and_accepted():
    from eap_schemas.client_tools import ClientToolDecl, expanded_node_count

    schema = {"type": "object", "properties": {f"p{i}": {"$ref": "#/$defs/s"} for i in range(20)},
              "$defs": {"s": {"type": "string"}}}
    assert expanded_node_count(schema) == 1 + 1 + 20 * 2  # root, properties, 20 x (ref node + def)
    ClientToolDecl(name="ok", parameters=schema)


def test_the_argument_check_is_time_bounded(monkeypatch):
    import asyncio
    import time

    from eap_worker import client_tools as ct

    def slow(schema, value):
        time.sleep(0.5)
        return []

    monkeypatch.setattr(ct, "schema_errors", slow)
    tool = ClientTool(WEATHER["name"], "", WEATHER["parameters"])
    async def timed() -> float:
        started = time.perf_counter()
        with pytest.raises(ClientToolArgsInvalid) as raised:
            await ct.client_arg_errors(tool, {"city": "x"}, budget=0.05)
        timed.error = raised.value  # type: ignore[attr-defined]
        return time.perf_counter() - started

    # The coroutine (the event loop) is released at the budget; asyncio.run then joins the thread.
    assert asyncio.run(timed()) < 0.4

    class _Info:
        value = timed.error  # type: ignore[attr-defined]

    info = _Info
    assert info.value.timed_out
    assert classify(info.value).code == "client_tool_args_invalid"
    assert pickle.loads(pickle.dumps(info.value)).timed_out


def test_chain_values_are_clipped():
    from eap_worker.client_tools import MAX_CHAIN_VALUE_CHARS, chain_link_size, clip_chain_text

    assert clip_chain_text(None) is None
    assert len(clip_chain_text("x" * 50_000)) == MAX_CHAIN_VALUE_CHARS
    assert clip_chain_text({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    link = {"input": "q", "calls": [{"tool_name": "t", "args_text": "{}"}], "results": {"c": "abc"}}
    assert chain_link_size(link) == 1 + 2 + 1 + 3


def test_client_results_in_the_index_pass_the_output_processors(worker, client, worker_settings, tenant):
    from worker_tests.test_observational_memory import wait_workflow
    from worker_tests.test_semantic_recall import embeddings, recall_spec

    spec = recall_spec(model="frontend_tool")
    spec["output_processors"] = [
        {"type": "pii_redact", "config": {"kinds": ["email"]}},
        {"type": "regex_blocklist", "config": {"patterns": ["sk-live-[a-z0-9]+"], "action": "redact"}},
    ]
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="m", resource_id="user-1")
    first = client_turn(worker, client, tenant, conv["id"], "who?", spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    call_id = events(worker, first)[-1][2]["pending_tool_call_ids"][0]
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[WEATHER],
                         results=[{"tool_call_id": call_id, "content": "alice@corp.com sk-live-abc123"}])
    assert wait_terminal(worker.run_store, tenant, second)["status"] == "completed"
    wait_workflow(worker_settings.dbos_system_schema, second)
    rows = embeddings(worker.engine, conv["id"])
    text = " ".join(r["text"] for r in rows)
    assert "Tool get_weather" in text and "who?" in text
    assert "alice@corp.com" not in text and "sk-live-abc123" not in text, rows


def test_results_for_a_call_that_cannot_be_continued_are_recorded(worker, client, tenant, monkeypatch):
    from eap_worker import workflow as wf

    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[WEATHER])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    call_id = events(worker, first)[-1][2]["pending_tool_call_ids"][0]
    monkeypatch.setattr(wf, "resumable", lambda history, ids: False)  # e.g. history limits dropped it
    second = client_turn(worker, client, tenant, conv["id"], "hi", spec, tools=[WEATHER],
                         results=[{"tool_call_id": call_id, "content": "sunny"}])
    run = wait_terminal(worker.run_store, tenant, second)
    assert run["status"] == "completed", run
    result = next(d for k, t, d in events(worker, second) if k == f"tool:{call_id}:result")
    assert result["status"] == "error" and result["not_continued"] is True and result["source"] == "client"
    assert "sunny" not in json.dumps(result)


def test_schemas_and_results_never_reach_spans(worker, client, tenant, telemetry):  # noqa: F811 - fixture
    from worker_tests.test_telemetry import _wait_for_run_span

    _provider, finished = telemetry
    schema_marker, result_marker = "SCHEMA-SPAN-SENTINEL-5d1e", "RESULT-SPAN-SENTINEL-c2a9"
    decl = {**WEATHER, "description": schema_marker,
            "parameters": {**WEATHER["parameters"], "description": schema_marker}}
    conv = new_conversation(worker, tenant)
    spec = frontend_spec()
    first = client_turn(worker, client, tenant, conv["id"], "weather?", spec, tools=[decl])
    assert wait_terminal(worker.run_store, tenant, first)["output"] is None
    call_id = events(worker, first)[-1][2]["pending_tool_call_ids"][0]
    second = client_turn(worker, client, tenant, conv["id"], "", spec, tools=[decl],
                         results=[{"tool_call_id": call_id, "content": result_marker}])
    assert wait_terminal(worker.run_store, tenant, second)["status"] == "completed"
    spans, logs = _wait_for_run_span(finished, second)
    assert [s for s in spans if s.name == "eap.run"], "no run span exported"
    exported = json.dumps(
        [
            {"name": s.name, "attributes": dict(s.attributes or {}),
             "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in s.events]}
            for s in spans
        ]
        + [{"body": str(r.body), "attributes": dict(r.attributes or {})} for r in logs],
        default=str,
    )
    leaks = [(s.name, k) for s in spans for k, v in (s.attributes or {}).items()
             if schema_marker in str(v) or result_marker in str(v)]
    assert schema_marker not in exported and result_marker not in exported, leaks


def test_a_slow_argument_check_fails_the_run_instead_of_stalling(worker, client, tenant, monkeypatch):
    import time

    from eap_worker import client_tools as ct

    real = ct.schema_errors

    def slow(schema, value):
        if schema == WEATHER["parameters"]:
            time.sleep(1.0)
        return real(schema, value)

    monkeypatch.setattr(ct, "schema_errors", slow)
    monkeypatch.setattr(ct, "CLIENT_ARGS_CHECK_SECONDS", 0.05)
    conv = new_conversation(worker, tenant)
    run_id = client_turn(worker, client, tenant, conv["id"], "weather?", frontend_spec(), tools=[WEATHER])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert events(worker, run_id)[-1][2]["error_code"] == "client_tool_args_invalid"
