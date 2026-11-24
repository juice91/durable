"""Provider of the audit read service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import AuditStoreDep
from eap_api.service_deps.iam import AuthzServiceDep
from eap_api.services.audit import AuditService


def get_audit_service(store: AuditStoreDep, authz: AuthzServiceDep) -> AuditService:
    return AuditService(store, authz)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]
