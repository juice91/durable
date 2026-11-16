"""Test suites (evals) API (agent capabilities milestone, AC.12).

Suites belong to an agent; cases to a suite. Every route is tenant-scoped (another tenant's suite,
case or run reads as not found, 404), and the agent-scoped routes also check that the suite or run
belongs to the agent in the path (another agent's is a 404 too).

Suites and cases:
- `GET  /agents/{agent_id}/eval-suites[?include_archived=true]`, `POST /agents/{agent_id}/eval-suites`
- `GET | PATCH | DELETE /eval-suites/{suite_id}` (GET includes the cases)
- `GET | POST /eval-suites/{suite_id}/cases`, `GET | PATCH | DELETE /eval-suites/{suite_id}/cases/{case_id}`
- `GET  /eval-suites/{suite_id}/export?format=yaml|json` and `POST /eval-suites/{suite_id}/import`:
  the `pydantic_evals` dataset file (eap_api.eval_format). Import takes the file as the raw body
  (at most 1 MiB; `?format=`, else JSON when the Content-Type says json, else YAML), checks the
  suite first (404 / 409 before any parse), parses off the event loop with bounded cost, and upserts
  the cases by name in one transaction (all or nothing); the file's `evaluators`, when present,
  replace the suite's.

Eval runs:
- `POST /agents/{agent_id}/evals/runs {run_id, suite_id, version, max_concurrency}`: freezes the
  suite into a snapshot (`runs.input`), records an `eval` run against the version (null: the latest)
  or the draft (validated like a publish and snapshotted), and enqueues `eap.eval_run` (launcher
  `launch_eval`). Idempotent by the client `run_id`: 201 when created, 200 for a retry of the same
  request (relaunched as recorded, so an enqueue failure heals), 409 for a different request.
  The response lists `warnings`: the target's tools with side effects that run for real.
- `GET  /agents/{agent_id}/evals/warnings[?version=N|draft]`: the same warnings, before a run.
- `GET  /agents/{agent_id}/evals/runs[?suite_id=&limit=&before=]` (newest first, with aggregates,
  `next_cursor`), `GET .../evals/runs/{run_id}` (with aggregates),
  `GET .../evals/runs/{run_id}/results[?limit=&after=]` (by position, `next_cursor`).
- `GET  /agents/{agent_id}/evals/compare?base=<run_id>&target=<run_id>`.

Workflow targets (parity round 2, D5): a suite targets an agent or a workflow, never both.
- `GET | POST /workflows/{workflow_id}/evals/suites`: the workflow's suites (same body as the agent's).
  A case `input` of a workflow suite is JSON text that must parse to an object valid against the
  workflow's input schema (422 `{code: "input_schema_errors", case, errors}`).
- `POST /workflows/{workflow_id}/evals/runs {run_id, suite_id, version, max_concurrency}`: as the
  agent route, against a published workflow version (null: the latest; `"draft"` is a 422). The eval
  run is stored with `kind="eval"`, `workflow_id` and `workflow_version`, `agent_id` null.
The agent routes answer 404 for a workflow suite and the other way round.

Across the tenant's agents (the Studio Datasets, Scorers and Experiments pages; `eap_api.eval_overview`):
- `GET  /eval-suites[?agent_id=&include_archived=&limit=&after=]`: suites by name with `agent_name`,
  `case_count` and `last_run` (the suite's newest run with `{cases, passed, failed, errors, pass_rate}`).
- `GET  /evals/runs[?agent_id=&suite_id=&limit=&before=]`: eval runs newest first with `agent_name`,
  `suite_name` and aggregates.
- `GET  /evals/scorers[?limit=&after=]`: evaluators in use by (kind, name) with the suites that use
  them and recent per-run values (`score`: mean score, `pass_rate`: share of passed assertions).
All three are paginated with an opaque `next_cursor` (422 for a cursor this API did not issue).

Errors: 404 (agent, suite, case, version or run not found), 409 (name already used, suite archived,
id already used, no published version or no draft), 413 (an eval case's hand-off payload over
EAP_MAX_RUN_PAYLOAD_BYTES), 422 (invalid suite, case, evaluator, file or draft), 503 (the run was
recorded but could not be enqueued; retry with the same run_id).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.agent_archive import refuse_if_archived
from eap_api.auth import Principal, current_principal
from eap_api import eval_overview
from eap_api.eval_format import (
    MAX_IMPORT_BYTES,
    CsvFormatError,
    EvalFormatError,
    ExportFormat,
    dataset_dict,
    dump_dataset,
    parse_csv_cases,
    parse_file,
    read_dataset,
)
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute, read_capped_body, unstorable_strings
from eap_api.routes.runs import SessionDep, StoresDep, _run_json
from eap_api.routes.workflow_runs import input_schema_errors, non_finite_errors
from eap_api.run_options import (
    DRAFT,
    RunTarget,
    check_spec_payload_size,
    resolve_target,
    resolve_workflow_target,
    stored_target,
)
from eap_api.workflows.models import WorkflowModel
from eap_api.schemas import (
    EvalCaseCreate,
    EvalCaseUpdate,
    EvalRunCreate,
    EvalSuiteCreate,
    EvalSuiteUpdate,
    ErrorDetail,
)
from eap_observability import current_trace_context, log_failure
from eap_run_store import ENQUEUE_FAILED_ERROR, InvalidRunCursor, RunCursor, RunIdConflict
from eap_run_store.cursor import MAX_CURSOR_LENGTH
from eap_run_store.evals import (
    EvalCaseLimitExceeded,
    EvalCaseNameConflict,
    EvalCaseNotFound,
    EvalCaseRecord,
    EvalCaseResult,
    EvalRunNotFound,
    EvalStore,
    EvalSuiteArchived,
    EvalSuiteNameConflict,
    EvalSuiteNotFound,
    EvalSuiteRecord,
    MAX_LIST_RESULTS,
    MAX_LIST_RUNS,
    dump_evaluators,
)
from eap_processors import Re2PatternError, compile_re2
from eap_schemas import AgentSpec
from eap_schemas.evals import EvalSuiteInvalid, EvalSuiteSnapshot
from eap_schemas.models_policy import ModelNotAllowed, ModelPolicy
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflows import WorkflowSpec

log = logging.getLogger("eap_api")

AGENT_NOT_FOUND = "Agent not found"
RUN_ID_IN_USE = "run_id is already in use"
RUN_ID_OTHER_REQUEST = "run_id is already used by a different run request"
SUITE_ID_IN_USE = "suite_id is already in use"
CASE_ID_IN_USE = "case_id is already in use"
IMPORT_TOO_LARGE = f"the suite file must be at most {MAX_IMPORT_BYTES // (1024 * 1024)} MiB"
IMPORT_NOT_UTF8 = "the suite file must be UTF-8 text"
EVALUATORS_NULL = "evaluators cannot be null; send [] to remove every suite evaluator"
NAME_NULL = "name cannot be null"
INVALID_CURSOR = "cursor is not valid; pass the previous page's next_cursor verbatim"
ENQUEUE_FAILED_DETAIL = "Eval run recorded but could not be started; retry the same request (same run_id)"

PrincipalDep = Annotated[Principal, Depends(current_principal)]
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]

_ERRORS = {404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}}
_json_router = APIRouter(tags=["evals"], responses=_ERRORS, route_class=GuardedJsonRoute)
# The import takes a raw YAML or JSON file (not a JSON body), with its own 1 MiB cap answered as a 422.
_file_router = APIRouter(tags=["evals"], responses=_ERRORS, route_class=APIRoute)


def get_eval_store(request: Request, stores: StoresDep) -> EvalStore:
    """The EvalStore on the run stores' engine, made once per app."""
    store = getattr(request.app.state, "eval_store", None)
    if store is None:
        run_store, _ = stores
        store = request.app.state.eval_store = EvalStore(run_store.engine)
    return store


