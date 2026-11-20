"""Reusable scorers on live agent runs (W2.3): the `score` run events.

A scorer (eap_run_store.scorers, 0020_scorers) is one deterministic evaluator attached to an agent.
`eap.start_run` reads the agent's attached scorers into the link (only when there are any, and only
for a top-level run: not an eval case child or a workflow step), so an agent without scorers keeps
its exact step layout. After `eap.finish_run` of a run that COMPLETED with an answer, one step
scores it:

    eap.finish_run
    eap.score_run       reads the run's recorded output and cost, applies each sampled scorer with the
                        eval runner's evaluators (eap_worker.evaluators.score_deterministic) and
                        appends one `score` event per scorer, key `score:{scorer_id}`:
                        {scorer, scorer_id, score, passed, reason}. No output text.

The events are written after `end` (store.POST_END_EVENT_KEY_PREFIXES allows `score:`), idempotent by
key, so a replayed step writes nothing twice. Sampling is a stable function of (run, scorer). A scorer
that raises is `passed: false`, `score: null` with a fixed reason; scoring never changes the run's
status or fails the run. Only the worker writes these events, never the API.

On demand (`POST /runs/{id}/score`): workflow `eap.score_run_on_demand(run_id, tenant_id, scorer_ids)` loads those
scorers (sampling rate 1: the caller asked for them; a scorer deleted meanwhile is skipped) and the
run's output schema, then runs the same scoring step, so the events, their keys and their rules are
the ones above. Scoring a run again with a scorer writes nothing new.

LLM-judge scorers are not supported: the judge is a durable model step of the eval workflow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from dbos import DBOS
from sqlalchemy import text

from eap_run_store.scorers import sampled
from eap_schemas.scorers import SCORE_EVENT_TYPE, score_event_key
from eap_worker.evaluators import PlannedEvaluator, score_deterministic
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.scoring")

SCORER_FAILED = "the scorer could not be applied"
# Fixed reasons of a failed check: the evaluators' own reasons may quote the output (Contains does).
FAILED_REASONS = {
    "contains": "the output does not contain the expected text",
    "regex": "the output does not match the pattern",
    "schema_valid": "the output does not match the schema",
    "field_equals": "the output field does not have the expected value",
    "max_cost": "the run cost more than the limit",
}
NEGATED_REGEX_REASON = "the output matches a pattern it must not match"


def score_data(scorer: dict[str, Any], outcome: Any) -> dict[str, Any] | None:
    """The `score` event data of one scorer from its CaseOutcome, None when the evaluator did not
    apply to this run."""
    base = {"scorer": scorer["name"], "scorer_id": scorer["id"]}
    if outcome.evaluator_failures:
        return {**base, "score": None, "passed": False, "reason": SCORER_FAILED}
    for table in (outcome.assertions, outcome.scores):
        for key, found in table.items():
            value = found["value"]
            if isinstance(value, bool):
                if value:
                    reason = None
                elif scorer.get("kind") == "regex" and scorer.get("negate"):
                    reason = NEGATED_REGEX_REASON
                else:
                    reason = FAILED_REASONS.get(scorer.get("kind", ""), "the check failed")
                return {**base, "score": 1.0 if value else 0.0, "passed": value, "reason": reason}
            return {**base, "score": float(value), "passed": float(value) > 0, "reason": None}
    return None


def _output_of(run: dict[str, Any]) -> Any:
    output = run.get("output")
    if run.get("output_format") == "json" and isinstance(output, str):
        try:
            return json.loads(output)
        except ValueError:
            return output
    return output


@DBOS.step(name="eap.score_run")
async def score_run_step(
    run_id: str, tenant_id: str, scorers: list[dict[str, Any]], output_schema: dict[str, Any] | None
) -> int:
    """Score the finished run with `scorers` (the link's records); returns the events written."""
    rt = get_runtime()
    run = await asyncio.to_thread(rt.run_store.get_run, tenant_id, UUID(run_id)) or {}
    if run.get("status") not in ("completed", "skipped") or run.get("output") is None:
        return 0
    output = _output_of(run)
    cost: str | None = None
    if any(s["evaluator"]["kind"] == "max_cost" for s in scorers):
        spent = await asyncio.to_thread(rt.run_store.recorded_cost_usd, tenant_id, UUID(run_id))
        cost = format(spent, "f")
    written = 0
    for index, scorer in enumerate(scorers):
        if not sampled(run_id, scorer["id"], float(scorer.get("sampling_rate", 1))):
            continue
        planned = PlannedEvaluator(index, scorer["name"], scorer["evaluator"]["kind"], scorer["evaluator"])
        try:
            outcome = await score_deterministic(
                [planned], {"name": scorer["name"], "input": run.get("input")}, output,
                duration_s=None, cost_usd=cost, output_schema=output_schema,
            )
        except Exception:  # noqa: BLE001 - scoring never fails the run
            log.warning("run %s: scorer %s failed", run_id, scorer["id"])
            continue
        data = score_data(
            {**scorer, "kind": scorer["evaluator"]["kind"], "negate": bool(scorer["evaluator"].get("negate"))}, outcome
        )
        if data is None:
            continue
        result = await asyncio.to_thread(
            rt.event_log.append, tenant_id, UUID(run_id), score_event_key(scorer["id"]), SCORE_EVENT_TYPE, data
        )
        written += 1 if result[1] else 0
    return written


_AGENT_SPEC = text("SELECT spec FROM agent_versions WHERE tenant_id = :t AND agent_id = :a AND version = :v")


@DBOS.step(name="eap.load_scorers")
async def load_scorers_step(run_id: str, tenant_id: str, scorer_ids: list[str]) -> dict[str, Any]:
    """`{scorers, output_schema}` for an on-demand score: the named scorers of the tenant (records as
    the link carries them, sampling rate 1; unknown ones left out) and the run's agent output schema."""
    from eap_run_store.scorers import ScorerNotFound, ScorerStore

    rt = get_runtime()
    store = ScorerStore(rt.engine)
    scorers: list[dict[str, Any]] = []
    for scorer_id in dict.fromkeys(scorer_ids):
        try:
            record = await asyncio.to_thread(store.get, tenant_id, scorer_id)
        except ScorerNotFound:
            continue
        scorers.append({"id": record.id, "name": record.name, "evaluator": record.evaluator, "sampling_rate": 1})
    run = await asyncio.to_thread(rt.run_store.get_run, tenant_id, UUID(run_id)) or {}
    spec = run.get("draft_spec")
    if spec is None and run.get("agent_id") and run.get("agent_version") is not None:
        def read() -> Any:
            with rt.engine.connect() as conn:
                row = conn.execute(
                    _AGENT_SPEC, {"t": tenant_id, "a": str(run["agent_id"]), "v": run["agent_version"]}
                ).first()
            return row[0] if row else None

        spec = await asyncio.to_thread(read)
    output = (spec or {}).get("output") if isinstance(spec, dict) else None
    schema = output.get("json_schema") if isinstance(output, dict) else None
    return {"scorers": scorers, "output_schema": schema}


@DBOS.workflow(name="eap.score_run_on_demand")
async def score_run(run_id: str, tenant_id: str, scorer_ids: list[str]) -> dict[str, int]:
    """Score a finished run with the named scorers (`POST /runs/{id}/score`); `{written}` events."""
    loaded = await load_scorers_step(run_id, tenant_id, scorer_ids)
    if not loaded["scorers"]:
        return {"written": 0}
    written = await score_run_step(run_id, tenant_id, loaded["scorers"], loaded["output_schema"])
    return {"written": written}


__all__ = ["SCORER_FAILED", "load_scorers_step", "score_data", "score_run", "score_run_step"]
