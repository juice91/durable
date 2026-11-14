"""Per-run usage limits (agent capabilities milestone, AC.1).

`AgentSpec.limits: UsageLimitsSpec | None`. The first five caps map to Pydantic AI's
`UsageLimits(request_limit, tool_calls_limit, input_tokens_limit, output_tokens_limit,
total_tokens_limit)` (`usage_limits_kwargs()`); `max_cost_usd` is the platform's own cap, checked
before each model request from the run's recorded usage rows. Limits apply per run (a turn, a
workflow agent step, an eval case).

A `limits` object with every field None is the same as no limits: AgentSpec turns it into None, so
it never reaches the JSON or the hash. Every None field is dropped from dumps. `max_cost_usd` is a
Decimal, normalised (no trailing zeros, no exponent) so equal amounts hash the same; JSON dumps
carry it as a string such as `"0.5"`. No I/O.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from eap_schemas._omit import OmitNoneModel

MAX_REQUESTS = 200
MAX_TOOL_CALLS = 500
MAX_TOKENS = 10_000_000
MIN_COST_USD = Decimal("0.0001")
MAX_COST_USD = Decimal("1000")
COST_DECIMAL_PLACES = 4

# UsageLimitsSpec field -> pydantic_ai.usage.UsageLimits keyword.
USAGE_LIMIT_KWARGS = {
    "max_requests": "request_limit",
    "max_tool_calls": "tool_calls_limit",
    "max_input_tokens": "input_tokens_limit",
    "max_output_tokens": "output_tokens_limit",
    "max_total_tokens": "total_tokens_limit",
}


class UsageLimitsSpec(OmitNoneModel):
    """Caps on one run. A run over a cap fails `usage_limit_exceeded` (or `run_budget_exceeded`)."""

    max_requests: int | None = Field(default=None, ge=1, le=MAX_REQUESTS)
    max_tool_calls: int | None = Field(default=None, ge=1, le=MAX_TOOL_CALLS)
    max_input_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS)
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS)
    max_total_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS)
    max_cost_usd: Decimal | None = Field(default=None, description="USD, 0.0001 to 1000, at most 4 decimals")

    @field_validator("max_cost_usd")
    @classmethod
    def _cost(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v
        if not v.is_finite():
            raise ValueError("max_cost_usd must be a finite number")
        if not MIN_COST_USD <= v <= MAX_COST_USD:
            raise ValueError(f"max_cost_usd must be between {MIN_COST_USD} and {MAX_COST_USD}")
        if v != v.quantize(Decimal(1).scaleb(-COST_DECIMAL_PLACES)):
            raise ValueError(f"max_cost_usd may have at most {COST_DECIMAL_PLACES} decimal places")
        return normalise_usd(v)

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in type(self).model_fields)

    def usage_limits_kwargs(self) -> dict[str, int]:
        """Keyword arguments for `pydantic_ai.usage.UsageLimits` (only the caps that are set; never
        the cost cap, which the platform enforces itself)."""
        return {
            kwarg: getattr(self, field)
            for field, kwarg in USAGE_LIMIT_KWARGS.items()
            if getattr(self, field) is not None
        }


def normalise_usd(value: Decimal) -> Decimal:
    """The canonical form of a USD amount: `Decimal("0.50")` -> `0.5`, `Decimal("1E+3")` -> `1000`."""
    value = value.normalize()
    if value.as_tuple().exponent > 0:  # type: ignore[operator]
        value = value.quantize(Decimal(1))
    return value


class RunBudgetExceeded(RuntimeError):
    """The run's recorded cost reached `limits.max_cost_usd` before a model request
    (`run_budget_exceeded`). Pickle-safe (raised inside a DBOS step)."""

    code = "run_budget_exceeded"

    def __init__(self, spent_usd: Decimal | str, cap_usd: Decimal | str) -> None:
        self.spent_usd = Decimal(str(spent_usd))
        self.cap_usd = Decimal(str(cap_usd))
        super().__init__(str(self.spent_usd), str(self.cap_usd))

    def __str__(self) -> str:
        return f"the run reached its cost limit of ${self.cap_usd} (spent ${self.spent_usd})"

    def data(self) -> dict[str, Any]:
        """`run_failed` event data (no text): the limit name and the amounts as strings."""
        return {"limit": "max_cost_usd", "value": str(self.cap_usd), "spent_usd": str(self.spent_usd)}


__all__ = [
    "COST_DECIMAL_PLACES",
    "MAX_COST_USD",
    "MAX_REQUESTS",
    "MAX_TOKENS",
    "MAX_TOOL_CALLS",
    "MIN_COST_USD",
    "RunBudgetExceeded",
    "USAGE_LIMIT_KWARGS",
    "normalise_usd",
    "UsageLimitsSpec",
]
