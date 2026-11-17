"""Reusable scorers (W2.3): contracts shared by the API and the worker.

A scorer is one named, deterministic `eap_schemas.evals` evaluator the worker applies to the output
of a finished agent run. The kinds that make sense on a live run (no expected output, no case):

- `contains {value, case_sensitive}`: `value` is required (there is no case to default from);
- `regex {pattern}`, `schema_valid {json_schema?}` (default: the agent's output schema),
  `field_equals {path, value}`, `max_cost {usd}`.

`equals_expected` (needs an expected output), `max_duration` and `llm_judge` are refused. The result
of a scorer on a run is the `score` run event: `SCORE_EVENT_KEY_PREFIX + scorer_id`, data
`{scorer, scorer_id, score, passed, reason}`.

No I/O.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter, ValidationError

from eap_schemas.evals import EvaluatorSpec

SCORE_EVENT_TYPE = "score"
SCORE_EVENT_KEY_PREFIX = "score:"
SCORER_KINDS = ("contains", "regex", "schema_valid", "field_equals", "max_cost")
MAX_SCORERS_PER_AGENT = 10
MAX_SCORER_REASON_CHARS = 2000

_ADAPTER: TypeAdapter[Any] = TypeAdapter(EvaluatorSpec)


class ScorerInvalid(ValueError):
    """A scorer's evaluator breaks the rules (`scorer_invalid`). Carries a fixed reason, no values."""

    code = "scorer_invalid"

    def __init__(self, reason: str, path: str = "evaluator") -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason, path)


def score_event_key(scorer_id: str) -> str:
    return f"{SCORE_EVENT_KEY_PREFIX}{scorer_id}"


def validate_scorer_evaluator(data: Any) -> dict[str, Any]:
    """The evaluator as canonical JSON, or ScorerInvalid."""
    if not isinstance(data, dict):
        raise ScorerInvalid("the evaluator must be an object")
    kind = data.get("kind")
    if kind not in SCORER_KINDS:
        raise ScorerInvalid(f"kind must be one of: {', '.join(SCORER_KINDS)}", "evaluator.kind")
    try:
        parsed = _ADAPTER.validate_python(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_context=False, include_input=False)[0]
        loc = ".".join(str(p) for p in first["loc"][1:]) or "evaluator"
        raise ScorerInvalid(str(first["msg"]), f"evaluator.{loc}" if loc != "evaluator" else loc) from None
    if kind == "contains" and parsed.value is None:
        raise ScorerInvalid("value is required", "evaluator.value")
    return parsed.model_dump(mode="json")