EvalStoreDep = Annotated[EvalStore, Depends(get_eval_store)]


# ---------------------------------------------------------------------------- helpers


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dump(evaluators: Any) -> list[dict[str, Any]]:
    """As stored: optional None fields left out, required ones kept even when null."""
    return dump_evaluators(evaluators)


def suite_json(suite: EvalSuiteRecord, target_name: str | None = None) -> dict[str, Any]:
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
    }


def case_json(case: EvalCaseRecord) -> dict[str, Any]:
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
    }


def result_json(result: EvalCaseResult) -> dict[str, Any]:
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


def _http(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _store_error(exc: Exception) -> HTTPException:
    """The HTTP answer for an EvalStore error."""
    if isinstance(exc, (EvalSuiteNotFound, EvalCaseNotFound, EvalRunNotFound)):
        return _http(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (EvalSuiteNameConflict, EvalCaseNameConflict, EvalSuiteArchived)):
        return _http(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, CsvFormatError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "csv_invalid", "row": exc.row, "message": exc.message},
        )
    if isinstance(exc, (EvalSuiteInvalid, EvalCaseLimitExceeded, EvalFormatError)):
        return _http(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    raise exc


_STORE_ERRORS = (
    EvalSuiteNotFound,
    EvalCaseNotFound,
    EvalRunNotFound,
    EvalSuiteNameConflict,
    EvalCaseNameConflict,
    EvalSuiteArchived,
    EvalSuiteInvalid,
    EvalCaseLimitExceeded,
    EvalFormatError,
)


def _agent_or_404(db, tenant_id: str, agent_id: uuid.UUID) -> AgentModel:
    agent = db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    if agent is None:
        raise _http(status.HTTP_404_NOT_FOUND, AGENT_NOT_FOUND)
    return agent


WORKFLOW_NOT_FOUND = "Workflow not found"


def _workflow_or_404(db, tenant_id: str, workflow_id: uuid.UUID) -> WorkflowModel:
    workflow = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
        .first()
    )
    if workflow is None or workflow.archived:
        raise _http(status.HTTP_404_NOT_FOUND, WORKFLOW_NOT_FOUND)
    return workflow


def _target_name(db, tenant_id: str, suite: EvalSuiteRecord) -> str | None:
    names = eval_overview.target_names(db, tenant_id, [suite])
    return names.get((suite.target_kind, suite.target_id))


def workflow_input_problem(spec: WorkflowSpec | None, case_name: str, text: str) -> HTTPException | None:
    """A 422 (`{code: "input_schema_errors", case, errors}`) when a workflow case's `input` is not
    JSON text of an object that satisfies the spec's input_schema (compact errors: input path and
    failing keyword, never the value); None when it is fine. `spec` None checks only that it is a
    JSON object (the workflow has no published version yet)."""
    try:
        value = json.loads(text)
    except ValueError:
        errors = [{"loc": ["input"], "msg": "must be JSON text", "type": "json_invalid"}]
    else:
        if not isinstance(value, dict):
            errors = [{"loc": ["input"], "msg": "must be a JSON object", "type": "json_object"}]
        elif spec is None:
            return None
        else:
            errors = non_finite_errors(value) or input_schema_errors(spec.input_schema, value)
    if not errors:
        return None
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "input_schema_errors", "case": case_name, "errors": errors},
    )


