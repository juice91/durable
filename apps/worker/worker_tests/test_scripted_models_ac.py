"""AC.2 scripted test models: unit tests through plain Pydantic AI agents (no database, no DBOS).

Every model is run both non-streamed (`agent.run`) and streamed (an `event_stream_handler` makes the
agent use `request_stream`, as the worker's DBOS model step does).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    NativeOutput,
    PromptedOutput,
    StructuredDict,
    ToolDenied,
    ToolOutput,
)
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.usage import UsageLimits

from eap_worker.models import (
    JUDGE_NO_OUTPUT_REASON,
    JUDGE_USAGE,
    LOOP_NO_TOOL_ANSWER,
    PRICED_USAGE,
    make_resolver,
    schema_instance,
    schema_violation,
    scripted_judge_verdict,
)

STREAMED = [False, True]
SCHEMAS: dict[str, dict[str, Any]] = {
    "flat": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}, "ok": {"type": "boolean"}},
        "required": ["name", "age", "ok"],
    },
    "enum": {
        "type": "object",
        "properties": {"color": {"type": "string", "enum": ["red", "green"]}, "kind": {"const": "fixed"}},
        "required": ["color", "kind"],
    },
    "nested": {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"id": {"type": "integer", "minimum": 5}, "tags": {"type": "array", "items": {"type": "string"}}},
                "required": ["id", "tags"],
                "additionalProperties": False,
            }
        },
        "required": ["user"],
        "additionalProperties": False,
    },
    "arrays": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 2,
                "items": {"type": "object", "properties": {"score": {"type": "number", "maximum": 0.5}}, "required": ["score"]},
            }
        },
        "required": ["items"],
    },
    "nullable": {
        "type": "object",
        "properties": {
            "note": {"type": ["string", "null"], "minLength": 3},
            "count": {"type": ["null", "integer"], "maximum": 0},
            "optional": {"type": "string"},
        },
        "required": ["note", "count"],
    },
}


def resolver():
    settings = SimpleNamespace(allowed_models=SimpleNamespace(check=lambda provider, name: None))
    return make_resolver(lambda: settings)


def model(name: str):
    built = resolver()(None, f"test:{name}")
    assert built is not None
    return built


async def _noop_handler(ctx, events) -> None:
    async for _ in events:
        pass


def run(agent: Agent, prompt: str, streamed: bool, **kwargs: Any):
    if streamed:
        kwargs["event_stream_handler"] = _noop_handler
    return asyncio.run(agent.run(prompt, **kwargs))


def output_type(schema: dict[str, Any], mode: str):
    structured = StructuredDict(schema, name="output", description="the answer")
    return {
        "tool": lambda: ToolOutput(structured, name="final_result"),
        "native": lambda: NativeOutput(structured),
        "prompted": lambda: PromptedOutput(structured),
    }[mode]()


def validating_agent(name: str, schema: dict[str, Any], mode: str, retries: int = 2) -> Agent:
    """A structured agent whose output validator enforces the JSON Schema, as AC.4's capability will."""
    agent = Agent(model(name), output_type=output_type(schema, mode), retries={"output": retries})

    @agent.output_validator
    def _check(value: Any) -> Any:
        from pydantic_ai import ModelRetry

        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise ModelRetry("; ".join(e.message for e in errors))
        return value

    return agent


def candidates(result, mode: str) -> int:
    responses = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
    if mode == "tool":
        return sum(1 for m in responses if any(isinstance(p, ToolCallPart) and p.tool_name == "final_result" for p in m.parts))
    return len(responses)


# --------------------------------------------------------------------------------------- resolver


@pytest.mark.parametrize(
    ("name", "model_name"),
    [
        ("structured", "scripted-structured"),
        ("structured-never", "scripted-structured-never"),
        ("approval", "scripted-approval"),
        ("fail-503", "scripted-fail-503"),
        ("fail-400", "scripted-fail-400"),
        ("loop", "scripted-loop"),
        ("priced", "scripted-priced"),
        ("judge", "scripted-judge"),
        ("ticket", "scripted-ticket"),
        ("retrieve", "scripted-retrieve"),
        ("memory", "scripted-memory"),
        ("observer", "scripted-observer"),
        ("anything-else", "scripted-echo"),
    ],
)
def test_resolver_registers_scripted_models(name: str, model_name: str) -> None:
    assert model(name).model_name == model_name


