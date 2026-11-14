"""AC.2 acceptance: the scripted models through the real `eap-agent` path (in-process DBOS worker),
exact usage/cost rows, streamed/non-streamed parity, replay determinism after a crash, a table of
JSON Schemas for schema_instance / schema_violation, and the older scripted models unchanged.

Adds to test_scripted_models_ac.py (plain-agent unit tests); no duplicates of its cases.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from sqlalchemy import text

from eap_worker.models import (
    JUDGE_USAGE,
    LOOP_NO_TOOL_ANSWER,
    PRICED_USAGE,
    FixedUsageFunctionModel,
    make_resolver,
    schema_instance,
    schema_violation,
)
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401


def spec_for(model: str, tools: tuple[str, ...] = (), instructions: str = "Be brief.") -> dict[str, Any]:
    return {
        "spec_version": 1,
        "name": f"AC.2 {model}",
        "instructions": instructions,
        "model": {"provider": "test", "name": model},
        "tools": list(tools),
        "model_settings": {},
    }


def go(worker, client, tenant, spec, prompt, timeout=90.0):
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt, app_version=APP_VERSION)
    return run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=timeout)


def usage_rows(engine, run_id) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT request_key, model_id, input_tokens, output_tokens, cost_usd, price_source, usage_measured"
            " FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)}).mappings()]


def plain_model(name: str):
    settings = SimpleNamespace(allowed_models=SimpleNamespace(check=lambda provider, name: None))
    built = make_resolver(lambda: settings)(None, f"test:{name}")
    assert built is not None
    return built


def plain_run(name: str, prompt: str, instructions: str | None = None):
    return asyncio.run(Agent(plain_model(name), instructions=instructions).run(prompt))


# ----------------------------------------------------------------------------- real eap-agent path


def test_priced_real_run_usage_and_cost_rows_are_exact(worker, client, tenant):
    run_id, run = go(worker, client, tenant, spec_for("priced"), "hello")
    assert run["status"] == "completed", run["error"]
    assert run["output"] == "Echo: hello"
    (row,) = usage_rows(worker.engine, run_id)
    assert (row["request_key"], row["model_id"]) == ("model:0", "test:priced")
    assert (row["input_tokens"], row["output_tokens"]) == PRICED_USAGE == (1000, 500)
    assert row["cost_usd"] == Decimal("0.02") and row["price_source"] == "override" and row["usage_measured"]
    assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (1000, 500, Decimal("0.02"))
    finals = [d for _, _, t, d in event_rows(worker.engine, run_id) if t == "model_message"]
    assert len(finals) == 1
    assert Decimal(str(finals[0]["cost_usd"])) == Decimal("0.02")
    assert (finals[0]["usage"]["input_tokens"], finals[0]["usage"]["output_tokens"]) == (1000, 500)


def test_judge_real_run_usage_and_cost_rows_are_exact(worker, client, tenant):
    rubric = "Judge. MUST_CONTAIN:shipped"
    verdicts = {}
    for label, answer, passed in (("pass", "the order shipped", True), ("fail", "still waiting", False)):
        run_id, run = go(worker, client, tenant, spec_for("judge", instructions=rubric), f"<output>\n{answer}\n</output>")
        assert run["status"] == "completed", run["error"]
        verdict = json.loads(run["output"])
        assert verdict["pass"] is passed and verdict["score"] == (1.0 if passed else 0.0) and "shipped" in verdict["reason"]
        (row,) = usage_rows(worker.engine, run_id)
        assert (row["input_tokens"], row["output_tokens"]) == JUDGE_USAGE == (200, 50)
        assert row["cost_usd"] == Decimal("0.0004") and row["price_source"] == "override"
        assert run["cost_usd"] == Decimal("0.0004")
        verdicts[label] = verdict
    assert verdicts["pass"] != verdicts["fail"]


def test_priced_conversation_turns_each_cost_exactly(worker, client, tenant):
    """Two independent runs of test:priced add up per run; no cross-run leakage in the rows."""
    ids = [go(worker, client, tenant, spec_for("priced"), f"turn {i}")[0] for i in range(2)]
    for run_id in ids:
        assert [(r["input_tokens"], r["output_tokens"], r["cost_usd"]) for r in usage_rows(worker.engine, run_id)] == [
            (1000, 500, Decimal("0.02"))
        ]


@pytest.mark.parametrize("model", ["structured", "structured-never"])
def test_structured_models_echo_when_no_structured_output_is_asked(worker, client, tenant, model):
    _, run = go(worker, client, tenant, spec_for(model), "plain question")
    assert run["status"] == "completed", run["error"]
    assert run["output"] == "Echo: plain question"


def test_approval_real_run_calls_the_named_tool_and_reports_the_outcome(worker, client, tenant):
    prompt = json.dumps({"tool": "tickets.create", "args": {"title": "Printer", "body": "on fire"}})
    run_id, run = go(worker, client, tenant, spec_for("approval", tools=("tickets.create",)), prompt)
    assert run["status"] == "completed", run["error"]
    assert run["output"].startswith("Tool tickets__create outcome=success content=")
    assert "ticket_id" in run["output"]
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1"]


def test_approval_real_run_without_tools_says_so(worker, client, tenant):
    _, run = go(worker, client, tenant, spec_for("approval"), "{}")
    assert run["status"] == "completed", run["error"]
    assert run["output"] == "No tool offered."


@pytest.mark.parametrize("status", [503, 400])
def test_fail_models_fail_the_real_run_before_any_output(worker, client, tenant, status):
    run_id, run = go(worker, client, tenant, spec_for(f"fail-{status}"), "hi")
    assert run["status"] == "failed" and run["output"] is None
    assert f"HTTP {status}" in run["error"]
    types = [t for _, _, t, _ in event_rows(worker.engine, run_id)]
    assert types[0] == "run_started" and types[-1] == "run_failed" and "model_message" not in types
    assert usage_rows(worker.engine, run_id) == []


def test_loop_real_run_without_tools_answers_and_with_a_tool_never_answers(worker, client, tenant):
    _, run = go(worker, client, tenant, spec_for("loop"), "go")
    assert run["status"] == "completed" and run["output"] == LOOP_NO_TOOL_ANSWER
    run_id, run = go(worker, client, tenant, spec_for("loop", tools=("clock.now",)), "go", timeout=180)
    assert run["status"] != "completed" and run["output"] is None, run
    types = [t for _, _, t, _ in event_rows(worker.engine, run_id)]
    assert "run_completed" not in types and types.count("model_message") >= 2


# ----------------------------------------------------------------------------- streamed / non-streamed parity


def test_real_streamed_run_matches_a_plain_non_streamed_agent(worker, client, tenant):
    """The worker's model step streams (request_stream); a plain Agent.run does not. Same text, same usage."""
    cases = [
        ("priced", "same?", None, "Echo: same?"),
        ("judge", "<output>\nok shipped\n</output>", "MUST_CONTAIN:shipped", None),
        ("structured", "hello", None, "Echo: hello"),
    ]
    for name, prompt, instructions, expected in cases:
        plain = plain_run(name, prompt, instructions)
        run_id, run = go(worker, client, tenant, spec_for(name, instructions=instructions or "Be brief."), prompt)
        assert run["status"] == "completed", run["error"]
        assert run["output"] == plain.output
        if expected is not None:
            assert run["output"] == expected
        (row,) = usage_rows(worker.engine, run_id)
        if name in ("priced", "judge"):
            assert (row["input_tokens"], row["output_tokens"]) == (plain.usage.input_tokens, plain.usage.output_tokens)


