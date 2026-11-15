"""Workflow runs: one DBOS workflow (`eap.workflow_run`) interprets a published workflow version.

Hand-off (apps/api/src/eap_api/launcher.py): the API inserts a `runs` row of kind 'workflow'
(workflow_id, workflow_version, input = the validated input as canonical JSON) and enqueues
`eap.workflow_run` on queue `eap-agent-runs` with workflow_id = run_id and positional args
`(run_id, tenant_id, workflow_version_id, input_json)`; `workflow_version_id` is the
`workflow_versions.id` of the version the row names.

Determinism: the spec is read once, in `eap.start_workflow_run`, and checkpointed; every later
decision is a pure function (eap_worker.workflow_eval) of the input and of step outputs recorded by
DBOS steps or child workflows, so a recovered run takes exactly the path the first attempt took.
Clock readings come from steps too (`eap.workflow_step_begin`, `eap.workflow_clock`).

Step layout of a run (DBOS steps and child workflows; `{id}` is a workflow step id):

    eap.start_workflow_run              load the version (tenant-scoped), runs row -> running
    eap.append_event   start            run_started {kind, workflow_id, workflow_version, input}
    per workflow step, in graph order:
      eap.workflow_step_begin           step_started  `step:{id}:started` {step_id, step_type, input?, ...}
      agent:    eap.workflow_resolve_agent   agent version (null = latest published, resolved ONCE here)
                                             and the child run row (kind agent, parent_run_id/step_id)
                child workflow eap.agent_run, workflow id = child run id = uuid5(run_id:step_id)
      tool:     eap.check_tool, eap.workflow_tool_call   (side effects under `{run_id}:{id}`)
      branch:   nothing durable: conditions are evaluated over recorded data
      parallel: one child workflow eap.workflow_branch per branch, id `{run_id}:{id}:{branch}`
      approval: eap.append_event step_waiting `step:{id}:waiting`, then a loop of
                eap.workflow_approval_decision / eap.workflow_clock / DBOS.recv(`approval:{id}`)
      loop:     per iteration i (LP.7, loops D5): child workflow eap.workflow_iteration, id
                `{run_id}:loop:{S}` (S = the iteration's scope, `L.i`, `outer.2/inner.0`), its
                result, then eap.append_event `loop:{S}:completed` | `loop:{S}:failed`
      foreach:  (LP.10, loops D7) a sliding window in item order: eap.workflow_iteration for
                items 0..c-1 (c = concurrency), then per item i its result,
                `loop:{S}:completed|failed` and the start of item i+c; so with c = 1 exactly the
                loop's 3 operations per item, and for any c a function of the recorded results only
      eap.append_event   step_completed `step:{id}:completed` {step_id, output, unresolved_refs?, warnings?}
                     or  step_failed    `step:{id}:failed`    {step_id, status, error, error_code}
    eap.append_event   end              run_completed {output, result} | run_failed {...}
    eap.finish_run                      completed | failed | needs_attention

    eap.start_workflow_run alone when the run is already terminal (cancelled before it started):
    RunStore.mark_running refuses to revive it and the workflow returns without an event.

One iteration (`eap.workflow_iteration(run_id, tenant_id, workflow_version_id, loop_path, scope,
index, state, limits, traceparent)`, loops D6: references, never the spec, outputs or an item):

    (not a step) the immutable read: the version's spec, the `start` event's input, the
                 `body_reads` outputs from their `...step:{id}:completed` events, and the
                 enclosing iterations' `loop:{S'}:started` events (their state)
    eap.append_event   `loop:{S}:started` loop_iteration_started {step_id, scope, index, state}
                 (a foreach: {step_id, scope, index, item}; the item is its `items` ref resolved
                 again over the same immutable reads, plus the producing step's completed event)
    the body's steps, exactly as above, with scoped keys and ids (D4): events
                 `loop:{S}:step:{id}:started|completed|failed` (data adds `scope`), child agent
                 run uuid5(`{run_id}:{S}:{id}`) with parent_step_id `{S}/{id}` (approval_mode
                 "deny", D8a), tool guard `{run_id}:{S}:{id}` and tool call id
                 `wf_{instance token}`, nested loops' iterations `{run_id}:loop:{S}/{inner}.{j}`
    then `carry` and `until` (pure); returns {status, done, state, last_key, until}
                 (a foreach iteration: {status, last_key}; it passes `state` None)

Iteration events carry the loop step's bare `step_id`, its `scope` (the enclosing iterations,
`[]` for a top-level loop) and the `index`. A loop step's output is {iterations, exited:
"condition" | "max_iterations", last, state}; `last` is read once from the event `last_key` names
and recorded only in the loop's step_completed. A foreach step's output is the list of each
item's `last` in item order, read the same way (one read, at the end); under `on_item_error:
collect` a failed item's place holds `{error_code, error}`. A foreach completed event carries
`last_key` only (no state, no until). A `while` that is UNKNOWN (a MISSING operand)
stops the loop, an UNKNOWN `until` does not end it, and a branch case that is UNKNOWN is not
taken; each adds a `condition_unknown` warning ({code, condition: "while" | "until" |
"cases.<i>", index?}) to the step's step_completed `warnings` (absent when there is none).

Step outputs (`steps.<id>.output`): agent -> the agent's final text, or for an agent with
structured output (AC.9) the validated object (`agent_run`'s `output_json`), so
`steps.<id>.output.<field>` reads its fields and the step output cap applies to it; tool -> the tool's JSON
result; branch -> {"case": <index of the matching case, or null for default>, "next": <target>};
parallel -> none (its step_completed carries `output: null` and `steps.<parallel>.output` is not
readable, per eap_schemas.workflows; from the join on, the steps its branches ran are); approval -> {"approved": true, "decided_by",
"comment"}. An agent step's input mapping becomes its prompt: the single value if the mapping has
exactly one string value, else the mapping as canonical JSON.

A branch condition `$ref` into a field of an agent step's structured output that resolves to
nothing (typically an unpinned agent whose schema changed) is false, as any missing ref is, and the
branch's step_completed data lists those paths as `unresolved_refs` (paths only, never values; the
key is absent when there are none). It rides on the existing event, so it adds no durable step.
(Input-template refs already fail the step, `input_ref_missing`.)

Run output: the output of the step that ended the main path (a step with no `next`). With an
`output_schema`, that value is validated against it (jsonschema, empty referencing.Registry); a
string that does not validate is parsed as JSON and validated again (an agent answering with JSON
text); failing both ends the run `output_invalid`. runs.output is the canonical JSON text.

Non-finite numbers (NaN, Infinity) are not JSON: a step output containing one fails the step with
`step_output_not_json`, so none ever reaches an event, a run row or a checkpoint.

Every knob below is read ONCE, in eap.start_workflow_run, and checkpointed with it (`limits`);
branch workflows receive that recorded copy as an argument, and an approval deadline is computed
once inside eap.workflow_approval_begin. A worker restarted with other values never changes the
path of a run already started. A non-finite number in the run input (the API refuses it) fails
the run with `input_invalid` before any step, and run_started then carries `input: null`.

Loop knobs (LP.7, D10; recorded in `limits`, read with `loop_limit`, which falls back to the default
for a run recorded before they existed): a loop step's output at most
EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES (256 KiB) instead of the step output cap; a loop state at most
EAP_WORKFLOW_MAX_LOOP_STATE_BYTES (16 KiB, `loop_state_too_large`); for a spec with loops, the
static execution bound and the run concurrency are re-checked against
EAP_WORKFLOW_MAX_STEP_EXECUTIONS and EAP_WORKFLOW_MAX_RUN_CONCURRENCY before any step.

Limits: each step output at most EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES (32 KiB) as JSON, all outputs
of a run at most EAP_WORKFLOW_MAX_CONTEXT_BYTES (1 MiB) in total (both are checkpointed and passed
to branch workflows); a step id executes at most once per run (per iteration inside a loop body),
so a graph executes at most len(steps) <= 50 steps (enforced, step_limit_exceeded); a step does not start once the run is
older than EAP_WORKFLOW_MAX_RUN_SECONDS (14 days; workflow_deadline_exceeded). Approval waits are
bounded by their own timeout_hours (an hour is EAP_WORKFLOW_HOUR_SECONDS, default 3600; tests
shorten it).

Approval: the API's approve/reject writes the decision event `step:{id}:decision` (step_approved /
step_rejected, with decided_by and comment) atomically, only while the step is waiting
(EventLog.append_step_decision), then wakes the waiting DBOS workflow with DBOS send on topic
`approval:{id}` (the waiting workflow's id is in the step_waiting event). The decision event is the
source of truth; the message is only a wake-up, and the wait re-checks the decision at least every
EAP_APPROVAL_POLL_SECONDS (3600), so a lost wake-up delays, never loses, a decision. At the
deadline the worker records step_timed_out under the same key; whoever writes the key first wins.
Rejection fails the run with `approval_rejected`, a timeout with `approval_timeout`.

Cancellation (loops D12, LP.8): `POST /runs/{id}/cancel` records `cancel_requested` (key `cancel`),
cancels the run's DBOS workflow with `cancel_children=True` (launcher.cancel_workflow_tree), which
reaches the whole tree level by level (parallel branch workflows `{run_id}:{step}:{branch}`, loop
iteration workflows `{run_id}:loop:{S}` and child agent runs), and then writes the guarded terminal
state (RunStore.cancel_run_tree: `end` run_failed `run_cancelled` and `failed`, or
`needs_attention` while a side effect is still `reserved`). The scheduled `eap.cancel_reconcile`
(eap_worker.cancel_reconcile) finishes a cancel the API did not. DBOS stops a cancelled workflow at
its next operation: a step already running completes first, and the store's terminal guards drop
its late events and refuse its late status (append_event_step and finish_run_step treat that as
success). A run cancelled before a worker started it is never started (`started` above). The
cooperative path (the loop tick seeing `cancel_requested`, with no DBOS cancel) is LP.11's.
`_parallel` still cancels the branches after the one it awaits when it is cancelled itself; with
the cascade that is redundant but harmless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from dbos import DBOS, SetWorkflowID
from dbos._error import DBOSWorkflowCancelledError  # not re-exported by dbos
from pydantic import ValidationError
from sqlalchemy import text

from eap_observability import log_context, log_failure, run_span, set_run_outcome
from eap_schemas.workflow_scope import (
    CHILD_RUN_NAMESPACE,
    EMPTY_SCOPE,
    Scope,
    child_scope,
    iteration_event_key,
    iteration_workflow_id,
    loop_path,
    parse_scope,
    scope_from_data,
    scope_string,
    scope_to_data,
    scoped_child_run_id,
    scoped_event_key,
    scoped_guard_key,
    scoped_parent_step_id,
    scoped_tool_call_id,
)
from eap_schemas.workflows import (
    LOOP_STEP_TYPES,
    AgentStep,
    ApprovalStep,
    BranchStep,
    ForeachStep,
    LoopStep,
    MAX_STEPS,
    ParallelStep,
    REF_KEY,
    ToolStep,
    WorkflowSpec,
    body_reads,
    iter_refs,
    parse_ref,
    run_concurrency,
    static_execution_bound,
    walk_steps,
)
from eap_worker.config import (
    WORKFLOW_BRANCH_NAME,
    WORKFLOW_ITERATION_NAME,
    WORKFLOW_LOOP_LIMIT_DEFAULTS,
    WORKFLOW_RUN_NAME,
)
from eap_worker.crash import crash_point
from eap_worker.errors import RunFailure, WorkflowStepError, classify
from eap_worker.runtime import get_runtime
from eap_worker.tool_run import _REJECTIONS, call_tool_guarded, check_tool_step
from eap_worker.workflow import _heartbeat, agent_run, append_event_step, finish_run_step
from eap_worker.workflow_eval import (
    MISSING,
    RefMissing,
    build_context,
    canonical_json,
    foreach_items,
    initial_state,
    iteration_exit,
    json_size,
    non_finite_path,
    outcome,
    resolve_ref,
    resolve_template,
    while_outcome,
)

log = logging.getLogger("eap_worker")

# Child agent run ids: uuid5(CHILD_RUN_NAMESPACE, f"{run_id}:{step_id}") (stable across replays);
# inside a loop body `{run_id}:{S}:{step_id}` (eap_schemas.workflow_scope, the only builder).


def child_run_id(run_id: str, step_id: str, scope: Scope = EMPTY_SCOPE) -> str:
    """The run id (and DBOS workflow id) of the agent run executing agent step `step_id`."""
    return scoped_child_run_id(run_id, scope, step_id)


def branch_workflow_id(run_id: str, parallel_id: str, branch: str) -> str:
    return f"{run_id}:{parallel_id}:{branch}"


def side_effect_key(run_id: str, step_id: str, scope: Scope = EMPTY_SCOPE) -> str:
    """The side_effects key guarding a workflow tool step's single call."""
    return scoped_guard_key(run_id, scope, step_id)


