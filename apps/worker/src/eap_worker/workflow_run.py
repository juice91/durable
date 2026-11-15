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
      eap.append_event   step_completed `step:{id}:completed` {step_id, output, unresolved_refs?}
                     or  step_failed    `step:{id}:failed`    {step_id, status, error, error_code}
    eap.append_event   end              run_completed {output, result} | run_failed {...}
    eap.finish_run                      completed | failed | needs_attention

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

Limits: each step output at most EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES (32 KiB) as JSON, all outputs
of a run at most EAP_WORKFLOW_MAX_CONTEXT_BYTES (1 MiB) in total (both are checkpointed and passed
to branch workflows); a step id executes at most once per run, so a run executes at most
len(steps) <= 50 steps (enforced, step_limit_exceeded); a step does not start once the run is
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

Known limitation, cancellation (there is no product cancel API yet): cancelling a run's DBOS
workflow (DBOS.cancel_workflow) does not cascade to its children. While a parallel step awaits its
branches, a cancelled parent cancels only the branches after the one it is awaiting at that moment
(best effort, in `_parallel`); the branch being awaited, and anything it already started (a child
agent run, an approval wait), keeps running until it ends on its own. Child agent runs of agent
steps are never cancelled with the parent either. A cancel API must cancel the run's whole tree
(branch workflows `{run_id}:{step}:{branch}` and child runs) explicitly.
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
from eap_schemas.workflows import (
    AgentStep,
    ApprovalStep,
    BranchStep,
    MAX_STEPS,
    ParallelStep,
    ToolStep,
    WorkflowSpec,
    iter_refs,
)
from eap_worker.config import WORKFLOW_BRANCH_NAME, WORKFLOW_RUN_NAME
from eap_worker.crash import crash_point
from eap_worker.errors import RunFailure, WorkflowStepError, classify
from eap_worker.runtime import get_runtime
from eap_worker.tool_run import _REJECTIONS, call_tool_guarded, check_tool_step
from eap_worker.workflow import _heartbeat, agent_run, append_event_step, finish_run_step
from eap_worker.workflow_eval import (
    MISSING,
    RefMissing,
    canonical_json,
    evaluate,
    json_size,
    non_finite_path,
    resolve_ref,
    resolve_template,
)

log = logging.getLogger("eap_worker")

# Child agent run ids: uuid5(CHILD_RUN_NAMESPACE, f"{run_id}:{step_id}") (stable across replays).
CHILD_RUN_NAMESPACE = uuid.UUID("6f2b8e0a-5f37-4c1e-9d0b-8f8e2c7a1d42")


def child_run_id(run_id: str, step_id: str) -> str:
    """The run id (and DBOS workflow id) of the agent run executing agent step `step_id`."""
    return str(uuid.uuid5(CHILD_RUN_NAMESPACE, f"{run_id}:{step_id}"))


def branch_workflow_id(run_id: str, parallel_id: str, branch: str) -> str:
    return f"{run_id}:{parallel_id}:{branch}"


def side_effect_key(run_id: str, step_id: str) -> str:
    """The side_effects key guarding a workflow tool step's single call."""
    return f"{run_id}:{step_id}"


def tool_call_id_for(step_id: str) -> str:
    """ctx.tool_call_id a workflow tool step's tool sees (tickets.create then reserves
    `{run_id}:wf_{step_id}`, distinct from the step's guard key)."""
    return f"wf_{step_id}"


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
    }


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
    this tenant), "limits" (read_limits() plus "started_at", epoch seconds)}."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        with rt.engine.connect() as conn:
            row = conn.execute(_VERSION, {"id": workflow_version_id, "t": tenant_id}).fetchone()
        if row is not None:
            # The API inserts the row first; idempotent safety net (RunIdConflict for another tenant).
            rt.run_store.create_workflow_run(tenant_id, row.workflow_id, row.version, input_json, uuid.UUID(run_id))
        # running, clearing an ENQUEUE_FAILED_ERROR left by a hand-off that raised after DBOS accepted it.
        rt.run_store.mark_running(tenant_id, uuid.UUID(run_id))
        rt.run_store.heartbeat(tenant_id, uuid.UUID(run_id))
        run = rt.run_store.get_run(tenant_id, uuid.UUID(run_id)) or {}
        return {
            "traceparent": run.get("traceparent"),
            "workflow_id": row.workflow_id if row is not None else run.get("workflow_id"),
            "workflow_version": row.version if row is not None else run.get("workflow_version"),
            "spec": dict(row.spec) if row is not None else None,
            "limits": {**read_limits(), "started_at": time.time()},
        }

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.workflow_step_begin")
async def step_begin_step(run_id: str, tenant_id: str, step_id: str, data: dict[str, Any]) -> float:
    """Record step_started (`step:{step_id}:started`) and return the current time (epoch seconds)."""
    rt = get_runtime()
    key = f"step:{step_id}:started"
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
) -> dict[str, Any]:
    """Resolve the agent version ONCE (pinned, or the latest published when None) and create the
    child agent run row. Returns {"found": False} (with "archived": True when the agent exists but
    is archived) or {"found": True, "version", "spec_json", "child_run_id"}; checkpointed, so a
    replay never re-resolves 'latest'.

    The agent must exist and not be archived only when the child row is CREATED. A re-executed
    step (killed after `after_child_run_insert`, before the checkpoint) finds the child row and
    reloads the version that row names, even if the agent has been archived since: the run
    already committed to that agent version."""
    rt = get_runtime()
    child = child_run_id(run_id, step_id)

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
            tenant_id, agent_id, row.version, input_text, uuid.UUID(child), run_id, step_id, traceparent=traceparent
        )
        crash_point("after_child_run_insert", event_key=f"step:{step_id}:child")
        if not created:
            # Lost a race with a concurrent attempt that inserted first: that row's version wins.
            existing = rt.run_store.get_run(tenant_id, uuid.UUID(child)) or {}
            if existing.get("agent_version") != row.version:
                return pinned(existing.get("agent_version"))
        return {"found": True, "version": row.version, "spec_json": json.dumps(row.spec), "child_run_id": child}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.workflow_tool_call", retries_allowed=False)