def test_fixed_usage_model_reports_the_same_usage_streamed_and_not() -> None:
    async def drain(ctx, events) -> None:
        async for _ in events:
            pass

    for name, expected in (("priced", PRICED_USAGE), ("judge", JUDGE_USAGE)):
        agent = Agent(plain_model(name), instructions="MUST_CONTAIN:a")
        assert isinstance(agent.model, FixedUsageFunctionModel)
        flat = asyncio.run(agent.run("<output>a</output>"))
        streamed = asyncio.run(agent.run("<output>a</output>", event_stream_handler=drain))
        for result in (flat, streamed):
            assert (result.usage.input_tokens, result.usage.output_tokens) == expected
        assert flat.output == streamed.output


def test_fail_503_is_a_model_api_error_streamed_and_not() -> None:
    async def drain(ctx, events) -> None:
        async for _ in events:
            pass

    for handler in (None, drain):
        with pytest.raises(ModelAPIError):
            asyncio.run(Agent(plain_model("fail-503")).run("x", event_stream_handler=handler))


# ----------------------------------------------------------------------------- schema table

DRAFT = jsonschema.Draft202012Validator
SCHEMA_TABLE: dict[str, dict[str, Any]] = {
    "string_bounds": {
        "type": "object",
        "properties": {"s": {"type": "string", "minLength": 4, "maxLength": 6}},
        "required": ["s"],
    },
    "string_max_below_default": {
        "type": "object",
        "properties": {"s": {"type": "string", "maxLength": 1}, "t": {"type": "string", "minLength": 3}},
        "required": ["s", "t"],
    },
    "int_negative_range": {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": -10, "maximum": -3}},
        "required": ["n"],
    },
    "number_fractional_min": {
        "type": "object",
        "properties": {"n": {"type": "number", "minimum": 2.5}},
        "required": ["n"],
    },
    "number_fractional_max": {
        "type": "object",
        "properties": {"n": {"type": "number", "maximum": 0.25}, "i": {"type": "integer", "minimum": 2.2}},
        "required": ["n", "i"],
    },
    "enum_mixed": {
        "type": "object",
        "properties": {"e": {"enum": [3, "three", None]}, "b": {"type": "boolean"}},
        "required": ["e", "b"],
    },
    "const_object": {
        "type": "object",
        "properties": {"c": {"const": {"a": [1, 2]}}, "k": {"type": "string", "const": "v"}},
        "required": ["c", "k"],
    },
    "nullable_pair": {
        "type": "object",
        "properties": {
            "a": {"type": ["null", "string"]},
            "b": {"type": ["integer", "null"], "minimum": 7},
            "c": {"type": "null"},
        },
        "required": ["a", "b", "c"],
    },
    "array_exact": {
        "type": "object",
        "properties": {"xs": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer", "minimum": 4}}},
        "required": ["xs"],
    },
    "array_of_arrays": {
        "type": "object",
        "properties": {
            "grid": {"type": "array", "items": {"type": "array", "minItems": 2, "items": {"type": "string", "minLength": 2}}}
        },
        "required": ["grid"],
    },
    "deep_nesting": {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {
                    "b": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["z", "y"]},
                            "rows": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {"id": {"type": "integer"}, "opt": {"type": "string"}},
                                    "required": ["id"],
                                },
                            },
                        },
                        "required": ["kind", "rows"],
                    }
                },
                "required": ["b"],
            }
        },
        "required": ["a"],
        "additionalProperties": False,
    },
    "optional_only_is_empty_object": {
        "type": "object",
        "properties": {"o": {"type": "string"}},
    },
    "untyped_members": {
        "type": "object",
        "properties": {"p": {"properties": {"q": {"type": "integer"}}, "required": ["q"]}, "r": {"items": {"type": "string"}}},
        "required": ["p", "r"],
    },
}


