"""Workflow loop scopes and every durable identifier derived from them (loops milestone, D4).

Pure functions, no I/O. Every caller that needs a step's event key, child run id, side-effect
guard key, tool call id, iteration workflow id, judge request key or instance token builds it
here; nobody rebuilds these strings by hand.

A **scope** is the chain of `(loop_id, index)` frames from the top of the spec down to the
current iteration, written `L.i` per frame and joined with `/`: `outer.2/inner.0`. The **empty
scope** (a top-level step) gives every identifier exactly as before loops existed, so runs
started before this milestone replay unchanged.

Event data carries the bare `step_id` plus `scope: [{"loop": L, "index": i}, ...]`
(`scope_to_data` / `scope_from_data`).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Literal, NamedTuple

# Loop ids are step ids (STEP_ID_PATTERN in eap_schemas.workflows); duplicated here so this module
# has no dependency on the spec models.
_LOOP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FRAME_RE = re.compile(r"^([a-z][a-z0-9_]{0,63})\.(0|[1-9][0-9]*)$")

# Child agent run ids: uuid5(CHILD_RUN_NAMESPACE, "{run_id}:{step_id}") for a top-level step (the
# value eap_worker.workflow_run has always used), "{run_id}:{S}:{step_id}" inside a loop body.
CHILD_RUN_NAMESPACE = uuid.UUID("6f2b8e0a-5f37-4c1e-9d0b-8f8e2c7a1d42")

# ctx.tool_call_id is at most this long. Longer scoped ids keep a prefix plus a digest (below).
MAX_TOOL_CALL_ID_LENGTH = 128
_TOOL_CALL_DIGEST_CHARS = 16


class ScopeFrame(NamedTuple):
    """One enclosing loop iteration: the loop step's id and the 0-based iteration index."""

    loop: str
    index: int


Scope = tuple[ScopeFrame, ...]
EMPTY_SCOPE: Scope = ()


class ScopeInvalid(ValueError):
    """A scope frame, scope string or scope data is malformed."""


def _frame(loop: Any, index: Any) -> ScopeFrame:
    if not isinstance(loop, str) or not _LOOP_ID_RE.fullmatch(loop):
        raise ScopeInvalid(f"invalid loop id in scope: {loop!r}")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ScopeInvalid(f"invalid iteration index in scope: {index!r}")
    return ScopeFrame(loop, index)


def make_scope(frames: Iterable[ScopeFrame | tuple[str, int]] = ()) -> Scope:
    """A validated scope from `(loop_id, index)` pairs (outermost first)."""
    return tuple(_frame(*frame) for frame in frames)


def child_scope(scope: Scope, loop_id: str, index: int) -> Scope:
    """The scope of iteration `index` of loop `loop_id` running inside `scope`."""
    return (*make_scope(scope), _frame(loop_id, index))


def scope_string(scope: Scope) -> str:
    """`outer.2/inner.0`; `""` for the empty scope."""
    return "/".join(f"{loop}.{index}" for loop, index in make_scope(scope))


def parse_scope(text: str) -> Scope:
    """The inverse of `scope_string` (`""` gives the empty scope)."""
    if text == "":
        return EMPTY_SCOPE
    frames = []
    for part in text.split("/"):
        match = _FRAME_RE.fullmatch(part)
        if match is None:
            raise ScopeInvalid(f"invalid scope string: {text!r}")
        frames.append(ScopeFrame(match.group(1), int(match.group(2))))
    return tuple(frames)


def scope_to_data(scope: Scope) -> list[dict[str, Any]]:
    """Event data form: `[{"loop": L, "index": i}, ...]` (outermost first)."""
    return [{"loop": loop, "index": index} for loop, index in make_scope(scope)]


def scope_from_data(data: Iterable[Mapping[str, Any]] | None) -> Scope:
    """The inverse of `scope_to_data`; a missing (`None`) scope is the empty scope."""
    if data is None:
        return EMPTY_SCOPE
    frames = []
    for item in data:
        if not isinstance(item, Mapping) or set(item) != {"loop", "index"}:
            raise ScopeInvalid(f"invalid scope frame: {item!r}")
        frames.append(_frame(item["loop"], item["index"]))
    return tuple(frames)


def loop_path(scope: Scope) -> tuple[str, ...]:
    """The chain of loop ids from the top (D6's `loop_path`)."""
    return tuple(frame.loop for frame in make_scope(scope))


def _require_step_id(step_id: str) -> str:
    if not isinstance(step_id, str) or not _LOOP_ID_RE.fullmatch(step_id):
        raise ScopeInvalid(f"invalid step id: {step_id!r}")
    return step_id


def _require_nonempty(scope: Scope, what: str) -> str:
    text = scope_string(scope)
    if not text:
        raise ScopeInvalid(f"{what} needs a non-empty scope")
    return text


# --------------------------------------------------------------------------- event keys

StepPhase = str  # started | completed | failed | waiting | decision | child | call | ...
IterationPhase = Literal["started", "completed", "failed"]