def tool_call_id_for(step_id: str, scope: Scope = EMPTY_SCOPE) -> str:
    """ctx.tool_call_id a workflow tool step's tool sees (tickets.create then reserves
    `{run_id}:wf_{step_id}`, distinct from the step's guard key; `wf_{instance token}` in a loop)."""
    return scoped_tool_call_id(scope, step_id)


def approval_topic(step_id: str) -> str:
    return f"approval:{step_id}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def max_step_output_bytes() -> int:
    return _env_int("EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES", 32 * 1024)


def max_context_bytes() -> int:
    return _env_int("EAP_WORKFLOW_MAX_CONTEXT_BYTES", 1024 * 1024)


def max_run_seconds() -> int:
    return _env_int("EAP_WORKFLOW_MAX_RUN_SECONDS", 14 * 24 * 3600)


def hour_seconds() -> float:
    """Seconds per `timeout_hours` hour: 3600; EAP_WORKFLOW_HOUR_SECONDS shortens it for tests."""
    try:
        return max(0.1, float(os.environ.get("EAP_WORKFLOW_HOUR_SECONDS", "3600")))
    except ValueError:
        return 3600.0


def approval_poll_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get("EAP_APPROVAL_POLL_SECONDS", "3600")))
    except ValueError:
        return 3600.0


def read_limits() -> dict[str, Any]:
    """Every environment knob of a run, read ONCE inside eap.start_workflow_run and checkpointed
    with it; workflow code (and branch workflows, which receive it as an argument) only ever uses
    this recorded copy, so a restart with other values never changes a replayed run's path."""
    return {
        "max_step_output_bytes": max_step_output_bytes(),
        "max_context_bytes": max_context_bytes(),
        "max_run_seconds": max_run_seconds(),
        "hour_seconds": hour_seconds(),
        "approval_poll_seconds": approval_poll_seconds(),
        **{key: _env_int(env, default) for key, (env, default) in WORKFLOW_LOOP_LIMIT_DEFAULTS.items()},
    }


def loop_limit(limits: dict[str, Any], key: str) -> int:
    """A loop knob from a run's recorded `limits`, or its default when the run was started before
    the knob existed (loops D10: never a KeyError on an old checkpoint)."""
    value = limits.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else (
        WORKFLOW_LOOP_LIMIT_DEFAULTS[key][1]
    )


# --------------------------------------------------------------------------- steps

_VERSION = text("SELECT workflow_id, version, spec FROM workflow_versions WHERE id = :id AND tenant_id = :t")
# The agent's archived flag (no row: not this tenant's agent). An archived agent is reported as such
# (`agent_archived`), not as missing.
_AGENT = text("SELECT archived FROM agents WHERE id = :a AND tenant_id = :t")
_AGENT_VERSION = text(
    "SELECT version, spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t AND version = :v"
)
_AGENT_LATEST = text(
    "SELECT version, spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t ORDER BY version DESC LIMIT 1"
)