@pytest.mark.parametrize("name", list(SCHEMA_TABLE))
def test_schema_instance_validates_and_is_deterministic(name: str) -> None:
    schema = SCHEMA_TABLE[name]
    DRAFT.check_schema(schema)
    first = schema_instance(schema)
    assert list(DRAFT(schema).iter_errors(first)) == [], first
    assert schema_instance(schema) == first and json.dumps(first, sort_keys=True) == json.dumps(schema_instance(schema), sort_keys=True)


@pytest.mark.parametrize("name", [n for n in SCHEMA_TABLE if n != "optional_only_is_empty_object"])
def test_schema_violation_is_invalid_and_deterministic(name: str) -> None:
    schema = SCHEMA_TABLE[name]
    bad = schema_violation(schema)
    assert list(DRAFT(schema).iter_errors(bad)), bad
    assert schema_violation(schema) == bad
    json.dumps(bad)


def test_schema_violation_when_nothing_is_constrained_is_still_not_an_object() -> None:
    schema = {"type": "object", "properties": {"free": {}}, "required": ["free"]}
    bad = schema_violation(schema)
    assert not DRAFT(schema).is_valid(bad)
    assert not isinstance(bad, dict)


def test_schema_instance_exact_values() -> None:
    assert schema_instance(SCHEMA_TABLE["string_bounds"]) == {"s": "xxxx"}
    assert schema_instance(SCHEMA_TABLE["string_max_below_default"]) == {"s": "x", "t": "xxx"}
    assert schema_instance(SCHEMA_TABLE["int_negative_range"]) == {"n": -3}
    assert schema_instance(SCHEMA_TABLE["number_fractional_min"]) == {"n": 2.5}
    assert schema_instance(SCHEMA_TABLE["number_fractional_max"]) == {"n": 0.25, "i": 3}
    assert schema_instance(SCHEMA_TABLE["enum_mixed"]) == {"e": 3, "b": True}
    assert schema_instance(SCHEMA_TABLE["const_object"]) == {"c": {"a": [1, 2]}, "k": "v"}
    assert schema_instance(SCHEMA_TABLE["nullable_pair"]) == {"a": "x", "b": 7, "c": None}
    assert schema_instance(SCHEMA_TABLE["array_exact"]) == {"xs": [4, 4, 4]}
    assert schema_instance(SCHEMA_TABLE["array_of_arrays"]) == {"grid": [[ "xx", "xx"]]}
    assert schema_instance(SCHEMA_TABLE["deep_nesting"]) == {"a": {"b": {"kind": "z", "rows": [{"id": 1}]}}}
    assert schema_instance(SCHEMA_TABLE["optional_only_is_empty_object"]) == {}
    assert schema_instance(SCHEMA_TABLE["untyped_members"]) == {"p": {"q": 1}, "r": ["x"]}


