"""Usage guard acceptance: impossible streamed usage (output < reasoning) is corrected, logged with
counts only and marked; everything else is unchanged and the correction flows into rows and totals."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.usage import RequestUsage

from eap_run_store.trace import RunData, TraceInput, build_trace
from eap_schemas.knowledge import approx_tokens
from eap_worker import pricing
from eap_worker.pricing import request_usage_input, usage_record

AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
SECRET = "SECRET-ANSWER-TEXT"
WORKER_SRC = Path(pricing.__file__).parent


@pytest.fixture(autouse=True)
def _prices(tmp_path, monkeypatch):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps({"models": {"litellm:*": [
        {"effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "8"}]}}))
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(p))


def _usage(out: int, reasoning: int) -> RequestUsage:
    return RequestUsage(input_tokens=100, output_tokens=out, output_reasoning_tokens=reasoning,
                        details={"reasoning_tokens": reasoning})


def _text_response(text: str = SECRET) -> ModelResponse:
    return ModelResponse(parts=[ThinkingPart(content="t" * 3000), TextPart(content=text)], model_name="q")


def _tool_response(args: dict) -> ModelResponse:
    return ModelResponse(parts=[ThinkingPart(content="t" * 3000),
                                ToolCallPart(tool_name="lookup", args=args, tool_call_id="c1")], model_name="q")


def _records(caplog):
    return [r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]


@pytest.mark.parametrize("reasoning", [79, 503])
def test_streamed_completion_1_with_reasoning_is_corrected_logged_and_marked(caplog, reasoning):
    response = _text_response()
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:q", "q", _usage(1, reasoning), streamed_response=response)
    expected = reasoning + approx_tokens(SECRET)
    assert usage["output_tokens"] == expected and usage["usage_estimated"] is True
    (rec,) = _records(caplog)
    assert rec.levelname == "WARNING" and rec.getMessage().startswith("usage_output_below_reasoning:")
    assert (rec.model_id, rec.reported_output_tokens, rec.reasoning_tokens, rec.estimated_output_tokens) == (
        "litellm:q", 1, reasoning, expected)
    assert SECRET not in caplog.text and SECRET not in str(rec.__dict__)
    row, fields = usage_record("model:0", usage, AT)
    assert row["output_tokens"] == expected == fields["usage"]["output_tokens"]
    assert fields["usage"]["estimated"] is True


def test_tool_call_finish_counts_arguments_not_thinking(caplog):
    args = {"city": SECRET, "n": 3}
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:q", "q", _usage(1, 79), streamed_response=_tool_response(args))
    expected = 79 + approx_tokens(json.dumps(args, separators=(",", ":")))
    assert usage["output_tokens"] == expected and usage["usage_estimated"] is True
    assert SECRET not in caplog.text
    # No text and no tool call at all: exactly the reasoning tokens.
    empty = ModelResponse(parts=[ThinkingPart(content="t" * 3000)], model_name="q")
    assert request_usage_input("litellm:q", "q", _usage(0, 79), streamed_response=empty)["output_tokens"] == 79


@pytest.mark.parametrize("out,reasoning", [(79, 79), (80, 79), (600, 503), (4, 0), (0, 0)])
def test_correct_streamed_usage_is_unchanged(caplog, out, reasoning):
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:q", "q", _usage(out, reasoning), streamed_response=_text_response())
    assert usage == request_usage_input("litellm:q", "q", _usage(out, reasoning))
    assert usage["output_tokens"] == out and "usage_estimated" not in usage
    _row, fields = usage_record("model:0", usage, AT)
    assert "estimated" not in fields["usage"] and not _records(caplog)


@pytest.mark.parametrize("reasoning", [79, 503])
def test_non_streamed_impossible_usage_is_unchanged(caplog, reasoning):
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:q", "q", _usage(1, reasoning))
    assert usage["output_tokens"] == 1 and "usage_estimated" not in usage and not _records(caplog)
    assert usage_record("model:0", usage, AT)[0]["output_tokens"] == 1


def _impossible_usage_model():
    """Streams SECRET as text, then reports the gateway's impossible usage (completion 2 < reasoning
    300) for the whole streamed response, like the LiteLLM usage chunk at the end of a stream."""
    from pydantic_ai.models.function import AgentInfo, FunctionModel, FunctionStreamedResponse

    class Streamed(FunctionStreamedResponse):
        async def _get_event_iterator(self):
            async for event in super()._get_event_iterator():
                yield event
            self._usage = RequestUsage(input_tokens=100, output_tokens=2, output_reasoning_tokens=300,
                                       details={"reasoning_tokens": 300})

    class Model(FunctionModel):
        def __init__(self):
            async def stream(messages, info: AgentInfo):
                for i in range(0, len(SECRET), 4):
                    yield SECRET[i:i + 4]

            super().__init__(stream_function=stream, model_name="fake-gateway")

        @asynccontextmanager
        async def request_stream(self, *args, **kwargs):
            async with super().request_stream(*args, **kwargs) as response:
                response.__class__ = Streamed  # same dataclass fields; only the usage at the end differs
                yield response

    return Model()


def test_streamed_agent_call_site_corrects_impossible_usage(worker, client, tenant, monkeypatch, caplog):
    """Behavioural: a real run through the workflow's streamed model request (DBOS
    model.request_stream, agent.iter node) with a model that reports output < reasoning."""
    import dataclasses

    from sqlalchemy import text

    from eap_schemas.models_policy import ModelPolicy
    from eap_worker import models
    from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal

    monkeypatch.setattr(models, "litellm_model", lambda name, settings: _impossible_usage_model())
    monkeypatch.setattr(worker, "settings", dataclasses.replace(
        worker.settings, allowed_models=ModelPolicy.parse("litellm:fake-gateway")))
    spec = ticket_spec(tools=())
    spec["model"] = {"provider": "litellm", "name": "fake-gateway"}
    with caplog.at_level("WARNING", logger="eap_worker"):
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="hi",
                             app_version=APP_VERSION)
        run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == SECRET
    final = next(e[3] for e in event_rows(worker.engine, run_id) if e[1] == "model:0:final")
    expected = 300 + approx_tokens(SECRET)
    assert final["usage"]["output_tokens"] == expected and final["usage"]["estimated"] is True
    with worker.engine.connect() as conn:
        out = conn.execute(text("SELECT output_tokens FROM run_model_requests WHERE run_id = :r"),
                           {"r": str(run_id)}).scalars().all()
    assert out == [expected] and run["output_tokens"] == expected
    assert [(r.reported_output_tokens, r.reasoning_tokens) for r in _records(caplog)] == [(2, 300)]


def test_no_native_token_limit_is_configured_in_the_worker():
    # AC.5 passes Pydantic AI a UsageLimits for request and tool-call caps only (usage_limits_for,
    # its one construction site); token caps are never native, so no Pydantic AI token check can
    # trip on a provider's impossible streamed count. (AC.5's own token caps are checked by the
    # workflow, token_limit_breach on the run's RunUsage.)
    from eap_schemas import AgentSpec

    from eap_worker.workflow import usage_limits_for
    from worker_tests.conftest import ticket_spec

    sites = [
        path.name
        for path in WORKER_SRC.rglob("*.py")
        for line in path.read_text().splitlines()
        if "UsageLimits(" in line and not line.lstrip().startswith("#") and "#" not in line.partition("UsageLimits(")[0]
    ]
    assert sites == ["workflow.py"], sites
    spec = {**ticket_spec(tools=()), "limits": {
        "max_requests": 2, "max_tool_calls": 3, "max_input_tokens": 5, "max_output_tokens": 6, "max_total_tokens": 7}}
    limits = usage_limits_for(AgentSpec.model_validate(spec))
    assert limits.request_limit == 2 and limits.tool_calls_limit == 3
    assert (limits.input_tokens_limit, limits.output_tokens_limit, limits.total_tokens_limit) == (None, None, None)


def test_corrected_values_flow_into_rows_cost_and_chat_totals():
    estimated = request_usage_input("litellm:q", "q", _usage(1, 503), streamed_response=_text_response())
    normal = request_usage_input("litellm:q", "q", _usage(40, 10), streamed_response=_text_response())
    rows, events = {}, [{"seq": 1, "event_key": "start", "type": "run_started", "data": {"input": "hi"},
                         "ts": AT}]
    for i, u in enumerate((estimated, normal)):
        row, fields = usage_record(f"model:{i}", u, AT)
        rows[f"model:{i}"] = {**row, "run_id": "r1", "operation": "chat"}
    out_est = 503 + approx_tokens(SECRET)
    assert rows["model:0"]["output_tokens"] == out_est
    assert rows["model:0"]["cost_usd"] == ((Decimal(100) * 2 + Decimal(out_est) * 8) / Decimal(10**6)).quantize(
        Decimal("1e-10"))
    run = {"id": "r1", "kind": "agent", "status": "completed", "display_status": "completed", "agent_id": "a",
           "agent_version": 1, "created_at": AT, "started_at": AT, "finished_at": AT, "updated_at": AT,
           "conversation_id": "c", "turn": 1, "input": "hi", "output": "x"}
    totals = build_trace(TraceInput(root=RunData(run=run, events=events, requests=rows)))["totals"]
    assert totals["chat_output_tokens"] == out_est + 40 and totals["chat_input_tokens"] == 200
    assert totals["chat_requests"] == 2
    assert Decimal(totals["chat_cost_usd"]) == sum(r["cost_usd"] for r in rows.values())


def test_guard_applies_to_the_answering_model_of_a_fallback_chain():
    # Merge of AC.5 with the guard: the answering model's streamed usage is corrected; without the
    # streamed response (non-streamed caller) it is left as reported.
    from eap_worker.pricing import fallback_usage

    response = _text_response()
    response.usage = _usage(2, 300)
    usage, extra, failed = fallback_usage(["litellm:a", "litellm:b"], response, streamed_response=response)
    assert usage["model_id"] == "litellm:a" and extra["fallback_index"] == 0 and failed == []
    assert usage["output_tokens"] == 300 + approx_tokens(SECRET) and usage["usage_estimated"] is True
    plain, _extra, _failed = fallback_usage(["litellm:a", "litellm:b"], response)
    assert plain["output_tokens"] == 2 and "usage_estimated" not in plain