# --------------------------------------------------------------------------------------- structured


@pytest.mark.parametrize("schema_name", sorted(SCHEMAS))
def test_schema_instance_is_valid(schema_name: str) -> None:
    schema = SCHEMAS[schema_name]
    jsonschema.validate(schema_instance(schema), schema, cls=jsonschema.Draft202012Validator)


def test_schema_instance_shapes() -> None:
    assert schema_instance(SCHEMAS["flat"]) == {"name": "x", "age": 1, "ok": True}
    assert schema_instance(SCHEMAS["enum"]) == {"color": "red", "kind": "fixed"}
    assert schema_instance(SCHEMAS["nested"]) == {"user": {"id": 5, "tags": ["x"]}}
    assert schema_instance(SCHEMAS["arrays"]) == {"items": [{"score": 0.5}, {"score": 0.5}]}
    assert schema_instance(SCHEMAS["nullable"]) == {"note": "xxx", "count": 0}


@pytest.mark.parametrize("schema_name", sorted(SCHEMAS))
def test_schema_violation_is_invalid(schema_name: str) -> None:
    schema = SCHEMAS[schema_name]
    assert not jsonschema.Draft202012Validator(schema).is_valid(schema_violation(schema))


def test_schema_violation_without_constrained_properties_is_an_array() -> None:
    assert schema_violation({"type": "object"}) == []
    assert schema_violation({"type": "object", "properties": {"free": {}}}) == []


@pytest.mark.parametrize("streamed", STREAMED)
@pytest.mark.parametrize("mode", ["tool", "native", "prompted"])
@pytest.mark.parametrize("schema_name", sorted(SCHEMAS))
def test_structured_answers_a_valid_instance(schema_name: str, mode: str, streamed: bool) -> None:
    schema = SCHEMAS[schema_name]
    result = run(validating_agent("structured", schema, mode), "go", streamed)
    jsonschema.validate(result.output, schema, cls=jsonschema.Draft202012Validator)
    assert result.output == schema_instance(schema)
    assert candidates(result, mode) == 1


@pytest.mark.parametrize("streamed", STREAMED)
@pytest.mark.parametrize("mode", ["tool", "native", "prompted"])
def test_structured_bad_first_gives_exactly_one_invalid_candidate(mode: str, streamed: bool) -> None:
    schema = SCHEMAS["nested"]
    result = run(validating_agent("structured", schema, mode), json.dumps({"bad_first": True}), streamed)
    assert result.output == schema_instance(schema)
    assert candidates(result, mode) == 2


@pytest.mark.parametrize("mode", ["tool", "native"])
def test_structured_output_override(mode: str) -> None:
    schema = SCHEMAS["flat"]
    wanted = {"name": "Ada", "age": 36, "ok": False}
    result = run(validating_agent("structured", schema, mode), json.dumps({"output": wanted}), True)
    assert result.output == wanted


@pytest.mark.parametrize("streamed", STREAMED)
def test_structured_no_native_refuses_native_but_serves_tool_and_prompted(streamed: bool) -> None:
    from pydantic_ai.exceptions import UserError

    schema = SCHEMAS["flat"]
    with pytest.raises(UserError):
        run(Agent(model("structured-no-native"), output_type=output_type(schema, "native")), "go", streamed)
    for mode in ("tool", "prompted"):
        result = run(Agent(model("structured-no-native"), output_type=output_type(schema, mode)), "go", streamed)
        assert result.output == schema_instance(schema)


def test_structured_without_output_echoes() -> None:
    assert run(Agent(model("structured")), "hello", True).output == "Echo: hello"


@pytest.mark.parametrize("streamed", STREAMED)
@pytest.mark.parametrize("mode", ["tool", "native", "prompted"])
def test_structured_never_exhausts_retries(mode: str, streamed: bool) -> None:
    agent = validating_agent("structured-never", SCHEMAS["flat"], mode, retries=2)
    with pytest.raises(UnexpectedModelBehavior):
        run(agent, "go", streamed)


# --------------------------------------------------------------------------------------- approval