def _latest_workflow_spec(db, tenant_id: str, workflow_id: str) -> WorkflowSpec | None:
    from eap_api.workflows.models import WorkflowVersionModel

    row = (
        db.query(WorkflowVersionModel)
        .filter(WorkflowVersionModel.workflow_id == workflow_id, WorkflowVersionModel.tenant_id == tenant_id)
        .order_by(WorkflowVersionModel.version.desc())
        .first()
    )
    return None if row is None else WorkflowSpec.model_validate(row.spec)


def side_effect_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """The target's tools that run for real during an evaluation: built-in tools declared with
    `side_effect` and every MCP tool (their effects are unknown), unless the spec gates them with
    an approval (a gated call is denied in an eval run, so it never runs)."""
    gated = set(spec.approvals.tools) if spec.approvals is not None else set()
    warnings: list[dict[str, str]] = []
    for tool_id in spec.tools:
        declaration = TOOL_DECLARATIONS.get(tool_id)
        if declaration is not None and declaration.side_effect and tool_id not in gated:
            warnings.append({
                "code": "tool_side_effect",
                "tool": tool_id,
                "kind": "builtin",
                "message": f"Tool {tool_id} has side effects; every case that calls it runs it for real",
            })
    for ref in spec.mcp_tools:
        name = f"{ref.server}/{ref.tool}"
        if name not in gated:
            warnings.append({
                "code": "mcp_tool_side_effect",
                "tool": name,
                "kind": "mcp",
                "message": f"MCP tool {name} may have side effects; every case that calls it runs it for real",
            })
    return warnings


REGEX_INVALID = "pattern is not valid RE2 syntax or is too complex"


def check_regex_evaluators(evaluators: Any, path: str) -> None:
    """EvalSuiteInvalid (422) naming `path.<i>.pattern` for a `regex` evaluator whose pattern RE2
    refuses (eap_processors.compile_re2, as the worker matches it). The message is fixed: the
    pattern, which RE2's own error text quotes, is never echoed. Models or dicts."""
    for index, evaluator in enumerate(evaluators or ()):
        kind = evaluator.get("kind") if isinstance(evaluator, dict) else getattr(evaluator, "kind", None)
        if kind != "regex":
            continue
        pattern = evaluator.get("pattern") if isinstance(evaluator, dict) else evaluator.pattern
        if not isinstance(pattern, str):
            continue  # the contract's own validation reports it
        try:
            compile_re2(pattern, allow_empty_match=True)
        except Re2PatternError:
            raise EvalSuiteInvalid(REGEX_INVALID, f"{path}.{index}.pattern") from None


def _check_case_regexes(cases: Any, path: str = "cases") -> None:
    for i, case in enumerate(cases or ()):
        evaluators = case.get("evaluators") if isinstance(case, dict) else getattr(case, "evaluators", None)
        name = case.get("name") if isinstance(case, dict) else getattr(case, "name", None)
        check_regex_evaluators(evaluators, f"{path}.{name if isinstance(name, str) else i}.evaluators")


def _check_judge_models(snapshot: EvalSuiteSnapshot) -> None:
    """422 when an llm_judge evaluator names a model outside the platform allowlist."""
    policy = ModelPolicy.from_env()
    for path, evaluators in [("evaluators", snapshot.evaluators)] + [
        (f"cases.{c.name}.evaluators", c.evaluators) for c in snapshot.ordered_cases()
    ]:
        for evaluator in evaluators:
            if evaluator.kind == "llm_judge" and not policy.allows(evaluator.model.provider, evaluator.model.name):
                raise _http(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"judge model at {path}: {ModelNotAllowed(evaluator.model.provider, evaluator.model.name)}",
                )


def _check_case_payloads(target: RunTarget, snapshot: EvalSuiteSnapshot) -> None:
    """413 when a case's child run hand-off (the spec plus the case input) would exceed the run
    payload limit: checked here, before the eval run exists, for the largest case input. For a
    workflow target, 422 when a case input no longer fits the version's input schema."""
    largest = max(snapshot.cases, key=lambda c: len(c.input.encode("utf-8")))
    check_spec_payload_size(target.spec_dict, largest.input)
    if target.kind == "workflow":
        # Every case input must still satisfy the target version's input schema (it may have changed
        # since the case was written): 422 naming the first failing case, before the run exists.
        for case in snapshot.ordered_cases():
            problem = workflow_input_problem(target.spec, case.name, case.input)
            if problem is not None:
                raise problem


def _version_query(version: str | None) -> int | str | None:
    if version is None or version == DRAFT:
        return version
    if version.isascii() and version.isdecimal() and int(version) >= 1:
        return int(version)
    raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, 'version must be a version number of at least 1 or "draft"')


# ---------------------------------------------------------------------------- suites


@_json_router.get("/agents/{agent_id}/eval-suites")
def list_suites(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    include_archived: bool = False,
) -> JSONResponse:
    """The agent's suites by name, with their case counts (archived ones only on request)."""
    _agent_or_404(db, principal.tenant_id, agent_id)
    suites = store.list_suites(principal.tenant_id, agent_id, include_archived=include_archived)
    return JSONResponse(content={"items": [suite_json(s) for s in suites]})


