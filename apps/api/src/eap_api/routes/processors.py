"""Processors (guardrails): the catalog, the test console and recorded outcomes (milestone P0, P0.9).

- `GET /processors`: the built-in processor types (eap_schemas `PROCESSOR_DECLARATIONS`) with their
  config JSON schemas and limits.
- `POST /processors/test`: runs deterministic processors (`eap_processors`) on a sample, in this
  request only: no run, no worker, no model, nothing stored or logged. `moderation` calls a model, so
  it is refused here (test it in the playground).
- `GET /processor-results`: the tenant's `processor_result` events (written by the worker), newest
  first, keyset-paged. Only the outcome fields are served (type, phase, action, match counts), never
  any text: the events carry no matched text by contract and this route serves no other field.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.agents import ProcessorServiceDep
from eap_api.services.processors import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    MAX_RESULTS_CURSOR_LENGTH,
    MAX_TEST_SAMPLE_BYTES,
    MODERATION_NOT_TESTABLE,
    RESULTS_TIMEOUT_DETAIL,
    RESULTS_UNAVAILABLE_DETAIL,
    ResultsCursor,
)
from eap_schemas.processors import MAX_PROCESSORS_PER_PHASE, ProcessorAction, ProcessorPhase, ProcessorSpec, ProcessorType

router = APIRouter(route_class=GuardedJsonRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.get("/processors", tags=["processors"])
def list_processors(principal: PrincipalDep, service: ProcessorServiceDep) -> JSONResponse:
    """The processor catalog: every built-in type with its config schema, and the limits."""
    return JSONResponse(content=service.catalog())


class ProcessorTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processors: list[ProcessorSpec] = Field(min_length=1, max_length=MAX_PROCESSORS_PER_PHASE)
    text: str


@router.post(
    "/processors/test",
    tags=["processors"],
    responses={422: {"model": ErrorDetail}},
)
def test_processors(body: ProcessorTestRequest, principal: PrincipalDep, service: ProcessorServiceDep) -> JSONResponse:
    """Run deterministic processors in order on `text` (at most 32 KiB). Stops at the first block.
    Nothing is stored or logged; the sample is answered to the caller only."""
    return JSONResponse(content=service.test(body.processors, body.text))


@router.get(
    "/processor-results",
    tags=["processors"],
    responses={422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
def list_processor_results(
    principal: PrincipalDep,
    service: ProcessorServiceDep,
    agent_id: uuid.UUID | None = None,
    processor_type: Annotated[ProcessorType | None, Query(alias="type")] = None,
    action: ProcessorAction | None = None,
    phase: ProcessorPhase | None = None,
    run_id: uuid.UUID | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_RESULTS_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
) -> JSONResponse:
    """The tenant's processor outcomes, newest first, filtered by agent, processor type, action,
    phase, run and time range (`from` inclusive, `to` exclusive; a time without an offset is UTC).
    `next_before` is set when a full page came back. A query past the 5 s statement timeout is a
    503 asking to narrow the range."""
    return JSONResponse(
        content=service.results(
            principal.tenant_id, agent_id=agent_id, processor_type=processor_type, action=action, phase=phase,
            run_id=run_id, start=from_, end=to, limit=limit, before=before,
        )
    )
