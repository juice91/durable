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

2. `genai-prices` (Pydantic's price data, bundled snapshot, no network; looked up in `eap_pricing`,
   so `eap_pricing.price_known` and this module can never disagree) for direct providers only.
   `test:*` and `litellm:*` are never priced from it: a model behind the LiteLLM proxy (an
   OpenAI-compatible endpoint) would otherwise be priced as if it were the OpenAI model of that name.

3. No price: cost None, source None (counted as unpriced).

An unreadable or invalid override file, or an EAP_MODEL_PRICES_FILE that does not exist, prices
nothing (cost None for every model, logged): a broken or misplaced file must never silently fall
back to genai-prices prices for models it was meant to price.

The cost is stored on the request's row and in its event and never recomputed, so historical cost
does not change when prices change.

Fallback models (AC.5; `fallback_usage`): a request answered through a FallbackModel is priced as
the model that ANSWERED, `chain[len(response.failed_attempts)]`, cross-checked against the
provider-reported model name where it names one of the chain's models; on a mismatch the row keeps
the chain's id and is marked `price_source = "fallback_unverified"`. Failed attempts that reported
usage get their own rows (`model:{n}:failed:{k}`), priced as `chain[k]`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from collections.abc import Sequence
from typing import Any

from eap_schemas.knowledge import approx_tokens

# The override file and its cost arithmetic are shared with the API's reindex estimate (eap_pricing);
# re-exported here so existing imports keep working.
from eap_pricing import (
    COST_QUANTUM,
    GENAI_PROVIDER_IDS,
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
    price_request,
)
from eap_pricing import DEFAULT_PRICES_FILE as _SHARED_DEFAULT_PRICES_FILE
from eap_pricing import prices_file_path as _shared_prices_file_path

log = logging.getLogger("eap_worker")

DEFAULT_PRICES_FILE = _SHARED_DEFAULT_PRICES_FILE


def prices_file_path() -> Path:
    """EAP_MODEL_PRICES_FILE (a relative path is resolved against the repository root, not the
    process's cwd), else the repo default config/model-prices.json."""
    return _shared_prices_file_path(DEFAULT_PRICES_FILE)


def _load_overrides() -> PriceFile | InvalidPriceFile:
    """The override file (eap_pricing.load_overrides), defaulting to this module's DEFAULT_PRICES_FILE."""
    return load_overrides(DEFAULT_PRICES_FILE)


def price(model_id: str, usage: Usage, at: datetime | None = None) -> tuple[Decimal | None, str | None]:
    """(cost in USD quantized to 10 decimals, price source) for one model request of `model_id`,
    or (None, None) when no price is known. An unmeasured request (the provider reported no usage)
    has no cost: (None, None). The lookup is `eap_pricing.price_request`, the same one
    `eap_pricing.price_known` reports on."""
    return price_request(model_id, usage, at or datetime.now(timezone.utc), _load_overrides())


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


USAGE_OUTPUT_BELOW_REASONING = "usage_output_below_reasoning"

# Providers (our model id prefix) whose output count INCLUDES reasoning: an OpenAI-compatible
# Chat Completions / Responses endpoint, mapped by pydantic_ai.models.openai (`completion_tokens`
# -> output_tokens, `completion_tokens_details.reasoning_tokens` -> details). There
# output_tokens < reasoning_tokens is impossible, so it is a gateway bug the guard corrects.
# Every other provider is left exactly as reported: xAI (pydantic_ai.models.xai) reports
# `completion_tokens` EXCLUDING reasoning (total = prompt + completion + reasoning), so a short
# answer after long reasoning is a correct count there, and others may follow either convention.
REASONING_INCLUSIVE_PROVIDERS = frozenset({"litellm", "openai", "azure"})


def reasoning_tokens(usage: Any) -> int:
    """The reasoning tokens a `RequestUsage` reports: `output_reasoning_tokens`, or the OpenAI
    `completion_tokens_details.reasoning_tokens` Pydantic AI keeps in `details`."""
    details = getattr(usage, "details", None) or {}
    from_details = details.get("reasoning_tokens", 0) if isinstance(details, dict) else 0
    return max(int(getattr(usage, "output_reasoning_tokens", 0) or 0), int(from_details or 0))


def response_output_approx_tokens(response: Any) -> int:
    """approx_tokens of a ModelResponse's text and tool-call arguments (thinking excluded: the
    provider's reasoning count covers it)."""
    total = 0
    for part in getattr(response, "parts", ()) or ():
        kind = getattr(part, "part_kind", None)
        if kind == "text":
            total += approx_tokens(part.content or "")
        elif kind in ("tool-call", "builtin-tool-call"):
            try:
                total += approx_tokens(part.args_as_json_str())
            except Exception:  # noqa: BLE001 - an estimate only
                total += approx_tokens(str(part.args or ""))
    return total


