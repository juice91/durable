"""Providers of the IAM services: the authorisation service and the capabilities answer."""

from __future__ import annotations

from typing import Annotated, Any

from eap_run_store.iam_catalog import RunStoreCatalog
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from eap_api.auth import AgUiPrincipal, Principal, core_principal, current_principal, otlp_principal
from eap_api.deps import ApiKeyStoreDep, AuditStoreDep, EngineDep, IamDep, SessionDep, iam_of
from eap_api.services.access import Access
from eap_api.services.authz import AuthzService
from eap_api.services.converse import require_converse
from eap_api.services.iam_explain import IamExplainService
from eap_api.services.resource_access import ResourceAccessService
from eap_api.services.iam_capabilities import IamCapabilitiesService
from eap_api.services.members import MembersService
from eap_api.services.personal_api_keys import PersonalApiKeyService
from eap_api.services.permissions_report import PermissionsReportService


def get_authz_service(iam: IamDep) -> AuthzService:
    return AuthzService(iam.policy)


AuthzServiceDep = Annotated[AuthzService, Depends(get_authz_service)]


def get_access(principal: Annotated[Principal, Depends(current_principal)], authz: AuthzServiceDep) -> Access:
    """The caller's access to the policy engine for the resource services (one per request)."""
    return Access(authz, core_principal(principal))


AccessDep = Annotated[Access, Depends(get_access)]


def get_key_access(request: Request, principal: Annotated[AgUiPrincipal, Depends(otlp_principal)]) -> Access:
    """The access of an OTLP ingest call: an API key (or the dev bearer), which `current_principal` would refuse."""
    return build_access(request.app, principal)


KeyAccessDep = Annotated[Access, Depends(get_key_access)]


def build_access(app: Any, principal: Any) -> Access:
    """An Access for an entry point that has an app and a compatibility principal but no Request (MCP control, AG-UI)."""
    return Access(AuthzService(iam_of(app).policy), core_principal(principal))


async def require_access(request: Any, principal: Any, action: str, kind: str, resource_id: str | None) -> None:
    """The per-resource check for an async entry point (AG-UI): the same decision as the services', made off the loop."""
    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(build_access(request.app, principal).require, action, kind, resource_id)


async def require_converse_access(request: Any, engine: Any, principal: Any, agent_id: str) -> Access | None:
    """The check of a new AG-UI turn (eap_api.services.converse): `run` on the agent, or the viewer's Architect
    exception. Returns the caller's Access when only the exception allows it (they may then continue only their own
    thread), else None."""
    from starlette.concurrency import run_in_threadpool

    access = build_access(request.app, principal)

    def check() -> bool:
        with Session(engine) as db:
            return require_converse(access, db, access.tenant_id, agent_id)

    return access if await run_in_threadpool(check) else None


def get_iam_capabilities_service(iam: IamDep, authz: AuthzServiceDep) -> IamCapabilitiesService:
    return IamCapabilitiesService(iam.describe(), authz)


IamCapabilitiesServiceDep = Annotated[IamCapabilitiesService, Depends(get_iam_capabilities_service)]


def get_resource_access_service(engine: EngineDep, access: AccessDep, iam: IamDep, audit: AuditStoreDep) -> ResourceAccessService:
    return ResourceAccessService(RunStoreCatalog(engine), access, iam.membership, audit)


ResourceAccessServiceDep = Annotated[ResourceAccessService, Depends(get_resource_access_service)]


def get_iam_explain_service(authz: AuthzServiceDep) -> IamExplainService:
    return IamExplainService(authz)


IamExplainServiceDep = Annotated[IamExplainService, Depends(get_iam_explain_service)]


def get_permissions_report_service(db: SessionDep, authz: AuthzServiceDep) -> PermissionsReportService:
    return PermissionsReportService(db, authz)


PermissionsReportServiceDep = Annotated[PermissionsReportService, Depends(get_permissions_report_service)]


def get_members_service(iam: IamDep, authz: AuthzServiceDep) -> MembersService:
    return MembersService(iam.membership, authz)


MembersServiceDep = Annotated[MembersService, Depends(get_members_service)]


def get_personal_api_key_service(store: ApiKeyStoreDep, iam: IamDep, authz: AuthzServiceDep) -> PersonalApiKeyService:
    return PersonalApiKeyService(store, iam.membership, authz)


PersonalApiKeyServiceDep = Annotated[PersonalApiKeyService, Depends(get_personal_api_key_service)]
