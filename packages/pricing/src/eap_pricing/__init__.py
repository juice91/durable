"""The operator model price override file and the cost arithmetic on it, shared by the worker
(`eap_worker.pricing`, which prices every model request when it is recorded) and the API (the
reindex dry run's embedding cost estimate), plus the genai-prices fallback both use (`genai_cost`,
`price_request`, `price_known`): the worker prices with exactly the lookup `price_known` reports on,
so the two can never disagree. Standard library, pydantic and genai-prices only.

The file is `EAP_MODEL_PRICES_FILE` (a relative path resolved against the repository root; default
`config/model-prices.json` there), reviewed like code. An exact `provider:name` key wins over a
`provider:*` key. Each key holds price periods; the one with the latest `effective_from` not after
the request time applies. Prices are USD per million tokens (input, output, cache read, cache write;
a missing cache price falls back to the input price):

    {"models": {"litellm:*": [{"effective_from": "2026-01-01", "input_per_mtok": "0",
                               "output_per_mtok": "0"}]}}

An unreadable or invalid file, or an EAP_MODEL_PRICES_FILE that does not exist, prices nothing
(`load_overrides` returns an InvalidPriceFile, logged once per change of the file): a broken or
misplaced file must never silently fall back to other prices for models it was meant to price.

Costs are Decimals quantized to COST_QUANTUM (Numeric(20, 10)); `format_cost` gives the decimal
string the API and events carry (e.g. "0.0000123000").
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("eap_pricing")

PRICES_FILE_ENV = "EAP_MODEL_PRICES_FILE"
# packages/pricing/src/eap_pricing/__init__.py -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_PRICES_FILE = REPO_ROOT / "config" / "model-prices.json"
# Providers that are never priced from genai-prices (override file only).
OVERRIDE_ONLY_PROVIDERS = frozenset({"test", "litellm"})
COST_QUANTUM = Decimal("0.0000000001")  # Numeric(20, 10)
MTOK = Decimal(1_000_000)

SOURCE_OVERRIDE = "override"


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


def prices_file_path(default: Path | None = None) -> Path:
    """EAP_MODEL_PRICES_FILE (a relative path is resolved against the repository root, not the
    process's cwd), else `default` (the repo's config/model-prices.json)."""
    configured = os.environ.get(PRICES_FILE_ENV)
    if not configured:
        return default if default is not None else DEFAULT_PRICES_FILE
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


def load_overrides(default: Path | None = None) -> PriceFile | InvalidPriceFile:
    """The override file, re-read when its path, size or mtime changes.

    The default file missing = no overrides. A file named by EAP_MODEL_PRICES_FILE that does not
    exist is invalid (prices nothing): an operator who configured overrides must never get other
    prices instead because of a wrong path. Any other error reaching the file (PermissionError, a
    directory component that is a file, ...) is invalid too, default or not."""
    path = prices_file_path(default)
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
            log.error("%s; the price file prices nothing (every model request is unpriced)", value)
        elif stat_error is not None:
            value = InvalidPriceFile(str(path), f"cannot access the file ({stat_error})")
            log.error("%s; the price file prices nothing (every model request is unpriced)", value)
        else:
            try:
                value = parse_price_file(path.read_text(encoding="utf-8"), str(path))
            except InvalidPriceFile as exc:
                value = exc
            except OSError as exc:
                value = InvalidPriceFile(str(path), type(exc).__name__)
            if isinstance(value, InvalidPriceFile):
                log.error("%s; the price file prices nothing (every model request is unpriced)", value)
        _cache["key"], _cache["value"] = key, value
        return value


def override_period(prices: PriceFile, model_id: str, day: date) -> PricePeriod | None:
    """The period of `model_id` in force on `day`: an exact `provider:name` key wins over
    `provider:*`; the latest `effective_from` not after `day`."""
    provider = model_id.partition(":")[0]
    for key in (model_id, f"{provider}:*"):
        periods = [p for p in prices.models.get(key, []) if p.effective_from <= day]
        if periods:
            return max(periods, key=lambda p: p.effective_from)
    return None


def override_cost(period: PricePeriod, usage: Usage) -> Decimal:
    """The unquantized USD cost of `usage` at `period`'s prices."""
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
    return total / MTOK


# ----------------------------------------------------------------------------- genai-prices

# Our provider id -> genai-prices provider id, where they differ.
GENAI_PROVIDER_IDS = {"google-gla": "google", "google-vertex": "google"}


def genai_source() -> str:
    """The price source label of a genai-prices price, e.g. "genai-prices/0.2.3"."""
    try:
        from importlib.metadata import version

        return f"genai-prices/{version('genai-prices')}"
    except Exception:  # noqa: BLE001 - only a label
        return "genai-prices"