@DBOS.step(name="eap.start_workflow_run")
async def start_workflow_run_step(
    run_id: str, tenant_id: str, workflow_version_id: str, input_json: str
) -> dict[str, Any]:
    """Returns {"traceparent", "workflow_id", "workflow_version", "spec" (None: no such version of
    this tenant), "limits" (read_limits() plus "started_at", epoch seconds), "started"}.

    `started` is False when the run is already terminal (loops D12: e.g. cancelled before a worker
    picked it up; RunStore.mark_running refuses to revive it): the workflow then stops without an
    event or a step. A checkpoint recorded before LP.7 has no `started` key: it ran."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        with rt.engine.connect() as conn:
            row = conn.execute(_VERSION, {"id": workflow_version_id, "t": tenant_id}).fetchone()
        if row is not None:
            # The API inserts the row first; idempotent safety net (RunIdConflict for another tenant).
            rt.run_store.create_workflow_run(tenant_id, row.workflow_id, row.version, input_json, uuid.UUID(run_id))
        # running, clearing an ENQUEUE_FAILED_ERROR left by a hand-off that raised after DBOS accepted it.
        started = rt.run_store.mark_running(tenant_id, uuid.UUID(run_id))
        if started:
            rt.run_store.heartbeat(tenant_id, uuid.UUID(run_id))
        run = rt.run_store.get_run(tenant_id, uuid.UUID(run_id)) or {}
        return {
            "started": bool(started),
            "traceparent": run.get("traceparent"),
            "workflow_id": row.workflow_id if row is not None else run.get("workflow_id"),
            "workflow_version": row.version if row is not None else run.get("workflow_version"),
            "spec": dict(row.spec) if row is not None else None,
            "limits": {**read_limits(), "started_at": time.time()},
        }

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.workflow_step_begin")
async def step_begin_step(
    run_id: str, tenant_id: str, step_id: str, data: dict[str, Any], key: str | None = None
) -> float:
    """Record step_started (`step:{step_id}:started`, or the scoped `key` of a body step) and
    return the current time (epoch seconds)."""
    rt = get_runtime()
    key = key or f"step:{step_id}:started"
    await asyncio.to_thread(rt.event_log.append, tenant_id, uuid.UUID(run_id), key, "step_started", data)
    crash_point("after_event_write", event_key=key)
    return time.time()


@DBOS.step(name="eap.workflow_clock")
async def clock_step() -> float:
    return time.time()


@DBOS.step(name="eap.workflow_resolve_agent")
async def resolve_agent_step(
    run_id: str,
    tenant_id: str,
    step_id: str,
    agent_id: str,
    version: int | None,
    input_text: str,
    traceparent: str | None,
    scope_text: str = "",
) -> dict[str, Any]:
    """Resolve the agent version ONCE (pinned, or the latest published when None) and create the
    child agent run row. Returns {"found": False} (with "archived": True when the agent exists but
    is archived) or {"found": True, "version", "spec_json", "child_run_id"}; checkpointed, so a
    replay never re-resolves 'latest'.

    The agent must exist and not be archived only when the child row is CREATED. A re-executed
    step (killed after `after_child_run_insert`, before the checkpoint) finds the child row and
    reloads the version that row names, even if the agent has been archived since: the run
    already committed to that agent version.

    Inside a loop body (`scope_text` = the scope string, D4) the child is
    `uuid5("{run_id}:{S}:{step_id}")` with `parent_step_id` `{S}/{step_id}`; eap.start_run reads
    that `/` and runs the child with approval_mode "deny" (D8a)."""
    rt = get_runtime()
    scope = parse_scope(scope_text)
    child = child_run_id(run_id, step_id, scope)
    parent_step_id = scoped_parent_step_id(scope, step_id)

    def pinned(agent_version: Any) -> dict[str, Any]:
        with rt.engine.connect() as conn:
            row = conn.execute(_AGENT_VERSION, {"a": agent_id, "t": tenant_id, "v": agent_version}).fetchone()
        if row is None:
            return {"found": False}
        return {"found": True, "version": row.version, "spec_json": json.dumps(row.spec), "child_run_id": child}

    def work() -> dict[str, Any]:
        existing = rt.run_store.get_run(tenant_id, uuid.UUID(child))
        if existing is not None:
            return pinned(existing.get("agent_version"))
        with rt.engine.connect() as conn:
            agent = conn.execute(_AGENT, {"a": agent_id, "t": tenant_id}).fetchone()
            if agent is None:
                return {"found": False}
            if agent.archived:
                return {"found": False, "archived": True}
            if version is None:
                row = conn.execute(_AGENT_LATEST, {"a": agent_id, "t": tenant_id}).fetchone()
            else:
                row = conn.execute(_AGENT_VERSION, {"a": agent_id, "t": tenant_id, "v": version}).fetchone()
        if row is None:
            return {"found": False}
        _, created = rt.run_store.create_child_agent_run(
            tenant_id, agent_id, row.version, input_text, uuid.UUID(child), run_id, parent_step_id,
            traceparent=traceparent,
        )
        crash_point("after_child_run_insert", event_key=scoped_event_key(scope, step_id, "child"))
        if not created:
            # Lost a race with a concurrent attempt that inserted first: that row's version wins.
            existing = rt.run_store.get_run(tenant_id, uuid.UUID(child)) or {}
            if existing.get("agent_version") != row.version:
                return pinned(existing.get("agent_version"))
        return {"found": True, "version": row.version, "spec_json": json.dumps(row.spec), "child_run_id": child}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.workflow_tool_call", retries_allowed=False)
async def workflow_tool_call_step(
    run_id: str, tenant_id: str, step_id: str, tool_id: str, args: dict[str, Any], side_effect: bool,
    scope_text: str = "",
) -> Any:
    """Same semantics as eap.tool_call, guarded by `{run_id}:{step_id}`; crash point
    `after_workflow_tool_call` (event_key `step:{step_id}:call`). Inside a loop body (`scope_text`,
    D4) the guard is `{run_id}:{S}:{step_id}`, the tool call id `wf_{instance token}` and the crash
    key `loop:{S}:step:{step_id}:call`."""
    scope = parse_scope(scope_text)
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        call_tool_guarded,
        run_id,
        tenant_id,
        tool_id,
        args,
        side_effect,
        guard_key=side_effect_key(run_id, step_id, scope),
        tool_call_id=tool_call_id_for(step_id, scope),
        crash_name="after_workflow_tool_call",
        crash_key=scoped_event_key(scope, step_id, "call"),
        loop=loop,
    )


@DBOS.step(name="eap.workflow_approval_begin")
async def approval_begin_step(
    run_id: str, tenant_id: str, step_id: str, data: dict[str, Any], timeout_seconds: float
) -> float:
    """Compute the approval deadline ONCE (now + timeout_seconds, epoch seconds), record the
    step_waiting event carrying it, and return it; a replay reuses the recorded deadline.

    The event and its open pending_approvals row (kind `step`, the approvals inbox) are written in
    one transaction by EventLog.append_approval_waiting (0014_approvals); a repeated write returns
    the first event and adds no row. Same event key, type and data as before the projection."""
    rt = get_runtime()
    deadline = time.time() + timeout_seconds
    key = f"step:{step_id}:waiting"
    event, _created = await asyncio.to_thread(
        rt.event_log.append_approval_waiting, tenant_id, run_id, "step", step_id,
        {**data, "deadline": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()},
    )
    crash_point("after_event_write", event_key=key)
    # Re-executed after the event committed: the deadline is the one the event already shows.
    stored = event.data.get("deadline") if isinstance(event.data, dict) else None
    if isinstance(stored, str):
        deadline = datetime.fromisoformat(stored).timestamp()
    return deadline


@DBOS.step(name="eap.workflow_approval_decision")
async def approval_decision_step(run_id: str, tenant_id: str, step_id: str, timed_out: bool) -> dict[str, Any] | None:
    """The step's recorded decision {"type", "data"}, or None while there is none. With
    `timed_out`, records step_timed_out unless the API's decision got there first."""
    rt = get_runtime()

    def work() -> dict[str, Any] | None:
        event = rt.event_log.get_event(tenant_id, run_id, f"step:{step_id}:decision")
        if event is None and timed_out:
            _outcome, event = rt.event_log.append_step_decision(
                tenant_id, run_id, step_id, "step_timed_out", {"step_id": step_id}
            )
            if event is None:  # not waiting any more (cannot happen while this run waits): a timeout
                return {"type": "step_timed_out", "data": {"step_id": step_id}}
        return None if event is None else {"type": event.type, "data": dict(event.data)}

    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------- the interpreter


class StepFailed(Exception):
    """A workflow step failed; its step_failed event is already recorded. Workflow-side only."""

    def __init__(self, step_id: str, failure: RunFailure) -> None:
        self.step_id = step_id
        self.failure = failure
        super().__init__(step_id, failure.code)


def _agent_prompt(resolved: dict[str, Any]) -> str:
    values = list(resolved.values())
    if len(values) == 1 and isinstance(values[0], str):
        return values[0]
    return canonical_json(resolved) if resolved else ""


def agent_step_output(result: dict[str, Any]) -> Any:
    """An agent step's output (`steps.<id>.output`) from the child's completed result (AC.9): the
    validated object of a structured agent (`output_json`), else the final text. A child recorded
    before AC.4 carries no `output_json` key, so a recovered old run reads the same text."""
    if result.get("output_json") is not None:
        return result["output_json"]
    return result.get("output")


def _failure_status(exc: BaseException, failure: RunFailure) -> str:
    from eap_worker.workflow import _is_side_effect_unknown

    return "needs_attention" if _is_side_effect_unknown(exc) else failure.status