def _approval_agent() -> Agent:
    agent = Agent(model("approval"), output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def delete_thing(thing_id: str = "none") -> str:
        return f"deleted {thing_id}"

    @agent.tool_plain(name="mcp__orders__lookup_order")
    def lookup(order_id: str = "none") -> dict[str, str]:
        return {"order": order_id}

    return agent


@pytest.mark.parametrize("streamed", STREAMED)
def test_approval_reports_denied(streamed: bool) -> None:
    agent = _approval_agent()
    prompt = json.dumps({"tool": "delete_thing", "args": {"thing_id": "t1"}})
    first = run(agent, prompt, streamed)
    assert isinstance(first.output, DeferredToolRequests)
    (call,) = first.output.approvals
    assert call.tool_name == "delete_thing" and call.args_as_dict() == {"thing_id": "t1"}
    second = run(
        agent,
        None,
        streamed,
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={call.tool_call_id: ToolDenied("The user denied this tool call.")}),
    )
    assert second.output == "Tool delete_thing outcome=denied content=The user denied this tool call."


@pytest.mark.parametrize("streamed", STREAMED)
def test_approval_reports_success_after_approval(streamed: bool) -> None:
    agent = _approval_agent()
    first = run(agent, json.dumps({"tool": "delete_thing", "args": {"thing_id": "t2"}}), streamed)
    (call,) = first.output.approvals
    second = run(
        agent, None, streamed, message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={call.tool_call_id: True}),
    )
    assert second.output == "Tool delete_thing outcome=success content=deleted t2"


@pytest.mark.parametrize("streamed", STREAMED)
def test_approval_several_calls_in_one_response(streamed: bool) -> None:
    agent = _approval_agent()
    prompt = json.dumps({"calls": [
        {"tool": "delete_thing", "args": {"thing_id": "a"}},
        {"tool": "orders/lookup_order", "args": {"order_id": "o1"}},
        {"tool": "delete_thing", "args": {"thing_id": "b"}},
    ]})
    first = run(agent, prompt, streamed)
    assert isinstance(first.output, DeferredToolRequests)
    responses = [m for m in first.all_messages() if isinstance(m, ModelResponse)]
    assert len(responses) == 1
    assert [p.tool_name for p in responses[0].parts if isinstance(p, ToolCallPart)] == [
        "delete_thing", "mcp__orders__lookup_order", "delete_thing",
    ]
    a, b = first.output.approvals
    second = run(
        agent, None, streamed, message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={a.tool_call_id: True, b.tool_call_id: ToolDenied("No.")}),
    )
    assert second.output.split("\n") == [
        "Tool delete_thing outcome=success content=deleted a",
        'Tool mcp__orders__lookup_order outcome=success content={"order": "o1"}',
        "Tool delete_thing outcome=denied content=No.",
    ]


def test_approval_resolves_mcp_ref_and_dumps_json_content() -> None:
    result = run(_approval_agent(), json.dumps({"tool": "orders/lookup_order", "args": {"order_id": "o9"}}), True)
    assert result.output == 'Tool mcp__orders__lookup_order outcome=success content={"order": "o9"}'


def test_approval_defaults_to_first_tool_and_handles_no_tools() -> None:
    first = run(_approval_agent(), "do it", False)
    assert isinstance(first.output, DeferredToolRequests)
    assert first.output.approvals[0].tool_name == "delete_thing"
    assert first.output.approvals[0].args_as_dict() == {}
    assert run(Agent(model("approval")), "do it", True).output == "No tool offered."


# --------------------------------------------------------------------------------------- failures


@pytest.mark.parametrize("streamed", STREAMED)
@pytest.mark.parametrize("status", [503, 400])
def test_fail_models_raise_model_http_error(status: int, streamed: bool) -> None:
    with pytest.raises(ModelHTTPError) as raised:
        run(Agent(model(f"fail-{status}")), "hi", streamed)
    assert isinstance(raised.value, ModelAPIError)
    assert raised.value.status_code == status


def test_fail_503_falls_back() -> None:
    from pydantic_ai.models.fallback import FallbackModel

    chain = FallbackModel(model("fail-503"), model("echo"))
    result = run(Agent(chain), "hi", True)
    assert result.output == "Echo: hi"


# --------------------------------------------------------------------------------------- loop


@pytest.mark.parametrize("streamed", STREAMED)
def test_loop_never_answers(streamed: bool) -> None:
    calls: list[int] = []
    agent = Agent(model("loop"))

    @agent.tool_plain
    def ping() -> str:
        calls.append(1)
        return "pong"

    with pytest.raises(UsageLimitExceeded):
        run(agent, "go", streamed, usage_limits=UsageLimits(request_limit=5))
    assert len(calls) == 5
    with pytest.raises(UsageLimitExceeded):
        run(agent, "go", streamed, usage_limits=UsageLimits(tool_calls_limit=3))


