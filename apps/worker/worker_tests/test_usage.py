"""P0.1 usage and cost recording: price lookup (unit) and a real test:ticket run (in-process worker)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_worker import pricing
from eap_worker.pricing import Usage, price, request_usage_input, usage_record

AT = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _write(tmp_path, models: dict) -> str:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"models": models}))
    return str(path)


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    def use(models: dict) -> str:
        path = _write(tmp_path, models)
        monkeypatch.setenv(pricing.PRICES_FILE_ENV, path)
        return path

    return use


def test_override_exact_beats_wildcard_and_cache_arithmetic(prices_file):
    prices_file({
        "test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "1"}],
        "test:ticket": [{
            "effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "8",
            "cache_read_per_mtok": "0.5", "cache_write_per_mtok": "2.5",
        }],
    })
    # 1000 input of which 200 cache read and 100 cache write -> 700 uncached.
    cost, source = price("test:ticket", Usage(1000, 50, 200, 100), AT)
    expected = (Decimal(700) * 2 + Decimal(200) * Decimal("0.5") + Decimal(100) * Decimal("2.5") + Decimal(50) * 8) / Decimal(10**6)
    assert cost == expected.quantize(Decimal("1e-10")) and source == "override"
    cost, _ = price("test:other", Usage(1000, 1000), AT)
    assert cost == Decimal("0.0020000000")


def test_effective_from_picks_latest_period_not_in_future(prices_file):
    prices_file({"test:*": [
        {"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "0"},
        {"effective_from": "2026-06-01", "input_per_mtok": "3", "output_per_mtok": "0"},
        {"effective_from": "2027-01-01", "input_per_mtok": "100", "output_per_mtok": "0"},
    ]})
    assert price("test:x", Usage(1_000_000, 0), AT)[0] == Decimal(3)
    # Before every period: no override; test:* is never priced from genai-prices.
    assert price("test:x", Usage(1_000_000, 0), datetime(2025, 1, 1, tzinfo=timezone.utc)) == (None, None)


def test_override_beats_genai_prices(prices_file):
    prices_file({"openai:gpt-4o": [{"effective_from": "2026-01-01", "input_per_mtok": "0", "output_per_mtok": "0"}]})
    assert price("openai:gpt-4o", Usage(1000, 100), AT) == (Decimal("0E-10"), "override")


def test_genai_prices_for_direct_providers_only(prices_file):
    prices_file({})
    cost, source = price("openai:gpt-4o", Usage(1000, 100), AT)
    assert cost is not None and cost > 0 and source.startswith("genai-prices")
    # A proxied name is never priced as the OpenAI model; unknown models are unpriced.
    assert price("litellm:gpt-4o", Usage(1000, 100), AT) == (None, None)
    assert price("test:gpt-4o", Usage(1000, 100), AT) == (None, None)
    assert price("openai:no-such-model-xyz", Usage(1000, 100), AT) == (None, None)


def test_unmeasured_usage_has_no_cost(prices_file):
    prices_file({"test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "1"}]})
    assert price("test:x", Usage(measured=False), AT) == (None, None)

    class Unmeasured:
        unmeasured_requests = 1
        input_tokens = 0
        output_tokens = 0

    usage = request_usage_input("test:x", None, Unmeasured())
    assert usage["usage_measured"] is False and usage["input_tokens"] == 0
    row, fields = usage_record("model:0", usage, AT)
    assert row["cost_usd"] is None and row["price_source"] is None and row["usage_measured"] is False
    assert fields["cost_usd"] is None and fields["usage"]["measured"] is False


def test_invalid_or_missing_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text('{"models": {"gpt-4o": []}}')  # key without provider
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(bad))
    # An invalid file prices nothing, not even from genai-prices.
    assert price("openai:gpt-4o", Usage(1000, 100), AT) == (None, None)
    # An explicitly set file that does not exist prices nothing either (no genai-prices fallback).
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(tmp_path / "missing.json"))
    assert price("openai:gpt-4o", Usage(1000, 100), AT) == (None, None)
    # Only the repo default file being absent means "no overrides".
    monkeypatch.delenv(pricing.PRICES_FILE_ENV)
    monkeypatch.setattr(pricing, "DEFAULT_PRICES_FILE", tmp_path / "no-default.json")
    assert price("openai:gpt-4o", Usage(1000, 100), AT)[1].startswith("genai-prices")
    with pytest.raises(pricing.InvalidPriceFile):
        pricing.parse_price_file('{"models": {"test:x": [{"effective_from": "2026-01-01", "input_per_mtok": "-1", "output_per_mtok": "0"}]}}')


def test_usage_record_event_fields(prices_file):
    prices_file({"test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "8"}]})
    usage = {"model_id": "test:ticket", "provider_model_name": "scripted-ticket", "input_tokens": 10,
             "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True}
    row, fields = usage_record("model:3", usage, AT)
    assert row["request_key"] == "model:3" and row["cost_usd"] == Decimal("0.0000600000")
    assert fields == {
        "model_id": "test:ticket",
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0,
                  "measured": True, "provider_model_name": "scripted-ticket", "price_source": "override"},
        "cost_usd": "0.0000600000",
    }


# ----------------------------------------------------------------------------- real run


def test_ticket_run_records_usage_and_totals(worker, client, tenant):
    from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal

    # conftest points EAP_MODEL_PRICES_FILE at config/model-prices.test.json (test:* = $2 / $8 per MTok).
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=ticket_spec(),
                         input_text="My printer is on fire", app_version=APP_VERSION)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]

    finals = {e[1]: e[3] for e in event_rows(worker.engine, run_id) if e[2] == "model_message"}
    assert set(finals) == {"model:0:final", "model:1:final"}
    with worker.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT request_key, model_id, provider_model_name, input_tokens, output_tokens, usage_measured,"
            " cost_usd, price_source, tenant_id FROM run_model_requests WHERE run_id = :r ORDER BY request_key"
        ), {"r": str(run_id)}).mappings().all()
    assert [r["request_key"] for r in rows] == ["model:0", "model:1"]
    for row in rows:
        assert row["tenant_id"] == tenant and row["model_id"] == "test:ticket"
        assert row["provider_model_name"] == "scripted-ticket" and row["usage_measured"] is True
        assert row["input_tokens"] > 0 and row["output_tokens"] > 0
        expected = (Decimal(row["input_tokens"]) * 2 + Decimal(row["output_tokens"]) * 8) / Decimal(10**6)
        assert row["cost_usd"] == expected and row["price_source"] == "override"
        data = finals[f"{row['request_key']}:final"]
        assert data["model_id"] == "test:ticket" and Decimal(data["cost_usd"]) == row["cost_usd"]
        assert data["usage"]["input_tokens"] == row["input_tokens"]
        assert data["usage"]["output_tokens"] == row["output_tokens"]

    assert run["input_tokens"] == sum(r["input_tokens"] for r in rows)
    assert run["output_tokens"] == sum(r["output_tokens"] for r in rows)
    assert run["cost_usd"] == sum(r["cost_usd"] for r in rows)
    assert run["started_at"] is not None and run["finished_at"] is not None
    assert run["created_at"] <= run["started_at"] <= run["finished_at"]


def test_relative_prices_path_resolves_against_repo_root(monkeypatch):
    monkeypatch.setenv(pricing.PRICES_FILE_ENV, "config/model-prices.test.json")
    monkeypatch.chdir("/")
    path = pricing.prices_file_path()
    assert path == pricing.REPO_ROOT / "config" / "model-prices.test.json" and path.is_file()
    assert price("test:ticket", Usage(1_000_000, 0), AT) == (Decimal("2.0000000000"), "override")


@pytest.mark.parametrize("explicit", [True, False])
def test_unreadable_prices_file_prices_nothing(tmp_path, monkeypatch, caplog, explicit):
    path = tmp_path / "prices.json"
    path.write_text('{"models": {}}')

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return original(self, *args, **kwargs)

    original = type(path).stat
    monkeypatch.setattr(type(path), "stat", denied)
    if explicit:
        monkeypatch.setenv(pricing.PRICES_FILE_ENV, str(path))
    else:
        monkeypatch.delenv(pricing.PRICES_FILE_ENV, raising=False)
        monkeypatch.setattr(pricing, "DEFAULT_PRICES_FILE", path)
    with caplog.at_level("ERROR", logger="eap_worker"):
        assert price("openai:gpt-4o", Usage(1000, 100), AT) == (None, None)
    assert "cannot access the file (PermissionError)" in caplog.text
    assert "the price file prices nothing" in caplog.text


# ----------------------------------------------------------------------------- impossible streamed usage


def _response(text: str = "", tool_args: dict | None = None):
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart

    parts: list = [ThinkingPart(content="x" * 4000)]  # thinking is covered by reasoning_tokens
    if text:
        parts.append(TextPart(content=text))
    if tool_args is not None:
        parts.append(ToolCallPart(tool_name="lookup", args=tool_args, tool_call_id="c1"))
    return ModelResponse(parts=parts, model_name="qwen3.8-thinking")


def _usage(output_tokens: int, reasoning: int, *, details_only: bool = False):
    from pydantic_ai.usage import RequestUsage

    if details_only:
        return RequestUsage(input_tokens=100, output_tokens=output_tokens, details={"reasoning_tokens": reasoning})
    return RequestUsage(input_tokens=100, output_tokens=output_tokens, output_reasoning_tokens=reasoning,
                        details={"reasoning_tokens": reasoning})


def test_streamed_usage_output_below_reasoning_is_corrected_and_logged(caplog, prices_file):
    from eap_schemas.knowledge import approx_tokens

    prices_file({"litellm:*": [{"effective_from": "2026-01-01", "input_per_mtok": "0", "output_per_mtok": "1"}]})
    text = "Paris is the capital of France. " * 10
    args = {"city": "Paris", "country": "France"}
    response = _response(text, args)
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:qwen3.8-thinking", response.model_name, _usage(2, 300),
                                    streamed_response=response)
    expected = 300 + approx_tokens(text) + approx_tokens(json.dumps(args, separators=(",", ":")))
    assert usage["output_tokens"] == expected and usage["usage_estimated"] is True
    assert usage["input_tokens"] == 100 and usage["usage_measured"] is True
    records = [r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]
    assert len(records) == 1
    r = records[0]
    assert (r.reported_output_tokens, r.reasoning_tokens, r.estimated_output_tokens) == (2, 300, expected)
    assert "Paris" not in r.getMessage() and "Paris" not in str(r.__dict__)  # counts only, no text
    # The corrected count is what is recorded and priced, and the event marks it as estimated.
    row, fields = usage_record("model:0", usage, AT)
    assert row["output_tokens"] == expected and fields["usage"]["output_tokens"] == expected
    assert fields["usage"]["estimated"] is True
    assert row["cost_usd"] == (Decimal(expected) / Decimal(10**6)).quantize(Decimal("1e-10"))


def test_streamed_usage_reasoning_only_in_details_is_corrected():
    response = _response("ok")
    usage = request_usage_input("litellm:q", None, _usage(1, 50, details_only=True), streamed_response=response)
    assert usage["output_tokens"] == 51 and usage["usage_estimated"] is True


def test_streamed_correct_usage_is_unchanged(caplog):
    response = _response("Paris.")
    with caplog.at_level("WARNING", logger="eap_worker"):
        for out, reasoning in ((300, 300), (350, 300), (5, 0)):
            usage = request_usage_input("litellm:q", None, _usage(out, reasoning), streamed_response=response)
            assert usage == request_usage_input("litellm:q", None, _usage(out, reasoning))
            assert usage["output_tokens"] == out and "usage_estimated" not in usage
            _row, fields = usage_record("model:0", usage, AT)
            assert "estimated" not in fields["usage"]
    assert not [r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]


def test_non_streamed_usage_is_unchanged(caplog):
    # Processors, observers and the judge call models without streaming: never corrected.
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("litellm:q", None, _usage(2, 300))
    assert usage["output_tokens"] == 2 and "usage_estimated" not in usage
    assert not [r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]


def test_streamed_unmeasured_usage_is_unchanged():
    from pydantic_ai.usage import RequestUsage

    usage = request_usage_input("litellm:q", None, RequestUsage(unmeasured_requests=1), streamed_response=_response("hi"))
    assert usage["usage_measured"] is False and usage["output_tokens"] == 0 and "usage_estimated" not in usage


def _xai_style_usage(completion: int, reasoning: int):
    """What pydantic_ai.models.xai builds: completion_tokens EXCLUDING reasoning as output_tokens,
    reasoning only in details (total = prompt + completion + reasoning)."""
    from pydantic_ai.usage import RequestUsage

    return RequestUsage(input_tokens=100, output_tokens=completion, details={"reasoning_tokens": reasoning})


def _litellm_style_usage(completion: int, reasoning: int):
    """What pydantic_ai.models.openai builds for the LiteLLM gateway: completion_tokens (which
    INCLUDES reasoning under OpenAI semantics) as output_tokens, completion_tokens_details in details."""
    from pydantic_ai.usage import RequestUsage

    return RequestUsage(input_tokens=100, output_tokens=completion,
                        details={"reasoning_tokens": reasoning, "accepted_prediction_tokens": 0})


@pytest.mark.parametrize("completion,reasoning", [(9, 300), (400, 50)])
def test_xai_style_streamed_usage_is_unchanged(caplog, completion, reasoning):
    # xAI: a short answer after long reasoning is an exact count, never "impossible".
    response = _response("Paris.")
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input("xai:grok-3-mini", "grok-3-mini", _xai_style_usage(completion, reasoning),
                                    streamed_response=response)
    assert usage["output_tokens"] == completion and "usage_estimated" not in usage
    assert usage == request_usage_input("xai:grok-3-mini", "grok-3-mini", _xai_style_usage(completion, reasoning))
    assert not [r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]


@pytest.mark.parametrize("provider", ["google-gla", "anthropic", "groq", "mistral"])
def test_other_providers_are_never_corrected(provider):
    usage = request_usage_input(f"{provider}:m", None, _xai_style_usage(2, 300), streamed_response=_response("hi"))
    assert usage["output_tokens"] == 2 and "usage_estimated" not in usage


@pytest.mark.parametrize("provider", sorted(pricing.REASONING_INCLUSIVE_PROVIDERS))
def test_litellm_style_streamed_usage_is_corrected(caplog, provider):
    from eap_schemas.knowledge import approx_tokens

    text = "Paris is the capital of France."
    with caplog.at_level("WARNING", logger="eap_worker"):
        usage = request_usage_input(f"{provider}:qwen3.8-thinking", None, _litellm_style_usage(2, 300),
                                    streamed_response=_response(text))
    assert usage["output_tokens"] == 300 + approx_tokens(text) and usage["usage_estimated"] is True
    assert len([r for r in caplog.records if getattr(r, "code", None) == pricing.USAGE_OUTPUT_BELOW_REASONING]) == 1


def test_memory_scrub_keeps_usage_estimated():
    from eap_run_store.memory import _scrub_usage

    usage = {"input_tokens": 1, "output_tokens": 310, "measured": True, "estimated": True, "text": "x"}
    assert _scrub_usage(usage) == {"input_tokens": 1, "output_tokens": 310, "measured": True, "estimated": True}
    assert "estimated" not in _scrub_usage({"estimated": "yes"})