def request_usage_input(
    model_id: str, provider_model_name: str | None, usage: Any, *, streamed_response: Any = None
) -> dict[str, Any]:
    """What the workflow passes to `eap.append_event` for a model request: plain JSON, built from
    the checkpointed response only (no pricing, no I/O).

    `streamed_response` is the ModelResponse `usage` came from, given only for a streamed request.
    Some OpenAI-compatible gateways (the Studio LiteLLM proxy) then report `completion_tokens`
    below `completion_tokens_details.reasoning_tokens`, which is impossible (completion includes
    reasoning). Such a usage is corrected to an estimate, reasoning tokens + approx_tokens of the
    response's text and tool-call arguments, logged (`usage_output_below_reasoning`, counts only)
    and marked `usage_estimated: True`; the corrected count is what is recorded, priced and summed
    into the run totals. A non-streamed request (no `streamed_response`) is never changed."""
    u = usage_from_response(usage)
    out: dict[str, Any] = {
        "model_id": model_id,
        "provider_model_name": provider_model_name,
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
        "usage_measured": u.measured,
    }
    # Streamed only, by design: the gateway misreport was seen on streamed responses (the usage
    # chunk at the end of the stream), and the estimate needs the whole response the usage belongs
    # to, which only the streamed call site (workflow.py, model.request_stream) passes. Non-streamed
    # callers (processors, the observer, moderation) record the provider's count unchanged.
    provider = model_id.partition(":")[0]
    if streamed_response is not None and u.measured and provider in REASONING_INCLUSIVE_PROVIDERS:
        reasoning = reasoning_tokens(usage)
        if u.output_tokens < reasoning:
            estimate = reasoning + response_output_approx_tokens(streamed_response)
            log.warning(
                "%s: model %s reported output_tokens=%d below reasoning_tokens=%d; recording %d (estimated)",
                USAGE_OUTPUT_BELOW_REASONING, model_id, u.output_tokens, reasoning, estimate,
                extra={"code": USAGE_OUTPUT_BELOW_REASONING, "model_id": model_id,
                       "reported_output_tokens": u.output_tokens, "reasoning_tokens": reasoning,
                       "estimated_output_tokens": estimate},
            )
            out["output_tokens"] = estimate
            out["usage_estimated"] = True
    return out


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
    if usage_input.get("price_source_override"):
        source = str(usage_input["price_source_override"])  # AC.5: fallback_unverified
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
    if usage_input.get("usage_estimated"):
        # Additive: output_tokens is our estimate (request_usage_input), not the provider's count.
        event_fields["usage"]["estimated"] = True
    return row, event_fields


# ----------------------------------------------------------------------------- fallback (AC.5)

PRICE_SOURCE_FALLBACK_UNVERIFIED = "fallback_unverified"
_MAX_ERROR_TYPE = 200


def _names_model(chain_id: str, reported: str | None) -> bool:
    return bool(reported) and reported in (chain_id, chain_id.partition(":")[2])


def answering_index(chain: Sequence[str], reported_model_name: str | None, failed: int) -> tuple[int, bool]:
    """(index of the answering model in `chain`, verified). The answering model is the one after the
    `failed` attempts; it is unverified when the provider-reported name names ANOTHER chain member
    (or the count runs past the chain, then the last member is used). A reported name that names no
    member (e.g. a scripted model's own name) does not contradict the count."""
    index = min(failed, len(chain) - 1)
    if failed >= len(chain):
        return index, False
    named = [k for k, member in enumerate(chain) if _names_model(member, reported_model_name)]
    return index, not named or index in named


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# ModelHTTPError's own text starts "status_code: <n>, model_name: ..., body: ...".
_STATUS_RE = re.compile(r"\bstatus_code: (\d{3})\b")


def attempt_model_id(chain: Sequence[str], k: int, reported_model_name: str | None) -> str:
    """The chain member failed attempt k was sent to: the member its reported model name names when
    exactly one does (robust to an extra attempt shifting positions, e.g. a pinned continuation that
    FallbackModel retried), else the member at position k (bounded to the chain)."""
    named = [member for member in chain if _names_model(member, reported_model_name)]
    if len(named) == 1:
        return named[0]
    return chain[min(k, len(chain) - 1)]


def _error_type(error: str | None) -> str | None:
    """`ModelRequestAttempt.error` is `"ExceptionType: message"`: only the type is kept (a plain
    identifier, bounded; anything else is "Exception")."""
    if not error:
        return None
    name = error.partition(":")[0].strip()[:_MAX_ERROR_TYPE]
    return name if _IDENTIFIER_RE.match(name) else "Exception"


