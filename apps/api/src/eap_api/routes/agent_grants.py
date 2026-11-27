"""Agent-bound grants: the Access section of an agent and of a workflow (docs/iam.md, "Agent and workflow grants").

- GET    /agents/{id}/access                    the MCP servers and secrets the agent may use (whoever may read it)
- POST   /agents/{id}/grants                    body {kind: mcp_server|secret, name}: grant (admin only); 200 the grant
- DELETE /agents/{id}/grants?kind=&name=        revoke (admin only); 204
- GET    /agents/{id}/publish-grants            what publishing the draft would newly grant, and whether the caller may approve
- the same four under /workflows/{id}

With the transitional `openaccess` engine the section reports `supported: false` and a write answers 503
`grants_unavailable`: that engine keeps no per-agent grants."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.agents import AgentServiceDep
from eap_api.service_deps.workflows import WorkflowServiceDep

router = APIRouter(
    tags=["agent grants"],
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
Kind = Literal["mcp_server", "secret"]


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=200)


@router.get("/agents/{agent_id}/access")
def get_agent_access(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    return service.access_status(principal.tenant_id, agent_id)


@router.post("/agents/{agent_id}/grants")
def add_agent_grant(agent_id: uuid.UUID, body: GrantBody, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    service.access_status(principal.tenant_id, agent_id)  # the agent must exist and be readable
    return service.access_grants(principal.tenant_id).grant("agent", str(agent_id), body.kind, body.name, body.reason)


@router.delete("/agents/{agent_id}/grants", status_code=status.HTTP_204_NO_CONTENT)
def remove_agent_grant(
    agent_id: uuid.UUID, kind: Annotated[Kind, Query()], name: Annotated[str, Query(min_length=1, max_length=200)],
    principal: PrincipalDep, service: AgentServiceDep,
) -> Response:
    service.access_grants(principal.tenant_id).revoke("agent", str(agent_id), kind, name)
    return Response(status_code=204)


@router.get("/agents/{agent_id}/publish-grants")
def get_agent_publish_grants(agent_id: uuid.UUID, principal: PrincipalDep, service: AgentServiceDep) -> dict:
    return service.publish_grants(principal.tenant_id, agent_id)


@router.get("/workflows/{workflow_id}/access")
def get_workflow_access(workflow_id: uuid.UUID, principal: PrincipalDep, service: WorkflowServiceDep) -> dict:
    return service.access_status(principal.tenant_id, str(workflow_id))


@router.post("/workflows/{workflow_id}/grants")
def add_workflow_grant(workflow_id: uuid.UUID, body: GrantBody, principal: PrincipalDep, service: WorkflowServiceDep) -> dict:
    service.access_status(principal.tenant_id, str(workflow_id))
    return service.access_grants().grant("workflow", str(workflow_id), body.kind, body.name, body.reason)


@router.delete("/workflows/{workflow_id}/grants", status_code=status.HTTP_204_NO_CONTENT)
def remove_workflow_grant(
    workflow_id: uuid.UUID, kind: Annotated[Kind, Query()], name: Annotated[str, Query(min_length=1, max_length=200)],
    principal: PrincipalDep, service: WorkflowServiceDep,
) -> Response:
    service.access_status(principal.tenant_id, str(workflow_id))
    service.access_grants().revoke("workflow", str(workflow_id), kind, name)
    return Response(status_code=204)


@router.get("/workflows/{workflow_id}/publish-grants")
def get_workflow_publish_grants(workflow_id: uuid.UUID, principal: PrincipalDep, service: WorkflowServiceDep) -> dict:
    return service.publish_grants(principal.tenant_id, str(workflow_id))
