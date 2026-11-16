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
      (LP.11) with `limits` only, before a top-level agent or evaluate step: eap.workflow_loop_tick
      agent:    eap.workflow_resolve_agent   agent version (null = latest published, resolved ONCE here)
                                             and the child run row (kind agent, parent_run_id/step_id)
                child workflow eap.agent_run, workflow id = child run id = uuid5(run_id:step_id)
      tool:     eap.check_tool, eap.workflow_tool_call   (side effects under `{run_id}:{id}`)
      branch:   nothing durable: conditions are evaluated over recorded data
      parallel: one child workflow eap.workflow_branch per branch, id `{run_id}:{id}:{branch}`
      evaluate: (LP.14, loops D15) eap.workflow_evaluate_score (only with a deterministic
                evaluator), then per `llm_judge` in spec order the judge's model step
                `eap-judge__model.request`, then eap.workflow_judge_usage (only when a judge
                request reported usage): its rows on THIS run, operation `chat`, request key
                `judge:{id}:{k}` (`judge:{S}:{id}:{k}` in a body; k = the evaluator's index)
      approval: eap.append_event step_waiting `step:{id}:waiting`, then a loop of
                eap.workflow_approval_decision / eap.workflow_clock / DBOS.recv(`approval:{id}`)
      loop:     per iteration i (LP.7, loops D5): eap.workflow_loop_tick (LP.11), child workflow
                eap.workflow_iteration, id `{run_id}:loop:{S}` (S = the iteration's scope, `L.i`,
                `outer.2/inner.0`), its result, then eap.append_event `loop:{S}:completed` |
                `loop:{S}:failed`
      foreach:  (LP.10, loops D7) a sliding window in item order: one eap.workflow_loop_tick,
                then eap.workflow_iteration for items 0..c-1 (c = concurrency) back to back, then
                per item i its result, `loop:{S}:completed|failed` and the tick + start of item
                i+c; so with c = 1 exactly the loop's 4 operations per item, and for any c a
                function of the recorded results only
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
"comment"}; evaluate -> {passed, score, reason, results} (`evaluate_output`). An agent step's input mapping becomes its prompt: the single value if the mapping has
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

Evaluate (LP.14, D2, D15): the step's `value` (and a `$ref` `expected`) resolved over its scope
(not resolving: `evaluate_failed`) is scored by AC.11's evaluators (eap_worker.evaluators) and its
durable judge (`eap-judge`, eap_worker.eval_run.run_judge), exactly as an eval case is: an
evaluator that raises, or a judge request that fails or does not validate, is a failed result
entry, never a step failure; a failed verdict (`passed: false`) is an ordinary output, which a
refine loop's `until` reads. An `llm_judge` with `include_input` sees the run's input. The judge's
cost is on the workflow run's own rows, so it counts in run_tree_cost and the cost cap (D11): a
refine loop stops at the tick before the attempt that would start at or over the cap. Crash point
`after_judge_call` (event key `step:{id}:judge_{k}`, scoped in a body) sits between a judge's
recorded model step and its usage row: recovery replays the response and writes the row once.

The loop tick (LP.11, D10-D12; `eap.workflow_loop_tick`, recorded): before every iteration starts
(one tick for a foreach's initial window, whose items start back to back), and with `limits` before every top-level agent and evaluate step (a parallel branch's included),
it reads the clock, whether the run has its `cancel_requested` event, and with
`limits.max_cost_usd` the run tree's cost (RunStore.run_tree_cost: the run, its child runs and the
judge rows on the run, AC.5's sum). The step about to start then fails, in this order:
`run_cancelled` (the cooperative cancel), `workflow_deadline_exceeded` (the recorded
max_run_seconds), `run_budget_exceeded` (cost >= the cap; RunBudgetExceeded, so step_failed and
run_failed carry AC.5's {limit: "max_cost_usd", value, spent_usd}). The same order applies at the
deadline gate in `run_step`, which used to raise `workflow_deadline_exceeded` before the tick could
see a cancel: `eap.workflow_step_begin` now also records whether `cancel` was already stored, and
that snapshot wins only when it is already past the deadline. Inside the deadline the tick still
decides, so a body step already started runs to its end. A checkpoint recorded before the snapshot
is a float and keeps that run's deadline-first outcome; the step name is unchanged, so the
operation list of a run recorded earlier does not grow. A foreach whose tick stops it
starts no more items, awaits the started ones, then fails; a tick failure (those three codes,
or the tick step itself raising, e.g. a lost DB connection: `internal_error`, marked run-level by
LoopTickFailed) is never collected as an error entry under `on_item_error: collect`, also when the
tick belongs to a loop nested inside one of the foreach's items, so a foreach never completes with
items missing. Concurrent iterations can overshoot the cap by what is already in flight (each child
agent is bounded by its own AC.5 cap). The tick runs between iterations only: a cooperative
cancel written while an iteration runs lets that iteration's remaining body steps (tool side
effects included) run to its end; no further iteration starts. The API's cancel also DBOS-cancels
the tree, which fences that. A loop or foreach run, or a run with `limits`, started before LP.11
(the preview) does not recover on this code (the tick is a new operation): drain or cancel those
first.

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
cooperative path (LP.11): the loop tick sees `cancel_requested` without any DBOS cancel (the API
died before its cascade) and fails the step about to start with `run_cancelled`, so the run ends
`run_failed` `run_cancelled` through the worker's own events, with no further iteration.
`_parallel` still cancels the branches after the one it awaits when it is cancelled itself; with
the cascade that is redundant but harmless.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dbos import DBOS, SetWorkflowID
from dbos._error import DBOSWorkflowCancelledError, DBOSWorkflowConflictIDError  # not re-exported by dbos
from pydantic import ValidationError
from sqlalchemy import text

