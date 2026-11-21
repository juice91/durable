"""Request context of an agent (P0.8), for the Request Context page and the playground.

- GET  /agents/{agent_id}/request-context?version=<n|draft>   the context fields and their JSON
  schema (latest published version when `version` is omitted)
- POST /agents/{agent_id}/request-context/validate            check a context without starting a run

Validation is exactly the run start's (`eap_schemas.request_context.validate_request_context`):
200 {"valid": true, "request_context": <accepted context>} or 422 {"detail": [{"loc": ["body",
"request_context", <field>], "msg", "type"}, ...]}, the same body a run start answers. Another
tenant's or an unknown agent is a 404; no published version is a 409.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import PrincipalDep
from eap_api.schemas import ErrorDetail, RequestContextValidate
from eap_api.service_deps.agents import RequestContextServiceDep
from eap_api.services.run_options import DRAFT

router = APIRouter(
    tags=["request-context"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def _parse_version(value: str | None) -> int | str | None:
    if value is None or value == DRAFT:
        return value
    if value.isdigit() and 1 <= int(value) <= 2**31 - 1:
        return int(value)
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="version must be a published version number or 'draft'",
    )


@router.get("/agents/{agent_id}/request-context")
def get_request_context(
    agent_id: uuid.UUID,
    principal: PrincipalDep,
    service: RequestContextServiceDep,
    version: Annotated[str | None, Query(max_length=16)] = None,
) -> JSONResponse:
    """{"agent_id", "version" (null for the draft), "draft", "fields": [...], "schema": {...}}."""
    return JSONResponse(content=service.fields(principal.tenant_id, agent_id, _parse_version(version)))


@router.post("/agents/{agent_id}/request-context/validate")
def validate_request_context_route(
    agent_id: uuid.UUID, body: RequestContextValidate, principal: PrincipalDep, service: RequestContextServiceDep
) -> JSONResponse:
    return JSONResponse(content=service.validate(principal.tenant_id, agent_id, body.version, body.request_context))
