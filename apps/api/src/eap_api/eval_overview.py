"""Tenant-wide eval overview queries (the Studio Datasets, Scorers and Experiments pages).

The agent-scoped eval routes (`eap_api.routes.evals`) list one agent's suites and runs. These read
across every agent of the tenant, always filtered by `tenant_id`:

- `list_suite_overview`: suites by (name, id) with the agent's name, and the latest run of each
  suite with its aggregates.
- `list_run_overview`: eval runs newest first, with the agent and suite names and aggregates.
- `list_scorer_overview`: the evaluators in use (suite level and case level) grouped by
  (kind, result name), the suites that use each, and its recent per-run values.

Pagination is keyset-based with opaque cursors; a cursor this API did not issue is `InvalidCursor`.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, cast, func, or_, select, text, true, tuple_
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, distinct_on

from eap_api.models import AgentModel
from eap_api.workflows.models import WorkflowModel
from eap_run_store import RunCursor
from eap_run_store.evals import EvalCaseModel, EvalStore, EvalSuiteModel, _suite_record
from eap_run_store.store import RunModel, _run_dict

MAX_HISTORY_POINTS = 10
# The newest eval runs of the scorers' suites read for score history (bounds the work per request).
MAX_HISTORY_RUNS = 200
SCORER_SUITES_SHOWN = 20


class InvalidCursor(ValueError):
    """A cursor this API did not issue."""


def _encode(value: list[str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(token: str, size: int) -> list[str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (binascii.Error, ValueError):
        raise InvalidCursor(token) from None
    if not isinstance(data, list) or len(data) != size or not all(isinstance(x, str) for x in data):
        raise InvalidCursor(token)
    return data


def _agents(db, tenant_id: str, agent_ids: set[str]) -> dict[str, AgentModel]:
    if not agent_ids:
        return {}
    rows = db.query(AgentModel).filter(AgentModel.tenant_id == tenant_id, AgentModel.id.in_(agent_ids)).all()
    return {row.id: row for row in rows}


def _workflows(db, tenant_id: str, workflow_ids: set[str]) -> dict[str, WorkflowModel]:
    if not workflow_ids:
        return {}
    rows = db.query(WorkflowModel).filter(WorkflowModel.tenant_id == tenant_id, WorkflowModel.id.in_(workflow_ids)).all()
    return {row.id: row for row in rows}


def target_names(db, tenant_id: str, suites) -> dict[tuple[str, str], str]:
    """(target_kind, target_id) -> name for the agents and workflows the suites target."""
    agents = _agents(db, tenant_id, {s.agent_id for s in suites if s.agent_id})
    workflows = _workflows(db, tenant_id, {s.workflow_id for s in suites if s.workflow_id})
    return {**{("agent", k): v.name for k, v in agents.items()}, **{("workflow", k): v.name for k, v in workflows.items()}}


def brief_aggregates(aggregates: dict[str, Any]) -> dict[str, Any]:
    return {key: aggregates[key] for key in ("cases", "passed", "failed", "errors", "pass_rate")}


# ---------------------------------------------------------------------------- suites


def _targets_visible(model, agent_ids: Sequence[str] | None, workflow_ids: Sequence[str] | None):
    """The per-resource access restriction (IAM P2b) on a row that targets an agent or a workflow (a suite, an eval run):
    its agent is in `agent_ids` or its workflow in `workflow_ids`. None = no limit on that kind (both None: no condition)."""
    if agent_ids is None and workflow_ids is None:
        return None
    agent_ok = true() if agent_ids is None else model.agent_id.in_(list(agent_ids))
    workflow_ok = true() if workflow_ids is None else model.workflow_id.in_(list(workflow_ids))
    return or_(and_(model.workflow_id.is_(None), agent_ok), and_(model.workflow_id.is_not(None), workflow_ok))


def list_suite_overview(
    store: EvalStore,
    db,
    tenant_id: str,
    *,
    agent_id: str | None,
    include_archived: bool,
    limit: int,
    after: str | None,
    visible_agent_ids: Sequence[str] | None = None,
    visible_workflow_ids: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """`(rows, next_cursor)`: each row is `{suite: EvalSuiteRecord, agent_name, agent_archived,
    last_run: {run, aggregates} | None}`."""
    cursor = _decode(after, 2) if after is not None else None
    if cursor is not None:
        try:
            uuid.UUID(cursor[1])
        except ValueError:
            raise InvalidCursor(after) from None
    counts = (
        select(EvalCaseModel.suite_id, func.count().label("n"))
        .where(EvalCaseModel.tenant_id == tenant_id)
        .group_by(EvalCaseModel.suite_id)
        .subquery()
    )
    stmt = (
        select(EvalSuiteModel, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.suite_id == EvalSuiteModel.id)
        .where(EvalSuiteModel.tenant_id == tenant_id)
        .order_by(EvalSuiteModel.name, EvalSuiteModel.id)
        .limit(limit)
    )
    if agent_id is not None:
        stmt = stmt.where(EvalSuiteModel.agent_id == agent_id)
    visible = _targets_visible(EvalSuiteModel, visible_agent_ids, visible_workflow_ids)
    if visible is not None:
        stmt = stmt.where(visible)
    if not include_archived:
        stmt = stmt.where(EvalSuiteModel.archived_at.is_(None))
    if cursor is not None:
        stmt = stmt.where(tuple_(EvalSuiteModel.name, EvalSuiteModel.id) > tuple_(cursor[0], cast(cursor[1], PG_UUID(as_uuid=False))))
    with store.Session() as session:
        found = [(_suite_record(row, int(n))) for row, n in session.execute(stmt).all()]
        last: dict[str, dict[str, Any]] = {}
        if found:
            newest = (
                select(RunModel)
                .ext(distinct_on(RunModel.eval_suite_id))
                .where(
                    RunModel.tenant_id == tenant_id,
                    RunModel.kind == "eval",
                    RunModel.eval_suite_id.in_([s.id for s in found]),
                )
                .order_by(RunModel.eval_suite_id, RunModel.created_at.desc(), RunModel.id.desc())
            )
            for run in session.execute(newest).scalars():
                last[run.eval_suite_id] = _run_dict(run)
    aggregates = store.aggregates_for_runs(tenant_id, [r["id"] for r in last.values()])
    agents = _agents(db, tenant_id, {s.agent_id for s in found if s.agent_id})
    names = target_names(db, tenant_id, found)
    rows = []
    for suite in found:
        agent = agents.get(suite.agent_id) if suite.agent_id else None
        run = last.get(suite.id)
        rows.append({
            "suite": suite,
            "agent_name": agent.name if agent else None,
            "agent_archived": bool(agent.archived) if agent else False,
            "target_name": names.get((suite.target_kind, suite.target_id)),
            "last_run": None if run is None else {"run": run, "aggregates": aggregates[str(run["id"])]},
        })
    next_cursor = _encode([found[-1].name, found[-1].id]) if len(found) == limit else None
    return rows, next_cursor


# ---------------------------------------------------------------------------- runs


def list_run_overview(
    store: EvalStore,
    db,
    tenant_id: str,
    *,
    agent_id: str | None,
    suite_id: str | None,
    limit: int,
    before: RunCursor | None,
    visible_agent_ids: Sequence[str] | None = None,
    visible_workflow_ids: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """`(rows, next_cursor)`: each row is `{run, aggregates, agent_name, suite_name}`."""
    stmt = (
        select(RunModel)
        .where(RunModel.tenant_id == tenant_id, RunModel.kind == "eval")
        .order_by(RunModel.created_at.desc(), RunModel.id.desc())
        .limit(limit)
    )
    if agent_id is not None:
        stmt = stmt.where(RunModel.agent_id == agent_id)
    visible = _targets_visible(RunModel, visible_agent_ids, visible_workflow_ids)
    if visible is not None:
        stmt = stmt.where(visible)
    if suite_id is not None:
        stmt = stmt.where(RunModel.eval_suite_id == suite_id)
    if before is not None:
        stmt = stmt.where(tuple_(RunModel.created_at, RunModel.id) < tuple_(before.created_at, before.id))
    with store.Session() as session:
        runs = [_run_dict(run) for run in session.execute(stmt).scalars()]
        suite_ids = {str(r["eval_suite_id"]) for r in runs if r.get("eval_suite_id")}
        suites = {}
        if suite_ids:
            suites = {
                row.id: row.name
                for row in session.execute(
                    select(EvalSuiteModel.id, EvalSuiteModel.name).where(
                        EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.id.in_(suite_ids)
                    )
                )
            }
    aggregates = store.aggregates_for_runs(tenant_id, [r["id"] for r in runs])
    agents = _agents(db, tenant_id, {str(r["agent_id"]) for r in runs if r.get("agent_id")})
    workflows = _workflows(db, tenant_id, {str(r["workflow_id"]) for r in runs if r.get("workflow_id")})
    rows = []
    for run in runs:
        agent = agents.get(str(run["agent_id"])) if run.get("agent_id") else None
        workflow = workflows.get(str(run["workflow_id"])) if run.get("workflow_id") else None
        rows.append({
            "run": run,
            "aggregates": aggregates[str(run["id"])],
            "agent_name": agent.name if agent else None,
            "target_kind": "workflow" if run.get("workflow_id") else "agent",
            "target_id": str(run["workflow_id"] if run.get("workflow_id") else run["agent_id"]),
            "target_name": workflow.name if workflow else (agent.name if agent else None),
            "suite_name": suites.get(str(run.get("eval_suite_id"))),
        })
    next_cursor = RunCursor.after(runs[-1]).encode() if len(runs) == limit else None
    return rows, next_cursor


# ---------------------------------------------------------------------------- scorers


def _run_values(store: EvalStore, tenant_id: str, run_ids: list[str]) -> dict[str, dict[str, tuple[str, float]]]:
    """run id -> result name -> ("score", mean score) or ("pass_rate", share of true assertions).
    One query per kind, aggregated in SQL (no case rows are loaded)."""
    out: dict[str, dict[str, tuple[str, float]]] = {r: {} for r in run_ids}
    if not run_ids:
        return out
    params = {"t": tenant_id, "ids": run_ids}
    with store.Session() as session:
        assertions = session.execute(
            text(
                "SELECT r.run_id, e.key, avg(CASE WHEN e.value->>'value' = 'true' THEN 1.0 ELSE 0.0 END) "
                "FROM eval_case_results r, jsonb_each(r.assertions) e "
                "WHERE r.tenant_id = :t AND r.run_id = ANY(:ids) GROUP BY r.run_id, e.key"
            ),
            params,
        )
        for run_id, key, value in assertions:
            out[run_id][key] = ("pass_rate", round(float(value), 6))
        scores = session.execute(
            text(
                "SELECT r.run_id, e.key, avg((e.value->>'value')::float8) "
                "FROM eval_case_results r, jsonb_each(r.scores) e "
                "WHERE r.tenant_id = :t AND r.run_id = ANY(:ids) GROUP BY r.run_id, e.key"
            ),
            params,
        )
        for run_id, key, value in scores:
            out[run_id][key] = ("score", round(float(value), 6))  # a score wins over its assertion
    return out


def list_scorer_overview(
    store: EvalStore, db, tenant_id: str, *, limit: int, after: str | None,
    visible_agent_ids: Sequence[str] | None = None, visible_workflow_ids: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """`(scorers, next_cursor)` ordered by (kind, name). A scorer is an evaluator `kind` with its
    result name (its `name`, else the kind, as the eval runtime names it) used by at least one
    active suite, at suite or case level."""
    cursor = _decode(after, 2) if after is not None else None
    suite_query = select(EvalSuiteModel).where(EvalSuiteModel.tenant_id == tenant_id, EvalSuiteModel.archived_at.is_(None))
    visible = _targets_visible(EvalSuiteModel, visible_agent_ids, visible_workflow_ids)
    if visible is not None:
        suite_query = suite_query.where(visible)
    with store.Session() as session:
        suites = {row.id: row for row in session.execute(suite_query).scalars()}
        case_rows = session.execute(
            select(EvalCaseModel.suite_id, EvalCaseModel.evaluators).where(
                EvalCaseModel.tenant_id == tenant_id,
                EvalCaseModel.suite_id.in_(list(suites)),
                func.jsonb_array_length(EvalCaseModel.evaluators) > 0,
            )
        ).all() if suites else []
    uses: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    def note(spec: Any, suite_id: str, level: str) -> None:
        if not isinstance(spec, dict) or not isinstance(spec.get("kind"), str):
            return
        key = (spec["kind"], spec.get("name") or spec["kind"])
        entry = uses.setdefault(key, {}).setdefault(suite_id, {"cases": 0})
        if level == "case":
            entry["cases"] += 1

    for suite in suites.values():
        for spec in suite.evaluators or []:
            note(spec, suite.id, "suite")
    for suite_id, evaluators in case_rows:
        for spec in evaluators or []:
            note(spec, suite_id, "case")
    keys = sorted(uses)
    if cursor is not None:
        keys = [k for k in keys if k > (cursor[0], cursor[1])]
    page = keys[:limit]
    next_cursor = _encode(list(page[-1])) if len(keys) > limit else None
    if not page:
        return [], None

    suite_ids = {sid for key in page for sid in uses[key]}
    with store.Session() as session:
        runs = [
            _run_dict(run)
            for run in session.execute(
                select(RunModel)
                .where(RunModel.tenant_id == tenant_id, RunModel.kind == "eval", RunModel.eval_suite_id.in_(suite_ids))
                .order_by(RunModel.created_at.desc(), RunModel.id.desc())
                .limit(MAX_HISTORY_RUNS)
            ).scalars()
        ]
    values = _run_values(store, tenant_id, [str(r["id"]) for r in runs])
    names = target_names(db, tenant_id, [suites[s] for s in suite_ids])  # ORM rows quack alike
    agents = _agents(db, tenant_id, {suites[s].agent_id for s in suite_ids if suites[s].agent_id})

    rows = []
    for key in page:
        kind, name = key
        suite_rows = sorted(uses[key].items(), key=lambda item: (suites[item[0]].name, item[0]))
        history = []
        for run in runs:
            if len(history) == MAX_HISTORY_POINTS:
                break
            if str(run["eval_suite_id"]) not in uses[key]:
                continue
            point = values[str(run["id"])].get(name)
            if point is None:
                continue
            history.append({
                "run_id": str(run["id"]),
                "suite_id": str(run["eval_suite_id"]),
                "agent_id": str(run["agent_id"]) if run.get("agent_id") else None,
                "created_at": run["created_at"].isoformat(),
                "metric": point[0],
                "value": point[1],
            })
        rows.append({
            "kind": kind,
            "name": name,
            "suite_count": len(suite_rows),
            "suites": [
                {
                    "id": sid,
                    "name": suites[sid].name,
                    "agent_id": suites[sid].agent_id,
                    "agent_name": agents[suites[sid].agent_id].name if suites[sid].agent_id in agents else None,
                    "workflow_id": suites[sid].workflow_id,
                    "target_kind": "workflow" if suites[sid].workflow_id else "agent",
                    "target_name": names.get(
                        ("workflow", suites[sid].workflow_id) if suites[sid].workflow_id else ("agent", suites[sid].agent_id)
                    ),
                    "case_level_uses": entry["cases"],
                }
                for sid, entry in suite_rows[:SCORER_SUITES_SHOWN]
            ],
            "history": history,
        })
    return rows, next_cursor