class _Interpreter:
    """Runs one graph of steps: the spec's top level, a parallel branch, or one loop iteration's
    body. `scope` is the chain of enclosing iterations (empty at the top level and in a parallel
    branch, so every key and id is exactly as before loops existed); `loops` holds their frames
    (`loop.<L>.index|item|state`, eap_worker.workflow_eval.with_loop_frame)."""

    def __init__(
        self,
        run_id: str,
        tenant_id: str,
        spec: WorkflowSpec,
        input_value: Any,
        outputs: dict[str, Any],
        limits: dict[str, Any],
        traceparent: str | None,
        *,
        steps: tuple[Any, ...] | None = None,
        scope: Scope = EMPTY_SCOPE,
        loops: dict[str, dict[str, Any]] | None = None,
        workflow_version_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.spec = spec
        self.spec_json = spec.model_dump(mode="json")
        self.by_id = {s.id: s for s in (spec.steps if steps is None else steps)}
        self.all_steps = {s.id: s for _, s in walk_steps(spec.steps)}  # every depth (ids are global)
        self.input = input_value
        self.outputs = dict(outputs)
        self.inherited = set(outputs)
        self.context_bytes = sum(json_size(v) for v in outputs.values())
        self.limits = limits
        self.traceparent = traceparent
        self.executed: set[str] = set()
        self.scope = scope
        self.scope_text = scope_string(scope)
        self.loops = loops or {}
        self.workflow_version_id = workflow_version_id
        self._warnings: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ scoped names (D4)

    def key(self, step_id: str, phase: str) -> str:
        return scoped_event_key(self.scope, step_id, phase)

    def _scoped(self, data: dict[str, Any]) -> dict[str, Any]:
        """Event data of a body step carries `scope` (D4); a top-level step's data is unchanged."""
        if self.scope:
            data["scope"] = scope_to_data(self.scope)
        return data

    def context(self) -> dict[str, Any]:
        return build_context(self.input, self.outputs, self.loops)

    def _warn(self, condition: str, **extra: Any) -> None:
        """A condition came out UNKNOWN (a MISSING operand, D9): recorded with the step's event and
        logged (paths and positions only, never values)."""
        warning = {"code": "condition_unknown", "condition": condition, **extra}
        self._warnings.append(warning)
        log.warning(
            "workflow condition is unknown (a $ref did not resolve)",
            extra={"run_id": self.run_id, "scope": self.scope_text, "condition": condition},
        )

    async def run_path(self, start: str, stop: str | None) -> tuple[str | None, Any]:
        """Run steps from `start` until `stop` (a parallel's join) or a step with no next.
        Returns (last step id, its output)."""
        current: str | None = start
        last: tuple[str | None, Any] = (None, None)
        while current is not None and current != stop:
            step = self.by_id[current]
            output = await self.run_step(step)
            last = (step.id, output)
            if isinstance(step, BranchStep):
                current = output["next"]
            elif isinstance(step, ParallelStep):
                current = step.join
            else:
                current = step.next
        return last

    async def run_step(self, step: Any) -> Any:
        step_id = step.id
        self._warnings = []
        data: dict[str, Any] = {"step_id": step_id, "step_type": step.type}
        problem: WorkflowStepError | None = None
        if step_id in self.executed or len(self.executed) >= MAX_STEPS:
            problem = WorkflowStepError("step_limit_exceeded", step_id)
        elif self.scope and isinstance(step, (ApprovalStep, ParallelStep)):
            problem = WorkflowStepError("loop_body_step_not_allowed", step_id)  # the schema forbids it
        self.executed.add(step_id)
        resolved: dict[str, Any] | None = None
        if isinstance(step, (AgentStep, ToolStep)) and problem is None:
            try:
                resolved = resolve_template(step.input, self.context())
                data["input"] = resolved
            except RefMissing:
                problem = WorkflowStepError("input_ref_missing", step_id)
        if isinstance(step, AgentStep):
            data.update(agent_id=str(step.agent_id), agent_version=step.version,
                        child_run_id=child_run_id(self.run_id, step_id, self.scope))
        elif isinstance(step, ToolStep):
            data["tool_id"] = step.tool_id
        started_key = self.key(step_id, "started")
        now = await step_begin_step(
            self.run_id, self.tenant_id, step_id, self._scoped(data), started_key if self.scope else None
        )
        crash_point("workflow_mid_step", event_key=started_key)
        try:
            if problem is not None:
                raise problem
            if now - self.limits["started_at"] > self.limits["max_run_seconds"]:
                raise WorkflowStepError("workflow_deadline_exceeded", step_id)
            output = await self._execute(step, resolved)
            try:
                size = json_size(output)
            except ValueError:
                raise WorkflowStepError("step_output_not_json", step_id) from None
            cap = (
                loop_limit(self.limits, "max_loop_output_bytes")
                if isinstance(step, LOOP_STEP_TYPES)
                else self.limits["max_step_output_bytes"]
            )
            if size > cap:
                raise WorkflowStepError("step_output_too_large", step_id)
            if self.context_bytes + size > self.limits["max_context_bytes"]:
                raise WorkflowStepError("workflow_context_too_large", step_id)
        except StepFailed as inner:
            # A nested failure (a parallel branch, a loop iteration): this step fails with it.
            await self._record_failure(step_id, inner.failure)
            raise StepFailed(step_id, inner.failure) from None
        except Exception as exc:  # noqa: BLE001 - every step failure is a durable step_failed
            failure = classify(exc)
            status = _failure_status(exc, failure)
            failure = RunFailure(status, failure.code, failure.error_type, failure.message)
            log_failure(
                log, f"workflow step {status}", exc, code=failure.code, error_type=failure.error_type,
                run_id=self.run_id, tenant_id=self.tenant_id,
            )
            await self._record_failure(step_id, failure)
            raise StepFailed(step_id, failure) from None
        if not isinstance(step, ParallelStep):  # a parallel step has no readable output
            self.context_bytes += size
            self.outputs[step_id] = output
        completed: dict[str, Any] = {"step_id": step_id, "output": output}
        unresolved = self.unresolved_output_refs(step) if isinstance(step, BranchStep) else []
        if unresolved:
            # AC.9: carried in step_completed (no new durable step, so the step layout is unchanged).
            completed["unresolved_refs"] = unresolved
            log.warning(
                "workflow branch read missing structured fields",
                extra={"run_id": self.run_id, "step_id": step_id, "paths": unresolved},
            )
        if self._warnings:
            # LP.7: `condition_unknown` (D9), on the existing event (no new durable step).
            completed["warnings"] = list(self._warnings)
        await append_event_step(
            self.run_id, self.tenant_id, self.key(step_id, "completed"), "step_completed", self._scoped(completed),
        )
        return output

    def unresolved_output_refs(self, step: Any) -> list[str]:
        """The step's `$ref` paths into a field of an agent step's STRUCTURED output (an object or
        array) that resolve to nothing, in spec order, without duplicates. A pure function of
        recorded outputs. Such a path usually means the (unpinned) agent's output schema changed;
        the condition is false, as for any missing ref, and the branch takes its default."""
        context = self.context()
        found: list[str] = []
        for path in dict.fromkeys(iter_refs(step)):
            parts = path.split(".")
            if parts[0] != "steps" or len(parts) < 4:
                continue
            target = self.all_steps.get(parts[1])
            if not isinstance(target, AgentStep) or parts[1] not in self.outputs:
                continue
            if not isinstance(self.outputs[parts[1]], (dict, list)):
                continue  # a text agent's output: publish warns for field refs into it
            if resolve_ref(context, path) is MISSING:
                found.append(path)
        return found

    async def _record_failure(self, step_id: str, failure: RunFailure) -> None:
        await append_event_step(
            self.run_id, self.tenant_id, self.key(step_id, "failed"), "step_failed",
            self._scoped(
                {"step_id": step_id, "status": failure.status, "error": failure.error, "error_code": failure.code}
            ),
        )

    async def _execute(self, step: Any, resolved: dict[str, Any] | None) -> Any:
        if isinstance(step, AgentStep):
            return await self._agent(step, resolved or {})
        if isinstance(step, ToolStep):
            return await self._tool(step, resolved or {})
        if isinstance(step, BranchStep):
            context = self.context()
            for index, case in enumerate(step.cases):
                result = outcome(case.when, context)
                if result == "true":
                    return {"case": index, "next": case.next}
                if result == "unknown":
                    self._warn(f"cases.{index}")
            return {"case": None, "next": step.default}
        if isinstance(step, LoopStep):
            return await self._loop(step)
        if isinstance(step, ForeachStep):
            return await self._foreach(step)
        if isinstance(step, ParallelStep):
            return await self._parallel(step)
        if isinstance(step, ApprovalStep):
            return await self._approval(step)
        # evaluate (LP.14) is refused at publish until its runtime lands; a version that has one
        # anyway fails like a spec an old worker cannot run.
        raise WorkflowStepError("workflow_spec_invalid", step.id)

    async def _agent(self, step: AgentStep, resolved: dict[str, Any]) -> Any:
        prompt = _agent_prompt(resolved)
        found = await resolve_agent_step(
            self.run_id, self.tenant_id, step.id, str(step.agent_id), step.version, prompt, self.traceparent,
            *((self.scope_text,) if self.scope else ()),
        )
        if not found.get("found"):
            raise WorkflowStepError("agent_archived" if found.get("archived") else "agent_not_found", step.id)
        child = found["child_run_id"]
        with SetWorkflowID(child):
            handle = await DBOS.start_workflow_async(
                agent_run, child, self.tenant_id, str(step.agent_id), found["version"], found["spec_json"], prompt
            )
        result = await handle.get_result()
        if result.get("status") != "completed":
            raise WorkflowStepError(
                str(result.get("error_code") or "internal_error"), step.id,
                str(result.get("status") or "failed"), result.get("error"),
            )
        return agent_step_output(result)

    async def _tool(self, step: ToolStep, resolved: dict[str, Any]) -> Any:
        check = await check_tool_step(step.tool_id, resolved)
        if check["rejected"] is not None:
            raise _REJECTIONS[check["rejected"]](step.tool_id)
        return await workflow_tool_call_step(
            self.run_id, self.tenant_id, step.id, step.tool_id, check["args"], bool(check["side_effect"]),
            *((self.scope_text,) if self.scope else ()),
        )

    # ------------------------------------------------------------------ loop (while / until)

    async def _loop(self, step: LoopStep) -> Any:
        """A while/until loop (D2, D3, D5, D6). The parent evaluates the initial `state` and the
        `while`; each iteration is one `eap.workflow_iteration` child workflow that runs the body
        and evaluates `carry` then `until` itself, and returns references (`last_key`), never body
        outputs. Per iteration the parent records the child's start, its result and
        `loop:{S}:completed|failed`. Output: {iterations, exited, last, state}; `last` (the output
        of the body step that ended the last iteration's path) is read once, at the end, through
        the immutable event read, and recorded once, in this step's step_completed."""
        if self.workflow_version_id is None:  # a parallel branch: the schema forbids loops there
            raise WorkflowStepError("loop_in_parallel_not_allowed", step.id)
        context = self.context()
        try:
            state = initial_state(step, context)
        except RefMissing:
            raise WorkflowStepError("input_ref_missing", step.id) from None
        iterations = 0
        exited: str | None = None
        last_key: str | None = None
        warned: set[str] = set()
        for index in range(step.max_iterations + 1):
            if step.while_ is not None:
                result = while_outcome(step, context, index, state)
                if result == "unknown" and "while" not in warned:
                    warned.add("while")
                    self._warn("while", index=index)
                if result != "true":
                    exited = "condition"
                    break
            if index == step.max_iterations:
                break  # the cap, with the condition still asking for another iteration
            self._check_state(step, state)
            outcome_ = await self._iteration(step, index, state)
            iterations += 1
            state = outcome_["state"]
            last_key = outcome_["last_key"]
            if outcome_.get("until") == "unknown" and "until" not in warned:
                warned.add("until")
                self._warn("until", index=index)
            if outcome_.get("done"):
                exited = "condition"
                break
        if exited is None:
            if step.on_max_iterations == "fail":
                raise WorkflowStepError("loop_max_iterations", step.id)
            exited = "max_iterations"
        last = await self._read_last(step, last_key)
        return {"iterations": iterations, "exited": exited, "last": last, "state": state}

    def _check_state(self, step: Any, state: Any) -> None:
        try:
            size = json_size(state)
        except ValueError:
            raise WorkflowStepError("step_output_not_json", step.id) from None
        if size > loop_limit(self.limits, "max_loop_state_bytes"):
            raise WorkflowStepError("loop_state_too_large", step.id)

    async def _iteration(self, step: LoopStep, index: int, state: dict[str, Any]) -> dict[str, Any]:
        """Start iteration `index` (a child workflow taking references, D6), await it, and record
        `loop:{S}:completed` or `loop:{S}:failed`. Returns the child's result; a failed iteration
        raises StepFailed with the failure of the body step that failed (its code and status)."""
        handle = await self._start_iteration(step, index, state)
        result = await self._finish_iteration(step, index, handle)
        if result.get("status") != "completed":
            raise StepFailed(step.id, _result_failure(result))
        return result

    async def _start_iteration(self, step: ForeachStep | LoopStep, index: int, state: dict[str, Any] | None) -> Any:
        """Start iteration `index` of `step` as an `eap.workflow_iteration` child (D5, D6): the
        arguments are references only (a foreach passes `state` None; its item is read by the
        iteration itself)."""
        scope = child_scope(self.scope, step.id, index)
        with SetWorkflowID(iteration_workflow_id(self.run_id, scope)):
            return await DBOS.start_workflow_async(
                workflow_iteration, self.run_id, self.tenant_id, self.workflow_version_id,
                [*loop_path(self.scope), step.id], scope_to_data(self.scope), index, state, self.limits,
                self.traceparent,
            )

    async def _finish_iteration(self, step: ForeachStep | LoopStep, index: int, handle: Any) -> dict[str, Any]:
        """Await an iteration and record `loop:{S}:completed` or `loop:{S}:failed`. Returns the
        child's result, or {status, error, error_code, step_id, cancelled} for an iteration that
        failed, raised, or was cancelled (`cancelled`: True only for an awaited child DBOS
        cancelled, which is `run_cancelled`, loops D7)."""
        scope = child_scope(self.scope, step.id, index)
        try:
            result = await handle.get_result()
        except Exception as exc:  # noqa: BLE001 - the child raised or was cancelled
            cancelled = type(exc).__name__ == "DBOSAwaitedWorkflowCancelledError"
            if cancelled:
                failure = classify(WorkflowStepError("run_cancelled", step.id))
            else:
                failure = classify(exc)
                failure = RunFailure(_failure_status(exc, failure), failure.code, failure.error_type, failure.message)
            result = {"status": failure.status, "error": failure.error, "error_code": failure.code, "step_id": None,
                      "cancelled": cancelled}
        base = {"step_id": step.id, "scope": scope_to_data(self.scope), "index": index}
        if result.get("status") != "completed":
            await append_event_step(
                self.run_id, self.tenant_id, iteration_event_key(scope, "failed"), "loop_iteration_failed",
                {**base, "status": result["status"], "error": result["error"], "error_code": result["error_code"],
                 "failed_step_id": result.get("step_id")},
            )
            return result
        if isinstance(step, ForeachStep):
            completed = {**base, "last_key": result["last_key"]}
        else:
            completed = {**base, "state": result["state"], "last_key": result["last_key"]}
            if step.until is not None:
                completed["until"] = result.get("until")
        await append_event_step(
            self.run_id, self.tenant_id, iteration_event_key(scope, "completed"), "loop_iteration_completed",
            completed,
        )
        return result

    # ------------------------------------------------------------------ foreach (LP.10)

    async def _foreach(self, step: ForeachStep) -> Any:
        """A foreach (D2, D3, D7): one `eap.workflow_iteration` child per item, at most
        `concurrency` at a time, in a sliding window in item order: start items 0..c-1, then await
        item i (recording `loop:{S}:completed|failed`) and start item i+c. The parent's operation
        order is therefore a function of the item count and the recorded results only, never of
        timing. Under `on_item_error: fail` the first failed item stops new starts; every started
        item is still awaited, then the step fails with that item's failure. Under `collect` a
        failed item's place in the output is `{error_code, error}`. Never collected: an item whose
        child was cancelled (`run_cancelled`, which wins over every other failure) and an item
        that ended `needs_attention` (a side effect of unknown outcome), which fail the step as
        under `fail`. Output: the list of each item's `last` (the output of the body step that
        ended its path), read once at the end through the immutable event read."""
        if self.workflow_version_id is None:  # a parallel branch: the schema forbids loops there
            raise WorkflowStepError("loop_in_parallel_not_allowed", step.id)
        try:
            items = foreach_items(step, self.context())
        except RefMissing:
            raise WorkflowStepError("input_ref_missing", step.id) from None
        count = len(items)
        width = min(step.concurrency, loop_limit(self.limits, "max_run_concurrency"))
        handles: dict[int, Any] = {}
        started = 0
        while started < min(width, count):
            handles[started] = await self._start_iteration(step, started, None)
            started += 1
        entries: list[tuple[str, Any]] = []  # ("key", last_key) or ("error", entry), in item order
        stopping: dict[str, Any] | None = None  # the failure that stops new starts
        cancelled: dict[str, Any] | None = None
        try:
            for index in range(count):
                if index >= started:
                    break  # stopped: every started item has been awaited
                result = await self._finish_iteration(step, index, handles.pop(index))
                if result.get("status") == "completed":
                    entries.append(("key", result.get("last_key")))
                elif result.get("cancelled"):
                    cancelled = cancelled or result
                elif step.on_item_error == "collect" and result.get("status") != "needs_attention":
                    entries.append(("error", {"error_code": result["error_code"], "error": result["error"]}))
                else:
                    stopping = stopping or result
                if stopping is None and cancelled is None and started < count:
                    handles[started] = await self._start_iteration(step, started, None)
                    # The refill's start is recorded; the parent has not awaited it yet.
                    refill = scope_string(child_scope(self.scope, step.id, started))
                    crash_point("foreach_refill", event_key=f"loop:{refill}:refill")
                    started += 1
        except DBOSWorkflowCancelledError:
            # This workflow itself was cancelled: cancel the started items it has not awaited (the
            # cascade does too; best effort, as in `_parallel`). ONLY this error: a plain
            # asyncio.CancelledError is a graceful worker shutdown (DBOS.destroy cancels every
            # task), after which the run must resume on another worker with its items intact, and
            # an ordinary Exception is a failure of this step, not a reason to cancel the items.
            for other in handles.values():
                try:
                    await DBOS.cancel_workflow_async(other.get_workflow_id())
                except BaseException:  # noqa: BLE001 - best effort while being cancelled
                    pass
            raise
        if cancelled is not None:
            raise StepFailed(step.id, _result_failure(cancelled))
        if stopping is not None:
            raise StepFailed(step.id, _result_failure(stopping))
        return await self._read_entries(step, entries)

    async def _read_entries(self, step: ForeachStep, entries: list[tuple[str, Any]]) -> list[Any]:
        """The foreach output in item order: each completed item's `last` (one immutable read for
        all of them), or its error entry. A `last_key` whose event is missing (D6 says it was
        committed before the item returned) fails the step `internal_error`, never a silent null."""
        keys = [value for kind, value in entries if kind == "key" and value is not None]
        events = (
            await immutable_read(get_runtime().event_log.read_outputs, self.tenant_id, self.run_id, keys)
            if keys else {}
        )
        output: list[Any] = []
        for kind, value in entries:
            if kind == "error":
                output.append(value)
            elif value is None:
                output.append(None)  # an empty path recorded no step (not reachable from the schema)
            else:
                event = events.get(value)
                if event is None:
                    raise _missing_last(step.id)
                output.append((event.data or {}).get("output"))
        return output

    async def _read_last(self, step: LoopStep, last_key: str | None) -> Any:
        """The output recorded by the event `last_key` (D6's immutable read: events are
        append-only and idempotent by key, and this one committed before the iteration returned).
        A missing event fails the step `internal_error` (`_missing_last`)."""
        if last_key is None:
            return None
        events = await immutable_read(get_runtime().event_log.read_outputs, self.tenant_id, self.run_id, [last_key])
        event = events.get(last_key)
        if event is None:
            raise _missing_last(step.id)
        return (event.data or {}).get("output")

    async def _parallel(self, step: ParallelStep) -> Any:
        # Branches are child workflows: each has its own deterministic step sequence, so running
        # them concurrently never makes this workflow's own step order depend on timing. They are
        # started in declaration order and their results awaited in that order (both recorded).
        handles = []
        for branch in step.branches:
            with SetWorkflowID(branch_workflow_id(self.run_id, step.id, branch)):
                handles.append(
                    await DBOS.start_workflow_async(
                        workflow_branch, self.run_id, self.tenant_id, self.spec_json, self.input,
                        dict(self.outputs), branch, step.join, self.limits, self.traceparent,
                    )
                )
        # Await EVERY branch before deciding anything, so no branch is still running (and calling
        # tools) once this run is terminal. A branch whose result raises (e.g. it was cancelled)
        # counts as failed. If this workflow itself is being cancelled (DBOSWorkflowCancelledError,
        # a BaseException), the remaining branches are cancelled too before it propagates. Any other
        # BaseException, notably the asyncio CancelledError DBOS.destroy sends at a graceful worker
        # shutdown (SIGTERM), propagates untouched: the branches stay pending and are recovered
        # with this workflow (cancelling them would record cancel operations recovery cannot replay).
        results: list[dict[str, Any]] = []
        for index, handle in enumerate(handles):
            try:
                results.append(await handle.get_result())
            except Exception as exc:  # noqa: BLE001 - a failed branch, decided after all settle
                failure = classify(exc)
                results.append({"status": _failure_status(exc, failure), "error": failure.error,
                                "error_code": failure.code, "step_id": None})
            except DBOSWorkflowCancelledError:
                # Only this workflow's own cancel, never asyncio.CancelledError (a graceful worker
                # shutdown, after which the branches must resume on recovery), see `_foreach`.
                for other in handles[index + 1:]:
                    try:
                        await DBOS.cancel_workflow_async(other.get_workflow_id())
                    except (Exception, DBOSWorkflowCancelledError):  # noqa: BLE001 - best effort while being cancelled
                        pass
                raise
        failed = [r for r in results if r.get("status") != "completed"]
        if failed:
            worst = next((r for r in failed if r.get("status") == "needs_attention"), failed[0])
            raise StepFailed(
                step.id,
                RunFailure(worst["status"], worst["error_code"], *_split_error(worst["error"])),
            )
        # A parallel step has no output of its own (eap_schemas.workflows): from the join on, the
        # steps its branches ran (those that end each branch included) are readable.
        for result in results:
            for sid, value in result["outputs"].items():
                self.outputs[sid] = value
                self.context_bytes += json_size(value)
        # Each branch checked its own outputs against the cap from the parent's context; together
        # they may still exceed it. Fail the parallel step here, before the join or any later step.
        if self.context_bytes > self.limits["max_context_bytes"]:
            raise WorkflowStepError("workflow_context_too_large", step.id)
        return None

    async def _approval(self, step: ApprovalStep) -> Any:
        timeout_seconds = step.timeout_hours * self.limits["hour_seconds"]
        deadline = await approval_begin_step(
            self.run_id, self.tenant_id, step.id,
            {
                "step_id": step.id,
                "prompt": step.prompt,
                "timeout_hours": step.timeout_hours,
                # The DBOS workflow that waits (the run, or a parallel branch's child workflow):
                # the API sends the wake-up here.
                "workflow_id": DBOS.workflow_id,
            },
            timeout_seconds,
        )
        crash_point("workflow_waiting", event_key=f"step:{step.id}:waiting")
        poll = self.limits["approval_poll_seconds"]
        timed_out = False
        max_rounds = math.ceil(timeout_seconds / poll) + 16
        decision: dict[str, Any] | None = None
        for _ in range(max_rounds):
            decision = await approval_decision_step(self.run_id, self.tenant_id, step.id, timed_out)
            if decision is not None:
                break
            remaining = deadline - await clock_step()
            if remaining <= 0:
                timed_out = True
                continue
            await DBOS.recv_async(approval_topic(step.id), timeout_seconds=min(remaining, poll))
        if decision is None:
            decision = await approval_decision_step(self.run_id, self.tenant_id, step.id, True)
        kind = (decision or {}).get("type")
        info = (decision or {}).get("data") or {}
        if kind == "step_approved":
            return {"approved": True, "decided_by": info.get("decided_by"), "comment": info.get("comment")}
        if kind == "step_rejected":
            raise WorkflowStepError("approval_rejected", step.id)
        raise WorkflowStepError("approval_timeout", step.id)


def _missing_last(step_id: str) -> WorkflowStepError:
    """A loop's `last_key` names an event that is not there (D6: impossible unless the event log
    lost a row): an internal error, so the run fails instead of reporting a wrong (null) output."""
    return WorkflowStepError(
        "internal_error", step_id, "failed", "WorkflowStepError: a loop iteration's last output event is missing"
    )


def _result_failure(result: dict[str, Any]) -> RunFailure:
    """The RunFailure of a failed child result ({status, error, error_code, ...})."""
    return RunFailure(result["status"], result["error_code"], *_split_error(result["error"]))


def _split_error(error: str) -> tuple[str, str]:
    error_type, _, message = str(error).partition(": ")
    return error_type, message


def _schema_validator(schema: dict[str, Any]) -> Any:
    import jsonschema
    from referencing import Registry

    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=Registry())  # empty registry: no remote $ref is ever retrieved