from eap_observability import (
    iteration_span,
    log_context,
    log_failure,
    run_span,
    set_iteration_outcome,
    set_run_outcome,
)
from eap_run_store import CANCEL_EVENT_KEY, END_EVENT_KEY, ParentRunStopped, RunNotFound, insert_usage_in_session
from eap_schemas.limits import RunBudgetExceeded
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
    scoped_judge_key,
    scoped_parent_step_id,
    scoped_tool_call_id,
    status_for_end,
)
from eap_schemas.workflows import (
    LOOP_STEP_TYPES,
    PAUSE_STEP_SUFFIX,
    AgentStep,
    ApprovalStep,
    BranchStep,
    EvaluateStep,
    ForeachStep,
    InputStep,
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
from eap_worker.eval_run import run_judge
from eap_worker.evaluators import (
    JUDGE_FAILED,
    MAX_REASON_CHARS,
    CaseOutcome,
    PlannedEvaluator,
    add_verdict,
    plan_evaluators,
    score_deterministic,
)
from eap_worker.pricing import usage_record
from eap_worker.errors import LoopTickFailed, RunFailure, WorkflowStepError, classify
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


# Top-level step kinds the loop tick guards when the spec has `limits` (D11): an agent step, and an
# evaluate step (LP.14) whose judge calls cost money. Never inside a loop body (the iteration's own
# tick covers it) and never without `limits` (loop-free goldens).
TICKED_STEP_TYPES = ("agent", "evaluate")

# Item failures a foreach never collects as an error entry (D7, D11, D12): they are about the run,
# not the item, so they fail the step as under `on_item_error: fail`. `run_cancelled` wins over all.
_RUN_LEVEL_CODES = frozenset({"run_cancelled", "run_budget_exceeded", "workflow_deadline_exceeded"})


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


def pause_timeout_hours() -> float:
    """EAP_PAUSE_TIMEOUT_HOURS (default 24): how long a step-by-step run waits at a pause."""
    try:
        value = float(os.environ.get("EAP_PAUSE_TIMEOUT_HOURS", "24"))
    except ValueError:
        return 24.0
    return value if value > 0 else 24.0


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
        "pause_timeout_hours": pause_timeout_hours(),
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
            "pause_after_each_step": bool(run.get("pause_after_each_step")),
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
) -> dict[str, Any]:
    """Record step_started (`step:{step_id}:started`, or the scoped `key` of a body step) and
    return the current time and whether cancellation was already requested.

    Checkpointed versions before the cancellation-order fix returned only the float timestamp;
    `run_step` keeps accepting that shape so those executions replay their original decision and
    operation sequence exactly."""
    rt = get_runtime()
    key = key or f"step:{step_id}:started"
    await asyncio.to_thread(rt.event_log.append, tenant_id, uuid.UUID(run_id), key, "step_started", data)
    crash_point("after_event_write", event_key=key)
    cancel = await asyncio.to_thread(rt.event_log.get_event, tenant_id, run_id, CANCEL_EVENT_KEY)
    return {
        "now": time.time(),
        "cancel_requested": cancel is not None and cancel.type == "cancel_requested",
    }


@DBOS.step(name="eap.workflow_clock")
async def clock_step() -> float:
    return time.time()


