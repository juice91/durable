"""Run failures as stable codes and user-safe messages, never exception text.

A failed run stores `runs.error` and a `run_failed` event that the run's tenant sees in Studio and
over the API. Exception text is not safe to store or show: a provider error can carry the rejected
request, model output or a raw response body, and a tool error can quote anything it received. So
the stored error is `"<ExceptionType>: <fixed message for that class>"`, built only from the class
and structured fields that come from the published spec or the platform (a model id, a tool id, an
HTTP status code). Logs and spans get only the code and the exception type (eap_observability).

The exception chain is searched (`__cause__`, `__context__`, exception groups, and the attempts
inside DBOS's DBOSMaxStepRetriesExceeded) so a wrapped failure is classified by its real cause.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded

from eap_observability import exception_type
from eap_runtime import SideEffectOutcomeUnknown, ToolNotAllowedError, UnknownToolError
from eap_schemas.models_policy import ModelNotAllowed

_MAX_FIELD = 200


@dataclass(frozen=True)
class RunFailure:
    status: str  # failed | needs_attention
    code: str  # stable, for logs, spans and alerting
    error_type: str  # the classified exception's class name
    message: str  # stored as runs.error and in the run_failed event

    @property
    def error(self) -> str:
        return f"{self.error_type}: {self.message}"


def _field(exc: BaseException, name: str) -> str:
    """A spec/platform-supplied identifier stored on the exception, bounded (never its text)."""
    return str(getattr(exc, name, "?"))[:_MAX_FIELD]


def _http_message(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    return f"the model provider returned HTTP {status}" if isinstance(status, int) else "the model provider request failed"


# (class, status, code, message) in priority order: the first class found anywhere in the chain wins.
_KNOWN: tuple[tuple[type[BaseException], str, str, Callable[[BaseException], str]], ...] = (
    (
        SideEffectOutcomeUnknown,
        "needs_attention",
        "side_effect_outcome_unknown",
        lambda e: "a tool's external side effect may or may not have happened; check the external system",
    ),
    (
        ModelNotAllowed,
        "failed",
        "model_not_allowed",
        lambda e: f"model '{_field(e, 'provider')}:{_field(e, 'name')}' is not allowed on this platform",
    ),
    (UnknownToolError, "failed", "unknown_tool", lambda e: f"tool '{_field(e, 'tool_id')}' is not registered"),
    (ToolNotAllowedError, "failed", "tool_not_allowed", lambda e: f"tool '{_field(e, 'tool_id')}' is not allowed on this worker"),
    (ModelHTTPError, "failed", "model_http_error", _http_message),
    (ModelAPIError, "failed", "model_api_error", lambda e: "the model provider request failed"),
    (UsageLimitExceeded, "failed", "usage_limit_exceeded", lambda e: "the run exceeded a usage limit"),
    (UnexpectedModelBehavior, "failed", "unexpected_model_behavior", lambda e: "the model responded in an unexpected way"),
)


def _chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        attempts = getattr(cur, "errors", None)  # DBOSMaxStepRetriesExceeded: one per attempt
        if isinstance(attempts, list):
            stack.extend(reversed([a for a in attempts if isinstance(a, BaseException)]))  # latest first
        for linked in (cur.__cause__, cur.__context__):
            if linked is not None:
                stack.append(linked)


def classify(exc: BaseException) -> RunFailure:
    chain = list(_chain(exc))
    for cls, status, code, message in _KNOWN:
        for cur in chain:
            # By name too: a step's recorded exception may come back as a re-created class.
            if isinstance(cur, cls) or type(cur).__name__ == cls.__name__:
                return RunFailure(status, code, exception_type(cur), message(cur))
    return RunFailure("failed", "internal_error", exception_type(exc), "the run failed with an internal error")


__all__ = ["RunFailure", "classify"]