def final_output(spec: WorkflowSpec, output: Any) -> Any:
    """The run's output (see the module docstring); WorkflowStepError('output_invalid'), also for
    a value with a non-finite number (an agent answering "NaN" to a number schema)."""
    if spec.output_schema is None:
        return output
    try:
        validator = _schema_validator(spec.output_schema)
        candidates = [output]
        if isinstance(output, str):
            try:
                candidates.append(json.loads(output))
            except ValueError:
                pass
    except Exception:  # noqa: BLE001 - an invalid schema, unresolvable $ref, non-JSON text
        raise WorkflowStepError("output_invalid") from None
    for value in candidates:
        try:
            if non_finite_path(value) is None and validator.is_valid(value):
                return value
        except Exception:  # noqa: BLE001 - validation itself failed (e.g. a $ref it cannot resolve)
            continue
    raise WorkflowStepError("output_invalid")


# --------------------------------------------------------------------------- the workflows


@DBOS.workflow(name=WORKFLOW_RUN_NAME)
async def workflow_run(run_id: str, tenant_id: str, workflow_version_id: str, input_json: str) -> dict[str, Any]:
    with log_context(tenant_id=tenant_id, run_id=run_id):  # records before run_span carry the ids too
        link = await start_workflow_run_step(run_id, tenant_id, workflow_version_id, input_json)
        if link.get("started") is False:
            # Already terminal (loops D12: cancelled before any worker ran it): never revived, no
            # event, no step. The run's `end` (written by the cancel) fixes its status.
            log.warning("workflow run %s not started: the run is already terminal", run_id)
            return {"status": "failed", "error_code": "run_not_started"}
    with run_span(run_id, tenant_id, "", "", traceparent=link.get("traceparent")) as span:
        span.set_attribute("eap.run_kind", "workflow")
        span.set_attribute("eap.workflow_id", str(link.get("workflow_id") or ""))
        log.info("workflow run %s started: tenant=%s workflow=%s", run_id, tenant_id, link.get("workflow_id"))
        result = await _workflow_body(run_id, tenant_id, input_json, link, workflow_version_id)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("workflow run %s %s", run_id, result["status"])
        return result