@DBOS.step(name="eap.workflow_loop_tick")
async def loop_tick_step(run_id: str, tenant_id: str, scope_text: str, step_id: str, with_cost: bool) -> dict[str, Any]:
    """The loop tick (loops D10, D11, D12; LP.11): before each loop iteration starts (`scope_text`
    = that iteration's scope, `L.i`) and, for a spec with `limits`, before each top-level agent
    and evaluate step (`scope_text` "", `step_id` that step). Returns {"now" (epoch seconds),
    "cancel_requested" (the run has its `cancel` event), "cost_usd" (with `with_cost`:
    RunStore.run_tree_cost as a decimal string, the run, its child runs and the judge rows on the
    run; else None)}. Recorded, so a replay decides exactly as the first attempt did; the decision
    itself is workflow code (`_Interpreter._tick`)."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        cancel = rt.event_log.get_event(tenant_id, run_id, CANCEL_EVENT_KEY)
        cost = rt.run_store.run_tree_cost(tenant_id, run_id) if with_cost else None
        return {
            "now": time.time(),
            "cancel_requested": cancel is not None and cancel.type == "cancel_requested",
            "cost_usd": None if cost is None else format(cost, "f"),
        }

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.workflow_evaluate_score")
async def evaluate_score_step(planned: list[dict[str, Any]], case: dict[str, Any], value: Any) -> dict[str, Any]:
    """An evaluate step's deterministic evaluators (LP.14; AC.11's `score_deterministic`, as
    `eap.eval_score` runs them for an eval case) on the step's value. Returns CaseOutcome JSON
    (`assertions`, `scores`, `labels`, `evaluator_failures`); an evaluator that raises is a
    failure entry with a fixed message, never a step failure. Recorded, so a replay (or a worker
    with other evaluator code) reads the same outcome."""
    outcome = await score_deterministic(
        [PlannedEvaluator.from_json(p) for p in planned], case, value,
        duration_s=None, cost_usd=None, output_schema=None,
    )
    return outcome.as_json()


_LOCK_RUN = text("SELECT id FROM runs WHERE tenant_id = :t AND id = :r FOR UPDATE")


@DBOS.step(name="eap.workflow_judge_usage")
async def judge_usage_step(run_id: str, tenant_id: str, usage: list[dict[str, Any]]) -> int:
    """Record an evaluate step's judge requests on the workflow run (loops D15, LP.14): one
    `run_model_requests` row per judge call, `request_key` `judge:{S}:{step_id}:{k}` (scoped_judge_key)
    and operation `chat`, exactly as AC.11 writes an eval case's judge rows. `usage`: [{"request_key",
    "usage": pricing.request_usage_input}], priced here. Written under the run's row lock, first write
    wins per key (a re-executed step adds nothing), and also after the run's `end` (the cost is real).
    The rows count in RunStore.run_tree_cost, so the next tick sees the judge's cost. Returns the
    number of rows passed."""
    rt = get_runtime()

    def work() -> int:
        rows = [usage_record(item["request_key"], item["usage"])[0] for item in usage]
        with rt.run_store.Session() as session:
            if session.execute(_LOCK_RUN, {"t": tenant_id, "r": run_id}).first() is None:
                session.rollback()
                raise RunNotFound(tenant_id, run_id)
            for row in rows:
                insert_usage_in_session(session, tenant_id, run_id, row)
            session.commit()
        return len(rows)

    return await asyncio.to_thread(work)


def parent_stop_failure(rt: Any, tenant_id: str, run_id: str) -> dict[str, Any]:
    """Why a child run was refused (`ParentRunStopped`, D12), as the failure of the step that asked
    for it: {"code", "status", "error"}, plus "budget" {value, spent_usd} for an `end` that failed
    on the cost cap with AC.5's data (the step then raises RunBudgetExceeded). The run's `end` is the truth when it exists (the API's
    `run_cancelled`, or the code the run already failed with: `run_budget_exceeded`,
    `workflow_deadline_exceeded`, ...); a `cancel` without `end` is `run_cancelled`, exactly as the
    cooperative cancel at the next tick. Runs inside the step, so a replay reads the recorded answer."""
    end = rt.event_log.get_event(tenant_id, run_id, END_EVENT_KEY)
    if end is not None and end.type == "run_failed" and (end.data or {}).get("error_code"):
        data = end.data or {}
        error = data.get("error")
        stopped: dict[str, Any] = {
            "code": str(data["error_code"]),
            "status": status_for_end(end.type, data),
            "error": str(error) if error else None,
        }
        if (
            data["error_code"] == "run_budget_exceeded" and data.get("limit") == "max_cost_usd"
            and data.get("value") is not None and data.get("spent_usd") is not None
        ):
            # Keep AC.5's {limit, value, spent_usd} on the refused step (RunBudgetExceeded).
            stopped["budget"] = {"value": str(data["value"]), "spent_usd": str(data["spent_usd"])}
        return stopped
    if end is not None:  # an end without a failure code: never expected, never hidden as a cancel
        return {"code": "internal_error", "status": None, "error": None}
    return {"code": "run_cancelled", "status": None, "error": None}


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
        try:
            _, created = rt.run_store.create_child_agent_run(
                tenant_id, agent_id, row.version, input_text, uuid.UUID(child), run_id, parent_step_id,
                traceparent=traceparent,
            )
        except ParentRunStopped:
            return {"found": False, "stopped": parent_stop_failure(rt, tenant_id, run_id)}
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
        pause_after_each_step: bool = False,
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
        # Step by step: only the run's root interpreter pauses (a loop iteration or a parallel
        # branch builds its own interpreter, which never gets the flag).
        self.pause_after_each_step = pause_after_each_step
        self._warnings: list[dict[str, Any]] = []
        # The cost cap (D11): the spec's `limits`, empty or absent for every spec published before
        # LP.11, so their runs never tick outside loops.
        self.limited = spec.limits is not None and not spec.limits.is_empty()
        self.cap_usd: Decimal | None = spec.limits.max_cost_usd if spec.limits is not None else None

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
            if self.pause_after_each_step:
                await self._pause(step)
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
        elif self.scope and isinstance(step, (ApprovalStep, InputStep, ParallelStep)):
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
        begin = await step_begin_step(
            self.run_id, self.tenant_id, step_id, self._scoped(data), started_key if self.scope else None
        )
        crash_point("workflow_mid_step", event_key=started_key)
        try:
            if problem is not None:
                raise problem
            # A float is a checkpoint from before the cancel snapshot. It keeps that run's
            # deadline-first outcome and the same next operation. A dict applies the snapshot only
            # when this step is already past the deadline (cancel wins); inside the deadline the
            # tick still owns cancel, so an in-flight iteration's body is not cut short here.
            now = begin["now"] if isinstance(begin, dict) else begin
            if now - self.limits["started_at"] > self.limits["max_run_seconds"]:
                if isinstance(begin, dict) and begin.get("cancel_requested"):
                    raise WorkflowStepError("run_cancelled", step_id)
                raise WorkflowStepError("workflow_deadline_exceeded", step_id)
            if self.limited and not self.scope and step.type in TICKED_STEP_TYPES:
                await self._tick(step_id, EMPTY_SCOPE)  # D11: only with `limits`, so goldens hold
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
            failure = dataclasses.replace(failure, status=status)  # keeps `data` (the cost cap's)
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
        # A failure's structured data (AC.5's {limit, value, spent_usd} for run_budget_exceeded)
        # rides along; every failure a loop-free run could record before LP.11 has none.
        await append_event_step(
            self.run_id, self.tenant_id, self.key(step_id, "failed"), "step_failed",
            self._scoped(
                {**(failure.data or {}), "step_id": step_id, "status": failure.status, "error": failure.error,
                 "error_code": failure.code}
            ),
        )

    # ------------------------------------------------------------------ the loop tick (LP.11)

    async def _tick(self, step_id: str, at: Scope) -> None:
        """`eap.workflow_loop_tick`, then its decision (a pure function of the recorded result):
        a `cancel_requested` event ends the step `run_cancelled` (the cooperative cancel, D12); a
        run older than its recorded `max_run_seconds` fails `workflow_deadline_exceeded`; with
        `limits.max_cost_usd`, a run-tree cost at or over the cap raises RunBudgetExceeded
        (`run_budget_exceeded`, data {limit: "max_cost_usd", value, spent_usd}, D11). `at` is the
        iteration about to start, or the empty scope before a top-level agent or evaluate step."""
        scope_text = scope_string(at)
        try:
            tick = await loop_tick_step(self.run_id, self.tenant_id, scope_text, step_id, self.cap_usd is not None)
        except Exception as exc:  # noqa: BLE001 - the tick step itself failed (e.g. a lost DB connection)
            # Run-level at any depth (RunFailure.run_level): a foreach never collects it, even when
            # this tick belongs to a loop nested inside one of its items.
            raise LoopTickFailed() from exc
        crash_point(
            "after_loop_tick",
            event_key=f"loop:{scope_text}:tick" if at else scoped_event_key(self.scope, step_id, "tick"),
        )
        if tick.get("cancel_requested"):
            raise WorkflowStepError("run_cancelled", step_id)
        if tick["now"] - self.limits["started_at"] > self.limits["max_run_seconds"]:
            raise WorkflowStepError("workflow_deadline_exceeded", step_id)
        spent = tick.get("cost_usd")
        if self.cap_usd is not None and spent is not None and Decimal(spent) >= self.cap_usd:
            raise RunBudgetExceeded(spent, self.cap_usd)

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
        if isinstance(step, (ApprovalStep, InputStep)):
            return await self._approval(step)
        if isinstance(step, EvaluateStep):
            return await self._evaluate(step)
        raise WorkflowStepError("workflow_spec_invalid", step.id)

    # ------------------------------------------------------------------ evaluate (LP.14)

    async def _evaluate(self, step: EvaluateStep) -> dict[str, Any]:
        """Score `value` with the step's evaluators (D2, D15; AC.11's evaluators and `eap-judge`).
        `value` and a `$ref` `expected` are resolved over the step's scope; either not resolving
        fails the step `evaluate_failed`. The deterministic evaluators run in one
        `eap.workflow_evaluate_score` step (only when there is one), then each `llm_judge` in spec
        order (the judge's model step), then `eap.workflow_judge_usage` records every judge request
        that reported usage (only when there is one). A judge whose answer did not validate, or
        that failed, is an evaluator failure with its classified code, as in AC.11; its request is
        still recorded when it was billed. Output: `evaluate_output`."""
        context = self.context()
        value = resolve_ref(context, step.value[REF_KEY])
        if value is MISSING:
            raise WorkflowStepError("evaluate_failed", step.id)
        expected: Any = None
        if step.expected is not None:
            expected = _resolve_operand(step.expected, context)
            if expected is MISSING:
                raise WorkflowStepError("evaluate_failed", step.id)
        planned = plan_evaluators(step.evaluators, ())
        case = {"name": step.id, "input": _judge_input(self.input), "expected_output": expected, "metadata": {}}
        outcome = CaseOutcome()
        deterministic = [p.as_json() for p in planned if not p.is_judge]
        if deterministic:
            outcome = CaseOutcome.from_json(await evaluate_score_step(deterministic, case, value))
        usage: list[dict[str, Any]] = []
        for evaluator in planned:
            if not evaluator.is_judge:
                continue
            verdict, used, failure = await run_judge(evaluator, case, value)
            if used is not None:
                usage.append({"request_key": scoped_judge_key(self.scope, step.id, evaluator.index), "usage": used})
            # After the judge's model step is recorded, before its usage row: recovery replays the
            # recorded response, so the row is written once, with the same price.
            crash_point("after_judge_call", event_key=self.key(step.id, f"judge_{evaluator.index}"))
            if verdict is not None:
                add_verdict(outcome, evaluator, verdict)
            else:
                code = classify(failure).code if failure is not None else "internal_error"
                outcome.add_failure(evaluator, failure, JUDGE_FAILED, error_code=code)
        if usage:
            await judge_usage_step(self.run_id, self.tenant_id, usage)
        return evaluate_output(planned, outcome, step.pass_)

    async def _agent(self, step: AgentStep, resolved: dict[str, Any]) -> Any:
        prompt = _agent_prompt(resolved)
        found = await resolve_agent_step(
            self.run_id, self.tenant_id, step.id, str(step.agent_id), step.version, prompt, self.traceparent,
            *((self.scope_text,) if self.scope else ()),
        )
        stopped = found.get("stopped")
        if stopped:
            # The child run was refused: the run was cancelled (or has ended) while this step was
            # starting (a recorded tick that predates the cancel, on recovery or as a live race).
            budget = stopped.get("budget")
            if budget:  # the run already failed on its cost cap: same code, with AC.5's data
                raise RunBudgetExceeded(budget["spent_usd"], budget["value"])
            raise WorkflowStepError(stopped["code"], step.id, stopped.get("status"), stopped.get("error"))
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
            await self._tick(step.id, child_scope(self.scope, step.id, index))
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
                failure = dataclasses.replace(failure, status=_failure_status(exc, failure))
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
        entries: list[tuple[str, Any]] = []  # ("key", last_key) or ("error", entry), in item order
        stopping: dict[str, Any] | None = None  # the failure that stops new starts
        cancelled: dict[str, Any] | None = None

        def settle(result: dict[str, Any]) -> None:
            nonlocal stopping, cancelled
            if result.get("status") == "completed":
                entries.append(("key", result.get("last_key")))
            elif result.get("cancelled") or result.get("error_code") == "run_cancelled":
                cancelled = cancelled or result
            elif (
                step.on_item_error == "collect" and result.get("status") != "needs_attention"
                and result.get("error_code") not in _RUN_LEVEL_CODES and not result.get("run_level")
            ):
                entries.append(("error", {"error_code": result["error_code"], "error": result["error"]}))
            else:
                stopping = stopping or result

        async def start(first: int, last: int) -> bool:
            """One tick, then the starts of items first..last-1 back to back (no await between
            them, so the tick's reading holds for all of them); False (and the tick's failure
            settled) when the tick stops the foreach: no item starts after it, started items are
            awaited."""
            nonlocal stopping, cancelled
            try:
                await self._tick(step.id, child_scope(self.scope, step.id, first))
            except Exception as exc:  # noqa: BLE001 - run_cancelled, the deadline, the cost cap, or the tick failed
                # A tick failure is a run-level failure, NEVER an item error (not even under
                # `collect`): it belongs to no item, and collecting it would drop the items that
                # never start. `run_cancelled` ends the run cancelled; every other code (the
                # deadline, the cost cap, an unexpected error such as a lost DB connection, which
                # classifies `internal_error`) stops the foreach and fails the step with it.
                result = _exception_result(exc)
                if result.get("error_code") == "run_cancelled":
                    cancelled = cancelled or result
                else:
                    stopping = stopping or result
                return False
            for index in range(first, last):
                handles[index] = await self._start_iteration(step, index, None)
            return True

        started = 0
        if count and await start(0, min(width, count)):  # the initial window: one tick
            started = min(width, count)
        try:
            for index in range(count):
                if index >= started:
                    break  # stopped: every started item has been awaited
                settle(await self._finish_iteration(step, index, handles.pop(index)))
                if stopping is None and cancelled is None and started < count:
                    if not await start(started, started + 1):
                        continue
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
            raise StepFailed(step.id, _result_failure(worst))
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

    async def _approval(self, step: ApprovalStep | InputStep) -> Any:
        """An approval step, or an input step (the same wait; it also records its `resume_schema` in
        the waiting event so the API validates a submission against what the run waits for, and
        outputs the submitted object as `data`)."""
        is_input = isinstance(step, InputStep)
        decision = await self._wait_decision(
            step.id,
            {
                "step_id": step.id,
                "prompt": step.prompt,
                "timeout_hours": step.timeout_hours,
                **({"resume_schema": step.resume_schema} if isinstance(step, InputStep) else {}),
            },
            step.timeout_hours * self.limits["hour_seconds"],
        )
        kind = (decision or {}).get("type")
        info = (decision or {}).get("data") or {}
        if kind == "step_approved" and is_input:
            return {"data": info.get("resume_data"), "decided_by": info.get("decided_by"),
                    "comment": info.get("comment")}
        if kind == "step_approved":
            return {"approved": True, "decided_by": info.get("decided_by"), "comment": info.get("comment")}
        if kind == "step_rejected":
            raise WorkflowStepError("approval_rejected", step.id)
        raise WorkflowStepError("approval_timeout", step.id)

    async def _pause(self, step: Any) -> None:
        """Step by step (D3): after a top-level step, wait exactly as an approval does under the
        reserved id `{step.id}_pause` (prompt "Paused after {step.id}", no `resume_schema`, waiting
        data carries `pause: true`). Approve continues; reject fails the run `cancelled_by_user`;
        the timeout (EAP_PAUSE_TIMEOUT_HOURS, default 24) fails it `pause_timed_out`."""
        pause_id = f"{step.id}{PAUSE_STEP_SUFFIX}"
        hours = self.limits.get("pause_timeout_hours", 24.0)
        decision = await self._wait_decision(
            pause_id,
            {"step_id": pause_id, "prompt": f"Paused after {step.id}", "pause": True, "timeout_hours": hours},
            hours * self.limits["hour_seconds"],
        )
        kind = (decision or {}).get("type")
        if kind == "step_approved":
            return
        if kind == "step_rejected":
            raise WorkflowStepError("cancelled_by_user", pause_id)
        raise WorkflowStepError("pause_timed_out", pause_id)

    async def _wait_decision(self, step_id: str, data: dict[str, Any], timeout_seconds: float) -> dict[str, Any] | None:
        """Record `step_id`'s waiting event (and inbox row) and wait for its decision or timeout."""
        deadline = await approval_begin_step(
            self.run_id, self.tenant_id, step_id,
            # The DBOS workflow that waits (the run, or a parallel branch's child workflow): the API
            # sends the wake-up here.
            {**data, "workflow_id": DBOS.workflow_id},
            timeout_seconds,
        )
        crash_point("workflow_waiting", event_key=f"step:{step_id}:waiting")
        poll = self.limits["approval_poll_seconds"]
        timed_out = False
        max_rounds = math.ceil(timeout_seconds / poll) + 16
        decision: dict[str, Any] | None = None
        for _ in range(max_rounds):
            decision = await approval_decision_step(self.run_id, self.tenant_id, step_id, timed_out)
            if decision is not None:
                break
            remaining = deadline - await clock_step()
            if remaining <= 0:
                timed_out = True
                continue
            await DBOS.recv_async(approval_topic(step_id), timeout_seconds=min(remaining, poll))
        if decision is None:
            decision = await approval_decision_step(self.run_id, self.tenant_id, step_id, True)
        return decision


def _resolve_operand(value: Any, context: dict[str, Any]) -> Any:
    """A literal as is, a `{"$ref": path}` resolved (MISSING when it does not resolve)."""
    if isinstance(value, dict) and set(value) == {REF_KEY} and isinstance(value[REF_KEY], str):
        return resolve_ref(context, value[REF_KEY])
    return value


def _judge_input(run_input: Any) -> str:
    """What an `llm_judge` with `include_input` sees as the input of an evaluate step: the run's
    input (a string as is, anything else as canonical JSON)."""
    if run_input is None:
        return ""
    return run_input if isinstance(run_input, str) else canonical_json(run_input)


def evaluate_output(planned: list[PlannedEvaluator], outcome: CaseOutcome, mode: str) -> dict[str, Any]:
    """An evaluate step's output `{passed, score, reason, results}` (D3) from its evaluators'
    outcome. Pure.

    - `results`: one entry per evaluator in spec order, `{name, kind, passed, score, reason}`.
      `passed` is the evaluator's assertion; `score` the judge's score, else 1.0 or 0.0 from
      `passed`. A failed evaluator (it raised, or its judge request failed) has `passed` false,
      `score` null, its fixed message as `reason`, and `error_type` (plus `error_code` for a
      judge). An evaluator that does not apply (it compares with an `expected` that is null) has
      `passed` and `score` null and `skipped` true, and is left out of the verdict.
    - `passed`: `mode` "all": every applying evaluator passed (true when none applies); "any": at
      least one did.
    - `score`: the mean of the non-null scores, null without any.
    - `reason`: the reasons of the evaluators whose `passed` equals the verdict (the failing ones
      of a failed step, so a refine loop's `carry` hands the judge's reason to the next attempt
      verbatim), joined by newlines, at most MAX_REASON_CHARS characters; null without any."""
    failures = {f["name"]: f for f in outcome.evaluator_failures}
    results: list[dict[str, Any]] = []
    for evaluator in planned:
        entry: dict[str, Any] = {"name": evaluator.name, "kind": evaluator.kind}
        failed = failures.get(evaluator.name)
        assertion = outcome.assertions.get(evaluator.name)
        scored = outcome.scores.get(evaluator.name)
        if failed is not None:
            entry.update(passed=False, score=None, reason=failed.get("error"), error_type=failed.get("error_type"))
            if failed.get("error_code"):
                entry["error_code"] = failed["error_code"]
        elif assertion is None and scored is None:
            entry.update(passed=None, score=None, reason=None, skipped=True)
        else:
            passed = None if assertion is None else bool(assertion["value"])
            if scored is not None:
                score: float | None = float(scored["value"])
            else:
                score = None if passed is None else (1.0 if passed else 0.0)
            reason = (assertion or scored or {}).get("reason")
            entry.update(passed=passed, score=score, reason=reason)
        results.append(entry)
    decided = [r for r in results if r["passed"] is not None]
    verdict = all(r["passed"] for r in decided) if mode == "all" else any(r["passed"] for r in decided)
    scores = [r["score"] for r in results if r["score"] is not None]
    reasons = [r["reason"] for r in decided if r["passed"] is verdict and r["reason"]]
    return {
        "passed": verdict,
        "score": sum(scores) / len(scores) if scores else None,
        "reason": "\n".join(reasons)[:MAX_REASON_CHARS] or None,
        "results": results,
    }


def _missing_last(step_id: str) -> WorkflowStepError:
    """A loop's `last_key` names an event that is not there (D6: impossible unless the event log
    lost a row): an internal error, so the run fails instead of reporting a wrong (null) output."""
    return WorkflowStepError(
        "internal_error", step_id, "failed", "WorkflowStepError: a loop iteration's last output event is missing"
    )


def _result_failure(result: dict[str, Any]) -> RunFailure:
    """The RunFailure of a failed child result ({status, error, error_code, data?, ...})."""
    return RunFailure(
        result["status"], result["error_code"], *_split_error(result["error"]), data=result.get("data") or None,
        run_level=bool(result.get("run_level")),
    )


def _failure_result(failure: RunFailure, step_id: str | None) -> dict[str, Any]:
    """A child workflow's failed result (a parallel branch, an iteration): {status, error,
    error_code, step_id}, plus `data` only when the failure carries some (LP.11: the cost cap's
    {limit, value, spent_usd}), so every result a loop-free run recorded keeps its shape."""
    result: dict[str, Any] = {"status": failure.status, "error": failure.error, "error_code": failure.code,
                              "step_id": step_id}
    if failure.data:
        result["data"] = failure.data
    if failure.run_level:
        result["run_level"] = True  # LP.14: a failed loop tick, never collected by a foreach
    return result


def _exception_result(exc: BaseException) -> dict[str, Any]:
    """`_failure_result` of an exception raised in workflow code (not inside a child)."""
    failure = classify(exc)
    return _failure_result(
        dataclasses.replace(failure, status=_failure_status(exc, failure)),
        None,
    )


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
            pause_after_each_step=bool(link.get("pause_after_each_step")),
        )
        _last_step, last_output = await interp.run_path(spec.start, None)
        output = final_output(spec, last_output)
        output_text = canonical_json(output)
    except StepFailed as exc:
        failure, failed_step = exc.failure, exc.step_id
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        failure = classify(exc)
        failure = dataclasses.replace(failure, status=_failure_status(exc, failure))
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
        {**(failure.data or {}), "status": failure.status, "error": failure.error, "error_code": failure.code,
         "step_id": failed_step},
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
        return _failure_result(exc.failure, exc.step_id)
    except Exception as exc:  # noqa: BLE001
        return _exception_result(exc)
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
    {"status", "error", "error_code", "step_id"} (the body step that failed, or None).

    Telemetry (LP.9): the whole iteration runs inside its `eap.loop_iteration` span (ids and counts
    only), ended with set_iteration_outcome: OK only for `completed`; a failed iteration (a nested
    tick's `run_cancelled` or `run_budget_exceeded` included) and this workflow's own DBOS cancel
    end it as an error."""
    outer = scope_from_data(scope)
    loop_id = path[-1] if path else ""
    try:  # a malformed path fails below as workflow_spec_invalid, never here
        span_scope = scope_string(child_scope(outer, loop_id, index)) if path else ""
    except ValueError:
        span_scope = ""
    counts = {"body_steps": 0}
    with iteration_span(run_id, tenant_id, loop_id, index, scope=span_scope, traceparent=traceparent) as span:
        try:
            result = await _iteration_run(
                span, counts, run_id, tenant_id, workflow_version_id, path, outer, index, state, limits, traceparent,
            )
        except DBOSWorkflowCancelledError:
            set_iteration_outcome(span, "failed", "run_cancelled", body_steps=counts["body_steps"])
            raise
        except DBOSWorkflowConflictIDError:
            # Another execution of this iteration workflow (a recovery or a concurrent replay)
            # recorded the same operation first, so this one stops. Telemetry only: name it
            # run_cancelled when the run has its `cancel` (the run is stopping anyway); otherwise
            # keep the bare error status, as the other execution goes on. Re-raised either way.
            if await _run_cancel_requested(run_id, tenant_id):
                set_iteration_outcome(span, "failed", "run_cancelled", body_steps=counts["body_steps"])
            raise
        set_iteration_outcome(span, result["status"], result.get("error_code"), body_steps=counts["body_steps"])
        return result


async def _run_cancel_requested(run_id: str, tenant_id: str) -> bool:
    """Telemetry only, on the way out of an iteration that lost its workflow: does the run have its
    `cancel` event? A plain read (no DBOS operation, its answer never reaches workflow logic);
    False when it cannot be read."""
    try:
        cancel = await asyncio.to_thread(get_runtime().event_log.get_event, tenant_id, run_id, CANCEL_EVENT_KEY)
    except Exception:  # noqa: BLE001 - best effort, the iteration is already stopping
        return False
    return cancel is not None and cancel.type == "cancel_requested"


async def _iteration_run(
    span: Any,
    counts: dict[str, int],
    run_id: str,
    tenant_id: str,
    workflow_version_id: str,
    path: list[str],
    outer: Scope,
    index: int,
    state: dict[str, Any] | None,
    limits: dict[str, Any],
    traceparent: str | None,
) -> dict[str, Any]:
    """workflow_iteration's work (see there). `counts["body_steps"]` is kept current with the body
    steps the iteration executed, for its span; `eap.loop_kind` is set once the step is known."""
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
        span.set_attribute("eap.loop_kind", step.type)
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
        try:
            last_step, _last = await interp.run_path(step.body.start, None)
        finally:
            counts["body_steps"] = len(interp.executed)
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
        return _failure_result(exc.failure, exc.step_id)
    except Exception as exc:  # noqa: BLE001
        return _exception_result(exc)
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
    "evaluate_output",
    "final_output",
    "side_effect_key",
    "tool_call_id_for",
    "workflow_branch",
    "workflow_iteration",
    "workflow_run",
]