def test_loop_without_tools_answers() -> None:
    assert run(Agent(model("loop")), "go", True).output == LOOP_NO_TOOL_ANSWER


# --------------------------------------------------------------------------------------- priced


@pytest.mark.parametrize("streamed", STREAMED)
def test_priced_reports_fixed_usage(streamed: bool) -> None:
    result = run(Agent(model("priced")), "hello", streamed)
    assert result.output == "Echo: hello"
    usage = result.usage
    assert (usage.input_tokens, usage.output_tokens, usage.requests) == (*PRICED_USAGE, 1)
    (response,) = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
    assert (response.usage.input_tokens, response.usage.output_tokens) == PRICED_USAGE


@pytest.mark.parametrize("streamed", STREAMED)
def test_priced_rounds_call_a_tool_k_times_then_echo(streamed: bool) -> None:
    agent = Agent(model("priced"))
    calls: list[int] = []

    @agent.tool_plain(name="clock__now")
    def now() -> str:
        calls.append(1)
        return "2026-11-15T00:00:00Z"

    prompt = json.dumps({"rounds": 3})
    result = run(agent, prompt, streamed)
    assert result.output == f"Echo: {prompt}"
    assert len(calls) == 3
    assert (result.usage.requests, result.usage.input_tokens, result.usage.output_tokens) == (
        4, 4 * PRICED_USAGE[0], 4 * PRICED_USAGE[1],
    )
    # Without a tool, rounds are ignored.
    assert run(Agent(model("priced")), prompt, streamed).output == f"Echo: {prompt}"


def test_test_price_file_prices_priced_and_judge(monkeypatch) -> None:
    from eap_worker import pricing

    path = Path(__file__).resolve().parents[3] / "config" / "model-prices.test.json"
    prices = pricing.parse_price_file(path.read_text(), str(path))
    usage = pricing.Usage(input_tokens=PRICED_USAGE[0], output_tokens=PRICED_USAGE[1])
    period = pricing._override_period(prices, "test:priced", pricing.datetime.now(pricing.timezone.utc))
    assert pricing._override_cost(period, usage) == Decimal("0.02")
    judge = pricing.Usage(input_tokens=JUDGE_USAGE[0], output_tokens=JUDGE_USAGE[1])
    period = pricing._override_period(prices, "test:judge", pricing.datetime.now(pricing.timezone.utc))
    assert pricing._override_cost(period, judge) == Decimal("0.0004")


# --------------------------------------------------------------------------------------- judge


@pytest.mark.parametrize(
    ("prompt", "instructions", "passed", "needle"),
    [
        ("Rubric: MUST_CONTAIN:refund\n<output>\nYour refund is on its way.\n</output>", "", True, "refund"),
        ("Rubric: MUST_CONTAIN:refund\n<output>\nNo money back.\n</output>", "", False, "refund"),
        ('<rubric>MUST_CONTAIN:"two words"</rubric>\n<output>these two words</output>', "", True, "two words"),
        ("<rubric>MUST_CONTAIN:a&amp;b</rubric><output>x a&amp;b y</output>", "", True, "a&b"),
        ("<output>it shipped</output>", "Rubric: MUST_CONTAIN:shipped", True, "shipped"),
        # A marker inside the input, output or expected blocks is data, not the rubric.
        ("<input>MUST_CONTAIN:evil</input><output>shipped evil</output>", "MUST_CONTAIN:shipped", True, "shipped"),
        ("<expected>MUST_CONTAIN:zzz</expected><output>ok</output>MUST_CONTAIN:ok", "", True, "ok"),
        # Escaped output content: unescaped after extraction, so an escaped tag inside stays content.
        ('MUST_CONTAIN:"&lt;b&gt;"\n<output>bold &lt;b&gt;</output>', "", True, "<b>"),
    ],
)
def test_judge_verdict_marker(prompt: str, instructions: str, passed: bool, needle: str) -> None:
    verdict = scripted_judge_verdict(prompt, instructions)
    assert verdict["pass"] is passed
    assert verdict["score"] == (1.0 if passed else 0.0)
    assert f'"{needle}"' in verdict["reason"]