async def _workflow_body(
    run_id: str, tenant_id: str, input_json: str, link: dict[str, Any], workflow_version_id: str | None = None
) -> dict[str, Any]:
    input_value = json.loads(input_json)
    if non_finite_path(input_value) is not None:
        # Defence in depth (the API refuses it): NaN/Infinity are not JSON and JSONB rejects them,
        # so the input is left out of run_started and the run fails before any step.
        input_value = None
        bad_input = True
    else:
        bad_input = False
    await append_event_step(
        run_id, tenant_id, "start", "run_started",
        {
            "kind": "workflow",
            "workflow_id": link.get("workflow_id"),
            "workflow_version": link.get("workflow_version"),
            "input": input_value,
        },
    )
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    failed_step: str | None = None
    try:
        if bad_input:
            raise WorkflowStepError("input_invalid")
        if link.get("spec") is None:
            raise WorkflowStepError("workflow_version_not_found")
        try:
            spec = WorkflowSpec.model_validate(link["spec"])
        except ValidationError:
            raise WorkflowStepError("workflow_spec_invalid") from None
        _check_run_bounds(spec, link["limits"])
        interp = _Interpreter(
            run_id, tenant_id, spec, input_value, {}, link["limits"], link.get("traceparent"),
            workflow_version_id=workflow_version_id,
        )
        _last_step, last_output = await interp.run_path(spec.start, None)
        output = final_output(spec, last_output)
        output_text = canonical_json(output)
    except StepFailed as exc:
        failure, failed_step = exc.failure, exc.step_id
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        failure = classify(exc)
        failure = RunFailure(_failure_status(exc, failure), failure.code, failure.error_type, failure.message)
        log_failure(
            log, f"workflow run {failure.status}", exc, code=failure.code, error_type=failure.error_type,
            run_id=run_id, tenant_id=tenant_id,
        )
    else:
        heartbeat.cancel()
        # End event before the terminal status, as in agent and tool runs.
        await append_event_step(run_id, tenant_id, "end", "run_completed", {"output": output_text, "result": output})
        await finish_run_step(run_id, tenant_id, "completed", output_text, None)
        return {"status": "completed", "output": output_text}
    finally:
        heartbeat.cancel()
    await append_event_step(
        run_id, tenant_id, "end", "run_failed",
        {"status": failure.status, "error": failure.error, "error_code": failure.code, "step_id": failed_step},
    )
    await finish_run_step(run_id, tenant_id, failure.status, None, failure.error)
    return {"status": failure.status, "error": failure.error, "error_code": failure.code, "step_id": failed_step}