# ----------------------------------------------------------------------------- existing models unchanged


def test_existing_scripted_models_still_resolve_and_behave() -> None:
    async def drain(ctx, events) -> None:
        async for _ in events:
            pass

    for handler in (None, drain):
        kw = {"event_stream_handler": handler}
        assert asyncio.run(Agent(plain_model("echo")).run("hi", **kw)).output == "Echo: hi"
        assert asyncio.run(Agent(plain_model("whatever")).run("hi", **kw)).output == "Echo: hi"  # unknown -> echo
        assert asyncio.run(Agent(plain_model("history")).run("one", **kw)).output.startswith("Turn 1. You said: one")
        assert asyncio.run(Agent(plain_model("context"), instructions="Hello ctx").run("x", **kw)).output == "Instructions: Hello ctx"
        # The moderation and observer models are request-only (no stream function) by design: never streamed.
        mod = asyncio.run(Agent(plain_model("moderation")).run("please FLAG_ME")).output
        assert json.loads(mod) == {"flagged": True, "categories": ["illicit"]}
        assert json.loads(asyncio.run(Agent(plain_model("moderation")).run("fine")).output)["flagged"] is False
        assert asyncio.run(Agent(plain_model("moderation-invalid")).run("x")).output == "I cannot classify this."
        assert asyncio.run(Agent(plain_model("retrieve")).run("q", **kw)).output == "Echo: q"
        assert asyncio.run(Agent(plain_model("memory")).run("hello", **kw)).output.startswith("Working memory:")
        assert asyncio.run(Agent(plain_model("observer-invalid")).run("x")).output == "I cannot summarise this."

        ticket = Agent(plain_model("ticket"))

        @ticket.tool_plain(name="tickets__create")
        def create(title: str, body: str) -> dict[str, str]:
            return {"ticket_id": "T-9"}

        assert asyncio.run(ticket.run("help", **kw)).output == "Created ticket T-9. Our team will follow up shortly."

        first = Agent(plain_model("first-tool"))
        calls: list[int] = []

        @first.tool_plain(name="noop")
        def noop() -> str:
            calls.append(1)
            return "ok"

        assert asyncio.run(first.run("go", **kw)).output == "Done." and calls == [1]


