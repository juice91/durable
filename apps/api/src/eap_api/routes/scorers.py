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
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from eap_api.bodies import Attachment, ScorerCreate
from eap_api.views import scorer_json
from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.evals import ScorerServiceDep
from eap_api.services.scorers import (  # noqa: F401 - the messages are re-exported: tests import them from here
    ALREADY_SCORED,
    LIMIT_REACHED,
    MAX_SCORE_SCORERS,
    NAME_IN_USE,
    REGEX_INVALID,
    SCORER_DESCRIPTION_MAX_CHARS,
    SCORER_NAME_MAX_CHARS,
    SCORER_NOT_FOUND,
    validate_evaluator,
)

router = APIRouter(
    tags=["scorers"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class ScorerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=SCORER_NAME_MAX_CHARS)
    description: str | None = Field(default=None, max_length=SCORER_DESCRIPTION_MAX_CHARS)
    evaluator: dict[str, Any] | None = None


def _iso(value: datetime) -> str:
    return value.isoformat()


_json = scorer_json


@router.post("/scorers", status_code=status.HTTP_201_CREATED)
async def create_scorer(body: ScorerCreate, principal: PrincipalDep, service: ScorerServiceDep) -> JSONResponse:
    record = await run_in_threadpool(
        service.create, principal.tenant_id, body.name, body.evaluator, body.description
    )
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=_json(record)), record.revision)


@router.get("/scorers")
async def list_scorers(principal: PrincipalDep, service: ScorerServiceDep) -> JSONResponse:
    records = await run_in_threadpool(service.list, principal.tenant_id)
    return JSONResponse(content={"items": [_json(r) for r in records]})


@router.get("/scorers/{scorer_id}", responses={**etag_responses(write=False)})
async def get_scorer(scorer_id: uuid.UUID, principal: PrincipalDep, service: ScorerServiceDep) -> JSONResponse:
    record = await run_in_threadpool(service.get, principal.tenant_id, scorer_id)
    return read_response(_json(record), record.revision)


@router.patch("/scorers/{scorer_id}", responses={**etag_responses()})
async def update_scorer(
    scorer_id: uuid.UUID, body: ScorerUpdate, principal: PrincipalDep, service: ScorerServiceDep, pre: IfMatch
) -> JSONResponse:
    """Change a scorer. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current scorer (see eap_api.concurrency)."""
    if body.evaluator is not None:
        validate_evaluator(body.evaluator)  # 422 before any 412, as it always answered
    tenant_id = principal.tenant_id

    def current() -> tuple[dict[str, Any], int]:
        found = service.get(tenant_id, scorer_id)
        return _json(found), found.revision

    if pre.expected is not None:
        pre.check((await run_in_threadpool(current))[1], lambda: current())
    with pre.guard(current):
        record = await run_in_threadpool(
            lambda: service.update(
                tenant_id, scorer_id, name=body.name, description=body.description,
                set_description="description" in body.model_fields_set, evaluator=body.evaluator,
                expected_revision=pre.expected,
            )
        )
    return pre.respond(_json(record), record.revision)


@router.delete("/scorers/{scorer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_scorer(scorer_id: uuid.UUID, principal: PrincipalDep, service: ScorerServiceDep) -> Response:
    await run_in_threadpool(service.delete, principal.tenant_id, scorer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/agents/{agent_id}/scorers")
async def list_agent_scorers(agent_id: uuid.UUID, principal: PrincipalDep, service: ScorerServiceDep) -> JSONResponse:
    items = await run_in_threadpool(service.attached_to_agent, principal.tenant_id, agent_id)
    return JSONResponse(content={"items": items})


@router.put("/agents/{agent_id}/scorers/{scorer_id}")
async def attach_scorer(
    agent_id: uuid.UUID, scorer_id: uuid.UUID, body: Attachment, principal: PrincipalDep, service: ScorerServiceDep,
) -> JSONResponse:
    items = await run_in_threadpool(service.attach, principal.tenant_id, agent_id, scorer_id, body.sampling_rate)
    return JSONResponse(content={"items": items})


@router.delete("/agents/{agent_id}/scorers/{scorer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_scorer(
    agent_id: uuid.UUID, scorer_id: uuid.UUID, principal: PrincipalDep, service: ScorerServiceDep
) -> Response:
    await run_in_threadpool(service.detach, principal.tenant_id, agent_id, scorer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------- score on demand


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scorer_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_SCORE_SCORERS)


@router.post(
    "/runs/{run_id}/score",
    status_code=status.HTTP_202_ACCEPTED,
    responses={503: {"model": ErrorDetail}},
)
async def score_run(
    run_id: uuid.UUID, body: ScoreRequest, principal: PrincipalDep, service: ScorerServiceDep,
) -> JSONResponse:
    """Score a finished run with the named scorers now (the Score button of the trace drawer).
    Enqueues DBOS workflow `eap.score_run`; the worker appends one `score` event per scorer (key
    `score:{scorer_id}`, first write wins) and the answer is `202 {run_id, scorer_ids, already_scored}`:
    `scorer_ids` are the ones queued (repeated ids count once), `already_scored` the requested ones
    that already have a score event on the run and are left alone. 404 for another tenant's run or
    scorer; 409 `run_not_finished` unless the run is `completed` (a failed or cancelled run has no
    answer to score); 409 `{detail: "already_scored", already_scored: [...]}` when every requested
    scorer already has its event; 422 for 0 or more than 8 ids."""
    outcome = await run_in_threadpool(service.score_run, principal.tenant_id, run_id, body.scorer_ids)
    if not outcome.scorer_ids:  # a score event is first-write-wins: another request would write nothing
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": ALREADY_SCORED, "already_scored": outcome.already_scored},
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"run_id": str(run_id), "scorer_ids": outcome.scorer_ids, "already_scored": outcome.already_scored},
    )