OVERVIEW_MAX_LIMIT = 100


def _overview_agent(db, tenant_id: str, agent_id: uuid.UUID | None) -> str | None:
    if agent_id is None:
        return None
    return _agent_or_404(db, tenant_id, agent_id).id


@_json_router.get("/eval-suites")
def list_tenant_suites(
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    agent_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=OVERVIEW_MAX_LIMIT)] = 50,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The tenant's suites across agents by name, with the agent's name and the latest run of each."""
    tenant_id = principal.tenant_id
    agent = _overview_agent(db, tenant_id, agent_id)
    try:
        rows, next_cursor = eval_overview.list_suite_overview(
            store, db, tenant_id, agent_id=agent, include_archived=include_archived, limit=limit, after=after
        )
    except eval_overview.InvalidCursor:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    items = []
    for row in rows:
        item = suite_json(row["suite"], row["target_name"])
        item["agent_name"] = row["agent_name"]
        item["agent_archived"] = row["agent_archived"]
        last = row["last_run"]
        item["last_run"] = None if last is None else {
            **{k: v for k, v in eval_run_json(last["run"]).items()
               if k in ("run_id", "version", "status", "display_status", "created_at", "updated_at")},
            "aggregates": eval_overview.brief_aggregates(last["aggregates"]),
        }
        items.append(item)
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


@_json_router.get("/evals/runs")
def list_tenant_eval_runs(
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    agent_id: uuid.UUID | None = None,
    suite_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RUNS)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The tenant's eval runs across agents, newest first, each with its aggregates and the agent
    and suite names. `next_cursor` is set when a full page came back (pass it as `before`)."""
    tenant_id = principal.tenant_id
    agent = _overview_agent(db, tenant_id, agent_id)
    cursor = None
    if before is not None:
        try:
            cursor = RunCursor.decode(before)
        except InvalidRunCursor:
            raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    rows, next_cursor = eval_overview.list_run_overview(
        store, db, tenant_id, agent_id=agent, suite_id=None if suite_id is None else str(suite_id),
        limit=limit, before=cursor,
    )
    items = [
        {
            **eval_run_json(row["run"], row["aggregates"]),
            "agent_name": row["agent_name"],
            "suite_name": row["suite_name"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "target_name": row["target_name"],
        }
        for row in rows
    ]
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


@_json_router.get("/evals/scorers")
def list_scorers(
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    limit: Annotated[int, Query(ge=1, le=OVERVIEW_MAX_LIMIT)] = 50,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The evaluators the tenant's active suites use, by (kind, name), each with its suites and
    recent per-run values from their eval runs."""
    try:
        rows, next_cursor = eval_overview.list_scorer_overview(store, db, principal.tenant_id, limit=limit, after=after)
    except eval_overview.InvalidCursor:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    return JSONResponse(content={"items": rows, "next_cursor": next_cursor})


@_json_router.post("/agents/{agent_id}/eval-suites", status_code=status.HTTP_201_CREATED)
def create_suite(
    agent_id: uuid.UUID, body: EvalSuiteCreate, principal: PrincipalDep, db: SessionDep, store: EvalStoreDep
) -> JSONResponse:
    """Create a suite (201). A repeat with the same `suite_id`, agent and name answers 200 with the
    suite; a `suite_id` used otherwise is a 409."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    return _create_suite(store, principal.tenant_id, "agent", agent_id, agent.name, body)


@_json_router.get("/workflows/{workflow_id}/evals/suites")
def list_workflow_suites(
    workflow_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    include_archived: bool = False,
) -> JSONResponse:
    """The workflow's suites by name, with their case counts (archived ones only on request)."""
    workflow = _workflow_or_404(db, principal.tenant_id, workflow_id)
    suites = store.list_workflow_suites(principal.tenant_id, workflow_id, include_archived=include_archived)
    return JSONResponse(content={"items": [suite_json(s, workflow.name) for s in suites]})


@_json_router.post("/workflows/{workflow_id}/evals/suites", status_code=status.HTTP_201_CREATED)
def create_workflow_suite(
    workflow_id: uuid.UUID, body: EvalSuiteCreate, principal: PrincipalDep, db: SessionDep, store: EvalStoreDep
) -> JSONResponse:
    """Create a suite that targets the workflow (201); idempotent by `suite_id` as the agent route."""
    workflow = _workflow_or_404(db, principal.tenant_id, workflow_id)
    return _create_suite(store, principal.tenant_id, "workflow", workflow_id, workflow.name, body)


def _create_suite(
    store: EvalStore, tenant_id: str, kind: str, target_id: uuid.UUID, target_name: str, body: EvalSuiteCreate
) -> JSONResponse:
    existing = _same_suite(store, tenant_id, kind, target_id, body)
    if existing is not None:
        return JSONResponse(status_code=status.HTTP_200_OK, content=suite_json(existing, target_name))
    try:
        check_regex_evaluators(body.evaluators, "evaluators")
        make = store.create_suite if kind == "agent" else store.create_workflow_suite
        suite = make(
            tenant_id, target_id, body.name, description=body.description, evaluators=body.evaluators,
            suite_id=body.suite_id,
        )
    except (EvalSuiteNameConflict, IntegrityError) as exc:
        # A concurrent identical request may have won the insert: answer its suite (200). Otherwise
        # the name is taken (409, the store's message) or the id is used elsewhere (409).
        existing = _same_suite(store, tenant_id, kind, target_id, body)
        if existing is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=suite_json(existing, target_name))
        if isinstance(exc, EvalSuiteNameConflict):
            raise _store_error(exc) from None
        raise _http(status.HTTP_409_CONFLICT, SUITE_ID_IN_USE) from None
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=suite_json(suite, target_name))