def genai_cost(model_id: str, usage: Usage, at: datetime) -> Decimal | None:
    """The unquantized genai-prices (bundled snapshot, no network) cost of `usage` for `model_id`
    at `at`, or None when it has no price. `test:*` and `litellm:*` are never priced from it: a
    model behind the LiteLLM proxy would otherwise be priced as the OpenAI model of that name."""
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
            provider_id=GENAI_PROVIDER_IDS.get(provider, provider),
            genai_request_timestamp=at,
        )
    except LookupError:
        return None
    except Exception as exc:  # noqa: BLE001 - a price lookup must never fail the run
        log.warning("genai-prices lookup failed for %s: %s", model_id, type(exc).__name__)
        return None
    return Decimal(result.total_price)


# ----------------------------------------------------------------------------- one lookup


def _resolve(
    model_id: str, usage: Usage, at: datetime, overrides: PriceFile | InvalidPriceFile | None
) -> tuple[Decimal | None, str | None]:
    """(unquantized cost, source) by the one lookup order: an invalid override file prices
    nothing; else the override period in force on `at`'s date (exact key over `provider:*`); else
    genai-prices; else (None, None)."""
    if overrides is None:
        overrides = load_overrides()
    if isinstance(overrides, InvalidPriceFile):
        return None, None
    period = override_period(overrides, model_id, at.date())
    if period is not None:
        return override_cost(period, usage), SOURCE_OVERRIDE
    cost = genai_cost(model_id, usage, at)
    if cost is None:
        return None, None
    return cost, genai_source()


def price_request(
    model_id: str, usage: Usage, at: datetime, overrides: PriceFile | InvalidPriceFile | None = None
) -> tuple[Decimal | None, str | None]:
    """(cost in USD quantized to COST_QUANTUM, price source) of one model request, or (None, None)
    when no price is known or the request is unmeasured. `overrides` defaults to `load_overrides()`
    (the repo default file); pass a loaded file to use another default path."""
    if not usage.measured:
        return None, None
    cost, source = _resolve(model_id, usage, at, overrides)
    return (None, None) if cost is None else (cost.quantize(COST_QUANTUM), source)


PriceStatus = Literal["priced", "zero", "unpriced"]
PRICE_PRICED: PriceStatus = "priced"
PRICE_ZERO: PriceStatus = "zero"
PRICE_UNPRICED: PriceStatus = "unpriced"
# A probe with every token kind: "zero" means none of them costs anything.
_PROBE_OVERRIDE = Usage(input_tokens=3 * 1_000_000, output_tokens=1_000_000,
                        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
_PROBE_GENAI = Usage(input_tokens=1_000_000, output_tokens=1_000_000)


def price_known(
    model_id: str, day: date | datetime, overrides: PriceFile | InvalidPriceFile | None = None
) -> PriceStatus:
    """Whether the worker would price a request of `model_id` on `day`: "priced" (a non-zero
    price), "zero" (a price of 0, e.g. `litellm:*` in the repo file) or "unpriced" (no price: the
    cost is recorded as None). Same lookup as `price_request`; an unreadable or invalid override
    file is "unpriced" for every model, as the worker prices nothing then.

    `day` is a date (genai-prices is asked at 00:00 UTC of it) or an aware datetime. `overrides`
    defaults to `load_overrides()`."""
    if isinstance(day, datetime):
        at = day if day.tzinfo is not None else day.replace(tzinfo=timezone.utc)
    else:
        at = datetime.combine(day, time.min, tzinfo=timezone.utc)
    if overrides is None:
        overrides = load_overrides()
    probe = _PROBE_OVERRIDE
    if not isinstance(overrides, InvalidPriceFile) and override_period(overrides, model_id, at.date()) is None:
        probe = _PROBE_GENAI  # genai-prices: input and output only (not every model has cache prices)
    cost, _source = _resolve(model_id, probe, at, overrides)
    if cost is None:
        return PRICE_UNPRICED
    return PRICE_ZERO if cost == 0 else PRICE_PRICED


def format_cost(cost: Decimal | None) -> str | None:
    """A cost as the decimal string events and API responses carry (e.g. "0.0000123000")."""
    return None if cost is None else format(cost, "f")


__all__ = [
    "COST_QUANTUM",
    "DEFAULT_PRICES_FILE",
    "GENAI_PROVIDER_IDS",
    "InvalidPriceFile",
    "MTOK",
    "OVERRIDE_ONLY_PROVIDERS",
    "PRICES_FILE_ENV",
    "PRICE_PRICED",
    "PRICE_UNPRICED",
    "PRICE_ZERO",
    "PriceFile",
    "PricePeriod",
    "PriceStatus",
    "REPO_ROOT",
    "SOURCE_OVERRIDE",
    "Usage",
    "format_cost",
    "genai_cost",
    "genai_source",
    "load_overrides",
    "override_cost",
    "override_period",
    "parse_price_file",
    "price_known",
    "price_request",
    "prices_file_path",
]
