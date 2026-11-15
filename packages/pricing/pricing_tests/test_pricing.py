"""eap_pricing: the override file and cost arithmetic shared by the worker and the API."""

from __future__ import annotations

import json
import pickle
from datetime import date
from decimal import Decimal

import pytest

import eap_pricing
from eap_pricing import (
    COST_QUANTUM,
    InvalidPriceFile,
    PriceFile,
    Usage,
    format_cost,
    load_overrides,
    override_cost,
    override_period,
    parse_price_file,
    prices_file_path,
)

DAY = date(2026, 11, 13)


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    def write(models):
        path = tmp_path / "prices.json"
        path.write_text(json.dumps({"models": models}))
        monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(path))
        return path

    return write


def test_exact_key_beats_wildcard_and_latest_period_applies():
    prices = parse_price_file(json.dumps({"models": {
        "test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "1"}],
        "test:x": [
            {"effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "0"},
            {"effective_from": "2026-06-01", "input_per_mtok": "3", "output_per_mtok": "0"},
            {"effective_from": "2027-01-01", "input_per_mtok": "100", "output_per_mtok": "0"},
        ],
    }}))
    assert override_period(prices, "test:x", DAY).input_per_mtok == Decimal("3")
    assert override_period(prices, "test:y", DAY).input_per_mtok == Decimal("1")
    assert override_period(prices, "other:x", DAY) is None
    assert override_period(prices, "test:x", date(2025, 1, 1)) is None


def test_cost_arithmetic_and_decimal_string():
    period = parse_price_file(json.dumps({"models": {"test:*": [{
        "effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "8",
        "cache_read_per_mtok": "0.5",
    }]}})).models["test:*"][0]
    # 1000 input (200 cache read, 100 cache write at the input price), 50 output.
    cost = override_cost(period, Usage(1000, 50, 200, 100)).quantize(COST_QUANTUM)
    assert cost == (Decimal(700 * 2 + 200 * Decimal("0.5") + 100 * 2 + 50 * 8) / Decimal(1_000_000)).quantize(COST_QUANTUM)
    assert format_cost(cost) == "0.0021000000"
    assert format_cost(Decimal("1").quantize(COST_QUANTUM)) == "1.0000000000"
    assert format_cost(None) is None


def test_invalid_files_and_keys():
    with pytest.raises(InvalidPriceFile):
        parse_price_file('{"models": {"test:x": [{"effective_from": "2026-01-01", "input_per_mtok": "-1", "output_per_mtok": "0"}]}}')
    with pytest.raises(InvalidPriceFile, match="bad model key"):
        parse_price_file('{"models": {"nocolon": []}}')
    exc = InvalidPriceFile("/p", "why")
    assert str(pickle.loads(pickle.dumps(exc))) == "invalid model price file /p: why"


def test_load_overrides_missing_default_is_empty_explicit_missing_is_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv(eap_pricing.PRICES_FILE_ENV, raising=False)
    assert load_overrides(tmp_path / "absent.json") == PriceFile()
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path / "absent.json"))
    value = load_overrides()
    assert isinstance(value, InvalidPriceFile) and value.reason.startswith("file not found")


def test_relative_path_resolves_against_repo_root(monkeypatch):
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, "config/model-prices.test.json")
    path = prices_file_path()
    assert path == eap_pricing.REPO_ROOT / "config" / "model-prices.test.json" and path.is_file()