def _same_suite(
    store: EvalStore, tenant_id: str, kind: str, target_id: uuid.UUID, body: EvalSuiteCreate
) -> EvalSuiteRecord | None:
    """The tenant's suite with the body's `suite_id` when it is this request's suite (same target and
    name), None when there is none; 409 when the id is used otherwise."""
    if body.suite_id is None:
        return None
    try:
        existing = store.get_suite(tenant_id, body.suite_id)
    except EvalSuiteNotFound:
        return None
    if existing.target_kind != kind or existing.target_id != str(target_id) or existing.name != body.name:
        raise _http(status.HTTP_409_CONFLICT, SUITE_ID_IN_USE)
    return existing


@_json_router.get("/eval-suites/{suite_id}")
def get_suite(suite_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, store: EvalStoreDep) -> JSONResponse:
    """The suite with its cases in execution order."""
    try:
        suite = store.get_suite(principal.tenant_id, suite_id)
        cases = store.list_cases(principal.tenant_id, suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    name = _target_name(db, principal.tenant_id, suite)
    return JSONResponse(content={**suite_json(suite, name), "cases": [case_json(c) for c in cases]})


@_json_router.patch("/eval-suites/{suite_id}")
def update_suite(
    suite_id: uuid.UUID, body: EvalSuiteUpdate, principal: PrincipalDep, db: SessionDep, store: EvalStoreDep
) -> JSONResponse:
    """Change the fields present in the body. An archived suite accepts only `archived: false` (409)."""
    fields = body.model_fields_set
    if "evaluators" in fields and body.evaluators is None:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, EVALUATORS_NULL)
    if "name" in fields and body.name is None:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, NAME_NULL)
    kwargs: dict[str, Any] = {k: getattr(body, k) for k in ("name", "description", "evaluators") if k in fields}
    if "archived" in fields and body.archived is not None:
        kwargs["archived"] = body.archived
    try:
        check_regex_evaluators(kwargs.get("evaluators"), "evaluators")
        suite = store.update_suite(principal.tenant_id, suite_id, **kwargs)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content=suite_json(suite, _target_name(db, principal.tenant_id, suite)))


@_json_router.delete("/eval-suites/{suite_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_suite(suite_id: uuid.UUID, principal: PrincipalDep, store: EvalStoreDep) -> Response:
    """Delete the suite and its cases (204). Its eval runs and their results stay."""
    try:
        store.delete_suite(principal.tenant_id, suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- cases


def _check_workflow_case_input(db, store: EvalStore, tenant_id: str, suite_id, name: str, text: str) -> None:
    """For a workflow suite, 422 unless `text` is a JSON object valid for the latest published
    version's input schema (only the object check while the workflow has none). Agent suites: no-op;
    an unknown suite is left to the store's 404."""
    try:
        suite = store.get_suite(tenant_id, suite_id)
    except EvalSuiteNotFound:
        return
    if suite.workflow_id is None:
        return
    problem = workflow_input_problem(_latest_workflow_spec(db, tenant_id, suite.workflow_id), name, text)
    if problem is not None:
        raise problem


@_json_router.get("/eval-suites/{suite_id}/cases")
def list_cases(suite_id: uuid.UUID, principal: PrincipalDep, store: EvalStoreDep) -> JSONResponse:
    try:
        cases = store.list_cases(principal.tenant_id, suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content={"items": [case_json(c) for c in cases]})


@_json_router.post("/eval-suites/{suite_id}/cases", status_code=status.HTTP_201_CREATED)
def add_case(
    suite_id: uuid.UUID, body: EvalCaseCreate, principal: PrincipalDep, db: SessionDep, store: EvalStoreDep
) -> JSONResponse:
    """Add a case (201): 409 for a name already in the suite or an archived suite, 422 for an
    invalid case (for a workflow suite, an input that is not a JSON object valid for the workflow's
    input schema) or past the 500-case cap."""
    case = body.model_dump(exclude={"case_id", "position"})
    try:
        _check_workflow_case_input(db, store, principal.tenant_id, suite_id, body.name, body.input)
        check_regex_evaluators(body.evaluators, "case.evaluators")
        record = store.add_case(
            principal.tenant_id, suite_id, case, position=body.position, case_id=body.case_id
        )
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    except IntegrityError:
        raise _http(status.HTTP_409_CONFLICT, CASE_ID_IN_USE) from None
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=case_json(record))


@_json_router.get("/eval-suites/{suite_id}/cases/{case_id}")
def get_case(suite_id: uuid.UUID, case_id: uuid.UUID, principal: PrincipalDep, store: EvalStoreDep) -> JSONResponse:
    try:
        record = store.get_case(principal.tenant_id, case_id, suite_id=suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content=case_json(record))


@_json_router.patch("/eval-suites/{suite_id}/cases/{case_id}")
def update_case(
    suite_id: uuid.UUID,
    case_id: uuid.UUID,
    body: EvalCaseUpdate,
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
) -> JSONResponse:
    """Change the fields present in the body; the merged case is validated (422)."""
    kwargs = {k: getattr(body, k) for k in body.model_fields_set}
    if kwargs.get("position", 0) is None:
        kwargs.pop("position")
    try:
        if isinstance(kwargs.get("input"), str):
            name = kwargs.get("name")
            if not isinstance(name, str):
                name = store.get_case(principal.tenant_id, case_id, suite_id=suite_id).name
            _check_workflow_case_input(db, store, principal.tenant_id, suite_id, name, kwargs["input"])
        check_regex_evaluators(kwargs.get("evaluators"), "case.evaluators")
        record = store.update_case(principal.tenant_id, case_id, suite_id=suite_id, **kwargs)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content=case_json(record))


