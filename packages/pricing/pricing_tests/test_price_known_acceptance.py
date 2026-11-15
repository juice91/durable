"""price_known acceptance: edge cases, parity with the worker's real price(), and unchanged costs."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import eap_pricing
from eap_pricing import (
    COST_QUANTUM, InvalidPriceFile, OVERRIDE_ONLY_PROVIDERS, PriceFile, Usage, override_cost, override_period,
)

DAY = date(2026, 11, 14)
AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _p(inp, out, start="2026-01-01", **extra):
    return {"effective_from": start, "input_per_mtok": inp, "output_per_mtok": out, **extra}


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    def write(models):
        path = tmp_path / "prices.json"
        path.write_text(json.dumps({"models": models}))
        monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(path))
        return path

    return write


FILE = {
    "litellm:*": [_p("0", "0")],
    "litellm:big": [_p("1", "2")],
    "litellm:old": [_p("1", "1", start="2026-01-01"), _p("0", "0", start="2026-06-01")],
    "litellm:soon": [_p("0", "0"), _p("9", "9", start="2027-01-01")],
    "test:*": [_p("2", "8")],
    "test:cacheonly": [_p("0", "0", cache_write_per_mtok="1")],
    "other:future": [_p("1", "1", start="2027-01-01")],
    "openai:gpt-4o": [_p("0", "0")],
}


def test_exact_key_beats_wildcard_both_ways(prices_file):
    prices_file(FILE)
    assert eap_pricing.price_known("litellm:big", DAY) == "priced"
    assert eap_pricing.price_known("litellm:other", DAY) == "zero"
    prices_file({"test:*": [_p("1", "1")], "test:free": [_p("0", "0")]})
    assert eap_pricing.price_known("test:free", DAY) == "zero"
    assert eap_pricing.price_known("test:any", DAY) == "priced"


def test_zero_prices_cache_only_and_override_over_genai(prices_file):
    prices_file(FILE)
    assert eap_pricing.price_known("litellm:zzz", DAY) == "zero"
    assert eap_pricing.price_known("test:cacheonly", DAY) == "priced"  # a cache price is a price
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "zero"  # override 0 beats genai-prices


def test_future_period_is_ignored_and_latest_started_wins(prices_file):
    prices_file(FILE)
    assert eap_pricing.price_known("litellm:soon", DAY) == "zero"
    assert eap_pricing.price_known("litellm:soon", date(2026, 12, 31)) == "zero"
    assert eap_pricing.price_known("litellm:soon", date(2027, 1, 1)) == "priced"
    assert eap_pricing.price_known("other:future", DAY) == "unpriced"
    assert eap_pricing.price_known("other:future", date(2027, 1, 1)) == "priced"
    # Latest period that has started: 2026-06-01 (zero) replaces 2026-01-01 (priced).
    assert eap_pricing.price_known("litellm:old", date(2026, 5, 31)) == "priced"
    assert eap_pricing.price_known("litellm:old", date(2026, 6, 1)) == "zero"
    assert eap_pricing.price_known("litellm:old", DAY) == "zero"


def test_datetime_day_and_naive_datetime(prices_file):
    prices_file(FILE)
    assert eap_pricing.price_known("other:future", datetime(2027, 1, 1, tzinfo=timezone.utc)) == "priced"
    assert eap_pricing.price_known("other:future", datetime(2027, 1, 1)) == "priced"  # naive = UTC
    assert eap_pricing.price_known("other:future", datetime(2026, 12, 31, 23, 59)) == "unpriced"


def test_genai_model_unknown_model_and_override_only_providers(prices_file):
    prices_file({})
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "priced"
    assert eap_pricing.price_known("openai:no-such-model-xyz", DAY) == "unpriced"
    assert eap_pricing.price_known("unknown-provider:x", DAY) == "unpriced"
    assert eap_pricing.price_known("noprefix", DAY) == "unpriced"
    for provider in OVERRIDE_ONLY_PROVIDERS:
        assert eap_pricing.price_known(f"{provider}:gpt-4o", DAY) == "unpriced"


def test_unreadable_or_invalid_override_file_is_unpriced_for_all(tmp_path, monkeypatch):
    for content in ("{not json", json.dumps({"models": {"bad key": [_p("1", "1")]}}),
                    json.dumps({"models": {"test:x": [{"effective_from": "nope"}]}})):
        f = tmp_path / "p.json"
        f.write_text(content)
        monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(f))
        for m in ("openai:gpt-4o", "litellm:x", "test:x"):
            assert eap_pricing.price_known(m, DAY) == "unpriced", (content, m)
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path))  # a directory
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "unpriced"
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path / "missing.json"))
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "unpriced"
    assert eap_pricing.price_known("openai:gpt-4o", DAY, overrides=InvalidPriceFile("p", "r")) == "unpriced"
    assert eap_pricing.price_known("openai:gpt-4o", DAY, overrides=PriceFile()) == "priced"


MODELS = ["litellm:big", "litellm:other", "litellm:old", "litellm:soon", "test:x", "test:cacheonly",
          "other:future", "openai:gpt-4o", "openai:gpt-4o-mini", "anthropic:claude-sonnet-4-5",
          "openai:no-such-model-xyz", "unknown:x", "google-gla:gemini-2.0-flash", "noprefix"]


@pytest.mark.parametrize("variant", ["file", "empty", "invalid"])
def test_parity_with_the_workers_real_price(prices_file, tmp_path, monkeypatch, variant):
    from eap_worker.pricing import price

    if variant == "file":
        prices_file(FILE)
    elif variant == "empty":
        prices_file({})
    else:
        bad = tmp_path / "bad.json"
        bad.write_text("{nope")
        monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(bad))
    for model in MODELS:
        for when in (AT, datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2027, 2, 1, tzinfo=timezone.utc)):
            cost, source = price(
                model, Usage(3_000_000, 1_000_000, 1_000_000, 1_000_000), when)
            expected = "unpriced" if cost is None else ("zero" if cost == 0 else "priced")
            assert eap_pricing.price_known(model, when) == expected, (variant, model, when)
            assert eap_pricing.price_known(model, when.date()) == expected, (variant, model, when)
            assert (cost is None) == (source is None)


def _reference_price(model_id, usage, at, overrides):
    """The pre-change worker lookup, frozen."""
    from genai_prices import Usage as GU, calc_price

    if not usage.measured or isinstance(overrides, InvalidPriceFile):
        return None, None
    period = override_period(overrides, model_id, at.date())
    if period is not None:
        return override_cost(period, usage).quantize(COST_QUANTUM), "override"
    provider, _, name = model_id.partition(":")
    if provider in OVERRIDE_ONLY_PROVIDERS or not name:
        return None, None
    try:
        r = calc_price(GU(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                          cache_read_tokens=usage.cache_read_tokens or None,
                          cache_write_tokens=usage.cache_write_tokens or None),
                       name, provider_id={"google-gla": "google", "google-vertex": "google"}.get(provider, provider),
                       genai_request_timestamp=at)
    except LookupError:
        return None, None
    return Decimal(r.total_price).quantize(COST_QUANTUM), eap_pricing.genai_source()


def test_costs_are_identical_to_the_pre_change_lookup(prices_file, monkeypatch):
    from eap_worker.pricing import price

    path = prices_file(FILE)
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(path))
    overrides = eap_pricing.load_overrides()
    usages = [Usage(1000, 100), Usage(1_000_000, 500_000, 200_000, 100_000), Usage(0, 0), Usage(measured=False),
              Usage(7, 3, 2, 1)]
    for model in MODELS:
        for u in usages:
            for when in (AT, datetime(2026, 3, 1, tzinfo=timezone.utc)):
                want = _reference_price(model, u, when, overrides)
                assert price(model, u, when) == want == eap_pricing.price_request(model, u, when, overrides), (
                    model, u, when)