@DBOS.workflow(name=WORKFLOW_BRANCH_NAME)
async def workflow_branch(
    run_id: str,
    tenant_id: str,
    spec_json: dict[str, Any],
    input_value: Any,
    outputs: dict[str, Any],
    branch: str,
    stop: str,
    limits: dict[str, Any],
    traceparent: str | None,
) -> dict[str, Any]:
    """One parallel branch: runs `branch` up to `stop` (the join) in the parent's run (same events,
    same run row). Never raises for a step failure: returns {"status": "completed", "outputs":
    {new step outputs}, "last": <last output>} or {"status", "error", "error_code", "step_id"}."""
    try:
        spec = WorkflowSpec.model_validate(spec_json)
        interp = _Interpreter(run_id, tenant_id, spec, input_value, outputs, limits, traceparent)
        _last_step, last = await interp.run_path(branch, stop)
    except StepFailed as exc:
        f = exc.failure
        return {"status": f.status, "error": f.error, "error_code": f.code, "step_id": exc.step_id}
    except Exception as exc:  # noqa: BLE001
        f = classify(exc)
        return {"status": _failure_status(exc, f), "error": f.error, "error_code": f.code, "step_id": None}
    new = {k: v for k, v in interp.outputs.items() if k not in interp.inherited}
    return {"status": "completed", "outputs": new, "last": last}


# --------------------------------------------------------------------------- loop iterations (D5, D6)


def _check_run_bounds(spec: WorkflowSpec, limits: dict[str, Any]) -> None:
    """A guard for a spec with loops (LP.5 refuses these at publish; recorded knobs, so a replay
    decides the same): the static execution bound and the run concurrency (D7a, D10). A loop-free
    spec is never checked, so its path and a pre-upgrade run's recorded `limits` are untouched."""
    if not any(isinstance(s, LOOP_STEP_TYPES) for _, s in walk_steps(spec.steps)):
        return
    if static_execution_bound(spec) > loop_limit(limits, "max_step_executions"):
        raise WorkflowStepError("workflow_too_many_executions")
    if run_concurrency(spec) > loop_limit(limits, "max_run_concurrency"):
        raise WorkflowStepError("workflow_concurrency_exceeded")


def _find_loop(spec: WorkflowSpec, path: list[str]) -> ForeachStep | LoopStep:
    """The loop step at `path` (loop ids from the top, D6's `loop_path`)."""
    steps: tuple[Any, ...] = spec.steps
    found: Any = None
    for loop_id in path:
        found = next((s for s in steps if s.id == loop_id and isinstance(s, LOOP_STEP_TYPES)), None)
        if found is None:
            raise WorkflowStepError("workflow_spec_invalid", loop_id)
        steps = found.body.steps
    if found is None:
        raise WorkflowStepError("workflow_spec_invalid")
    return found


# D6's immutable reads are not DBOS steps (recording them would copy the spec and the read outputs
# into every iteration's checkpoint), so DBOS never retries them. A transient database error
# (a dropped connection, a failover, the pool timing out) is retried here instead, with backoff:
# about two minutes in all before the error is let through. The reads are idempotent and their
# result never depends on when they succeed (immutable rows), so retrying cannot change a path.
IMMUTABLE_READ_DELAYS = (0.2, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)


def _transient_db_error(exc: BaseException) -> bool:
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    if isinstance(exc, (OperationalError, InterfaceError, PoolTimeout)):
        return True
    return isinstance(exc, DBAPIError) and bool(exc.connection_invalidated)


