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

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded, UserError

from eap_observability import exception_type
from eap_runtime import SideEffectOutcomeUnknown, ToolNotAllowedError, UnknownToolError
from eap_schemas.models_policy import ModelNotAllowed
from eap_schemas.output import OutputInvalid
from eap_worker.mcp import McpToolUnavailable

_MAX_FIELD = 200


class ToolInputInvalid(Exception):
    """A tool test run's input does not match the tool's input model (re-checked in the worker)."""

    def __init__(self, tool_id: str) -> None:
        self.tool_id = tool_id
        # args are the constructor arguments, so pickle (DBOS step errors) rebuilds it exactly.
        super().__init__(tool_id)

    def __str__(self) -> str:
        return f"input does not match tool {self.tool_id!r}"


REQUEST_CONTEXT_INVALID_MESSAGE = "the request context does not match the agent's context fields"


class RequestContextInvalid(Exception):
    """The run's request context does not satisfy the spec's `context_fields` (e.g. a required field
    is missing, as in a workflow agent step, which runs with an empty context). Carries no values."""

    def __init__(self) -> None:
        super().__init__()  # pickle-safe: no constructor arguments

    def __str__(self) -> str:
        return REQUEST_CONTEXT_INVALID_MESSAGE


class InputBlocked(Exception):
    """An input processor (P0.17) blocked the turn's input: the main model is never called. Carries
    no text, only the processor's position and type (from the published spec)."""

    def __init__(self, index: int, processor_type: str) -> None:
        self.index = index
        self.processor_type = processor_type
        super().__init__(index, processor_type)  # pickle-safe

    def __str__(self) -> str:
        return f"input blocked by input_processors[{self.index}] ({self.processor_type})"


class OutputBlocked(Exception):
    """An output processor (P0.17) blocked a model response: it is never stored or shown.

    `reason` (AC.4) is a fixed code when the block is not a processor's own verdict:
    SCHEMA_INVALID_AFTER_PROCESSING when the processed structured output no longer matches the
    output schema (e.g. a redaction broke a `const` or `minLength`)."""

    def __init__(self, index: int, processor_type: str, reason: str | None = None) -> None:
        self.index = index
        self.processor_type = processor_type
        self.reason = reason
        super().__init__(index, processor_type, reason)  # pickle-safe

    def __str__(self) -> str:
        suffix = f": {self.reason}" if self.reason else ""
        return f"output blocked by output_processors[{self.index}] ({self.processor_type}){suffix}"


SCHEMA_INVALID_AFTER_PROCESSING = "schema_invalid_after_processing"


class OutputModeUnsupported(Exception):
    """The run's model cannot produce the spec's structured output mode (AC.4: `native` on a model
    whose profile has no JSON-schema output). Raised at the first model request; no output."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        super().__init__(mode)  # pickle-safe

    def __str__(self) -> str:
        return f"the model does not support {self.mode} structured output"


# Pydantic AI's fixed UserError text when NativeOutput meets a model without JSON-schema output.
_NATIVE_UNSUPPORTED = "Native structured output is not supported by this model"


# The fixed, user-safe messages of a blocked turn (stored as runs.error and in run_failed).
INPUT_BLOCKED_MESSAGE = "the message was blocked by this agent's input processors"
OUTPUT_BLOCKED_MESSAGE = "the answer was blocked by this agent's output processors"
SCHEMA_INVALID_AFTER_PROCESSING_MESSAGE = (
    "the answer no longer matched the output schema after this agent's output processors"
)
OUTPUT_INVALID_MESSAGE = "the answer did not match the agent's output schema within the allowed retries"
OUTPUT_MODE_UNSUPPORTED_MESSAGE = (
    "the model does not support this structured output mode; use tool mode or another model"
)


# Workflow runs (eap_worker.workflow_run): a step or run failure with a stable code and a fixed
# message. The step id comes from the published spec (validated pattern), never from user input.
WORKFLOW_FAILURES: dict[str, tuple[str, str]] = {
    # code: (status, message)
    "approval_rejected": ("failed", "the approval was rejected"),
    "approval_timeout": ("failed", "the approval was not decided before its timeout"),
    "agent_not_found": ("failed", "the agent was not found or has no published version"),
    "input_invalid": ("failed", "the workflow input is not valid JSON (e.g. NaN or Infinity)"),
    "input_ref_missing": ("failed", "a $ref in the step input does not resolve"),
    "step_output_too_large": ("failed", "the step output exceeds the workflow output size limit"),
    "step_output_not_json": ("failed", "the step output is not valid JSON (e.g. NaN or Infinity)"),
    "workflow_context_too_large": ("failed", "the workflow's step outputs exceed the total size limit"),
    "output_invalid": ("failed", "the workflow output does not match the output schema"),
    "workflow_spec_invalid": ("failed", "the published workflow spec is invalid"),
    "workflow_version_not_found": ("failed", "the workflow version was not found"),
    "step_limit_exceeded": ("failed", "the workflow exceeded its step execution limit"),
    "workflow_deadline_exceeded": ("failed", "the workflow exceeded its maximum run time"),
}


class WorkflowStepError(Exception):
    """A workflow step (or the run) failed with a known code from WORKFLOW_FAILURES, or with a
    child run's already user-safe (status, code, error) when `error` is given."""

    def __init__(self, code: str, step_id: str | None = None, status: str | None = None, error: str | None = None):
        self.code = code
        self.step_id = step_id
        self.status = status
        self.error = error
        # args are the constructor arguments, so pickle rebuilds it exactly.
        super().__init__(code, step_id, status, error)

    def __str__(self) -> str:
        return f"workflow step {self.step_id!r} failed: {self.code}"


