"""Providers of the IAM services: the authorisation service and the capabilities answer."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import ApiKeyStoreDep, IamDep, SessionDep
from eap_api.services.authz import AuthzService
from eap_api.services.iam_capabilities import IamCapabilitiesService
from eap_api.services.members import MembersService
from eap_api.services.personal_api_keys import PersonalApiKeyService
from eap_api.services.permissions_report import PermissionsReportService


def get_authz_service(iam: IamDep) -> AuthzService:
    return AuthzService(iam.policy)


AuthzServiceDep = Annotated[AuthzService, Depends(get_authz_service)]


def get_iam_capabilities_service(iam: IamDep, authz: AuthzServiceDep) -> IamCapabilitiesService:
    return IamCapabilitiesService(iam.describe(), authz)


IamCapabilitiesServiceDep = Annotated[IamCapabilitiesService, Depends(get_iam_capabilities_service)]


def get_permissions_report_service(db: SessionDep, authz: AuthzServiceDep) -> PermissionsReportService:
    return PermissionsReportService(db, authz)


PermissionsReportServiceDep = Annotated[PermissionsReportService, Depends(get_permissions_report_service)]


def get_members_service(iam: IamDep, authz: AuthzServiceDep) -> MembersService:
    return MembersService(iam.membership, authz)


MembersServiceDep = Annotated[MembersService, Depends(get_members_service)]


def get_personal_api_key_service(store: ApiKeyStoreDep, iam: IamDep) -> PersonalApiKeyService:
    return PersonalApiKeyService(store, iam.membership)


PersonalApiKeyServiceDep = Annotated[PersonalApiKeyService, Depends(get_personal_api_key_service)]