@_json_router.delete("/eval-suites/{suite_id}/cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_case(suite_id: uuid.UUID, case_id: uuid.UUID, principal: PrincipalDep, store: EvalStoreDep) -> Response:
    try:
        store.delete_case(principal.tenant_id, case_id, suite_id=suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- import and export


def _filename(name: str, fmt: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:80] or "suite"
    return f"{stem}.{fmt}"


@_json_router.get("/eval-suites/{suite_id}/export")
def export_suite(
    suite_id: uuid.UUID,
    principal: PrincipalDep,
    store: EvalStoreDep,
    fmt: Annotated[Literal["yaml", "json"], Query(alias="format")] = "yaml",
) -> Response:
    """The suite as a `pydantic_evals` dataset file (attachment), YAML by default."""
    try:
        suite = store.get_suite(principal.tenant_id, suite_id)
        cases = store.list_cases(principal.tenant_id, suite_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    content = dump_dataset(dataset_dict(suite.name, suite.evaluators, cases), fmt)
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json" if fmt == "json" else "application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{_filename(suite.name, fmt)}"'},
    )


def _import_sync(
    store: EvalStore, engine, tenant_id: str, suite_id: uuid.UUID, text: str, fmt: ExportFormat
) -> dict[str, Any]:
    """Off the event loop. The suite is checked before the file is parsed: an unknown or foreign
    suite is a 404 and an archived one a 409, whatever the file holds, and no parse runs for them.
    Everything is validated before the one transaction that writes (all or nothing)."""
    suite = store.get_suite(tenant_id, suite_id)
    if suite.archived:
        raise EvalSuiteArchived(suite.id)
    if fmt == "csv":
        cases, evaluators = parse_csv_cases(text), None
        issues = unstorable_strings(cases)
    else:
        data = parse_file(text, fmt)
        issues = unstorable_strings(data)
        if not issues:
            cases, evaluators = read_dataset(data)
    if issues:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=issues)
    check_regex_evaluators(evaluators, "evaluators")
    _check_case_regexes(cases)
    if suite.workflow_id is not None:
        with Session(engine) as db:  # this thread's own short session, not the request's
            spec = _latest_workflow_spec(db, tenant_id, suite.workflow_id)
        for case in cases:
            problem = workflow_input_problem(spec, case["name"], case["input"])
            if problem is not None:
                raise problem
    kwargs: dict[str, Any] = {} if evaluators is None else {"evaluators": evaluators}
    result = store.upsert_cases(tenant_id, suite_id, cases, keep_case_evaluators=fmt == "csv", **kwargs)
    suite = store.get_suite(tenant_id, suite_id)
    with Session(engine) as db:
        name = _target_name(db, tenant_id, suite)
    return {"created": result.created, "updated": result.updated, "suite": suite_json(suite, name)}


@_file_router.post("/eval-suites/{suite_id}/import")
async def import_suite(
    suite_id: uuid.UUID,
    request: Request,
    principal: PrincipalDep,
    store: EvalStoreDep,
    fmt: Annotated[Literal["yaml", "json", "csv"] | None, Query(alias="format")] = None,
) -> JSONResponse:
    """Import a `pydantic_evals` dataset file, or a CSV file (the raw body, at most 1 MiB): cases are
    upserted by name in one transaction, all or nothing (an existing case keeps its id and position,
    new ones are appended); the dataset file's `evaluators`, when present, replace the suite's. A CSV
    file (`?format=csv`, or Content-Type text/csv) has the columns `name,input[,expected_output]
    [,metadata]` and leaves the evaluators alone. 200 `{created, updated, suite}`; 422 for a file
    that is too large, not UTF-8, not parseable, an unknown evaluator or an invalid case (a CSV
    problem is `{code: "csv_invalid", row, message}`; for a workflow suite a case input that does not
    fit the workflow's input schema is `{code: "input_schema_errors", case, errors}`); 404 / 409 as
    for the other suite routes."""
    body = await read_capped_body(request, MAX_IMPORT_BYTES)
    if body is None:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, IMPORT_TOO_LARGE)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, IMPORT_NOT_UTF8) from None
    if fmt is None:
        content_type = request.headers.get("content-type", "").lower()
        fmt = "csv" if "csv" in content_type else "json" if "json" in content_type else "yaml"
    try:
        content = await run_in_threadpool(_import_sync, store, request.app.state.database_engine, principal.tenant_id, suite_id, text, fmt)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content=content)


# ---------------------------------------------------------------------------- eval runs


def _same_request(run: dict[str, Any], agent_id: uuid.UUID, body: EvalRunCreate) -> bool:
    """Whether a retry (same run_id) asks for the eval run as recorded."""
    if run.get("kind") != "eval" or run.get("agent_id") != str(agent_id):
        return False
    if run.get("eval_suite_id") != str(body.suite_id):
        return False
    if body.version == DRAFT:
        if run.get("draft_spec") is None:
            return False
    elif run.get("draft_spec") is not None or (body.version is not None and run.get("agent_version") != body.version):
        return False
    try:
        recorded = json.loads(run["input"]).get("max_concurrency")
    except (TypeError, ValueError, AttributeError):
        return False
    return recorded == body.max_concurrency