def _http_status(error: str | None) -> int | None:
    """The HTTP status of a ModelHTTPError attempt, from its fixed `status_code: <n>` prefix."""
    if not error:
        return None
    match = _STATUS_RE.search(error.partition(":")[2])
    return int(match.group(1)) if match else None


def safe_attempt_error(error: str | None) -> str | None:
    """A failed attempt's `error` without the exception text (which can quote the provider's
    body, the request or the output): `"<ExceptionType>"`, or `"<ExceptionType>: HTTP <status>"`."""
    name = _error_type(error)
    if name is None:
        return None
    status = _http_status(error)
    return f"{name}: HTTP {status}" if status is not None else name


def scrub_failed_attempts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages as JSON with every response's `failed_attempts[].error` reduced to
    `safe_attempt_error` (AC.5: stored messages, events and history never carry provider error
    text). Pure; other fields (model, outcome, timing, usage) are kept."""
    return [scrub_failed_attempts_message(m) for m in messages]


def scrub_failed_attempts_message(message: dict[str, Any]) -> dict[str, Any]:
    attempts = message.get("failed_attempts") if isinstance(message, dict) else None
    if not attempts:
        return message
    cleaned = [
        {**a, "error": safe_attempt_error(a.get("error"))} if isinstance(a, dict) else a for a in attempts
    ]
    return {**message, "failed_attempts": cleaned}


def fallback_usage(
    chain: Sequence[str], response: Any, *, streamed_response: Any = None
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """For a response of a request that tried `chain` (len > 1): (the answering model's
    `request_usage_input`, the extra `model_message` data `{requested_model_id, fallback_index,
    failed_attempts: [{model_id, outcome, error_type}]}` (no error text), the failed attempts'
    usage inputs that reported usage, each with its index `k`). Pure: from the checkpointed response.

    `streamed_response` (the streamed call site passes the response itself) applies
    request_usage_input's streamed usage guard to the ANSWERING model's usage, checked against the
    answering model's provider. Failed attempts' usage is recorded as reported (no streamed
    response of theirs exists)."""
    attempts = list(getattr(response, "failed_attempts", None) or [])
    index, verified = answering_index(chain, getattr(response, "model_name", None), len(attempts))
    usage = request_usage_input(
        chain[index], getattr(response, "model_name", None), response.usage, streamed_response=streamed_response
    )
    if not verified:
        usage["price_source_override"] = PRICE_SOURCE_FALLBACK_UNVERIFIED
    failed_data: list[dict[str, Any]] = []
    failed_usage: list[dict[str, Any]] = []
    for k, attempt in enumerate(attempts):
        model_id = attempt_model_id(chain, k, getattr(attempt, "model_name", None))
        entry: dict[str, Any] = {
            "model_id": model_id, "outcome": getattr(attempt, "outcome", "error"), "error_type": _error_type(attempt.error)
        }
        status = _http_status(attempt.error)
        if status is not None:
            entry["http_status"] = status
        failed_data.append(entry)
        if attempt.usage is not None:
            failed_usage.append({"k": k, **request_usage_input(model_id, attempt.model_name, attempt.usage)})
    # answering_model: the answering model's provider:name id (= chain[fallback_index]).
    extra = {
        "requested_model_id": chain[0],
        "answering_model": chain[index],
        "fallback_index": index,
        "failed_attempts": failed_data,
    }
    return usage, extra, failed_usage


def failed_attempt_rows(request_key: str, failed_usage: Sequence[dict[str, Any]], at: datetime | None = None) -> list[dict[str, Any]]:
    """The `run_model_requests` rows of failed attempts (`{request_key}:failed:{k}`), priced as their
    own model; operation `chat`."""
    rows = []
    for item in failed_usage:
        u = {key: value for key, value in item.items() if key != "k"}
        row, _fields = usage_record(f"{request_key}:failed:{item['k']}", u, at)
        rows.append(row)
    return rows


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
    "GENAI_PROVIDER_IDS",
    "OPERATION_CHAT",
    "OPERATION_EMBEDDING",
    "PRICE_SOURCE_FALLBACK_UNVERIFIED",
    "answering_index",
    "attempt_model_id",
    "failed_attempt_rows",
    "fallback_usage",
    "safe_attempt_error",
    "scrub_failed_attempts",
    "scrub_failed_attempts_message",
    "embedding_usage_record",
    "InvalidPriceFile",
    "PRICES_FILE_ENV",
    "REASONING_INCLUSIVE_PROVIDERS",
    "USAGE_OUTPUT_BELOW_REASONING",
    "Usage",
    "parse_price_file",
    "price",
    "prices_file_path",
    "reasoning_tokens",
    "request_usage_input",
    "response_output_approx_tokens",
    "usage_from_response",
    "usage_record",
]