def test_existing_scripted_models_still_work_in_the_real_path(worker, client, tenant):
    from worker_tests.conftest import ticket_spec

    run_id, run = go(worker, client, tenant, ticket_spec(), "Laptop will not boot")
    assert run["status"] == "completed", run["error"]
    assert run["output"].startswith("Created ticket")
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    _, run = go(worker, client, tenant, spec_for("echo"), "hi")
    assert run["output"] == "Echo: hi"
    _, run = go(worker, client, tenant, spec_for("retrieve"), "no tool here")
    assert run["output"] == "Echo: no tool here"
    _, run = go(worker, client, tenant, spec_for("first-tool", tools=("tickets.create",)), "go")
    assert run["status"] in ("completed", "failed")


def test_new_models_resolve_with_distinct_model_names() -> None:
    names = {n: plain_model(n).model_name for n in (
        "structured", "structured-never", "approval", "fail-503", "fail-400", "loop", "priced", "judge")}
    assert len(set(names.values())) == len(names)


# ----------------------------------------------------------------------------- determinism across replay

CRASH_CASES = [
    pytest.param("priced", (), "replay me", id="priced"),
    pytest.param("judge", (), "<output>\nit shipped\n</output>", id="judge"),
    pytest.param(
        "approval", ("tickets.create",),
        json.dumps({"tool": "tickets.create", "args": {"title": "T", "body": "B"}}), id="approval",
    ),
]


@pytest.mark.parametrize(("model", "tools", "prompt"), CRASH_CASES)
def test_crash_and_recovery_give_the_same_outputs_and_rows(worker, client, tenant, crash_schema, tmp_path, model, tools, prompt):
    spec = spec_for(model, tools, instructions="Rubric: MUST_CONTAIN:shipped" if model == "judge" else "Be brief.")
    clean_id, clean = go(worker, client, tenant, spec, prompt)
    assert clean["status"] == "completed", clean["error"]
    clean_rows = usage_rows(worker.engine, clean_id)

    executor_id = f"ac2-crash-{uuid.uuid4().hex[:8]}"
    marker = str(tmp_path / "marker")
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="after_event_write",
                               EAP_CRASH_EVENT_KEY="model:0:final", EAP_CRASH_MARKER=marker), tmp_path / "w1.log")
    second = None
    dbos_client = None
    try:
        first.wait_ready()
        from dbos import DBOSClient

        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_run(dbos_client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt,
                             app_version=CRASH_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
        assert "Recovering" in (tmp_path / "w2.log").read_text()

        # A ticket id is generated per run (never the same across two runs); everything else is the same.
        norm = lambda out: re.sub(r"TCK-[0-9A-Fa-f]+", "TCK-N", out)  # noqa: E731
        assert norm(run["output"]) == norm(clean["output"])
        rows = usage_rows(worker.engine, run_id)
        strip = lambda rs: [{k: v for k, v in r.items()} for r in rs]  # noqa: E731
        assert strip(rows) == strip(clean_rows)
        assert (run["input_tokens"], run["output_tokens"], run["cost_usd"]) == (
            clean["input_tokens"], clean["output_tokens"], clean["cost_usd"])
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        clean_events = event_rows(worker.engine, clean_id)
        # Tool call ids are generated per model response (pyd_ai_<hex>): the same shape, not the same id.
        shape = lambda es: [(re.sub(r"pyd_ai_[0-9a-f]+", "ID", e[1]), e[2]) for e in es]  # noqa: E731
        assert shape(events) == shape(clean_events)
        if model == "approval":
            assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
