"""Model request prices: what one model request cost, computed once when it is recorded.

Lookup is by OUR model id (`provider:name`, the effective model of the run), never by the
provider-reported model name:

1. The operator override file (`EAP_MODEL_PRICES_FILE`, a relative path resolved against the
   repository root; default `config/model-prices.json` there), reviewed like code. Its schema,
   loading and cost arithmetic live in `eap_pricing` (shared with the API's reindex estimate). An exact `provider:name` key wins over a `provider:*` key.
   Each key holds price periods; the one with the latest `effective_from` not after the request time
   applies. Prices are USD per million tokens (input, output, cache read, cache write; a missing
   cache price falls back to the input price):

       {"models": {"litellm:*": [{"effective_from": "2026-01-01", "input_per_mtok": "0",
                                  "output_per_mtok": "0"}]}}

2. `genai-prices` (Pydantic's price data, bundled snapshot, no network) for direct providers only.
   `test:*` and `litellm:*` are never priced from it: a model behind the LiteLLM proxy (an
   OpenAI-compatible endpoint) would otherwise be priced as if it were the OpenAI model of that name.

3. No price: cost None, source None (counted as unpriced).

An unreadable or invalid override file, or an EAP_MODEL_PRICES_FILE that does not exist, prices
nothing (cost None for every model, logged): a broken or misplaced file must never silently fall
back to genai-prices prices for models it was meant to price.

The cost is stored on the request's row and in its event and never recomputed, so historical cost
does not change when prices change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

# The override file and its cost arithmetic are shared with the API's reindex estimate (eap_pricing);
# re-exported here so existing imports keep working.
from eap_pricing import (
    COST_QUANTUM,
    OVERRIDE_ONLY_PROVIDERS,
    PRICES_FILE_ENV,
    REPO_ROOT,
    SOURCE_OVERRIDE,
    InvalidPriceFile,
    PriceFile,
    PricePeriod,
    Usage,
    load_overrides,
    override_cost,
    override_period,
    parse_price_file,
)
from eap_pricing import DEFAULT_PRICES_FILE as _SHARED_DEFAULT_PRICES_FILE
from eap_pricing import prices_file_path as _shared_prices_file_path

log = logging.getLogger("eap_worker")

DEFAULT_PRICES_FILE = _SHARED_DEFAULT_PRICES_FILE
# Our provider id -> genai-prices provider id, where they differ.
_GENAI_PROVIDER_IDS = {"google-gla": "google", "google-vertex": "google"}


def _genai_source() -> str:
    try:
        from importlib.metadata import version

        return f"genai-prices/{version('genai-prices')}"
    except Exception:  # noqa: BLE001 - only a label
        return "genai-prices"


def prices_file_path() -> Path:
    """EAP_MODEL_PRICES_FILE (a relative path is resolved against the repository root, not the
    process's cwd), else the repo default config/model-prices.json."""
    return _shared_prices_file_path(DEFAULT_PRICES_FILE)


def _load_overrides() -> PriceFile | InvalidPriceFile:
    """The override file (eap_pricing.load_overrides), defaulting to this module's DEFAULT_PRICES_FILE."""
    return load_overrides(DEFAULT_PRICES_FILE)


def _genai_cost(model_id: str, usage: Usage, at: datetime) -> Decimal | None:
    provider, _, name = model_id.partition(":")
    if provider in OVERRIDE_ONLY_PROVIDERS or not name:
        return None
    try:
        from genai_prices import Usage as GenaiUsage, calc_price

        result = calc_price(
            GenaiUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens or None,
                cache_write_tokens=usage.cache_write_tokens or None,
            ),
            name,
            provider_id=_GENAI_PROVIDER_IDS.get(provider, provider),
            genai_request_timestamp=at,
        )
    except LookupError:
        return None
    except Exception as exc:  # noqa: BLE001 - a price lookup must never fail the run
        log.warning("genai-prices lookup failed for %s: %s", model_id, type(exc).__name__)
        return None
    return Decimal(result.total_price)


def price(model_id: str, usage: Usage, at: datetime | None = None) -> tuple[Decimal | None, str | None]:
    """(cost in USD quantized to 10 decimals, price source) for one model request of `model_id`,
    or (None, None) when no price is known. An unmeasured request (the provider reported no usage)
    has no cost: (None, None)."""
    if not usage.measured:
        return None, None
    at = at or datetime.now(timezone.utc)
    overrides = _load_overrides()
    if isinstance(overrides, InvalidPriceFile):
        return None, None
    period = override_period(overrides, model_id, at.date())
    if period is not None:
        return override_cost(period, usage).quantize(COST_QUANTUM), SOURCE_OVERRIDE
    cost = _genai_cost(model_id, usage, at)
    if cost is None:
        return None, None
    return cost.quantize(COST_QUANTUM), _genai_source()


