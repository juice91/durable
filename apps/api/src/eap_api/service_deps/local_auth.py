"""Provider of the password sign-in service: the identity provider of the chain that can sign in, if any."""

from __future__ import annotations

from typing import Annotated

from eap_iam import PasswordSignInProvider
from fastapi import Depends, Request

from eap_api.deps import AuditStoreDep, IamDep
from eap_api.services.local_auth import LocalAuthService


def get_local_auth_service(request: Request, iam: IamDep, audit: AuditStoreDep) -> LocalAuthService:
    provider = next((p for p in iam.providers if isinstance(p, PasswordSignInProvider)), None)
    return LocalAuthService(provider, audit, getattr(request.state, "request_id", None))


LocalAuthServiceDep = Annotated[LocalAuthService, Depends(get_local_auth_service)]
