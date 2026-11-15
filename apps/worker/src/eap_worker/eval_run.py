"""Eval runs (AC.11): one DBOS workflow (`eap.eval_run`) runs an agent's test suite snapshot.

Hand-off (apps/api/src/eap_api/launcher.py `launch_eval`): the API inserts the `runs` row of kind
'eval' (RunStore.create_eval_run: `agent_id` with `agent_version` OR `draft_spec`, `eval_suite_id`,
`eval_suite_hash`, `input` = the suite snapshot's canonical JSON) and enqueues `eap.eval_run` on
queue `eap-agent-runs` with workflow_id = run_id and positional args `(run_id, tenant_id,
input_json)`.

**The snapshot comes from `input_json`** (the decision AC.12's launcher note left to AC.11). DBOS
already stores the workflow's arguments, so reading the argument costs nothing more, is immutable
and is the same on every replay. Reading `runs.input` inside a step instead would store the whole
snapshot (up to ~32 MiB) a second time as that step's checkpointed output. `eap.start_eval_run`
returns the row's `eval_suite_hash`, and the workflow fails `eval_suite_mismatch` unless the
argument hashes to it, so the argument can never differ from the run's recorded suite. A row
without a hash (NULL) also fails `eval_suite_mismatch`: RunStore.create_eval_run always records it,
and the `eval` kind and the column arrived together (0015_evals), so no legitimate row lacks one.

Step layout (DBOS steps and child workflows):

    eap.start_eval_run                  the run row -> running; the agent version's spec (or the draft
                                        snapshot), the traceparent and the row's suite hash
    eap.append_event   start            run_started {kind: eval, agent_id, agent_version | draft,
                                        suite_id, suite_hash, cases, max_concurrency}
    per batch of `max_concurrency` cases (snapshot order: position, then id):
      eap.eval_create_children          the batch's child agent run rows (idempotent)
      child workflows eap.agent_run     started in case order, workflow id = child run id
                                        = uuid5(EVAL_NS, f"{run_id}:{case_id}"); no conversation (so no
                                        memory steps), approval_mode deny (eap.start_run: parent is eval)
      per case, in case order (awaiting its child first):
        eap.eval_score                  deterministic evaluators (pydantic_evals, evaluate_async) on
                                        the child's recorded output, duration and cost
        eap-judge__model.request        per llm_judge evaluator (the judge's model step)
        eap.eval_record_case            eval_case_results row + judge usage rows + the
                                        `eval_case_result` event (key case:{case_id}), ONE transaction
    eap.eval_aggregates                 {cases, passed, failed, errors, pass_rate, scores_avg,
                                        duration_ms_p50, cost_usd} from the recorded results
    eap.append_event   end              run_completed {output: aggregates as canonical JSON, aggregates}
    eap.finish_run                      completed (output_format json)

Determinism: children start in order and are awaited in order (the parallel-branch argument), every
decision is a pure function of the snapshot and recorded step outputs, and each case's result row
is keyed `(run_id, case_id)`, written once (a replayed record step returns the first result).

A case whose agent fails (an agent error, a limit, output_invalid, ...) is `status: error` with the
child's error code; its evaluators do not run. It never fails the eval run. The eval run itself
fails only when it cannot run at all: `eval_agent_not_found`, `eval_suite_invalid`,
`eval_suite_mismatch`.

Judge (`eap-judge`): a third generic agent like `eap-moderator`, constructed at import (before
DBOS.launch()) with ResolveModelId (allowlist re-checked at call time, no fallback) and
DBOSDurability, `output_type=ToolOutput(JudgeVerdict)`, `retries=0` (one request per evaluator call,
so one usage row). Fixed instructions (`evaluators.JUDGE_INSTRUCTIONS`, version 1); the rubric,
input, output and expected output go in HTML-escaped blocks of the user prompt. Its usage is
recorded on the eval run with `request_key = judge:{case_id}:{evaluator_index}`, even when its
answer did not validate (the request was billed). A failed judge call is an `evaluator_failures`
entry with the classified error code. The judge is never counted against the case's agent limits.

Crash points (eap_worker.crash): `eval_child_done` right after a case's child result is awaited
(other children of the batch may still run) and `eval_case_scored` after its scoring and judge
steps, before `eap.eval_record_case`; both filtered by `EAP_CRASH_EVENT_KEY` against
`position:{case position}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from dbos import DBOS, SetWorkflowID
from pydantic import ValidationError
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.capabilities import ResolveModelId
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.messages import ModelResponse
from pydantic_ai.settings import ModelSettings
from sqlalchemy import text

from eap_observability import log_context, log_failure, run_span, set_run_outcome
from eap_run_store.evals import CaseResultWrite, EvalStore, judge_request_key
from eap_schemas.evals import EvalSuiteSnapshot, JudgeVerdict
from eap_worker.crash import crash_point
from eap_worker.errors import EvalRunError, RunFailure, classify
from eap_worker.evaluators import (
    JUDGE_FAILED,
    JUDGE_INSTRUCTIONS,
    CaseOutcome,
    PlannedEvaluator,
    add_verdict,
    canonical_json,
    judge_model_id,
    judge_prompt,
    plan_evaluators,
    score_deterministic,
)
from eap_worker.models import PLACEHOLDER_MODEL, make_resolver
from eap_worker.pricing import request_usage_input, usage_record
from eap_worker.runtime import get_runtime
from eap_worker.workflow import DRAFT_VERSION, _heartbeat, agent_run, append_event_step, finish_run_step

log = logging.getLogger("eap_worker")

EVAL_WORKFLOW_NAME = "eap.eval_run"
JUDGE_NAME = "eap-judge"

# Child run ids: uuid5(EVAL_NS, f"{run_id}:{case_id}") (stable across replays).
EVAL_NS = uuid.UUID("3c9e4f1a-8b2d-4e6f-a1c7-5d0e9b8f2a61")

_AGENT_VERSION = text(
    "SELECT spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t AND version = :v"
)


def eval_child_run_id(run_id: str, case_id: str) -> str:
    """The run id (and DBOS workflow id) of the agent run executing case `case_id`."""
    return str(uuid.uuid5(EVAL_NS, f"{run_id}:{case_id}"))


def _eval_store() -> EvalStore:
    return EvalStore(get_runtime().engine)


# --------------------------------------------------------------------------- the judge agent

judge: Agent[None, JudgeVerdict] = Agent(
    PLACEHOLDER_MODEL,  # placeholder; each call passes model=<the evaluator's model id>
    name=JUDGE_NAME,
    instructions=JUDGE_INSTRUCTIONS,
    output_type=ToolOutput(JudgeVerdict, name="final_result"),
    # One request per evaluator call: an answer that does not validate is a failure, never a second
    # (unrecorded) request.
    retries=0,
    model_settings=ModelSettings(max_retries=0),
    capabilities=[
        ResolveModelId(make_resolver(lambda: get_runtime().settings)),
        DBOSDurability(
            model_step_config={"retries_allowed": True, "max_attempts": 2, "interval_seconds": 1.0, "backoff_rate": 2.0},
        ),
    ],
)


async def run_judge(
    evaluator: PlannedEvaluator, case: dict[str, Any], output: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, BaseException | None]:
    """One judge call (workflow-side; its model request is the DBOS step). Returns (verdict JSON or
    None, the request's usage input or None, the failure or None). Never raises an Exception."""
    model_id = judge_model_id(evaluator.spec)
    response: ModelResponse | None = None
    try:
        async with judge.iter(judge_prompt(evaluator.spec, case, output), model=model_id) as agent_run_:
            async for node in agent_run_:
                if Agent.is_call_tools_node(node):
                    response = node.model_response
        result = agent_run_.result
        verdict = result.output.model_dump(mode="json", by_alias=True) if result is not None else None
        failure: BaseException | None = None if verdict is not None else RuntimeError("no judge result")
    except Exception as exc:  # noqa: BLE001 - a judge failure is an evaluator_failures entry
        verdict, failure = None, exc
    usage = request_usage_input(model_id, response.model_name, response.usage) if response is not None else None
    return verdict, usage, failure


# --------------------------------------------------------------------------- steps


@DBOS.step(name="eap.start_eval_run")
async def start_eval_run_step(run_id: str, tenant_id: str) -> dict[str, Any]:
    """Marks the run running. Returns {"found", "agent_id", "agent_version", "is_draft", "spec",
    "traceparent", "suite_hash"}; "found" is False when the run row, or its agent version, is not
    this tenant's eval run."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        run = rt.run_store.get_run(tenant_id, uuid.UUID(run_id))
        if run is None or run.get("kind") != "eval":
            return {"found": False}
        rt.run_store.mark_running(tenant_id, uuid.UUID(run_id))
        rt.run_store.heartbeat(tenant_id, uuid.UUID(run_id))
        out = {
            "found": True,
            "agent_id": str(run["agent_id"]),
            "agent_version": run.get("agent_version"),
            "is_draft": run.get("draft_spec") is not None,
            "spec": run.get("draft_spec"),
            "traceparent": run.get("traceparent"),
            "suite_hash": run.get("eval_suite_hash"),
        }
        if run.get("agent_version") is not None:
            with rt.engine.connect() as conn:
                row = conn.execute(
                    _AGENT_VERSION, {"a": str(run["agent_id"]), "t": tenant_id, "v": run["agent_version"]}
                ).fetchone()
            out["spec"] = dict(row.spec) if row is not None else None
        if out["spec"] is None:
            out["found"] = False
        return out

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.eval_create_children")
async def create_children_step(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    agent_version: int | None,
    draft_spec: dict[str, Any] | None,
    cases: list[dict[str, str]],
    traceparent: str | None,
) -> list[str]:
    """The child agent run rows of one batch (`cases`: [{id, input}]), idempotent per child id.
    Returns the child run ids in case order."""
    rt = get_runtime()

    def work() -> list[str]:
        ids: list[str] = []
        for case in cases:
            child = eval_child_run_id(run_id, case["id"])
            rt.run_store.create_eval_child_run(
                tenant_id, run_id, case["id"], agent_id, agent_version, case["input"], uuid.UUID(child),
                draft_spec=draft_spec, traceparent=traceparent,
            )
            ids.append(child)
        return ids

    return await asyncio.to_thread(work)


def _duration_ms(run: dict[str, Any]) -> int | None:
    start, end = run.get("started_at") or run.get("created_at"), run.get("finished_at")
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return max(0, int(round((end - start).total_seconds() * 1000)))


@DBOS.step(name="eap.eval_score")
async def eval_score_step(
    tenant_id: str,
    child_run_id: str,
    case: dict[str, Any],
    planned: list[dict[str, Any]],
    output: Any,
    output_schema: dict[str, Any] | None,
    run_evaluators: bool,
) -> dict[str, Any]:
    """The child run's recorded duration (ms) and, with `run_evaluators`, the deterministic
    evaluators' outcome. Returns {"duration_ms", "outcome"}."""
    rt = get_runtime()
    child = await asyncio.to_thread(rt.run_store.get_run, tenant_id, uuid.UUID(child_run_id))
    child = child or {}
    duration_ms = _duration_ms(child)
    outcome = CaseOutcome()
    if run_evaluators:
        cost = child.get("cost_usd")
        outcome = await score_deterministic(
            [PlannedEvaluator.from_json(p) for p in planned],
            case,
            output,
            duration_s=None if duration_ms is None else duration_ms / 1000.0,
            cost_usd=None if cost is None else format(cost, "f"),
            output_schema=output_schema,
        )
    return {"duration_ms": duration_ms, "outcome": outcome.as_json()}


@DBOS.step(name="eap.eval_record_case")
async def eval_record_case_step(
    run_id: str, tenant_id: str, result: dict[str, Any], judge_usage: list[dict[str, Any]]
) -> dict[str, Any]:
    """Record the case (EvalStore.record_case_result; exactly once per case). `judge_usage`:
    [{"index", "usage": request_usage_input}], priced here. Returns {"status", "created"}."""

    def work() -> dict[str, Any]:
        rows = []
        for item in judge_usage:
            row, _fields = usage_record(judge_request_key(result["case_id"], item["index"]), item["usage"])
            rows.append(row)
        recorded, created = _eval_store().record_case_result(
            tenant_id, run_id, CaseResultWrite(**result), judge_usage=rows
        )
        return {"status": recorded.status, "created": created}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.eval_aggregates")
async def eval_aggregates_step(run_id: str, tenant_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_eval_store().aggregates, tenant_id, run_id)


# --------------------------------------------------------------------------- the workflow


def _batches(items: tuple[Any, ...], size: int) -> list[tuple[Any, ...]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _output_schema(spec: dict[str, Any]) -> dict[str, Any] | None:
    output = spec.get("output")
    return output.get("json_schema") if isinstance(output, dict) else None


async def _case_result(
    run_id: str,
    tenant_id: str,
    snapshot: EvalSuiteSnapshot,
    case: Any,
    child: str,
    handle: Any,
    output_schema: dict[str, Any] | None,
) -> None:
    """Await one case's child, score it, run its judges and record it."""
    case_json = case.model_dump(mode="json")
    planned = plan_evaluators(snapshot.evaluators, case.evaluators)
    try:
        result = await handle.get_result()
    except Exception as exc:  # noqa: BLE001 - e.g. a cancelled child: the case is an error
        failure = classify(exc)
        result = {"status": "failed", "error_code": failure.code}
    crash_point("eval_child_done", event_key=f"position:{case.position}")
    completed = result.get("status") == "completed"
    output = None
    if completed:
        output = result["output_json"] if result.get("output_json") is not None else result.get("output")
    scored = await eval_score_step(
        tenant_id, child, case_json, [p.as_json() for p in planned], output, output_schema, completed
    )
    outcome = CaseOutcome.from_json(scored["outcome"])
    judge_usage: list[dict[str, Any]] = []
    if completed:
        for evaluator in planned:
            if not evaluator.is_judge:
                continue
            verdict, usage, failure = await run_judge(evaluator, case_json, output)
            if usage is not None:
                judge_usage.append({"index": evaluator.index, "usage": usage})
            if verdict is not None:
                add_verdict(outcome, evaluator, verdict)
            else:
                code = classify(failure).code if failure is not None else "internal_error"
                outcome.add_failure(evaluator, failure, JUDGE_FAILED, error_code=code)
    crash_point("eval_case_scored", event_key=f"position:{case.position}")
    if completed:
        status = "passed" if outcome.passed() else "failed"
        error_code = None
    else:
        status = "error"
        error_code = str(result.get("error_code") or "internal_error")
    await eval_record_case_step(
        run_id,
        tenant_id,
        {
            "case_id": str(case.id),
            "case_name": case.name,
            "position": case.position,
            "status": status,
            "child_run_id": child,
            "output": output,
            **outcome.as_json(),
            "duration_ms": scored["duration_ms"],
            "error_code": error_code,
        },
        judge_usage,
    )


async def _run_cases(
    run_id: str, tenant_id: str, link: dict[str, Any], snapshot: EvalSuiteSnapshot
) -> None:
    spec: dict[str, Any] = link["spec"]
    spec_json = json.dumps(spec)
    draft = bool(link.get("is_draft"))
    version = DRAFT_VERSION if draft else int(link["agent_version"])
    output_schema = _output_schema(spec)
    for batch in _batches(snapshot.ordered_cases(), snapshot.max_concurrency):
        children = await create_children_step(
            run_id, tenant_id, link["agent_id"], None if draft else version, spec if draft else None,
            [{"id": str(c.id), "input": c.input} for c in batch], link.get("traceparent"),
        )
        handles = []
        for case, child in zip(batch, children):
            with SetWorkflowID(child):
                handles.append(
                    await DBOS.start_workflow_async(
                        agent_run, child, tenant_id, link["agent_id"], version, spec_json, case.input
                    )
                )
        for case, child, handle in zip(batch, children, handles):
            await _case_result(run_id, tenant_id, snapshot, case, child, handle, output_schema)


@DBOS.workflow(name=EVAL_WORKFLOW_NAME)
async def eval_run(run_id: str, tenant_id: str, input_json: str) -> dict[str, Any]:
    with log_context(tenant_id=tenant_id, run_id=run_id):
        link = await start_eval_run_step(run_id, tenant_id)
    with run_span(
        run_id, tenant_id, str(link.get("agent_id") or ""), link.get("agent_version") or DRAFT_VERSION,
        traceparent=link.get("traceparent"),
    ) as span:
        span.set_attribute("eap.run_kind", "eval")
        log.info("eval run %s started: tenant=%s agent=%s", run_id, tenant_id, link.get("agent_id"))
        result = await _eval_body(run_id, tenant_id, input_json, link)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("eval run %s %s", run_id, result["status"])
        return result


async def _eval_body(run_id: str, tenant_id: str, input_json: str, link: dict[str, Any]) -> dict[str, Any]:
    snapshot: EvalSuiteSnapshot | None = None
    try:
        snapshot = EvalSuiteSnapshot.model_validate_json(input_json)
    except ValidationError:
        snapshot = None
    started: dict[str, Any] = {"kind": "eval", "agent_id": link.get("agent_id")}
    if link.get("is_draft"):
        started["draft"] = True
    else:
        started["agent_version"] = link.get("agent_version")
    if snapshot is not None:
        started.update(
            suite_id=str(snapshot.suite_id), suite_hash=snapshot.suite_hash(), cases=len(snapshot.cases),
            max_concurrency=snapshot.max_concurrency,
        )
    await append_event_step(run_id, tenant_id, "start", "run_started", started)
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        if not link.get("found"):
            raise EvalRunError("eval_agent_not_found")
        if snapshot is None:
            from eap_schemas.evals import EvalSuiteInvalid

            raise EvalSuiteInvalid("the run input is not a suite snapshot")
        # Fail closed: every eval run row records its hash at creation (RunStore.create_eval_run;
        # the `eval` kind and the column were added together in 0015_evals, so no older eval run
        # exists). A row without one was not made by that path and is never run unchecked.
        if link.get("suite_hash") is None or link["suite_hash"] != snapshot.suite_hash():
            raise EvalRunError("eval_suite_mismatch")
        await _run_cases(run_id, tenant_id, link, snapshot)
        aggregates = await eval_aggregates_step(run_id, tenant_id)
    except Exception as exc:  # noqa: BLE001 - every eval run failure ends with a durable run_failed
        failure: RunFailure = classify(exc)
        log_failure(
            log, "eval run failed", exc, code=failure.code, error_type=failure.error_type,
            run_id=run_id, tenant_id=tenant_id,
        )
        heartbeat.cancel()
        await append_event_step(
            run_id, tenant_id, "end", "run_failed",
            {"status": failure.status, "error": failure.error, "error_code": failure.code},
        )
        await finish_run_step(run_id, tenant_id, failure.status, None, failure.error)
        return {"status": failure.status, "error": failure.error, "error_code": failure.code}
    finally:
        heartbeat.cancel()
    output = canonical_json(aggregates)
    await append_event_step(run_id, tenant_id, "end", "run_completed", {"output": output, "aggregates": aggregates})
    await finish_run_step(run_id, tenant_id, "completed", output, None, output_format="json")
    return {"status": "completed", "output": output, "aggregates": aggregates}


__all__ = [
    "EVAL_NS",
    "EVAL_WORKFLOW_NAME",
    "JUDGE_NAME",
    "eval_child_run_id",
    "eval_run",
    "judge",
    "run_judge",
]
