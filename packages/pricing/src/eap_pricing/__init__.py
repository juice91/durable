"""The operator model price override file and the cost arithmetic on it, shared by the worker
(`eap_worker.pricing`, which prices every model request when it is recorded) and the API (the
reindex dry run's embedding cost estimate). Standard library and pydantic only: genai-prices lookups
stay in the worker, which the API may not consult.

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
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

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


def format_cost(cost: Decimal | None) -> str | None:
    """A cost as the decimal string events and API responses carry (e.g. "0.0000123000")."""
    return None if cost is None else format(cost, "f")


__all__ = [
    "COST_QUANTUM",
    "DEFAULT_PRICES_FILE",
    "InvalidPriceFile",
    "MTOK",
    "OVERRIDE_ONLY_PROVIDERS",
    "PRICES_FILE_ENV",
    "PriceFile",
    "PricePeriod",
    "REPO_ROOT",
    "SOURCE_OVERRIDE",
    "Usage",
    "format_cost",
    "load_overrides",
    "override_cost",
    "override_period",
    "parse_price_file",
    "prices_file_path",
]