async def immutable_read(fn: Any, *args: Any) -> Any:
    """Run the blocking immutable read `fn(*args)` in a thread, retrying transient database errors
    (IMMUTABLE_READ_DELAYS). Any other error, or the last transient one, is raised."""
    for delay in (*IMMUTABLE_READ_DELAYS, None):
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:  # noqa: BLE001 - only transient database errors are retried
            if delay is None or not _transient_db_error(exc):
                raise
            log.warning(
                "workflow loop read failed, retrying",
                extra={"error_type": type(exc).__name__, "retry_in_s": delay},
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _read_iteration_inputs(
    tenant_id: str, run_id: str, workflow_version_id: str, path: list[str], outer: Scope
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """D6's immutable read, NOT a recorded step: the published spec (versions are never edited)
    and the run's events this iteration reads, by key (events are append-only and idempotent by
    key, and each of these was committed before the parent recorded this child's start), so a
    replay reads the same bytes. Returns (spec JSON or None, {event_key: data})."""
    rt = get_runtime()
    with rt.engine.connect() as conn:
        row = conn.execute(_VERSION, {"id": workflow_version_id, "t": tenant_id}).fetchone()
    if row is None:
        return None, {}
    try:
        spec = WorkflowSpec.model_validate(dict(row.spec))
        step = _find_loop(spec, path)
    except (ValidationError, WorkflowStepError):
        return dict(row.spec), {}
    keys = ["start", *_body_read_keys(spec, step, outer).values(), *_frame_keys(outer)]
    if isinstance(step, ForeachStep):
        keys += _items_read_keys(spec, step, outer).values()
    events = rt.event_log.read_outputs(tenant_id, run_id, keys)
    return dict(row.spec), {k: dict(e.data or {}) for k, e in events.items()}


def _body_read_keys(spec: WorkflowSpec, step: Any, outer: Scope) -> dict[str, str]:
    """{step id: its `step:{id}:completed` (or scoped) key} for the outputs one iteration of
    `step` reads (`body_reads`): a step inside enclosing loops sits in the matching prefix of
    `outer` (the same iteration of each enclosing loop)."""
    enclosing = {s.id: loops for loops, s in walk_steps(spec.steps)}
    keys: dict[str, str] = {}
    for step_id in body_reads(step):
        loops = enclosing.get(step_id)
        if loops is not None:
            keys[step_id] = scoped_event_key(outer[: len(loops)], step_id, "completed")
    return keys


def _items_read_keys(spec: WorkflowSpec, step: ForeachStep, outer: Scope) -> dict[str, str]:
    """{step id: its completed key} of the step a foreach's `items` reads, if it reads one (an
    `input` or `loop.<L>` items ref is served by the `start` and `loop:{S}:started` reads)."""
    root, name, _ = parse_ref(step.items[REF_KEY])
    if root != "steps" or name is None:
        return {}
    loops = {s.id: loops for loops, s in walk_steps(spec.steps)}.get(name)
    return {} if loops is None else {name: scoped_event_key(outer[: len(loops)], name, "completed")}


def _iteration_item(
    spec: WorkflowSpec, step: ForeachStep, outer: Scope, events: dict[str, Any], frames: dict[str, dict[str, Any]],
    index: int,
) -> Any:
    """Item `index` of a foreach (D6): its `items` ref resolved again, inside the iteration, over
    the same recorded values the parent resolved it over (the run input, the producing step's
    completed event, the enclosing iterations' started events), so it is never passed as an
    argument. The parent checked the list (a list, at most max_items) before starting this child."""
    outputs = {
        step_id: events[key]["output"]
        for step_id, key in _items_read_keys(spec, step, outer).items()
        if "output" in events.get(key, {})
    }
    context = build_context((events.get("start") or {}).get("input"), outputs, frames)
    items = foreach_items(step, context)
    if index >= len(items):
        raise WorkflowStepError("workflow_spec_invalid", step.id)
    return items[index]


def _frame_keys(outer: Scope) -> list[str]:
    """The `loop:{S}:started` key of every enclosing iteration (it carries the state that
    iteration started with, for `loop.<outer>.state`)."""
    return [iteration_event_key(outer[: i + 1], "started") for i in range(len(outer))]


def _iteration_frames(spec: WorkflowSpec, outer: Scope, events: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`loop.<L>.*` frames of the enclosing iterations, from their `loop:{S}:started` events."""
    frames: dict[str, dict[str, Any]] = {}
    for depth, (loop_id, index) in enumerate(outer):
        frame: dict[str, Any] = {"index": index}
        started = events.get(iteration_event_key(outer[: depth + 1], "started")) or {}
        if "state" in started:
            frame["state"] = started["state"]
        if "item" in started:
            frame["item"] = started["item"]
        frames[loop_id] = frame
    return frames


@DBOS.workflow(name=WORKFLOW_ITERATION_NAME)
async def workflow_iteration(
    run_id: str,
    tenant_id: str,
    workflow_version_id: str,
    path: list[str],
    scope: list[dict[str, Any]],
    index: int,
    state: dict[str, Any] | None,
    limits: dict[str, Any],
    traceparent: str | None,
) -> dict[str, Any]:
    """One iteration of the loop or foreach step at `path` (D5, D6), inside the parent's run (same
    events, same run row). Its arguments are references: no spec, no outputs, no item. `scope` is
    the loop step's own scope (the enclosing iterations), `state` the state carried into this
    iteration (capped by the parent; None for a foreach, whose item is read here, `_iteration_item`).
    A foreach iteration returns {"status": "completed", "last_key"} and evaluates no carry or
    until. It loads what it reads through `_read_iteration_inputs`, records `loop:{S}:started`
    {step_id, scope, index, state | item}, runs the body, then evaluates
    `carry` and `until` (D3). Never raises for a step failure. Returns
    {"status": "completed", "done", "state", "last_key", "until"} (`last_key`: the event key of the
    body step that ended the path; `until`: its outcome, None for a `while` loop), or
    {"status", "error", "error_code", "step_id"} (the body step that failed, or None)."""
    outer = scope_from_data(scope)
    iteration = EMPTY_SCOPE
    try:
        spec_json, events = await immutable_read(
            _read_iteration_inputs, tenant_id, run_id, workflow_version_id, path, outer
        )
        if spec_json is None:
            raise WorkflowStepError("workflow_version_not_found", path[-1] if path else None)
        try:
            spec = WorkflowSpec.model_validate(spec_json)
        except ValidationError:
            raise WorkflowStepError("workflow_spec_invalid") from None
        step = _find_loop(spec, path)
        if list(loop_path(outer)) != list(path[:-1]):
            raise WorkflowStepError("workflow_spec_invalid", step.id)
        iteration = child_scope(outer, step.id, index)
        frames = _iteration_frames(spec, outer, events)
        started: dict[str, Any] = {"step_id": step.id, "scope": scope_to_data(outer), "index": index}
        if isinstance(step, ForeachStep):
            item = _iteration_item(spec, step, outer, events, frames, index)
            started["item"] = item
            frames[step.id] = {"index": index, "item": item}
        else:
            started["state"] = state
            frames[step.id] = {"index": index, "state": state}
        await append_event_step(
            run_id, tenant_id, iteration_event_key(iteration, "started"), "loop_iteration_started", started,
        )
        start = events.get("start") or {}
        outputs = {
            step_id: events[key]["output"]
            for step_id, key in _body_read_keys(spec, step, outer).items()
            if "output" in events.get(key, {})
        }
        interp = _Interpreter(
            run_id, tenant_id, spec, start.get("input"), outputs, limits, traceparent,
            steps=step.body.steps, scope=iteration, loops=frames, workflow_version_id=workflow_version_id,
        )
        last_step, _last = await interp.run_path(step.body.start, None)
        last_key = scoped_event_key(iteration, last_step, "completed") if last_step is not None else None
        if isinstance(step, ForeachStep):
            return {"status": "completed", "last_key": last_key}
        try:
            exit_ = iteration_exit(step, interp.context())
        except RefMissing:
            raise WorkflowStepError("input_ref_missing", step.id) from None
        try:
            size = json_size(exit_.state)
        except ValueError:
            raise WorkflowStepError("step_output_not_json", step.id) from None
        if size > loop_limit(limits, "max_loop_state_bytes"):
            raise WorkflowStepError("loop_state_too_large", step.id)
    except StepFailed as exc:
        f = exc.failure
        return {"status": f.status, "error": f.error, "error_code": f.code, "step_id": exc.step_id}
    except Exception as exc:  # noqa: BLE001
        f = classify(exc)
        return {"status": _failure_status(exc, f), "error": f.error, "error_code": f.code, "step_id": None}
    return {
        "status": "completed",
        "done": exit_.done,
        "state": exit_.state,
        "last_key": last_key,
        "until": exit_.until,
    }


__all__ = [
    "CHILD_RUN_NAMESPACE",
    "agent_step_output",
    "approval_topic",
    "branch_workflow_id",
    "child_run_id",
    "final_output",
    "side_effect_key",
    "tool_call_id_for",
    "workflow_branch",
    "workflow_iteration",
    "workflow_run",
]