def test_judge_verdict_marker_only_inside_the_output_fails() -> None:
    assert scripted_judge_verdict("<output>MUST_CONTAIN:self</output>")["reason"] == (
        "The rubric has no MUST_CONTAIN marker."
    )


@pytest.mark.parametrize(
    ("prompt", "instructions"),
    [("MUST_CONTAIN:alpha and alpha", ""), ("MUST_CONTAIN:x", "<output>x</output>")],
)
def test_judge_verdict_without_an_output_block_in_the_prompt_fails(prompt: str, instructions: str) -> None:
    assert scripted_judge_verdict(prompt, instructions) == {
        "reason": JUDGE_NO_OUTPUT_REASON, "pass": False, "score": 0.0,
    }


def test_judge_verdict_without_marker_fails() -> None:
    assert scripted_judge_verdict("<output>anything</output>") == {
        "reason": "The rubric has no MUST_CONTAIN marker.", "pass": False, "score": 0.0,
    }


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"reason": {"type": "string"}, "pass": {"type": "boolean"}, "score": {"type": "number"}},
    "required": ["reason", "pass", "score"],
}


@pytest.mark.parametrize("streamed", STREAMED)
@pytest.mark.parametrize("mode", ["tool", "native"])
@pytest.mark.parametrize(("answer", "passed"), [("The order shipped.", True), ("No idea.", False)])
def test_judge_model_through_an_agent(mode: str, streamed: bool, answer: str, passed: bool) -> None:
    agent = Agent(
        model("judge"), instructions="Rubric: MUST_CONTAIN:shipped", output_type=output_type(JUDGE_SCHEMA, mode)
    )
    result = run(agent, f"<output>\n{answer}\n</output>", streamed)
    assert result.output["pass"] is passed
    assert result.output["score"] == (1.0 if passed else 0.0)
    assert (result.usage.input_tokens, result.usage.output_tokens) == JUDGE_USAGE


# --------------------------------------------------------------------------------------- existing models unchanged


@pytest.mark.parametrize("streamed", STREAMED)
def test_existing_scripted_models_still_answer(streamed: bool) -> None:
    assert run(Agent(model("echo")), "hi", streamed).output == "Echo: hi"
    agent = Agent(model("ticket"))

    @agent.tool_plain(name="tickets__create")
    def create(title: str, body: str) -> dict[str, str]:
        return {"ticket_id": "T-1"}

    assert run(agent, "help", streamed).output == "Created ticket T-1. Our team will follow up shortly."


# --------------------------------------------------------------------------------------- retrieve with structured output

CITED = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "confidence": {"type": "string", "enum": ["high", "low"]}},
    "required": ["answer", "confidence"],
}
SOURCES = "[1] Refund policy — https://example.test/refunds\n[2] Shipping — https://example.test/shipping"


@pytest.mark.parametrize("streamed", STREAMED)
def test_retrieve_ends_through_the_output_tool(streamed: bool) -> None:
    agent = Agent(model("retrieve"), output_type=output_type(CITED, "tool"))

    @agent.tool_plain(name="retrieve")
    def retrieve(search_query: str) -> str:
        return SOURCES

    result = run(agent, "refunds?", streamed)
    assert result.output == {
        "answer": "According to [1] Refund policy: https://example.test/refunds", "confidence": "high",
    }


@pytest.mark.parametrize("streamed", STREAMED)
def test_retrieve_multi_ends_through_the_output_tool(streamed: bool) -> None:
    agent = Agent(model("retrieve-multi"), output_type=output_type(CITED, "tool"))

    @agent.tool_plain(name="retrieve")
    def retrieve(search_query: str) -> str:
        return SOURCES

    result = run(agent, json.dumps({"rounds": [["a", "b"]]}), streamed)
    assert result.output["answer"].startswith("Sources: [1] Refund policy: https://example.test/refunds; [2] Shipping")
    assert result.output["confidence"] == "high"


def test_schema_instance_with_text_fills_free_strings_only() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "maxLength": 4},
            "b": {"type": "string", "minLength": 8},
            "c": {"type": "array", "items": {"type": "string"}},
            "d": {"const": "k"},
        },
        "required": ["a", "b", "c", "d"],
    }
    assert schema_instance(schema, text="[1] cite") == {"a": "[1] ", "b": "[1] cite", "c": ["[1] cite"], "d": "k"}
    assert schema_instance(schema, text="[1]")["b"] == "[1]xxxxx"
