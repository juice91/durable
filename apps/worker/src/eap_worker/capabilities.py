"""Spec-driven behaviour of `eap-agent` as one construction-time capability (agent capabilities milestone).

Pydantic AI rejects per-run capabilities inside a DBOS workflow, and an agent-level output validator
cannot be combined with a per-run `output_type`. So every behaviour that depends on the run's spec
lives in ONE capability attached when `eap-agent` is constructed, which reads the spec from
`ctx.deps.spec` on every run:

    capabilities=[ResolveModelId(...), RetrieveNumbering(), SpecCapability(), DBOSDurability(...)]

Its hooks run workflow-side (never inside a DBOS step) and are pure, so they add no durable
operation and behave identically on replay. A spec without the matching field is untouched.

AC.4 (structured output): `after_output_validate` checks a structured candidate against
`spec.output.json_schema` (Draft 2020-12 `jsonschema` with an empty registry, the same rule as
`workflow_run.final_output`). `StructuredDict` only checks for a dict, so this is the real schema
check. A failure raises `ModelRetry` with at most MAX_OUTPUT_ERRORS errors as `<json path>:
<message>`, at most MAX_OUTPUT_ERROR_BYTES, and never a value from the candidate. The hook fires
only for structured output, never for plain text.

The per-run inputs are built here too: `output_type_for(spec)` and `retries_for(spec)`. They are
passed to `agent.iter` only when `spec.output` is set, so a spec without it runs exactly as before.

AC.6 (tool approval): `prepare_tools` marks the spec's approval-required tools (`approvals.tools`,
as their model-facing names) `kind="unapproved"`, so Pydantic AI never runs such a call unresolved:
it first runs the response's UNGATED tool calls as usual, then collects the gated ones and hands
them to `handle_deferred_tool_calls`; no gated call runs before every call of the batch is decided. That hook performs the durable wait inside
the run (the DBOS workflow waits; the run is never ended and resumed):

    eap.tool_approval_begin            per gated call, in response order: the batch's one deadline
                                       (computed by the first call's step; a replay reuses it), the `tool_approval_waiting` event
                                       (`tool:{id}:approval:waiting`, data {tool_call_id, tool_name,
                                       args, deadline, workflow_id, batch}) and its pending_approvals
                                       row in one transaction (EventLog.append_approval_waiting)
    eap.tool_approval_decisions        one loop for the whole batch: the decision events
    eap.workflow_clock                 the clock
    DBOS.recv                          the wake-up on `tool_approval:{run_id}` (only a nudge), at most
                                       min(remaining, EAP_APPROVAL_POLL_SECONDS)
    ...                                until every call is decided or the deadline passes, then
    eap.tool_approval_decisions        (timed_out) `tool_approval_timed_out` for every undecided call
                                       under its decision key (first writer wins; no decided_by)

and returns `DeferredToolResults(approvals={id: ToolApproved() | ToolDenied(message)})`. The
decision EVENTS are the source of truth: a lost wake-up only delays a decision until the next poll.
An approved call then runs through the ordinary call_tool step (reserve -> call -> complete, key
`{run_id}:{tool_call_id}`), so nothing is reserved before approval; a denied or timed-out one never
reaches the toolset and the model reads a `ToolReturnPart` with `outcome="denied"`.

Without the wait (one `eap.tool_approval_deny` step per call, no inbox row, a `tool_denied` event at
the decision key):
- `approval_mode == "deny"` (a child run of an eval run, from eap.start_run): denied at once with
  EVAL_DENIED_MESSAGE, `decided_by: "eval"`;
- `approval_mode == "deny"` with `approval_deny_reason == "loop_body"` (an agent step inside a
  workflow loop body, loops D8a): denied at once with LOOP_BODY_DENIED_MESSAGE,
  `decided_by: "system"`, reason `loop_body`;
- a tool call id the decide route could never address (not 1-256 printable ASCII without spaces,
  AC.8's rule): denied at once with INVALID_ID_MESSAGE, `decided_by: "system"`, reason
  `invalid_tool_call_id`, so it never sits undecidable in the inbox.

The run's approval settings (mode, hour and poll seconds, all recorded by eap.start_run) reach the
hook through a context variable `_drive_agent` sets around the agent run (set_approval_settings)
(Pydantic AI capabilities get only `ctx.deps`, and WorkerDeps carries no run options).
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from uuid import UUID

from dbos import DBOS
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.output import NativeOutput, PromptedOutput, StructuredDict, ToolOutput
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolApproved, ToolDefinition, ToolDenied
from pydantic_core import to_jsonable_python

from eap_run_store.approvals import decision_event_key, tool_approval_topic
from eap_schemas import AgentSpec, approval_required_tool_names
from eap_schemas.approvals import EVAL_DENIED_MESSAGE, TIMED_OUT_MESSAGE, denial_message
from eap_schemas.output import MAX_OUTPUT_ERROR_BYTES, MAX_OUTPUT_ERRORS, OUTPUT_TOOL_NAME
from eap_worker.crash import crash_point
from eap_worker.runtime import get_runtime

# The first line of the retry message the model gets for a schema-invalid candidate.
RETRY_HEADING = "The output does not match the required JSON schema. Fix these errors and answer again:"


# --------------------------------------------------------------------------- per-run inputs


def output_type_for(spec: AgentSpec) -> Any | None:
    """The per-run `output_type` for a structured spec (None without `spec.output`): a
    `StructuredDict` of the spec's schema, as the output tool `final_result` (tool mode, the
    default), `NativeOutput` or `PromptedOutput`."""
    out = spec.output
    if out is None:
        return None
    # StructuredDict keeps (and annotates) the schema it is given: never the spec's own dict.
    structured = StructuredDict(copy.deepcopy(out.json_schema), name=out.effective_name(), description=out.description)
    mode = out.effective_mode()
    if mode == "native":
        return NativeOutput(structured, name=out.effective_name(), description=out.description)
    if mode == "prompted":
        return PromptedOutput(structured, name=out.effective_name(), description=out.description)
    return ToolOutput(structured, name=OUTPUT_TOOL_NAME, description=out.description)


def retries_for(spec: AgentSpec) -> dict[str, int] | None:
    """The per-run `retries` budgets, or None when the spec sets neither `output` nor
    `model_settings.max_retries` (the agent's defaults apply). `max_retries` is the tool and output
    retry budget; a structured spec's own output budget (`output.max_retries`) wins for output."""
    max_retries = spec.model_settings.max_retries
    if spec.output is None and max_retries is None:
        return None
    retries: dict[str, int] = {}
    if max_retries is not None:
        retries["tools"] = max_retries
    if spec.output is not None:
        retries["output"] = spec.output.effective_max_retries()
    elif max_retries is not None:
        retries["output"] = max_retries
    return retries


# --------------------------------------------------------------------------- schema errors


@lru_cache(maxsize=256)
def _validator(schema_json: str) -> Any:
    import jsonschema
    from referencing import Registry

    schema = json.loads(schema_json)
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    return cls(schema, registry=Registry())  # empty registry: no $ref is ever retrieved


def validator_for(schema: dict[str, Any]) -> Any:
    """A cached jsonschema validator for an output schema (already checked by the spec contract)."""
    return _validator(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def _path(parts: Any) -> str:
    """A JSONPath-like `$.a[0].b` from path segments (strings and ints)."""
    out = "$"
    for seg in parts:
        if isinstance(seg, int):
            out += f"[{seg}]"
        elif isinstance(seg, str) and seg.isidentifier():
            out += f".{seg}"
        else:
            out += f"[{json.dumps(str(seg), ensure_ascii=False)}]"
    return out


def _schema_path(schema: Any, parts: Any) -> list[Any]:
    """`parts` cut at the first object key `schema` does not declare as a property: such a key is
    chosen by the model (`additionalProperties`) and may hold anything, so it never goes into an
    error path. Array indexes are kept."""
    out: list[Any] = []
    node = schema
    for seg in parts:
        if isinstance(seg, int):
            out.append(seg)
            items = node.get("items") if isinstance(node, dict) else None
            node = items if isinstance(items, dict) else {}
            continue
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or seg not in properties:
            break
        out.append(seg)
        node = properties[seg]
    return out


def _non_finite(value: Any) -> list[Any] | None:
    stack: list[tuple[Any, list[Any]]] = [(value, [])]
    while stack:
        item, path = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return path
        if isinstance(item, dict):
            stack.extend((v, [*path, k]) for k, v in item.items())
        elif isinstance(item, (list, tuple)):
            stack.extend((v, [*path, i]) for i, v in enumerate(item))
    return None


def _schema_value(value: Any) -> str:
    """A value FROM THE SCHEMA (enum, const, a bound), rendered for the model, bounded."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 200 else text[:197] + "..."


_COMPARE = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}
_SIZE = {
    "minLength": ("at least", "characters"),
    "maxLength": ("at most", "characters"),
    "minItems": ("at least", "items"),
    "maxItems": ("at most", "items"),
    "minProperties": ("at least", "properties"),
    "maxProperties": ("at most", "properties"),
}


def _message(error: Any) -> str:
    """A fixed message per failed keyword, built only from the schema (never the candidate)."""
    keyword, expected = error.validator, error.validator_value
    if keyword == "type":
        types = expected if isinstance(expected, list) else [expected]
        return "expected type " + " or ".join(str(t) for t in types)
    if keyword == "required":
        missing = [p for p in expected if isinstance(error.instance, dict) and p not in error.instance]
        return "missing required properties " + _schema_value(missing) if len(missing) > 1 else (
            "missing required property " + _schema_value(missing[0] if missing else "?")
        )
    if keyword == "additionalProperties":
        return "additional properties are not allowed"
    if keyword == "enum":
        return "must be one of " + _schema_value(expected)
    if keyword == "const":
        return "must equal " + _schema_value(expected)
    if keyword in _COMPARE:
        return f"must be {_COMPARE[keyword]} {_schema_value(expected)}"
    if keyword in _SIZE:
        bound, unit = _SIZE[keyword]
        return f"must have {bound} {_schema_value(expected)} {unit}"
    return f"does not satisfy {keyword}"


def schema_errors(schema: dict[str, Any], value: Any) -> list[tuple[str, str]]:
    """`(json path, message)` for every way `value` breaks `schema`, sorted, deduplicated, at most
    MAX_OUTPUT_ERRORS. Empty when valid. No message ever contains a value from `value`, and no path
    a key the schema does not declare (the path stops at the object holding it)."""
    bad = _non_finite(value)
    if bad is not None:
        return [(_path(_schema_path(schema, bad)), "must be a finite number")]
    found: set[tuple[str, str]] = set()
    for error in validator_for(schema).iter_errors(value):
        found.add((_path(_schema_path(schema, error.absolute_path)), _message(error)))
    return sorted(found)[:MAX_OUTPUT_ERRORS]


def retry_message(errors: list[tuple[str, str]]) -> str:
    """The model's feedback for a rejected candidate: one `- <path>: <message>` line per error,
    whole lines only, at most MAX_OUTPUT_ERROR_BYTES in UTF-8."""
    lines = [RETRY_HEADING]
    size = len(RETRY_HEADING.encode("utf-8"))
    for path, message in errors:
        line = f"- {path}: {message}"
        size += 1 + len(line.encode("utf-8"))
        if size > MAX_OUTPUT_ERROR_BYTES:
            break
        lines.append(line)
    return "\n".join(lines)


def error_paths(content: Any) -> list[str]:
    """The JSON paths of a Pydantic validation error list (a retry part's content), deduplicated,
    in order; [] for anything else."""
    if not isinstance(content, list):
        return []
    paths: list[str] = []
    for detail in content:
        if isinstance(detail, dict):
            path = _path(detail.get("loc") or ())
            if path not in paths:
                paths.append(path)
    return paths[:MAX_OUTPUT_ERRORS]


# --------------------------------------------------------------------------- tool approval (AC.6)

APPROVAL_MODES = ("interactive", "deny")
# AC.8's decide route: a tool call id of 1-256 printable ASCII characters, no spaces.
MAX_TOOL_CALL_ID_CHARS = 256
_TOOL_CALL_ID = re.compile(r"[\x21-\x7e]+")
INVALID_ID_MESSAGE = (
    "This tool call could not be offered for approval (its tool call id is not 1-256 printable "
    "characters without spaces); it was not run."
)
INVALID_ID_REASON = "invalid_tool_call_id"
# Loops D8a: an agent step inside a workflow loop body never waits for an approval.
LOOP_BODY_DENY_REASON = "loop_body"
LOOP_BODY_DENIED_MESSAGE = "Tool approvals are not available inside loops; this tool call was not run."

# A gated call whose id an EARLIER model request of this run already used for an approval: its old
# decision must never authorise this (different) call, so it is denied at once (security).
DUPLICATE_ID_MESSAGE = (
    "This tool call reused the id of an earlier tool call of this run, so it could not be offered "
    "for approval; it was not run."
)
DUPLICATE_ID_REASON = "duplicate_tool_call_id"
EVAL_DECIDER = "eval"
SYSTEM_DECIDER = "system"


def approvable_tool_call_id(tool_call_id: Any) -> bool:
    """Whether the API's decide route can address this id (AC.8: 1-256 printable ASCII, no spaces)."""
    return (
        isinstance(tool_call_id, str)
        and 1 <= len(tool_call_id) <= MAX_TOOL_CALL_ID_CHARS
        and _TOOL_CALL_ID.fullmatch(tool_call_id) is not None
    )


def _env_seconds(name: str, default: float) -> float:
    import os

    try:
        return max(0.1, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def read_approval_timing() -> dict[str, float]:
    """The approval knobs, read ONCE inside eap.start_run and recorded with it: seconds per
    `timeout_hours` hour (EAP_WORKFLOW_HOUR_SECONDS, default 3600, as for workflow approval steps)
    and the longest single wait for a wake-up (EAP_APPROVAL_POLL_SECONDS, default 3600)."""
    return {
        "hour_seconds": _env_seconds("EAP_WORKFLOW_HOUR_SECONDS", 3600.0),
        "poll_seconds": _env_seconds("EAP_APPROVAL_POLL_SECONDS", 3600.0),
    }


@dataclass(frozen=True)
class ApprovalSettings:
    """A run's approval options, from eap.start_run's recorded output."""

    mode: str = "interactive"
    hour_seconds: float = 3600.0
    poll_seconds: float = 3600.0
    # Why "deny" mode denies: None for an eval child (EVAL_DENIED_MESSAGE), LOOP_BODY_DENY_REASON for
    # a workflow loop body's agent step (LOOP_BODY_DENIED_MESSAGE).
    deny_reason: str | None = None

    @classmethod
    def from_link(cls, link: dict[str, Any]) -> ApprovalSettings:
        """From eap.start_run's recorded output. A checkpoint recorded before AC.6 has no keys:
        interactive with the production defaults (3600 s per hour and per poll). The environment is
        never read here: this is workflow code, so a replay must not depend on the process's env."""
        mode = link.get("approval_mode")
        timing = link.get("approval_timing")
        if not isinstance(timing, dict):
            timing = {}
        reason = link.get("approval_deny_reason")
        return cls(
            mode=mode if mode in APPROVAL_MODES else "interactive",
            hour_seconds=float(timing.get("hour_seconds") or 3600.0),
            poll_seconds=float(timing.get("poll_seconds") or 3600.0),
            deny_reason=reason if reason == LOOP_BODY_DENY_REASON else None,
        )


_APPROVAL_SETTINGS: ContextVar[ApprovalSettings | None] = ContextVar("eap_approval_settings", default=None)


def set_approval_settings(settings: ApprovalSettings) -> Token[ApprovalSettings | None]:
    """Make `settings` the current run's approval options for SpecCapability's hooks (a context
    variable: each workflow runs in its own asyncio task, so concurrent runs never share it).
    Returns the token for reset_approval_settings."""
    return _APPROVAL_SETTINGS.set(settings)


def reset_approval_settings(token: Token[ApprovalSettings | None]) -> None:
    _APPROVAL_SETTINGS.reset(token)


def current_approval_settings() -> ApprovalSettings:
    """The current run's settings; outside a run (never in practice) the defaults, never the env."""
    return _APPROVAL_SETTINGS.get() or ApprovalSettings()


def _call_args(call: ToolCallPart) -> Any:
    try:
        return to_jsonable_python(call.args_as_dict(), fallback=str)
    except (ValueError, AssertionError):  # cannot happen: deferred calls are validated first
        return {}


def _approval_summary(call: ToolCallPart) -> str | None:
    """The registry's summary of a gated call, or None (no summariser, an MCP or client tool, or a
    summariser that fails: the card then shows the arguments alone)."""
    from eap_worker.runtime import get_runtime

    registry = get_runtime().registry
    tool_id = registry.tool_id_for_name(call.tool_name)
    if tool_id is None:
        return None
    try:
        return registry.summary_for(tool_id, _call_args(call))
    except Exception:  # noqa: BLE001 - a summary is a courtesy, never a reason to fail the run
        return None


def _enriched_summary(tenant_id: str, data: dict[str, Any]) -> str | None:
    """The Architect's gated tools word their approval card with facts read from the database (publish
    warnings, versions, referencing workflows, schedules). The registry summariser is pure and has no
    tenant, so those facts are read here, inside the begin step: its recorded result keeps the wording
    stable across a replay. None for any other tool, or when the facts cannot be read (the registry's
    summary then stands)."""
    from eap_worker.runtime import get_runtime

    rt = get_runtime()
    tool_id = rt.registry.tool_id_for_name(str(data.get("tool_name", "")))
    if tool_id is None or not tool_id.startswith("architect."):
        return None
    try:
        from eap_worker.architect_tools import approval_summary_text

        args = data.get("args")
        return approval_summary_text(rt.engine, tenant_id, tool_id, args if isinstance(args, dict) else {})
    except Exception:  # noqa: BLE001 - a summary is a courtesy, never a reason to fail the run
        return None


@DBOS.step(name="eap.tool_approval_begin")
async def tool_approval_begin_step(
    run_id: str,
    tenant_id: str,
    tool_call_id: str,
    data: dict[str, Any],
    timeout_seconds: float,
    batch_deadline: float | None = None,
) -> dict[str, Any]:
    """Compute the deadline ONCE (now + timeout_seconds, epoch seconds; or `batch_deadline`, the
    batch's deadline recorded by its first call's step, so every call of a batch shows and times out
    at the same instant), write the `tool_approval_waiting` event carrying it and its open
    pending_approvals row in one transaction (EventLog.append_approval_waiting), and return
    `{"deadline": <epoch>, "duplicate": False}`. A re-executed step finds the first event and
    returns ITS deadline (a repeated write adds nothing).

    Reused ids (security): `data["model_request"]` names the model response the call came from.
    When the call's waiting event already exists for ANOTHER model request of this run (a provider
    that numbers calls `call_0`, `call_1` per response), nothing is written and the step returns
    `{"deadline": None, "duplicate": True}`: the caller denies the call, so the earlier call's
    decision can never authorise it. A waiting event without `model_request` (written before this
    rule) counts as this request, so an in-flight run replays as before."""
    rt = get_runtime()
    key = f"tool:{tool_call_id}:approval:waiting"

    def work() -> dict[str, Any]:
        existing = rt.event_log.get_event(tenant_id, run_id, key)
        if existing is not None and isinstance(existing.data, dict):
            earlier = existing.data.get("model_request")
            if earlier is not None and earlier != data.get("model_request"):
                return {"deadline": None, "duplicate": True}
        deadline = batch_deadline if batch_deadline is not None else time.time() + timeout_seconds
        payload = {**data, "deadline": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()}
        if (facts := _enriched_summary(tenant_id, data)) is not None:
            payload["summary"] = facts
        event, _created = rt.event_log.append_approval_waiting(tenant_id, run_id, "tool", tool_call_id, payload)
        crash_point("after_event_write", event_key=key)
        stored = event.data.get("deadline") if isinstance(event.data, dict) else None
        if isinstance(stored, str):
            deadline = datetime.fromisoformat(stored).timestamp()
        return {"deadline": deadline, "duplicate": False}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.tool_approval_decisions")
async def tool_approval_decisions_step(
    run_id: str, tenant_id: str, tool_call_ids: list[str], timed_out: bool
) -> dict[str, dict[str, Any]]:
    """The recorded decisions `{tool_call_id: {"type", "data"}}` of the batch's calls (only the
    decided ones). With `timed_out`, every undecided call first gets `tool_approval_timed_out`
    (append_tool_decision: a person's decision that got there first wins), so all are decided."""
    rt = get_runtime()

    def work() -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for call_id in tool_call_ids:
            event = rt.event_log.get_event(tenant_id, run_id, decision_event_key("tool", call_id))
            if event is None and timed_out:
                _outcome, event = rt.event_log.append_tool_decision(
                    tenant_id, run_id, call_id, "tool_approval_timed_out", {"tool_call_id": call_id}
                )
                if event is None:  # not waiting any more (cannot happen while this run waits)
                    found[call_id] = {"type": "tool_approval_timed_out", "data": {"tool_call_id": call_id}}
                    continue
            if event is not None:
                found[call_id] = {"type": event.type, "data": dict(event.data or {})}
        return found

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.tool_approval_deny")
async def tool_approval_deny_step(
    run_id: str, tenant_id: str, tool_call_id: str, data: dict[str, Any], event_key: str | None = None
) -> None:
    """Deny a gated call at once, with no wait and no inbox row: the `tool_denied` event at its
    decision key (idempotent per key), or at `event_key`. Eval runs (`decided_by: "eval"`), tool
    call ids the API could never address and reused ids (`decided_by: "system"`; a reused id's event
    is `tool:{id}:approval:duplicate:{model_request}`, since its decision key holds the earlier
    call's decision)."""
    rt = get_runtime()
    key = event_key or decision_event_key("tool", tool_call_id)
    await asyncio.to_thread(rt.event_log.append, tenant_id, UUID(run_id), key, "tool_denied", data)
    crash_point("after_event_write", event_key=key)


def _result_for(decision: dict[str, Any] | None) -> ToolApproved | ToolDenied:
    kind = (decision or {}).get("type")
    data = (decision or {}).get("data") or {}
    if kind == "tool_approved":
        return ToolApproved()
    if kind == "tool_denied":
        if data.get("decided_by") == EVAL_DECIDER:
            return ToolDenied(EVAL_DENIED_MESSAGE)
        if data.get("reason") == INVALID_ID_REASON:
            return ToolDenied(INVALID_ID_MESSAGE)
        if data.get("reason") == LOOP_BODY_DENY_REASON:
            return ToolDenied(LOOP_BODY_DENIED_MESSAGE)
        if data.get("reason") == DUPLICATE_ID_REASON:
            return ToolDenied(DUPLICATE_ID_MESSAGE)
        comment = data.get("comment")
        return ToolDenied(denial_message(comment if isinstance(comment, str) else None))
    return ToolDenied(TIMED_OUT_MESSAGE)


async def wait_for_tool_approvals(
    run_id: str,
    tenant_id: str,
    calls: list[ToolCallPart],
    timeout_hours: int,
    settings: ApprovalSettings,
    model_request: int | None = None,
) -> DeferredToolResults:
    """The durable wait for one batch of gated calls (see the module docstring). Workflow code:
    every read is a step and `recv` is recorded, so a replay takes the same path."""
    from eap_worker.workflow_run import clock_step  # `eap.workflow_clock`; workflow_run imports workflow

    decisions: dict[str, dict[str, Any]] = {}
    waiting: list[ToolCallPart] = []
    for call in calls:
        if settings.mode == "deny" and settings.deny_reason == LOOP_BODY_DENY_REASON:
            data = {
                "tool_call_id": call.tool_call_id, "decided_by": SYSTEM_DECIDER, "comment": None,
                "reason": LOOP_BODY_DENY_REASON,
            }
        elif settings.mode == "deny":
            data = {"tool_call_id": call.tool_call_id, "decided_by": EVAL_DECIDER, "comment": None}
        elif not approvable_tool_call_id(call.tool_call_id):
            data = {
                "tool_call_id": call.tool_call_id, "decided_by": SYSTEM_DECIDER, "comment": None,
                "reason": INVALID_ID_REASON,
            }
        else:
            waiting.append(call)
            continue
        await tool_approval_deny_step(run_id, tenant_id, call.tool_call_id, data)
        decisions[call.tool_call_id] = {"type": "tool_denied", "data": data}

    if waiting:
        timeout_seconds = timeout_hours * settings.hour_seconds
        batch = [c.tool_call_id for c in waiting]
        deadline: float | None = None
        begun: list[str] = []
        for call in waiting:
            payload: dict[str, Any] = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "args": _call_args(call),
            }
            summary = _approval_summary(call)
            if summary is not None:  # only tools with a summariser: other payloads are unchanged
                payload["summary"] = summary
            recorded = await tool_approval_begin_step(
                run_id, tenant_id, call.tool_call_id,
                {
                    **payload,
                    # The DBOS workflow that waits: the API sends the wake-up here.
                    "workflow_id": DBOS.workflow_id,
                    # AG-UI (Q7): every waiting call of this deferred batch, for one interrupt per batch.
                    "batch": batch,
                    # The model response it came from: a later reuse of this id is refused.
                    "model_request": model_request,
                },
                timeout_seconds,
                deadline,  # None for the first call; then the batch's recorded deadline
            )
            # A checkpoint recorded before reuse detection holds the bare deadline.
            info = recorded if isinstance(recorded, dict) else {"deadline": recorded, "duplicate": False}
            if info.get("duplicate"):
                data = {
                    "tool_call_id": call.tool_call_id, "decided_by": SYSTEM_DECIDER, "comment": None,
                    "reason": DUPLICATE_ID_REASON, "model_request": model_request,
                }
                await tool_approval_deny_step(
                    run_id, tenant_id, call.tool_call_id, data,
                    event_key=f"tool:{call.tool_call_id}:approval:duplicate:{model_request}",
                )
                decisions[call.tool_call_id] = {"type": "tool_denied", "data": data}
                continue
            begun.append(call.tool_call_id)
            # Equal for every call of the batch; min() only guards an event written before this rule.
            value = float(info["deadline"])
            deadline = value if deadline is None else min(deadline, value)
            crash_point("tool_approval_waiting", event_key=f"tool:{call.tool_call_id}:approval:waiting")
        batch = begun
    if waiting and batch:
        assert deadline is not None
        topic = tool_approval_topic(run_id)
        while True:
            found = await tool_approval_decisions_step(run_id, tenant_id, batch, False)
            if len(found) == len(batch):
                break
            remaining = deadline - await clock_step()
            if remaining <= 0:
                found = await tool_approval_decisions_step(run_id, tenant_id, batch, True)
                break
            # Only a nudge: the next round reads the decision events, whatever the message says.
            await DBOS.recv_async(topic, timeout_seconds=min(remaining, settings.poll_seconds))
        decisions.update(found)

    return DeferredToolResults(approvals={c.tool_call_id: _result_for(decisions.get(c.tool_call_id)) for c in calls})


# --------------------------------------------------------------------------- the capability


class SpecCapability(AbstractCapability[Any]):
    """The run spec's behaviour (see the module docstring). Workflow-side and pure (no step), except
    the approval wait, which runs only when the model calls a gated tool."""

    async def prepare_tools(self, ctx: RunContext[Any], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
        spec = getattr(ctx.deps, "spec", None)
        if getattr(spec, "approvals", None) is None:
            return tool_defs
        gated = approval_required_tool_names(spec)
        return [
            replace(td, kind="unapproved") if td.name in gated and td.kind == "function" else td
            for td in tool_defs
        ]

    async def handle_deferred_tool_calls(
        self, ctx: RunContext[Any], *, requests: DeferredToolRequests
    ) -> DeferredToolResults | None:
        spec = getattr(ctx.deps, "spec", None)
        if getattr(spec, "approvals", None) is None or not requests.approvals:
            return None  # external calls (`requests.calls`) are never produced by our specs
        gated = approval_required_tool_names(spec)
        calls = [c for c in requests.approvals if c.tool_name in gated]
        if not calls:
            return None
        results = await wait_for_tool_approvals(
            str(ctx.deps.run_id), ctx.deps.tenant_id, calls, spec.approvals.effective_timeout_hours(),
            current_approval_settings(),
            # The response's model request n (run_step counts requests from 1), as in `model:{n}:final`.
            model_request=ctx.run_step - 1,
        )
        check_approved_tool_calls(ctx, results)
        return results

    async def after_output_validate(
        self, ctx: RunContext[Any], *, output_context: Any, output: Any
    ) -> Any:
        spec = getattr(ctx.deps, "spec", None)
        out = getattr(spec, "output", None)
        if out is None or ctx.partial_output:
            return output
        errors = schema_errors(out.json_schema, output)
        if errors:
            raise ModelRetry(retry_message(errors))
        return output

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None


def check_approved_tool_calls(ctx: RunContext[Any], results: DeferredToolResults) -> None:
    """Approved calls count against the tool-call limit exactly like ordinary calls.

    Pydantic AI checks `tool_calls_limit` once per response, up front, for its function-kind
    calls only, so a deferred (`unapproved`) call that is approved and then run would bypass it.
    The same check, with the run's own UsageLimits and live usage, runs here for the approved calls
    before any of them runs: the same UsageLimitExceeded (message, `limit` / `value` via
    errors.describe_usage_limit) at the same point (before the batch's approved calls run; ungated calls of the response have
    already run and are counted in the live usage, as Pydantic AI counts them). Denied and timed-out
    calls never run, so they do not count, as Pydantic AI counts calls (plan: "native limits count
    ... tool calls, as Pydantic AI counts them"). Pure: the decisions are recorded, the usage is
    replayed, so a replay raises identically."""
    limits = getattr(ctx, "usage_limits", None)
    if limits is None or getattr(limits, "tool_calls_limit", None) is None:
        return
    approved = sum(1 for result in results.approvals.values() if isinstance(result, ToolApproved))
    if not approved:
        return
    projected = copy.deepcopy(ctx.usage)
    projected.tool_calls += approved
    limits.check_before_tool_call(projected)


__all__ = [
    "APPROVAL_MODES",
    "ApprovalSettings",
    "DUPLICATE_ID_MESSAGE",
    "DUPLICATE_ID_REASON",
    "INVALID_ID_MESSAGE",
    "INVALID_ID_REASON",
    "LOOP_BODY_DENIED_MESSAGE",
    "LOOP_BODY_DENY_REASON",
    "RETRY_HEADING",
    "SpecCapability",
    "approvable_tool_call_id",
    "check_approved_tool_calls",
    "current_approval_settings",
    "read_approval_timing",
    "reset_approval_settings",
    "set_approval_settings",
    "tool_approval_begin_step",
    "tool_approval_decisions_step",
    "tool_approval_deny_step",
    "wait_for_tool_approvals",
    "error_paths",
    "output_type_for",
    "retries_for",
    "retry_message",
    "schema_errors",
    "validator_for",
]