def test_reload_on_change(prices_file):
    path = prices_file({"test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "0"}]})
    assert override_period(load_overrides(), "test:a", DAY).input_per_mtok == Decimal("1")
    path.write_text(json.dumps({"models": {"test:*": [
        {"effective_from": "2026-01-01", "input_per_mtok": "22", "output_per_mtok": "0"}]}}))
    assert override_period(load_overrides(), "test:a", DAY).input_per_mtok == Decimal("22")


# --- the API's estimate and the worker's price agree (one implementation) ---------------------------


def test_api_estimate_equals_worker_override_price(prices_file):
    from datetime import datetime, timezone

    from eap_api.routes.knowledge import estimate_embedding_cost
    from eap_worker.pricing import price

    prices_file({"litellm:emb": [{"effective_from": "2026-01-01", "input_per_mtok": "0.0234567", "output_per_mtok": "0"}]})
    at = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    for tokens in (1, 7, 12_345, 1_000_000, 987_654_321):
        estimate, source, note = estimate_embedding_cost("litellm:emb", tokens, at.date())
        worker_cost, worker_source = price("litellm:emb", Usage(input_tokens=tokens), at)
        assert (estimate, source, note) == (format(worker_cost, "f"), worker_source, None)


def test_api_estimate_rejects_a_file_the_worker_rejects(prices_file):
    """Before eap_pricing the API parsed the file loosely and estimated from a file the worker
    rejects (so the worker would record no cost). Now both reject it."""
    from eap_api.routes.knowledge import estimate_embedding_cost
    from eap_worker.pricing import price

    prices_file({"test:*": [{"effective_from": "2026-01-01", "input_per_mtok": "2", "output_per_mtok": "0",
                             "typo_per_mtok": "1"}]})
    cost, source, note = estimate_embedding_cost("test:hash", 10, DAY)
    assert (cost, source) == (None, None) and "unreadable or invalid" in note
    assert price("test:hash", Usage(input_tokens=10)) == (None, None)


# ----------------------------------------------------------------------------- price_known


def _period(inp: str, out: str, start: str = "2026-01-01", **extra: str) -> dict:
    return {"effective_from": start, "input_per_mtok": inp, "output_per_mtok": out, **extra}


def test_price_known_exact_key_beats_wildcard(prices_file):
    prices_file({"litellm:*": [_period("0", "0")], "litellm:big": [_period("1", "2")]})
    assert eap_pricing.price_known("litellm:big", DAY) == "priced"
    assert eap_pricing.price_known("litellm:other", DAY) == "zero"
    # And the reverse: a zero exact key beats a priced wildcard.
    prices_file({"test:*": [_period("1", "1")], "test:free": [_period("0", "0")]})
    assert eap_pricing.price_known("test:free", DAY) == "zero"
    assert eap_pricing.price_known("test:x", DAY) == "priced"


def test_price_known_zero_prices_and_cache_only_price(prices_file):
    prices_file({"test:*": [_period("0", "0")], "test:cache": [_period("0", "0", cache_read_per_mtok="0.1")]})
    assert eap_pricing.price_known("test:x", DAY) == "zero"
    # Any token kind with a price makes it "priced".
    assert eap_pricing.price_known("test:cache", DAY) == "priced"
    # An override of 0 also wins over genai-prices for a direct provider.
    prices_file({"openai:gpt-4o": [_period("0", "0")]})
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "zero"


def test_price_known_future_period(prices_file):
    prices_file({"test:*": [_period("0", "0"), _period("5", "5", start="2027-01-01")]})
    assert eap_pricing.price_known("test:x", DAY) == "zero"
    assert eap_pricing.price_known("test:x", date(2027, 1, 1)) == "priced"
    # Only a future period: no price yet (test:* is never priced from genai-prices).
    prices_file({"test:*": [_period("5", "5", start="2027-01-01")]})
    assert eap_pricing.price_known("test:x", DAY) == "unpriced"


def test_price_known_genai_prices_model(prices_file):
    from datetime import datetime, timezone

    prices_file({})
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "priced"
    assert eap_pricing.price_known("openai:gpt-4o", datetime(2026, 11, 13, 12, tzinfo=timezone.utc)) == "priced"
    # A proxied name is never priced as the OpenAI model of that name.
    assert eap_pricing.price_known("litellm:gpt-4o", DAY) == "unpriced"


def test_price_known_unknown_model(prices_file):
    prices_file({"litellm:*": [_period("0", "0")]})
    assert eap_pricing.price_known("openai:no-such-model-xyz", DAY) == "unpriced"
    assert eap_pricing.price_known("no-such-provider:x", DAY) == "unpriced"
    assert eap_pricing.price_known("test:x", DAY) == "unpriced"


def test_price_known_unreadable_override_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(bad))
    # The worker prices nothing then, not even from genai-prices: so nothing is known.
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "unpriced"
    assert eap_pricing.price_known("litellm:x", DAY) == "unpriced"
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path))  # a directory: unreadable
    assert eap_pricing.price_known("openai:gpt-4o", DAY) == "unpriced"
    # An explicitly passed invalid file behaves the same.
    assert eap_pricing.price_known("openai:gpt-4o", DAY, overrides=InvalidPriceFile("x", "y")) == "unpriced"


def test_price_known_explicit_overrides_argument(monkeypatch, tmp_path):
    monkeypatch.setenv(eap_pricing.PRICES_FILE_ENV, str(tmp_path / "missing.json"))  # would be invalid
    overrides = PriceFile.model_validate({"models": {"test:*": [_period("1", "1")]}})
    assert eap_pricing.price_known("test:x", DAY, overrides=overrides) == "priced"
    assert eap_pricing.price_known("openai:gpt-4o", DAY, overrides=PriceFile()) == "priced"


def test_price_known_agrees_with_price_request(prices_file):
    """The worker's lookup (price_request) and price_known never disagree."""
    from datetime import datetime, timezone

    at = datetime(2026, 11, 13, tzinfo=timezone.utc)
    prices_file({"litellm:*": [_period("0", "0")], "litellm:big": [_period("1", "2")],
                 "test:later": [_period("1", "1", start="2027-01-01")]})
    for model in ("litellm:big", "litellm:x", "test:later", "openai:gpt-4o", "openai:no-such-model-xyz", "test:x"):
        cost, source = eap_pricing.price_request(model, Usage(1000, 1000), at)
        expected = "unpriced" if cost is None else ("zero" if cost == 0 else "priced")
        assert eap_pricing.price_known(model, at) == expected, model
        assert (cost is None) == (source is None)
