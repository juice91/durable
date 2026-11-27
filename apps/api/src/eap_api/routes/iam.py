"""`GET /iam/capabilities`: which IAM plug-ins are active and what the caller may do (design 5.7). Any authenticated
principal may ask; the answer names plug-ins and features, never secrets or configuration values."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Response
from pydantic import BaseModel, Field

from eap_api.auth import CorePrincipalDep
from eap_api.service_deps.iam import IamCapabilitiesServiceDep, MembersServiceDep, PermissionsReportServiceDep

router = APIRouter(prefix="/iam", tags=["iam"])


class IdentityPluginView(BaseModel):
    name: str
    kinds: list[str]


class PolicyView(BaseModel):
    name: str
    transitional: bool
    features: list[str]


class MembershipView(BaseModel):
    name: str
    mutable: bool


class MeView(BaseModel):
    subject: str
    email: str | None
    tenant_id: str
    kind: str
    roles: list[str]
    provider: str


class IamCapabilities(BaseModel):
    mode: str
    identity: list[IdentityPluginView]
    policy: PolicyView
    membership: MembershipView | None
    me: MeView
    actions: list[str]


@router.get("/capabilities", response_model=IamCapabilities)
async def get_capabilities(principal: CorePrincipalDep, service: IamCapabilitiesServiceDep) -> dict:
    return await service.capabilities(principal)


class ImpliedPermissionsItem(BaseModel):
    kind: str
    id: str
    name: str | None
    version: int
    declared: bool  # the spec already carries an explicit `permissions` block
    implied: dict[str, Any]  # the block its references imply (omitted lists are empty)
    dynamic: bool  # a secret, host or agent is computed at run time: the block is incomplete
    dynamic_paths: list[str]


class ImpliedPermissionsSummary(BaseModel):
    total: int
    dynamic: int
    declared: int


class ImpliedPermissionsReport(BaseModel):
    items: list[ImpliedPermissionsItem]
    truncated: bool
    summary: ImpliedPermissionsSummary
    enforced: bool  # always false in P0b: the report only reads


@router.get("/implied-permissions", response_model=ImpliedPermissionsReport)
async def implied_permissions_report(principal: CorePrincipalDep, service: PermissionsReportServiceDep) -> dict:
    """What the published agents and workflows would need declared (design 3.4). Read only: nothing is stored or
    enforced. Admin only."""
    return await service.report(principal)


# --- Members (design 7.3): the membership source's list, invite, role change and removal. Admin only; 404 with no source.

MemberRole = Literal["admin", "editor", "viewer"]
MemberId = Annotated[str, Path(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.@-]+$")]


class MemberView(BaseModel):
    id: str
    email: str | None
    display_name: str | None
    roles: list[str]
    status: str


class MemberList(BaseModel):
    items: list[MemberView]
    mutable: bool
    can_create_users: bool  # False: an invitation only adds a user who already exists in the identity provider


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: MemberRole


class RoleBody(BaseModel):
    role: MemberRole


@router.get("/members", response_model=MemberList)
async def list_members(principal: CorePrincipalDep, service: MembersServiceDep) -> dict:
    return await service.list(principal)


@router.post("/members", response_model=MemberView, status_code=201)
async def invite_member(body: InviteBody, principal: CorePrincipalDep, service: MembersServiceDep) -> dict:
    return await service.invite(principal, body.email, body.role)


@router.patch("/members/{member_id}", response_model=MemberView)
async def change_member_role(member_id: MemberId, body: RoleBody, principal: CorePrincipalDep, service: MembersServiceDep) -> dict:
    return await service.set_role(principal, member_id, body.role)


@router.delete("/members/{member_id}", status_code=204)
async def remove_member(member_id: MemberId, principal: CorePrincipalDep, service: MembersServiceDep) -> Response:
    await service.remove(principal, member_id)
    return Response(status_code=204)
