"""Per-resource access (Studio's Access tab) and "why can't I?" (docs/iam.md, "The Access tab and explain").

- GET    /iam/access/{kind}/{id}            who holds which relation (whoever may read the resource)
- POST   /iam/access/{kind}/{id}/grants     body {relation, member_id | role}: grant (admin only)
- DELETE /iam/access/{kind}/{id}/grants?relation=&subject_type=&subject=      revoke (admin only); 204
- GET    /iam/explain?kind=&id=&action=     the caller's own decision(s) and what each action needs

`kind` is agent, workflow, knowledge_base, secret (id: its name) or mcp_server. With the transitional `openaccess`
engine the status reports `supported: false` and a write answers 503 `grants_unavailable`."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import CorePrincipalDep
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.iam import IamExplainServiceDep, ResourceAccessServiceDep

router = APIRouter(
    prefix="/iam",
    tags=["iam"],
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
)

Relation = Literal["owner", "editor", "runner", "approver", "viewer"]
Role = Literal["admin", "editor", "viewer"]
ObjectId = Annotated[str, Path(min_length=1, max_length=200)]
ExplainKind = Literal["agent", "workflow", "knowledge_base", "secret", "mcp_server", "schedule", "api_key", "tenant"]
ActionName = Annotated[str, Field(pattern=r"^[a-z_]{1,40}$")]


class RelationView(BaseModel):
    relation: str
    roles: list[str]  # the tenant roles that may hold it; empty: people only


class GrantView(BaseModel):
    relation: str
    subject_type: str  # "user" | "role"
    subject: str


class ResourceView(BaseModel):
    kind: str
    id: str


class AccessView(BaseModel):
    resource: ResourceView
    supported: bool
    can_manage: bool
    relations: list[RelationView]
    grants: list[GrantView]
    used_by: list[ResourceView]  # agents and workflows granted a server or secret (those the caller may read)


class GrantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relation: Relation
    member_id: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.@-]+$")
    role: Role | None = None


class NeededView(BaseModel):
    roles: list[str]
    relations: list[str]


class DecisionView(BaseModel):
    action: str
    allowed: bool
    reason: str  # "allowed" | "not_a_member" | "no_permission" | "engine_unavailable"
    needed: NeededView | None


class ExplainView(BaseModel):
    supported: bool
    kind: str
    id: str | None
    roles: list[str]
    decisions: list[DecisionView]


@router.get("/access/{kind}/{object_id}", response_model=AccessView)
def get_resource_access(kind: str, object_id: ObjectId, service: ResourceAccessServiceDep) -> dict:
    return service.status(kind, object_id)


@router.post("/access/{kind}/{object_id}/grants", response_model=GrantView)
def add_resource_grant(kind: str, object_id: ObjectId, body: GrantBody, service: ResourceAccessServiceDep) -> dict:
    return service.grant(kind, object_id, body.relation, body.member_id, body.role)


@router.delete("/access/{kind}/{object_id}/grants", status_code=status.HTTP_204_NO_CONTENT)
def remove_resource_grant(
    kind: str,
    object_id: ObjectId,
    relation: Annotated[Relation, Query()],
    subject_type: Annotated[Literal["user", "role"], Query()],
    subject: Annotated[str, Query(min_length=1, max_length=256)],
    service: ResourceAccessServiceDep,
) -> Response:
    service.revoke(kind, object_id, relation, subject_type, subject)
    return Response(status_code=204)


@router.get("/explain", response_model=ExplainView)
async def explain_access(
    principal: CorePrincipalDep,
    service: IamExplainServiceDep,
    kind: Annotated[ExplainKind, Query()],
    action: Annotated[list[ActionName], Query(min_length=1, max_length=8)],
    id: Annotated[str | None, Query(min_length=1, max_length=200)] = None,  # noqa: A002 - the query parameter's name
) -> dict:
    return await service.explain(principal, kind, id, action)
