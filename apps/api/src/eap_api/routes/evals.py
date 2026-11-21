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

import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from eap_api import eval_overview
from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.eval_format import MAX_IMPORT_BYTES
from eap_api.request_guard import GuardedJsonRoute, read_capped_body
from eap_api.routes.runs import _run_json
from eap_api.schemas import (
    ErrorDetail,
    EvalCaseCreate,
    EvalCaseUpdate,
    EvalRunCreate,
    EvalSuiteCreate,
    EvalSuiteUpdate,
)
from eap_api.service_deps.evals import EvalServiceDep
from eap_api.services.evals import (  # noqa: F401 - the checks and limits are re-exported: tests and the MCP tools import them from here
    MAX_CURSOR_LENGTH,
    MAX_LIST_RESULTS,
    MAX_LIST_RUNS,
    OVERVIEW_MAX_LIMIT,
    REGEX_INVALID,
    StartedEvalRun,
    check_regex_evaluators,
    dump_evaluators,
    side_effect_warnings,
    workflow_input_problem,
)
from eap_api.services.run_options import DRAFT
from eap_observability import current_trace_context

IMPORT_TOO_LARGE = f"the suite file must be at most {MAX_IMPORT_BYTES // (1024 * 1024)} MiB"
IMPORT_NOT_UTF8 = "the suite file must be UTF-8 text"
EVALUATORS_NULL = "evaluators cannot be null; send [] to remove every suite evaluator"
NAME_NULL = "name cannot be null"

PrincipalDep = Annotated[Principal, Depends(current_principal)]

_ERRORS = {404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}}
_json_router = APIRouter(tags=["evals"], responses=_ERRORS, route_class=GuardedJsonRoute)
# The import takes a raw YAML or JSON file (not a JSON body), with its own 1 MiB cap answered as a 422.
_file_router = APIRouter(tags=["evals"], responses=_ERRORS, route_class=APIRoute)


# ---------------------------------------------------------------------------- response shapes


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


def suite_with_cases_json(suite: Any, cases: list[Any], target_name: str | None) -> dict[str, Any]:
    return {**suite_json(suite, target_name), "cases": [case_json(c) for c in cases]}


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


def _http(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


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
    service: EvalServiceDep,
    include_archived: bool = False,
) -> JSONResponse:
    """The agent's suites by name, with their case counts (archived ones only on request)."""
    suites = service.list_suites(principal.tenant_id, agent_id, include_archived=include_archived)
    return JSONResponse(content={"items": [suite_json(s) for s in suites]})