async def workflow_tool_call_step(
    run_id: str, tenant_id: str, step_id: str, tool_id: str, args: dict[str, Any], side_effect: bool
) -> Any:
    """Same semantics as eap.tool_call, guarded by `{run_id}:{step_id}`; crash point
    `after_workflow_tool_call` (event_key `step:{step_id}:call`)."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        call_tool_guarded,
        run_id,
        tenant_id,
        tool_id,
        args,
        side_effect,
        guard_key=side_effect_key(run_id, step_id),
        tool_call_id=tool_call_id_for(step_id),
        crash_name="after_workflow_tool_call",
        crash_key=f"step:{step_id}:call",
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
    def __init__(
        self,
        run_id: str,
        tenant_id: str,
        spec: WorkflowSpec,
        input_value: Any,
        outputs: dict[str, Any],
        limits: dict[str, Any],
        traceparent: str | None,
    ) -> None:
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.spec = spec
        self.spec_json = spec.model_dump(mode="json")
        self.by_id = {s.id: s for s in spec.steps}
        self.input = input_value
        self.outputs = dict(outputs)
        self.inherited = set(outputs)
        self.context_bytes = sum(json_size(v) for v in outputs.values())
        self.limits = limits
        self.traceparent = traceparent
        self.executed: set[str] = set()

    def context(self) -> dict[str, Any]:
        return {"input": self.input, "steps": {k: {"output": v} for k, v in self.outputs.items()}}

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
        data: dict[str, Any] = {"step_id": step_id, "step_type": step.type}
        problem: WorkflowStepError | None = None
        if step_id in self.executed or len(self.executed) >= MAX_STEPS:
            problem = WorkflowStepError("step_limit_exceeded", step_id)
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
                        child_run_id=child_run_id(self.run_id, step_id))
        elif isinstance(step, ToolStep):
            data["tool_id"] = step.tool_id
        now = await step_begin_step(self.run_id, self.tenant_id, step_id, data)
        crash_point("workflow_mid_step", event_key=f"step:{step_id}:started")
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
            if size > self.limits["max_step_output_bytes"]:
                raise WorkflowStepError("step_output_too_large", step_id)
            if self.context_bytes + size > self.limits["max_context_bytes"]:
                raise WorkflowStepError("workflow_context_too_large", step_id)
        except StepFailed as inner:
            # A nested failure (parallel branch): this step fails with the branch's failure.
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
        await append_event_step(
            self.run_id, self.tenant_id, f"step:{step_id}:completed", "step_completed", completed,
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
            target = self.by_id.get(parts[1])
            if not isinstance(target, AgentStep) or parts[1] not in self.outputs:
                continue
            if not isinstance(self.outputs[parts[1]], (dict, list)):
                continue  # a text agent's output: publish warns for field refs into it
            if resolve_ref(context, path) is MISSING:
                found.append(path)
        return found

    async def _record_failure(self, step_id: str, failure: RunFailure) -> None:
        await append_event_step(
            self.run_id, self.tenant_id, f"step:{step_id}:failed", "step_failed",
            {"step_id": step_id, "status": failure.status, "error": failure.error, "error_code": failure.code},
        )

    async def _execute(self, step: Any, resolved: dict[str, Any] | None) -> Any:
        if isinstance(step, AgentStep):
            return await self._agent(step, resolved or {})
        if isinstance(step, ToolStep):
            return await self._tool(step, resolved or {})
        if isinstance(step, BranchStep):
            context = self.context()
            for index, case in enumerate(step.cases):
                if evaluate(case.when, context):
                    return {"case": index, "next": case.next}
            return {"case": None, "next": step.default}
        if isinstance(step, ParallelStep):
            return await self._parallel(step)
        if isinstance(step, ApprovalStep):
            return await self._approval(step)
        raise WorkflowStepError("workflow_spec_invalid", step.id)  # pragma: no cover

    async def _agent(self, step: AgentStep, resolved: dict[str, Any]) -> Any:
        prompt = _agent_prompt(resolved)
        found = await resolve_agent_step(
            self.run_id, self.tenant_id, step.id, str(step.agent_id), step.version, prompt, self.traceparent
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
            self.run_id, self.tenant_id, step.id, step.tool_id, check["args"], bool(check["side_effect"])
        )

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
    with run_span(run_id, tenant_id, "", "", traceparent=link.get("traceparent")) as span:
        span.set_attribute("eap.run_kind", "workflow")
        span.set_attribute("eap.workflow_id", str(link.get("workflow_id") or ""))
        log.info("workflow run %s started: tenant=%s workflow=%s", run_id, tenant_id, link.get("workflow_id"))
        result = await _workflow_body(run_id, tenant_id, input_json, link)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("workflow run %s %s", run_id, result["status"])
        return result


async def _workflow_body(run_id: str, tenant_id: str, input_json: str, link: dict[str, Any]) -> dict[str, Any]:
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
        interp = _Interpreter(run_id, tenant_id, spec, input_value, {}, link["limits"], link.get("traceparent"))
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
    "workflow_run",
]