@dataclass(frozen=True)
class RunFailure:
    status: str  # failed | needs_attention
    code: str  # stable, for logs, spans and alerting
    error_type: str  # the classified exception's class name
    message: str  # stored as runs.error and in the run_failed event
    reason: str | None = None  # a fixed sub-code for run_failed data (AC.4: output_blocked's reason)

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
    (RequestContextInvalid, "failed", "request_context_invalid", lambda e: REQUEST_CONTEXT_INVALID_MESSAGE),
    (InputBlocked, "failed", "input_blocked", lambda e: INPUT_BLOCKED_MESSAGE),
    (
        OutputBlocked,
        "failed",
        "output_blocked",
        lambda e: SCHEMA_INVALID_AFTER_PROCESSING_MESSAGE
        if getattr(e, "reason", None) == SCHEMA_INVALID_AFTER_PROCESSING
        else OUTPUT_BLOCKED_MESSAGE,
    ),
    (OutputInvalid, "failed", "output_invalid", lambda e: OUTPUT_INVALID_MESSAGE),
    (OutputModeUnsupported, "failed", "output_mode_unsupported", lambda e: OUTPUT_MODE_UNSUPPORTED_MESSAGE),
    (UnknownToolError, "failed", "unknown_tool", lambda e: f"tool '{_field(e, 'tool_id')}' is not registered"),
    (ToolNotAllowedError, "failed", "tool_not_allowed", lambda e: f"tool '{_field(e, 'tool_id')}' is not allowed on this worker"),
    (ToolInputInvalid, "failed", "tool_input_invalid", lambda e: f"the input does not match tool '{_field(e, 'tool_id')}'"),
    (
        McpToolUnavailable,
        "failed",
        "mcp_tool_unavailable",
        lambda e: f"MCP tool '{_field(e, 'server')}/{_field(e, 'tool')}' is unavailable ({_field(e, 'code')})",
    ),
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
    for cur in chain:
        if isinstance(cur, WorkflowStepError) or type(cur).__name__ == "WorkflowStepError":
            return _workflow_failure(cur)
    for cls, status, code, message in _KNOWN:
        for cur in chain:
            # By name too: a step's recorded exception may come back as a re-created class.
            if isinstance(cur, cls) or type(cur).__name__ == cls.__name__:
                return RunFailure(status, code, exception_type(cur), message(cur), _reason(cur))
    return RunFailure("failed", "internal_error", exception_type(exc), "the run failed with an internal error")


def _reason(exc: BaseException) -> str | None:
    """A known fixed reason code carried by the exception (never free text)."""
    reason = getattr(exc, "reason", None)
    return reason if reason == SCHEMA_INVALID_AFTER_PROCESSING else None


def native_output_unsupported(exc: BaseException) -> bool:
    """Whether Pydantic AI refused NativeOutput for the run's model anywhere in the exception chain
    (also inside DBOS's step-retry wrapper: the refusal happens in the model request step)."""
    return any(
        (isinstance(cur, UserError) or type(cur).__name__ == "UserError") and _NATIVE_UNSUPPORTED in str(cur)
        for cur in _chain(exc)
    )


def _workflow_failure(exc: BaseException) -> RunFailure:
    code = str(getattr(exc, "code", "internal_error"))[:_MAX_FIELD]
    status, message = WORKFLOW_FAILURES.get(code, ("failed", "the workflow step failed"))
    status = getattr(exc, "status", None) or status
    error = getattr(exc, "error", None)
    if error:  # a child run's stored error: already "<Type>: <fixed message>"
        error_type, _, message = str(error).partition(": ")
        return RunFailure(status, code, error_type[:_MAX_FIELD], message[:500])
    return RunFailure(status, code, "WorkflowStepError", message)


__all__ = [
    "INPUT_BLOCKED_MESSAGE",
    "OUTPUT_BLOCKED_MESSAGE",
    "OUTPUT_INVALID_MESSAGE",
    "OUTPUT_MODE_UNSUPPORTED_MESSAGE",
    "SCHEMA_INVALID_AFTER_PROCESSING",
    "SCHEMA_INVALID_AFTER_PROCESSING_MESSAGE",
    "REQUEST_CONTEXT_INVALID_MESSAGE",
    "WORKFLOW_FAILURES",
    "InputBlocked",
    "OutputBlocked",
    "OutputModeUnsupported",
    "RequestContextInvalid",
    "RunFailure",
    "ToolInputInvalid",
    "WorkflowStepError",
    "classify",
    "native_output_unsupported",
]
