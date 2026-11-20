"""Reusable scorers (W2.3).

- `POST /scorers`, `GET /scorers`, `GET|PATCH|DELETE /scorers/{id}`: tenant-scoped named evaluators
  (eap_schemas.scorers: contains, regex, schema_valid, field_equals, max_cost). `GET /scorers` lists
  each with the agents it is attached to. (`GET /evals/scorers` is the separate aggregated view of
  the evaluators the suites use.)
- `GET /agents/{id}/scorers`, `PUT|DELETE /agents/{id}/scorers/{scorer_id}`: attach a scorer to an
  agent (with a `sampling_rate` in (0, 1], default 1) or detach it.

`POST /runs/{id}/score` asks the worker to score one finished run now. Otherwise the worker applies an agent's attached scorers after each of its runs
finished and records the results as `score` run events. Another tenant's scorer or agent is a 404.
Errors: 404, 409 (name used, attachment limit), 422 (invalid scorer).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import StoresDep, _get_run_or_404
from eap_api.schemas import ErrorDetail
from eap_processors import Re2PatternError, compile_re2
from eap_run_store import TERMINAL_RUN_STATUSES
from eap_run_store.scorers import (
    SCORER_DESCRIPTION_MAX_CHARS,
    SCORER_NAME_MAX_CHARS,
    ScorerLimitExceeded,
    ScorerNameConflict,
    ScorerNotFound,
    ScorerRecord,
    ScorerStore,
)
from eap_schemas.scorers import ScorerInvalid, validate_scorer_evaluator

SCORER_NOT_FOUND = "Scorer not found"
AGENT_NOT_FOUND = "Agent not found"
NAME_IN_USE = "a scorer with this name already exists"
LIMIT_REACHED = "the agent already has the maximum number of scorers"
REGEX_INVALID = "pattern is not valid RE2 syntax or is too complex"

router = APIRouter(
    tags=["scorers"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]


def get_scorer_store(request: Request, stores: StoresDep) -> ScorerStore:
    store = getattr(request.app.state, "scorer_store", None)
    if store is None:
        run_store, _ = stores
        store = request.app.state.scorer_store = ScorerStore(run_store.engine)
    return store


ScorerStoreDep = Annotated[ScorerStore, Depends(get_scorer_store)]


class ScorerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=SCORER_NAME_MAX_CHARS)
    description: str | None = Field(default=None, max_length=SCORER_DESCRIPTION_MAX_CHARS)
    evaluator: dict[str, Any]


class ScorerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=SCORER_NAME_MAX_CHARS)
    description: str | None = Field(default=None, max_length=SCORER_DESCRIPTION_MAX_CHARS)
    evaluator: dict[str, Any] | None = None


class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sampling_rate: Decimal = Field(default=Decimal(1), gt=0, le=1, max_digits=5, decimal_places=4)


def _http(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _json(record: ScorerRecord) -> dict[str, Any]:
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


def _check(evaluator: Any) -> dict[str, Any]:
    """The validated evaluator, or a 422 naming the path with a fixed message."""
    try:
        spec = validate_scorer_evaluator(evaluator)
    except ScorerInvalid as exc:
        raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, f"{exc.path}: {exc.reason}") from None
    if spec["kind"] == "regex":
        try:
            compile_re2(spec["pattern"], allow_empty_match=True)
        except Re2PatternError:
            raise _http(status.HTTP_422_UNPROCESSABLE_CONTENT, f"evaluator.pattern: {REGEX_INVALID}") from None
    return spec


def _agent_or_404(db: Session, tenant_id: str, agent_id: uuid.UUID) -> None:
    found = db.query(AgentModel.id).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    if found is None:
        raise _http(status.HTTP_404_NOT_FOUND, AGENT_NOT_FOUND)


@router.post("/scorers", status_code=status.HTTP_201_CREATED)
async def create_scorer(body: ScorerCreate, principal: PrincipalDep, store: ScorerStoreDep) -> JSONResponse:
    spec = _check(body.evaluator)
    try:
        record = await run_in_threadpool(store.create, principal.tenant_id, body.name, spec, body.description)
    except ScorerNameConflict:
        raise _http(status.HTTP_409_CONFLICT, NAME_IN_USE) from None
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=_json(record)), record.revision)


@router.get("/scorers")
async def list_scorers(principal: PrincipalDep, store: ScorerStoreDep) -> JSONResponse:
    records = await run_in_threadpool(store.list, principal.tenant_id)
    return JSONResponse(content={"items": [_json(r) for r in records]})


@router.get("/scorers/{scorer_id}", responses={**etag_responses(write=False)})
async def get_scorer(scorer_id: uuid.UUID, principal: PrincipalDep, store: ScorerStoreDep) -> JSONResponse:
    try:
        record = await run_in_threadpool(store.get, principal.tenant_id, scorer_id)
    except ScorerNotFound:
        raise _http(status.HTTP_404_NOT_FOUND, SCORER_NOT_FOUND) from None
    return read_response(_json(record), record.revision)


@router.patch("/scorers/{scorer_id}", responses={**etag_responses()})
async def update_scorer(
    scorer_id: uuid.UUID, body: ScorerUpdate, principal: PrincipalDep, store: ScorerStoreDep, pre: IfMatch
) -> JSONResponse:
    """Change a scorer. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current scorer (see eap_api.concurrency)."""
    spec = _check(body.evaluator) if body.evaluator is not None else None

    def current() -> tuple[dict[str, Any], int]:
        found = store.get(principal.tenant_id, scorer_id)
        return _json(found), found.revision

    try:
        if pre.expected is not None:
            pre.check((await run_in_threadpool(current))[1], lambda: current())
        with pre.guard(current):
            record = await run_in_threadpool(
                lambda: store.update(
                    principal.tenant_id, scorer_id, name=body.name, description=body.description,
                    set_description="description" in body.model_fields_set, evaluator=spec,
                    expected_revision=pre.expected,
                )
            )
    except ScorerNotFound:
        raise _http(status.HTTP_404_NOT_FOUND, SCORER_NOT_FOUND) from None
    except ScorerNameConflict:
        raise _http(status.HTTP_409_CONFLICT, NAME_IN_USE) from None
    return pre.respond(_json(record), record.revision)


@router.delete("/scorers/{scorer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scorer(scorer_id: uuid.UUID, principal: PrincipalDep, store: ScorerStoreDep) -> Response:
    try:
        await run_in_threadpool(store.delete, principal.tenant_id, scorer_id)
    except ScorerNotFound:
        raise _http(status.HTTP_404_NOT_FOUND, SCORER_NOT_FOUND) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents/{agent_id}/scorers")
async def list_agent_scorers(
    agent_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, store: ScorerStoreDep
) -> JSONResponse:
    _agent_or_404(db, principal.tenant_id, agent_id)
    items = await run_in_threadpool(store.attached_to_agent, principal.tenant_id, agent_id)
    return JSONResponse(content={"items": items})


@router.put("/agents/{agent_id}/scorers/{scorer_id}")
async def attach_scorer(
    agent_id: uuid.UUID, scorer_id: uuid.UUID, body: Attachment, principal: PrincipalDep, db: SessionDep,
    store: ScorerStoreDep,
) -> JSONResponse:
    _agent_or_404(db, principal.tenant_id, agent_id)
    try:
        await run_in_threadpool(store.attach, principal.tenant_id, agent_id, scorer_id, body.sampling_rate)
    except ScorerNotFound:
        raise _http(status.HTTP_404_NOT_FOUND, SCORER_NOT_FOUND) from None
    except ScorerLimitExceeded:
        raise _http(status.HTTP_409_CONFLICT, LIMIT_REACHED) from None
    items = await run_in_threadpool(store.attached_to_agent, principal.tenant_id, agent_id)
    return JSONResponse(content={"items": items})


@router.delete("/agents/{agent_id}/scorers/{scorer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_scorer(
    agent_id: uuid.UUID, scorer_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, store: ScorerStoreDep
) -> Response:
    _agent_or_404(db, principal.tenant_id, agent_id)
    await run_in_threadpool(store.detach, principal.tenant_id, agent_id, scorer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- score on demand

RUN_NOT_FINISHED = "run_not_finished"
ALREADY_SCORED = "already_scored"
MAX_SCORE_SCORERS = 8
SCORE_START_FAILED = "scoring could not be started; retry"
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]


def _already_scored(engine: Any, tenant_id: str, run_id: uuid.UUID, scorer_ids: list[str]) -> set[str]:
    """The scorer ids that already have a `score:{id}` event on the run."""
    from sqlalchemy import text

    keys = {f"score:{s}": s for s in scorer_ids}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event_key FROM execution_events WHERE tenant_id = :t AND run_id = :r AND event_key = ANY(:k)"),
            {"t": tenant_id, "r": str(run_id), "k": list(keys)},
        )
        return {keys[r[0]] for r in rows}


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorer_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_SCORE_SCORERS)


@router.post(
    "/runs/{run_id}/score",
    status_code=status.HTTP_202_ACCEPTED,
    responses={503: {"model": ErrorDetail}},
)
async def score_run(
    run_id: uuid.UUID, body: ScoreRequest, principal: PrincipalDep, stores: StoresDep, store: ScorerStoreDep,
    launcher: LauncherDep,
) -> JSONResponse:
    """Score a finished run with the named scorers now (the Score button of the trace drawer).
    Enqueues DBOS workflow `eap.score_run`; the worker appends one `score` event per scorer (key
    `score:{scorer_id}`, first write wins) and the answer is `202 {run_id, scorer_ids, already_scored}`:
    `scorer_ids` are the ones queued (repeated ids count once), `already_scored` the requested ones
    that already have a score event on the run and are left alone. 404 for another tenant's run or
    scorer; 409 `run_not_finished` unless the run is `completed` (a failed or cancelled run has no
    answer to score); 409 `{detail: "already_scored", already_scored: [...]}` when every requested
    scorer already has its event; 422 for 0 or more than 8 ids."""
    run_store, _ = stores
    tenant_id = principal.tenant_id
    run = await run_in_threadpool(_get_run_or_404, run_store, tenant_id, run_id)
    if run["status"] not in ("completed", "skipped"):  # scoring reads the answer: a failed or cancelled run has none
        raise _http(status.HTTP_409_CONFLICT, RUN_NOT_FINISHED)
    requested = list(dict.fromkeys(str(s) for s in body.scorer_ids))
    for scorer_id in requested:
        try:
            await run_in_threadpool(store.get, tenant_id, scorer_id)
        except ScorerNotFound:
            raise _http(status.HTTP_404_NOT_FOUND, SCORER_NOT_FOUND) from None
    done = await run_in_threadpool(_already_scored, run_store.engine, tenant_id, run_id, requested)
    already = [s for s in requested if s in done]
    scorer_ids = [s for s in requested if s not in done]
    if not scorer_ids:  # a score event is first-write-wins: another request would write nothing
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": ALREADY_SCORED, "already_scored": already}
        )
    try:
        await run_in_threadpool(
            lambda: launcher.launch_score(run_id=run_id, tenant_id=tenant_id, scorer_ids=scorer_ids)
        )
    except Exception:  # noqa: BLE001 - nothing was recorded; the caller may retry
        raise _http(status.HTTP_503_SERVICE_UNAVAILABLE, SCORE_START_FAILED) from None
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": str(run_id), "scorer_ids": scorer_ids, "already_scored": already},
    )
