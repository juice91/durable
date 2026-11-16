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

LLM-judge scorers are not supported: the judge is a durable model step of the eval workflow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from dbos import DBOS

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
                reason = None if value else FAILED_REASONS.get(scorer.get("kind", ""), "the check failed")
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
    if run.get("status") != "completed" or run.get("output") is None:
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
        data = score_data({**scorer, "kind": scorer["evaluator"]["kind"]}, outcome)
        if data is None:
            continue
        result = await asyncio.to_thread(
            rt.event_log.append, tenant_id, UUID(run_id), score_event_key(scorer["id"]), SCORE_EVENT_TYPE, data
        )
        written += 1 if result[1] else 0
    return written


__all__ = ["SCORER_FAILED", "score_data", "score_run_step"]