def scoped_event_key(scope: Scope, step_id: str, phase: StepPhase) -> str:
    """A step's event key: `step:{id}:{phase}` at the top level (unchanged), else
    `loop:{S}:step:{id}:{phase}`."""
    _require_step_id(step_id)
    if not isinstance(phase, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", phase):
        raise ScopeInvalid(f"invalid event phase: {phase!r}")
    text = scope_string(scope)
    if not text:
        return f"step:{step_id}:{phase}"
    return f"loop:{text}:step:{step_id}:{phase}"


def iteration_event_key(scope: Scope, phase: IterationPhase) -> str:
    """An iteration's own event key `loop:{S}:started|completed|failed`, where `scope` ends with
    the iteration's frame (`child_scope(outer, L, i)`)."""
    if phase not in ("started", "completed", "failed"):
        raise ScopeInvalid(f"invalid iteration phase: {phase!r}")
    return f"loop:{_require_nonempty(scope, 'an iteration event key')}:{phase}"


# --------------------------------------------------------------------------- run-side identifiers


def scoped_child_run_id(run_id: str, scope: Scope, step_id: str) -> str:
    """The run id (and DBOS workflow id) of the agent run of agent step `step_id`:
    `uuid5(NS, "{run_id}:{step_id}")` at the top level (unchanged), else
    `uuid5(NS, "{run_id}:{S}:{step_id}")`."""
    _require_step_id(step_id)
    text = scope_string(scope)
    seed = f"{run_id}:{step_id}" if not text else f"{run_id}:{text}:{step_id}"
    return str(uuid.uuid5(CHILD_RUN_NAMESPACE, seed))


def scoped_parent_step_id(scope: Scope, step_id: str) -> str:
    """A child run's `parent_step_id`: `step_id` at the top level, else `{S}/{step_id}`."""
    _require_step_id(step_id)
    text = scope_string(scope)
    return step_id if not text else f"{text}/{step_id}"


def scoped_guard_key(run_id: str, scope: Scope, step_id: str) -> str:
    """The side_effects key guarding a tool step's single call: `{run_id}:{step_id}` at the top
    level (unchanged), else `{run_id}:{S}:{step_id}`."""
    _require_step_id(step_id)
    text = scope_string(scope)
    return f"{run_id}:{step_id}" if not text else f"{run_id}:{text}:{step_id}"


def scoped_tool_call_id(scope: Scope, step_id: str) -> str:
    """The `ctx.tool_call_id` a tool step's tool sees: `wf_{instance_token(scope, step_id)}`.

    At the top level that is `wf_{step_id}`, unchanged. Inside a loop it is `wf_each.3~update` or
    `wf_outer.2~inner.0~update`. The mapping is injective: instance tokens are canonical (see
    `parse_instance_token`), and `~` never appears in a top-level id. Every character is printable
    ASCII and URL-path safe (AC.8's decide route), with no `/`.

    The result is always at most MAX_TOOL_CALL_ID_LENGTH characters. A longer id keeps its first
    characters and ends with `_` plus 16 hex characters of the sha256 of the full id.
    """
    full = f"wf_{instance_token(scope, step_id)}"
    if len(full) <= MAX_TOOL_CALL_ID_LENGTH:
        return full
    digest = hashlib.sha256(full.encode()).hexdigest()[:_TOOL_CALL_DIGEST_CHARS]
    keep = MAX_TOOL_CALL_ID_LENGTH - _TOOL_CALL_DIGEST_CHARS - 1
    return f"{full[:keep]}_{digest}"


def iteration_workflow_id(run_id: str, scope: Scope) -> str:
    """The DBOS workflow id of one iteration (`eap.workflow_iteration`): `{run_id}:loop:{S}`,
    where `scope` ends with the iteration's frame."""
    return f"{run_id}:loop:{_require_nonempty(scope, 'an iteration workflow id')}"


def scoped_judge_key(scope: Scope, step_id: str, evaluator_index: int) -> str:
    """The usage-row request key of an evaluate step's judge call `k` (operation `chat`, D15):
    `judge:{step_id}:{k}` at the top level, else `judge:{S}:{step_id}:{k}`. The `judge:` prefix is
    what metrics derive the `judge` operation from."""
    _require_step_id(step_id)
    if isinstance(evaluator_index, bool) or not isinstance(evaluator_index, int) or evaluator_index < 0:
        raise ScopeInvalid(f"invalid evaluator index: {evaluator_index!r}")
    text = scope_string(scope)
    return f"judge:{step_id}:{evaluator_index}" if not text else f"judge:{text}:{step_id}:{evaluator_index}"


def instance_token(scope: Scope, step_id: str) -> str:
    """One path segment naming a step instance (AG-UI step names and message ids):
    `step_id` at the top level, else `{S}~{step_id}` with `/` written `~`
    (`each.3~triage`, `outer.2~inner.0~triage`). Never contains `/`."""
    _require_step_id(step_id)
    text = scope_string(scope)
    return step_id if not text else f"{text.replace('/', '~')}~{step_id}"


def parse_instance_token(token: str) -> tuple[Scope, str]:
    """The inverse of `instance_token`: `(scope, step_id)`. Only canonical tokens parse, so
    `parse_instance_token(instance_token(s, id)) == (s, id)` and every token has one meaning
    (`~a`, `a~` and `~` are rejected)."""
    if not isinstance(token, str):
        raise ScopeInvalid(f"invalid instance token: {token!r}")
    *parts, step_id = token.split("~")
    frames = []
    for part in parts:
        match = _FRAME_RE.fullmatch(part)
        if match is None:
            raise ScopeInvalid(f"invalid instance token: {token!r}")
        frames.append(ScopeFrame(match.group(1), int(match.group(2))))
    return tuple(frames), _require_step_id(step_id)


# --------------------------------------------------------------------------- terminal status


def status_for_end(event_type: str, data: Mapping[str, Any] | None = None) -> str:
    """The run status an `end` event fixes (D12): `run_completed` → `completed`; `run_failed` →
    the event's `status` (`failed` or `needs_attention`), `failed` when absent or anything else."""
    if event_type == "run_completed":
        return "completed"
    if event_type == "run_failed":
        status = (data or {}).get("status")
        return status if status in ("failed", "needs_attention") else "failed"
    raise ValueError(f"not an end event type: {event_type!r}")


# --------------------------------------------------------------------------- error codes

LoopErrorCode = Literal[
    "loop_items_exceeded",
    "loop_items_not_list",
    "loop_max_iterations",
    "loop_state_too_large",
    "loop_body_step_not_allowed",
    "loop_in_parallel_not_allowed",
    "loop_ref_not_on_every_path",
    "workflow_too_many_executions",
    "workflow_concurrency_exceeded",
    "run_cancelled",
    "evaluate_failed",
]

# Every loops-milestone error code with its fixed, user-safe message. The status in
# LOOP_WORKFLOW_FAILURES is only the default (`failed`): as for every WORKFLOW_FAILURES code, the
# worker's actual status can override it. In particular a cancelled run whose side effect is still
# `reserved` ends `needs_attention` with code `run_cancelled` (D12), so never force `failed` from
# this table. The first four, `run_cancelled` and `evaluate_failed` are
# run-time step failures (the worker merges these into WORKFLOW_FAILURES); the other five are spec
# rejections (422 at draft save or publish) that the worker may also raise at run time as a guard.
# The cost cap reuses AC.5's `run_budget_exceeded` (RunBudgetExceeded), so it is not listed here.
LOOP_ERROR_MESSAGES: dict[str, str] = {
    "loop_items_exceeded": "the foreach list has more items than the step's max_items",
    "loop_items_not_list": "the foreach items value is not a list",
    "loop_max_iterations": "the loop reached its max_iterations without meeting its condition",
    "loop_state_too_large": "the loop state exceeds the loop state size limit",
    "loop_body_step_not_allowed": "approval and parallel steps are not allowed inside a loop body",
    "loop_in_parallel_not_allowed": "a loop step is not allowed inside a parallel branch",
    "loop_ref_not_on_every_path": "a loop condition or carry reads a body step that does not run on every path",
    "workflow_too_many_executions": "the workflow can execute more steps than the step execution limit",
    "workflow_concurrency_exceeded": "the workflow's nested foreach concurrency exceeds the run concurrency limit",
    "run_cancelled": "the run was cancelled",
    "evaluate_failed": "the evaluate step could not evaluate the value",
}

LOOP_ERROR_CODES: tuple[str, ...] = tuple(LOOP_ERROR_MESSAGES)

# The same codes in eap_worker.errors.WORKFLOW_FAILURES's shape: code -> (default status, message).
LOOP_WORKFLOW_FAILURES: dict[str, tuple[str, str]] = {
    code: ("failed", message) for code, message in LOOP_ERROR_MESSAGES.items()
}


__all__ = [
    "CHILD_RUN_NAMESPACE",
    "EMPTY_SCOPE",
    "LOOP_ERROR_CODES",
    "LOOP_ERROR_MESSAGES",
    "LOOP_WORKFLOW_FAILURES",
    "MAX_TOOL_CALL_ID_LENGTH",
    "IterationPhase",
    "LoopErrorCode",
    "Scope",
    "ScopeFrame",
    "ScopeInvalid",
    "child_scope",
    "instance_token",
    "iteration_event_key",
    "iteration_workflow_id",
    "scoped_judge_key",
    "loop_path",
    "make_scope",
    "parse_instance_token",
    "parse_scope",
    "scope_from_data",
    "scope_string",
    "scope_to_data",
    "scoped_child_run_id",
    "scoped_event_key",
    "scoped_guard_key",
    "scoped_parent_step_id",
    "scoped_tool_call_id",
    "status_for_end",
]
