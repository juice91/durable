"""Model request prices: what one model request cost, computed once when it is recorded.

Lookup is by OUR model id (`provider:name`, the effective model of the run), never by the
provider-reported model name:

1. The operator override file (`EAP_MODEL_PRICES_FILE`, a relative path resolved against the
   repository root; default `config/model-prices.json` there), reviewed like code. An exact `provider:name` key wins over a `provider:*` key.
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
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("eap_worker")

PRICES_FILE_ENV = "EAP_MODEL_PRICES_FILE"
DEFAULT_PRICES_FILE = Path(__file__).resolve().parents[4] / "config" / "model-prices.json"
# Providers that are never priced from genai-prices (override file only).
OVERRIDE_ONLY_PROVIDERS = frozenset({"test", "litellm"})
# Our provider id -> genai-prices provider id, where they differ.
_GENAI_PROVIDER_IDS = {"google-gla": "google", "google-vertex": "google"}
COST_QUANTUM = Decimal("0.0000000001")  # Numeric(20, 10)
_MTOK = Decimal(1_000_000)

SOURCE_OVERRIDE = "override"


def _genai_source() -> str:
    try:
        from importlib.metadata import version

        return f"genai-prices/{version('genai-prices')}"
    except Exception:  # noqa: BLE001 - only a label
        return "genai-prices"


# ----------------------------------------------------------------------------- usage


@dataclass(frozen=True)
class Usage:
    """One model request's token counts. `input_tokens` includes cache reads and writes (Pydantic AI
    and genai-prices convention). `measured` is False when the provider reported no usage."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    measured: bool = True


# ----------------------------------------------------------------------------- override file


class PricePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effective_from: date
    input_per_mtok: Decimal = Field(ge=0)
    output_per_mtok: Decimal = Field(ge=0)
    cache_read_per_mtok: Decimal | None = Field(default=None, ge=0)
    cache_write_per_mtok: Decimal | None = Field(default=None, ge=0)


class PriceFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: dict[str, list[PricePeriod]] = Field(default_factory=dict)


class InvalidPriceFile(Exception):
    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(path, reason)  # pickle-safe

    def __str__(self) -> str:
        return f"invalid model price file {self.path}: {self.reason}"


REPO_ROOT = DEFAULT_PRICES_FILE.parents[1]


def prices_file_path() -> Path:
    """EAP_MODEL_PRICES_FILE (a relative path is resolved against the repository root, not the
    process's cwd), else the repo default config/model-prices.json."""
    configured = os.environ.get(PRICES_FILE_ENV)
    if not configured:
        return DEFAULT_PRICES_FILE
    path = Path(configured).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def parse_price_file(text: str, path: str = "<string>") -> PriceFile:
    try:
        parsed = PriceFile.model_validate_json(text)
    except ValidationError as exc:
        raise InvalidPriceFile(path, f"{exc.error_count()} validation error(s)") from exc
    for key in parsed.models:
        provider, sep, name = key.partition(":")
        if not sep or not provider or not name or "*" in provider or ("*" in name and name != "*"):
            raise InvalidPriceFile(path, f"bad model key {key!r}: expected 'provider:name' or 'provider:*'")
    return parsed


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "value": None}


def _load_overrides() -> PriceFile | InvalidPriceFile:
    """The override file, re-read when its path, size or mtime changes.

    The repo default file missing = no overrides. A file named by EAP_MODEL_PRICES_FILE that does
    not exist is invalid (prices nothing): an operator who configured overrides must never get
    genai-prices prices instead because of a wrong path. Any other error reaching the file
    (PermissionError, a directory component that is a file, ...) is invalid too, default or not."""
    path = prices_file_path()
    explicit = bool(os.environ.get(PRICES_FILE_ENV))
    stat_error: str | None = None
    try:
        stat = path.stat()
        key: tuple[Any, ...] = (str(path), explicit, stat.st_mtime_ns, stat.st_size, None)
    except FileNotFoundError:
        stat_error = "FileNotFoundError"
    except OSError as exc:
        stat_error = type(exc).__name__
    if stat_error is not None:
        key = (str(path), explicit, None, None, stat_error)
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["value"]
        value: PriceFile | InvalidPriceFile
        if stat_error == "FileNotFoundError" and not explicit:
            value = PriceFile()
        elif stat_error == "FileNotFoundError":
            value = InvalidPriceFile(str(path), f"file not found ({PRICES_FILE_ENV} is set)")
            log.error("%s; model requests are recorded without a price", value)
        elif stat_error is not None:
            value = InvalidPriceFile(str(path), f"cannot access the file ({stat_error})")
            log.error("%s; model requests are recorded without a price", value)
        else:
            try:
                value = parse_price_file(path.read_text(encoding="utf-8"), str(path))
            except InvalidPriceFile as exc:
                value = exc
            except OSError as exc:
                value = InvalidPriceFile(str(path), type(exc).__name__)
            if isinstance(value, InvalidPriceFile):
                log.error("%s; model requests are recorded without a price", value)
        _cache["key"], _cache["value"] = key, value
        return value


def _override_period(prices: PriceFile, model_id: str, at: datetime) -> PricePeriod | None:
    provider = model_id.partition(":")[0]
    for key in (model_id, f"{provider}:*"):
        periods = [p for p in prices.models.get(key, []) if p.effective_from <= at.date()]
        if periods:
            return max(periods, key=lambda p: p.effective_from)
    return None


def _override_cost(period: PricePeriod, usage: Usage) -> Decimal:
    cache_read = usage.cache_read_tokens
    cache_write = usage.cache_write_tokens
    uncached = max(usage.input_tokens - cache_read - cache_write, 0)
    read_price = period.cache_read_per_mtok if period.cache_read_per_mtok is not None else period.input_per_mtok
    write_price = period.cache_write_per_mtok if period.cache_write_per_mtok is not None else period.input_per_mtok
    total = (
        uncached * period.input_per_mtok
        + cache_read * read_price
        + cache_write * write_price
        + usage.output_tokens * period.output_per_mtok
    )
    return total / _MTOK


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
    period = _override_period(overrides, model_id, at)
    if period is not None:
        return _override_cost(period, usage).quantize(COST_QUANTUM), SOURCE_OVERRIDE
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