@_json_router.get("/eval-suites")
def list_tenant_suites(
    principal: PrincipalDep,
    service: EvalServiceDep,
    agent_id: uuid.UUID | None = None,
    include_archived: bool = False,
    limit: Annotated[int, Query(ge=1, le=OVERVIEW_MAX_LIMIT)] = 50,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The tenant's suites across agents by name, with the agent's name and the latest run of each."""
    rows, next_cursor = service.list_tenant_suites(
        principal.tenant_id, agent_id=agent_id, include_archived=include_archived, limit=limit, after=after
    )
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
    service: EvalServiceDep,
    agent_id: uuid.UUID | None = None,
    suite_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RUNS)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The tenant's eval runs across agents, newest first, each with its aggregates and the agent
    and suite names. `next_cursor` is set when a full page came back (pass it as `before`)."""
    rows, next_cursor = service.list_tenant_eval_runs(
        principal.tenant_id, agent_id=agent_id, suite_id=suite_id, limit=limit, before=before
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
    service: EvalServiceDep,
    limit: Annotated[int, Query(ge=1, le=OVERVIEW_MAX_LIMIT)] = 50,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The evaluators the tenant's active suites use, by (kind, name), each with its suites and
    recent per-run values from their eval runs."""
    rows, next_cursor = service.list_scorer_overview(principal.tenant_id, limit=limit, after=after)
    return JSONResponse(content={"items": rows, "next_cursor": next_cursor})


def _created_suite_response(suite: Any, target_name: str, created: bool) -> JSONResponse:
    if not created:
        return JSONResponse(status_code=status.HTTP_200_OK, content=suite_json(suite, target_name))
    return stamp(
        JSONResponse(status_code=status.HTTP_201_CREATED, content=suite_json(suite, target_name)), suite.revision
    )


@_json_router.post("/agents/{agent_id}/eval-suites", status_code=status.HTTP_201_CREATED)
def create_suite(
    agent_id: uuid.UUID, body: EvalSuiteCreate, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """Create a suite (201). A repeat with the same `suite_id`, agent and name answers 200 with the
    suite; a `suite_id` used otherwise is a 409."""
    suite, name, created = service.create_suite(
        principal.tenant_id, "agent", agent_id, name=body.name, description=body.description,
        evaluators=body.evaluators, suite_id=body.suite_id,
    )
    return _created_suite_response(suite, name, created)


@_json_router.get("/workflows/{workflow_id}/evals/suites")
def list_workflow_suites(
    workflow_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    include_archived: bool = False,
) -> JSONResponse:
    """The workflow's suites by name, with their case counts (archived ones only on request)."""
    name, suites = service.list_workflow_suites(principal.tenant_id, workflow_id, include_archived=include_archived)
    return JSONResponse(content={"items": [suite_json(s, name) for s in suites]})


@_json_router.post("/workflows/{workflow_id}/evals/suites", status_code=status.HTTP_201_CREATED)
def create_workflow_suite(
    workflow_id: uuid.UUID, body: EvalSuiteCreate, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """Create a suite that targets the workflow (201); idempotent by `suite_id` as the agent route."""
    suite, name, created = service.create_suite(
        principal.tenant_id, "workflow", workflow_id, name=body.name, description=body.description,
        evaluators=body.evaluators, suite_id=body.suite_id,
    )
    return _created_suite_response(suite, name, created)


@_json_router.get("/eval-suites/{suite_id}", responses={**etag_responses(write=False)})
def get_suite(suite_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep) -> JSONResponse:
    """The suite with its cases in execution order."""
    suite, cases, name = service.get_suite(principal.tenant_id, suite_id)
    return read_response(suite_with_cases_json(suite, cases, name), suite.revision)


@_json_router.patch("/eval-suites/{suite_id}", responses={**etag_responses()})
def update_suite(
    suite_id: uuid.UUID, body: EvalSuiteUpdate, principal: PrincipalDep, service: EvalServiceDep, pre: IfMatch,
) -> JSONResponse:
    """Change the fields present in the body. An archived suite accepts only `archived: false` (409).
    Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch` with the current suite
    (see eap_api.concurrency). Case edits have their own revision and do not move the suite's."""
    fields = body.model_fields_set
    if "evaluators" in fields and body.evaluators is None:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, EVALUATORS_NULL)
    if "name" in fields and body.name is None:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, NAME_NULL)
    kwargs: dict[str, Any] = {k: getattr(body, k) for k in ("name", "description", "evaluators") if k in fields}
    if "archived" in fields and body.archived is not None:
        kwargs["archived"] = body.archived
    tenant_id = principal.tenant_id

    def current() -> tuple[dict[str, Any], int]:
        found, cases, name = service.get_suite(tenant_id, suite_id)
        return suite_with_cases_json(found, cases, name), found.revision

    if pre.expected is not None:
        pre.check(service.suite_revision(tenant_id, suite_id), current)
    with pre.guard(current):
        suite, name = service.update_suite(tenant_id, suite_id, kwargs, expected_revision=pre.expected)
    return pre.respond(suite_json(suite, name), suite.revision)


@_json_router.delete("/eval-suites/{suite_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_suite(suite_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep) -> Response:
    """Delete the suite and its cases (204). Its eval runs and their results stay."""
    service.delete_suite(principal.tenant_id, suite_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- cases


@_json_router.get("/eval-suites/{suite_id}/cases")
def list_cases(suite_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep) -> JSONResponse:
    return JSONResponse(content={"items": [case_json(c) for c in service.list_cases(principal.tenant_id, suite_id)]})


@_json_router.post("/eval-suites/{suite_id}/cases", status_code=status.HTTP_201_CREATED)
def add_case(
    suite_id: uuid.UUID, body: EvalCaseCreate, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """Add a case (201): 409 for a name already in the suite or an archived suite, 422 for an
    invalid case (for a workflow suite, an input that is not a JSON object valid for the workflow's
    input schema) or past the 500-case cap."""
    case = body.model_dump(exclude={"case_id", "position"})
    record = service.add_case(principal.tenant_id, suite_id, case, position=body.position, case_id=body.case_id)
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=case_json(record)), record.revision)


@_json_router.get("/eval-suites/{suite_id}/cases/{case_id}", responses={**etag_responses(write=False)})
def get_case(
    suite_id: uuid.UUID, case_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    record = service.get_case(principal.tenant_id, suite_id, case_id)
    return read_response(case_json(record), record.revision)


@_json_router.patch("/eval-suites/{suite_id}/cases/{case_id}", responses={**etag_responses()})
def update_case(
    suite_id: uuid.UUID,
    case_id: uuid.UUID,
    body: EvalCaseUpdate,
    principal: PrincipalDep,
    service: EvalServiceDep,
    pre: IfMatch,
) -> JSONResponse:
    """Change the fields present in the body; the merged case is validated (422). Send the ETag you
    read as `If-Match`: a stale one is 412 `revision_mismatch` with the current case."""
    kwargs = {k: getattr(body, k) for k in body.model_fields_set}
    if kwargs.get("position", 0) is None:
        kwargs.pop("position")
    tenant_id = principal.tenant_id

    def current() -> tuple[dict[str, Any], int]:
        found = service.get_case(tenant_id, suite_id, case_id)
        return case_json(found), found.revision

    if pre.expected is not None:
        pre.check(current()[1], current)
    with pre.guard(current):
        record = service.update_case(tenant_id, suite_id, case_id, kwargs, expected_revision=pre.expected)
    return pre.respond(case_json(record), record.revision)


@_json_router.delete("/eval-suites/{suite_id}/cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_case(
    suite_id: uuid.UUID, case_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep
) -> Response:
    service.delete_case(principal.tenant_id, suite_id, case_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- import and export


def _filename(name: str, fmt: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:80] or "suite"
    return f"{stem}.{fmt}"


@_json_router.get("/eval-suites/{suite_id}/export")
def export_suite(
    suite_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    fmt: Annotated[Literal["yaml", "json"], Query(alias="format")] = "yaml",
) -> Response:
    """The suite as a `pydantic_evals` dataset file (attachment), YAML by default."""
    name, content = service.export_suite(principal.tenant_id, suite_id, fmt)
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json" if fmt == "json" else "application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{_filename(name, fmt)}"'},
    )


@_file_router.post("/eval-suites/{suite_id}/import")
async def import_suite(
    suite_id: uuid.UUID,
    request: Request,
    principal: PrincipalDep,
    service: EvalServiceDep,
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
    result, suite, name = await run_in_threadpool(service.import_cases, principal.tenant_id, suite_id, text, fmt)
    return JSONResponse(content={"created": result.created, "updated": result.updated, "suite": suite_json(suite, name)})


# ---------------------------------------------------------------------------- eval runs


def _run_started_response(started: StartedEvalRun, body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if started.created else status.HTTP_200_OK, content=body
    )


@_json_router.post(
    "/agents/{agent_id}/evals/runs",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
def start_eval_run(
    agent_id: uuid.UUID, body: EvalRunCreate, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """Record an eval run of the suite against the target and enqueue `eap.eval_run`."""
    # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
    trace_id, traceparent = current_trace_context()
    started = service.start_agent_eval_run(
        principal.tenant_id, agent_id, run_id=body.run_id, suite_id=body.suite_id, version=body.version,
        max_concurrency=body.max_concurrency, trace_id=trace_id, traceparent=traceparent,
    )
    return _run_started_response(started, agent_run_started_json(started))


@_json_router.post(
    "/workflows/{workflow_id}/evals/runs",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
def start_workflow_eval_run(
    workflow_id: uuid.UUID, body: EvalRunCreate, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """Record an eval run of a workflow suite against a published workflow version and enqueue
    `eap.eval_run`. A workflow has no draft target: `version: "draft"` is a 422."""
    trace_id, traceparent = current_trace_context()
    started = service.start_workflow_eval_run(
        principal.tenant_id, workflow_id, run_id=body.run_id, suite_id=body.suite_id, version=body.version,
        max_concurrency=body.max_concurrency, trace_id=trace_id, traceparent=traceparent,
    )
    return _run_started_response(started, workflow_run_started_json(started))


@_json_router.get("/agents/{agent_id}/evals/warnings")
def eval_warnings(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    version: Annotated[str | None, Query(max_length=16)] = None,
) -> JSONResponse:
    """The side-effect warnings an eval run of this target would give (`version`: a number,
    "draft", or absent for the latest version)."""
    target, warnings = service.target_warnings(principal.tenant_id, agent_id, _version_query(version))
    return JSONResponse(content={"version": target.version, "draft": target.is_draft, "warnings": warnings})


def _runs_page(runs: list[dict[str, Any]], aggregates: dict[str, Any], next_cursor: str | None) -> JSONResponse:
    items = [eval_run_json(run, aggregates[str(run["id"])]) for run in runs]
    return JSONResponse(content={"items": items, "next_cursor": next_cursor})


@_json_router.get("/workflows/{workflow_id}/evals/runs")
def list_workflow_eval_runs(
    workflow_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    suite_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RUNS)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The workflow's eval runs, as `GET /agents/{id}/evals/runs` lists an agent's."""
    return _runs_page(*service.list_eval_runs(
        principal.tenant_id, workflow_id, workflow=True, suite_id=suite_id, limit=limit, before=before
    ))


@_json_router.get("/workflows/{workflow_id}/evals/runs/{run_id}")
def get_workflow_eval_run(
    workflow_id: uuid.UUID, run_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """The workflow eval run with its aggregates (404 when it is another workflow's)."""
    run, aggregates = service.get_eval_run(principal.tenant_id, workflow_id, run_id, workflow=True)
    return JSONResponse(content=eval_run_json(run, aggregates))


def _results_page(results: list[Any], next_cursor: str | None) -> JSONResponse:
    return JSONResponse(content={"items": [result_json(r) for r in results], "next_cursor": next_cursor})


@_json_router.get("/workflows/{workflow_id}/evals/runs/{run_id}/results")
def list_workflow_eval_results(
    workflow_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RESULTS)] = 100,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The workflow eval run's per-case results by position, paged as the agent route's."""
    return _results_page(*service.list_eval_results(
        principal.tenant_id, workflow_id, run_id, workflow=True, limit=limit, after=after
    ))


@_json_router.get("/agents/{agent_id}/evals/runs")
def list_eval_runs(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    suite_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RUNS)] = 50,
    before: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The agent's eval runs (one suite's with `suite_id`), newest first, each with its aggregates
    (one query for all of them). `next_cursor` is set when a full page came back: pass it as
    `before` for the next page (422 for a cursor this API did not issue)."""
    return _runs_page(*service.list_eval_runs(
        principal.tenant_id, agent_id, workflow=False, suite_id=suite_id, limit=limit, before=before
    ))


@_json_router.get("/agents/{agent_id}/evals/runs/{run_id}")
def get_eval_run(
    agent_id: uuid.UUID, run_id: uuid.UUID, principal: PrincipalDep, service: EvalServiceDep
) -> JSONResponse:
    """The eval run with its aggregates (`{cases, passed, failed, errors, pass_rate, scores_avg,
    duration_ms_p50, cost_usd}` over the results recorded so far)."""
    run, aggregates = service.get_eval_run(principal.tenant_id, agent_id, run_id, workflow=False)
    return JSONResponse(content=eval_run_json(run, aggregates))


@_json_router.get("/agents/{agent_id}/evals/runs/{run_id}/results")
def list_eval_results(
    agent_id: uuid.UUID,
    run_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_RESULTS)] = 100,
    after: Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)] = None,
) -> JSONResponse:
    """The eval run's per-case results by position, a page at a time (`limit` 1..500, default
    100). `next_cursor` is set when a full page came back: pass it as `after` for the next page
    (422 for a cursor this API did not issue)."""
    return _results_page(*service.list_eval_results(
        principal.tenant_id, agent_id, run_id, workflow=False, limit=limit, after=after
    ))


@_json_router.get("/agents/{agent_id}/evals/compare")
def compare_eval_runs(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: EvalServiceDep,
    base: uuid.UUID,
    target: uuid.UUID,
) -> JSONResponse:
    """Compare two eval runs of this agent case by case (404 when either is not this agent's eval
    run). `same_suite` is false and `warnings` says so when the suite hashes differ;
    `base_version` / `target_version` (null for a draft run) let Studio open the spec diff."""
    result = service.compare(principal.tenant_id, agent_id, base, target)
    result["base_version"] = result["base"]["agent_version"]
    result["target_version"] = result["target"]["agent_version"]
    return JSONResponse(content=result)


router = APIRouter()
router.include_router(_json_router)
router.include_router(_file_router)

__all__ = ["router", "side_effect_warnings"]