# ----------------------------------------------------------------------------- recording


def usage_from_response(usage: Any) -> Usage:
    """A Pydantic AI `RequestUsage` -> Usage. `unmeasured_requests` > 0 means the provider omitted
    usage: tokens are recorded as zero and the request as unmeasured."""
    if getattr(usage, "unmeasured_requests", 0):
        return Usage(measured=False)
    return Usage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
        cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
    )


def request_usage_input(model_id: str, provider_model_name: str | None, usage: Any) -> dict[str, Any]:
    """What the workflow passes to `eap.append_event` for a model request: plain JSON, built from
    the checkpointed response only (no pricing, no I/O)."""
    u = usage_from_response(usage)
    return {
        "model_id": model_id,
        "provider_model_name": provider_model_name,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
        "usage_measured": u.measured,
    }


def usage_record(request_key: str, usage_input: dict[str, Any], at: datetime | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Price one model request. Returns (the run_model_requests row for EventLog.append, the fields
    added to its `model_message` event data: `model_id`, `usage`, `cost_usd`).

    `cost_usd` is a decimal string (exact; e.g. "0.0000123000") or None for no price."""
    at = at or datetime.now(timezone.utc)
    u = Usage(
        input_tokens=usage_input["input_tokens"],
        output_tokens=usage_input["output_tokens"],
        cache_read_tokens=usage_input["cache_read_tokens"],
        cache_write_tokens=usage_input["cache_write_tokens"],
        measured=usage_input["usage_measured"],
    )
    cost, source = price(usage_input["model_id"], u, at)
    row = {
        "request_key": request_key,
        "model_id": usage_input["model_id"],
        "provider_model_name": usage_input.get("provider_model_name"),
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
        "usage_measured": u.measured,
        "cost_usd": cost,
        "price_source": source,
        "created_at": at,
    }
    event_fields = {
        "model_id": usage_input["model_id"],
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_read_tokens": u.cache_read_tokens,
            "cache_write_tokens": u.cache_write_tokens,
            "measured": u.measured,
            "provider_model_name": usage_input.get("provider_model_name"),
            "price_source": source,
        },
        "cost_usd": None if cost is None else format(cost, "f"),
    }
    return row, event_fields


# ----------------------------------------------------------------------------- embeddings (RM.6)

OPERATION_CHAT = "chat"
OPERATION_EMBEDDING = "embedding"


def embedding_usage_record(
    request_key: str,
    *,
    model_id: str,
    provider_model_name: str | None,
    input_tokens: int,
    usage_measured: bool,
    inputs: int,
    dimensions: int,
    input_type: str,
    at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Price one embedding call with the same `price()` as chat (output tokens 0). Returns (the
    run_model_requests row for EventLog.append / KnowledgeStore.write_embeddings `usage=`, with
    `operation='embedding'`, the `embedding` event's data).

    The event data never holds text: `{model_id, inputs (count), input_tokens, cost_usd,
    usage_measured, price_source, provider_model_name, dimensions, input_type}`. An unmeasured call
    (the provider omitted usage) records 0 tokens and no cost, as for chat."""
    row, fields = usage_record(
        request_key,
        {
            "model_id": model_id,
            "provider_model_name": provider_model_name,
            "input_tokens": input_tokens if usage_measured else 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "usage_measured": usage_measured,
        },
        at,
    )
    row["operation"] = OPERATION_EMBEDDING
    event_data = {
        "model_id": model_id,
        "inputs": inputs,
        "input_tokens": row["input_tokens"],
        "cost_usd": fields["cost_usd"],
        "usage_measured": usage_measured,
        "price_source": row["price_source"],
        "provider_model_name": provider_model_name,
        "dimensions": dimensions,
        "input_type": input_type,
    }
    return row, event_data


__all__ = [
    "DEFAULT_PRICES_FILE",
    "OPERATION_CHAT",
    "OPERATION_EMBEDDING",
    "embedding_usage_record",
    "InvalidPriceFile",
    "PRICES_FILE_ENV",
    "Usage",
    "parse_price_file",
    "price",
    "prices_file_path",
    "request_usage_input",
    "usage_from_response",
    "usage_record",
]
