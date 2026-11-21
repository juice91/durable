"""Response shapes shared by the REST routes and the MCP control tools (scorers, suites, cases, eval runs).

Plain functions over service records: no web framework, no data access.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from eap_api.services.evals import StartedEvalRun, dump_evaluators
from eap_api.services.runs import run_json as _run_json


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dump(evaluators: Any) -> list[dict[str, Any]]:
    """As stored: optional None fields left out, required ones kept even when null."""
    return dump_evaluators(evaluators)


def suite_json(suite: Any, target_name: str | None = None) -> dict[str, Any]:
    return {
        "id": suite.id,
        "agent_id": suite.agent_id,
        "workflow_id": suite.workflow_id,
        "target_kind": suite.target_kind,
        "target_id": suite.target_id,
        "target_name": target_name,
        "name": suite.name,
        "description": suite.description,
        "evaluators": _dump(suite.evaluators),
        "case_count": suite.case_count,
        "archived": suite.archived,
        "archived_at": _iso(suite.archived_at),
        "created_at": _iso(suite.created_at),
        "updated_at": _iso(suite.updated_at),
        "revision": suite.revision,
    }


def case_json(case: Any) -> dict[str, Any]:
    return {
        "id": case.id,
        "suite_id": case.suite_id,
        "name": case.name,
        "input": case.input,
        "expected_output": case.expected_output,
        "metadata": case.metadata,
        "evaluators": _dump(case.evaluators),
        "position": case.position,
        "created_at": _iso(case.created_at),
        "updated_at": _iso(case.updated_at),
        "revision": case.revision,
    }


def result_json(result: Any) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "case_name": result.case_name,
        "position": result.position,
        "child_run_id": result.child_run_id,
        "status": result.status,
        "output": result.output,
        "assertions": result.assertions,
        "scores": result.scores,
        "labels": result.labels,
        "evaluator_failures": result.evaluator_failures,
        "duration_ms": result.duration_ms,
        "cost_usd": None if result.cost_usd is None else format(result.cost_usd, "f"),
        "error_code": result.error_code,
        "created_at": _iso(result.created_at),
    }


def eval_run_json(run: dict[str, Any], aggregates: dict[str, Any] | None = None) -> dict[str, Any]:
    """The run shape of GET /runs/{id} without `input` (the suite snapshot, possibly megabytes;
    GET /runs/{id} has it), plus `suite_id`, `suite_hash` and, when given, `aggregates`."""
    out = _run_json(run)
    out.pop("input", None)
    out["suite_id"] = run.get("eval_suite_id")
    out["suite_hash"] = run.get("eval_suite_hash")
    if aggregates is not None:
        out["aggregates"] = aggregates
    return out


def agent_run_started_json(started: StartedEvalRun) -> dict[str, Any]:
    """The body of POST /agents/{id}/evals/runs (and of the MCP tool eval.run.start)."""
    run = started.run
    snapshot_meta = _snapshot_meta(run)
    return {
        "run_id": str(run["id"]),
        "status": run["status"],
        "display_status": run["display_status"],
        "agent_id": str(run["agent_id"]),
        "version": run["agent_version"],
        "draft": run.get("draft_spec") is not None,
        "suite_id": run["eval_suite_id"],
        "suite_hash": run["eval_suite_hash"],
        "cases": len(snapshot_meta.get("cases") or ()),
        "max_concurrency": snapshot_meta.get("max_concurrency"),
        "trace_id": run["trace_id"],
        "warnings": started.warnings,
    }


def workflow_run_started_json(started: StartedEvalRun) -> dict[str, Any]:
    """The body of POST /workflows/{id}/evals/runs (and of the MCP tool eval.run.start)."""
    run = started.run
    snapshot_meta = _snapshot_meta(run)
    return {
        "run_id": str(run["id"]),
        "status": run["status"],
        "display_status": run["display_status"],
        "agent_id": None,
        "workflow_id": str(run["workflow_id"]),
        "target_kind": "workflow",
        "version": run["workflow_version"],
        "draft": False,
        "suite_id": run["eval_suite_id"],
        "suite_hash": run["eval_suite_hash"],
        "cases": len(snapshot_meta.get("cases") or ()),
        "max_concurrency": snapshot_meta.get("max_concurrency"),
        "trace_id": run["trace_id"],
        "warnings": [],
    }


def _snapshot_meta(run: dict[str, Any]) -> dict[str, Any]:
    return json.loads(run["input"])


def scorer_json(record: Any) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": record.description,
        "evaluator": record.evaluator,
        "agent_ids": list(record.agent_ids),
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
        "revision": record.revision,
    }