def _launch(launcher: RunLauncher, run_store, tenant_id: str, run: dict[str, Any], created: bool) -> dict[str, Any]:
    """Hand the recorded eval run to the orchestrator (the same rules as launch_recorded_run: a
    retry re-enqueues the recorded run, a failed enqueue marks an owned queued run failed, 503)."""
    run_id = uuid.UUID(str(run["id"]))
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
    try:
        launcher.launch_eval(
            run_id=run_id, tenant_id=tenant_id, input_json=run["input"], traceparent=run.get("traceparent")
        )
    except Exception as exc:
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=run["id"], tenant_id=tenant_id,
        )
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                log_failure(
                    log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                    level=logging.ERROR, run_id=run["id"], tenant_id=tenant_id,
                )
        raise _http(status.HTTP_503_SERVICE_UNAVAILABLE, ENQUEUE_FAILED_DETAIL) from None
    run = run_store.get_run(tenant_id, run_id) or run
    if run["error"] == ENQUEUE_FAILED_ERROR:
        run = {**run, "status": "queued", "display_status": "queued", "error": None}
    log.info(
        "eval run %s queued: tenant=%s target=%s suite=%s",
        run["id"], tenant_id, run["agent_id"] or run.get("workflow_id"), run["eval_suite_id"],
    )
    return run


@_json_router.post(
    "/agents/{agent_id}/evals/runs",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
def start_eval_run(
    agent_id: uuid.UUID,
    body: EvalRunCreate,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    store: EvalStoreDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Record an eval run of the suite against the target and enqueue `eap.eval_run`."""
    run_store, _ = stores
    tenant_id = principal.tenant_id
    agent = _agent_or_404(db, tenant_id, agent_id)
    run = run_store.get_run(tenant_id, body.run_id)
    created = False
    if run is None:
        # As the run routes: a new eval run of an archived agent is a 409 (AGENT_ARCHIVED); a retry
        # of a recorded one (same run_id) is answered and relaunched as recorded.
        refuse_if_archived(agent)
        target = resolve_target(db, tenant_id, agent, body.version)
        try:
            snapshot = store.snapshot(tenant_id, body.suite_id, agent_id=agent_id, max_concurrency=body.max_concurrency)
        except _STORE_ERRORS as exc:
            raise _store_error(exc) from None
        _check_judge_models(snapshot)
        _check_case_payloads(target, snapshot)
        trace_id, traceparent = current_trace_context()
        try:
            run_id, created = run_store.create_eval_run(
                tenant_id, agent_id, target.version, body.suite_id, snapshot.canonical_json(), body.run_id,
                trace_id=trace_id, traceparent=traceparent, draft_spec=target.draft_spec,
            )
        except RunIdConflict:
            raise _http(status.HTTP_409_CONFLICT, RUN_ID_IN_USE) from None
        run = run_store.get_run(tenant_id, run_id)
        if run is None:  # pragma: no cover - the row was just written
            raise _http(status.HTTP_404_NOT_FOUND, "Run not found")
    if not created and not _same_request(run, agent_id, body):
        raise _http(status.HTTP_409_CONFLICT, RUN_ID_OTHER_REQUEST)

    spec = stored_target(db, run).spec  # the recorded target: its draft snapshot or its version
    run = _launch(launcher, run_store, tenant_id, run, created)
    snapshot_meta = json.loads(run["input"])
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={
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
            "warnings": side_effect_warnings(spec),
        },
    )


def _same_workflow_request(run: dict[str, Any], workflow_id: uuid.UUID, body: EvalRunCreate) -> bool:
    """Whether a retry (same run_id) asks for the workflow eval run as recorded."""
    if run.get("kind") != "eval" or run.get("workflow_id") != str(workflow_id):
        return False
    if run.get("eval_suite_id") != str(body.suite_id):
        return False
    if body.version is not None and run.get("workflow_version") != body.version:
        return False
    try:
        recorded = json.loads(run["input"]).get("max_concurrency")
    except (TypeError, ValueError, AttributeError):
        return False
    return recorded == body.max_concurrency


@_json_router.post(
    "/workflows/{workflow_id}/evals/runs",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
def start_workflow_eval_run(
    workflow_id: uuid.UUID,
    body: EvalRunCreate,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    store: EvalStoreDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Record an eval run of a workflow suite against a published workflow version and enqueue
    `eap.eval_run`. A workflow has no draft target: `version: "draft"` is a 422."""
    run_store, _ = stores
    tenant_id = principal.tenant_id
    if body.version == DRAFT:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, "a workflow is evaluated at a published version, not a draft")
    workflow = _workflow_or_404(db, tenant_id, workflow_id)
    run = run_store.get_run(tenant_id, body.run_id)
    created = False
    if run is None:
        target = resolve_workflow_target(db, tenant_id, workflow.id, body.version)
        try:
            snapshot = store.snapshot(
                tenant_id, body.suite_id, workflow_id=workflow_id, max_concurrency=body.max_concurrency
            )
        except _STORE_ERRORS as exc:
            raise _store_error(exc) from None
        _check_judge_models(snapshot)
        _check_case_payloads(target, snapshot)
        trace_id, traceparent = current_trace_context()
        try:
            run_id, created = run_store.create_workflow_eval_run(
                tenant_id, workflow_id, target.version, body.suite_id, snapshot.canonical_json(), body.run_id,
                trace_id=trace_id, traceparent=traceparent,
            )
        except RunIdConflict:
            raise _http(status.HTTP_409_CONFLICT, RUN_ID_IN_USE) from None
        run = run_store.get_run(tenant_id, run_id)
        if run is None:  # pragma: no cover - the row was just written
            raise _http(status.HTTP_404_NOT_FOUND, "Run not found")
    if not created and not _same_workflow_request(run, workflow_id, body):
        raise _http(status.HTTP_409_CONFLICT, RUN_ID_OTHER_REQUEST)

    run = _launch(launcher, run_store, tenant_id, run, created)
    snapshot_meta = json.loads(run["input"])
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content={
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
        },
    )


