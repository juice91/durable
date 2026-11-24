"""`GET /api/v1/iam/capabilities`: the active IAM plug-ins and what the caller may do (design 5.7).

Names, features and allowed actions only; never a secret or a configuration value."""

from __future__ import annotations

from typing import Any

from eap_iam import Principal

from eap_api.services.authz import AuthzService


class IamCapabilitiesService:
    def __init__(self, description: dict[str, Any], authz: AuthzService) -> None:
        self._description = description
        self._authz = authz

    async def capabilities(self, principal: Principal) -> dict[str, Any]:
        # The e-mail is its own attribute: a plug-in whose subject is a stable id (zitadel:<sub>) carries it in `extra`;
        # one whose subject is the e-mail (cfaccess) is read from the subject.
        extra_email = principal.extra.get("email") if isinstance(principal.extra.get("email"), str) else None
        email = extra_email.lower() if extra_email else (
            principal.subject.lower() if principal.kind == "user" and "@" in principal.subject else None
        )
        return {
            **self._description,
            "me": {
                "subject": principal.subject,
                "email": email,
                "tenant_id": principal.tenant_id,
                "kind": principal.kind,
                "roles": sorted(principal.roles),
                "provider": principal.provider,
            },
            "actions": await self._authz.allowed_actions(principal),
        }
