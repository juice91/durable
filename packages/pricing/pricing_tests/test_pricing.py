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