@_json_router.get("/agents/{agent_id}/evals/warnings")
def eval_warnings(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    version: Annotated[str | None, Query(max_length=16)] = None,
) -> JSONResponse:
    """The side-effect warnings an eval run of this target would give (`version`: a number,
    "draft", or absent for the latest version)."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    target = resolve_target(db, principal.tenant_id, agent, _version_query(version))
    return JSONResponse(
        content={"version": target.version, "draft": target.is_draft, "warnings": side_effect_warnings(target.spec)}
    )


@_json_router.get("/agents/{agent_id}/evals/runs")
def list_eval_runs(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    store: EvalStoreDep,
    suite_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RUNS)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The agent's eval runs (one suite's with `suite_id`), newest first, each with its aggregates
    (one query for all of them). `next_cursor` is set when a full page came back: pass it as
    `before` for the next page (422 for a cursor this API did not issue)."""
    tenant_id = principal.tenant_id
    _agent_or_404(db, tenant_id, agent_id)
    cursor = None
    if before is not None:
        try:
            cursor = RunCursor.decode(before)
        except InvalidRunCursor:
            raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None
    runs = store.list_runs(tenant_id, agent_id, suite_id=suite_id, limit=limit, before=cursor)
    aggregates = store.aggregates_for_runs(tenant_id, [r["id"] for r in runs])
    items = [eval_run_json(run, aggregates[str(run["id"])]) for run in runs]
    next_cursor = RunCursor.after(runs[-1]).encode() if len(runs) == limit else None
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


def _eval_run_or_404(run_store, tenant_id: str, agent_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    run = run_store.get_run(tenant_id, run_id)
    if run is None or run.get("kind") != "eval" or run.get("agent_id") != str(agent_id):
        raise _http(status.HTTP_404_NOT_FOUND, str(EvalRunNotFound(tenant_id, run_id)))
    return run


@_json_router.get("/agents/{agent_id}/evals/runs/{run_id}")
def get_eval_run(
    agent_id: uuid.UUID, run_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep, store: EvalStoreDep
) -> JSONResponse:
    """The eval run with its aggregates (`{cases, passed, failed, errors, pass_rate, scores_avg,
    duration_ms_p50, cost_usd}` over the results recorded so far)."""
    run_store, _ = stores
    run = _eval_run_or_404(run_store, principal.tenant_id, agent_id, run_id)
    try:
        aggregates = store.aggregates(principal.tenant_id, run_id, agent_id=agent_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    return JSONResponse(content=eval_run_json(run, aggregates))


@_json_router.get("/agents/{agent_id}/evals/runs/{run_id}/results")
def list_eval_results(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: PrincipalDep,
    store: EvalStoreDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RESULTS)] = 100,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The eval run's per-case results by position, a page at a time (`limit` 1..500, default
    100). `next_cursor` is set when a full page came back: pass it as `after` for the next page
    (422 for a cursor this API did not issue)."""
    position_after = _decode_result_cursor(after) if after is not None else None
    try:
        results = store.list_case_results(
            principal.tenant_id, run_id, agent_id=agent_id, after=position_after, limit=limit
        )
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    next_cursor = _result_cursor(results[-1]) if len(results) == limit else None
    return JSONResponse(content={"items": [result_json(r) for r in results], "next_cursor": next_cursor})


def _result_cursor(result: EvalCaseResult) -> str:
    raw = json.dumps({"p": result.position, "c": result.case_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_result_cursor(token: str) -> tuple[int, str]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        position, case_id = data["p"], data["c"]
        if set(data) != {"p", "c"} or not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ValueError
        return position, str(uuid.UUID(case_id))
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, INVALID_CURSOR) from None


@_json_router.get("/agents/{agent_id}/evals/compare")
def compare_eval_runs(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    store: EvalStoreDep,
    base: uuid.UUID,
    target: uuid.UUID,
) -> JSONResponse:
    """Compare two eval runs of this agent case by case (404 when either is not this agent's eval
    run). `same_suite` is false and `warnings` says so when the suite hashes differ;
    `base_version` / `target_version` (null for a draft run) let Studio open the spec diff."""
    try:
        result = store.compare(principal.tenant_id, base, target, agent_id=agent_id)
    except _STORE_ERRORS as exc:
        raise _store_error(exc) from None
    result["base_version"] = result["base"]["agent_version"]
    result["target_version"] = result["target"]["agent_version"]
    return JSONResponse(content=result)


router = APIRouter()
router.include_router(_json_router)
router.include_router(_file_router)

__all__ = ["get_eval_store", "router", "side_effect_warnings"]
