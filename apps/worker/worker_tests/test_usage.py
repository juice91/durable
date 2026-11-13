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
    assert "recorded without a price" in caplog.text
