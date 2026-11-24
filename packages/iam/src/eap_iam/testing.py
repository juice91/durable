"""Fakes for unit tests and for running the API suite with every plug-in absent (`EAP_IAM_IGNORE_PLUGINS`).

`FakeIdentityProvider` accepts the dev bearer `Bearer dev:<tenant>:<user>` from any peer; `FakePolicyEngine` allows
everything (or nothing). `register_fakes()` registers them locally as `fake`."""

from __future__ import annotations

import re
from collections.abc import Sequence

from eap_iam.protocols import GROUP_IDENTITY, GROUP_POLICY
from eap_iam.registry import PluginContext, register_local
from eap_iam.types import AuthError, Decision, IdFilter, All, Principal, RequestView, ResourceRef

_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")


class FakeIdentityProvider:
    """The dev bearer from any peer, with the same format rules and messages as `eap-iam-devloopback`."""

    name = "fake"
    kinds = frozenset({"user"})

    async def authenticate(self, request: RequestView) -> Principal | None:
        header = request.header("authorization")
        if header is None or not header.startswith("Bearer dev:"):
            return None
        parts = header.split(":")
        if len(parts) != 3 or parts[0] != "Bearer dev":
            raise AuthError("Invalid Authorization header format", www_authenticate=False)
        tenant, user = parts[1], parts[2]
        if not tenant.strip():
            raise AuthError("Tenant ID cannot be empty", www_authenticate=False)
        if not user.strip():
            raise AuthError("User ID cannot be empty", www_authenticate=False)
        if not _SAFE.match(tenant):
            raise AuthError("Invalid tenant ID format", www_authenticate=False)
        if not _SAFE.match(user):
            raise AuthError("Invalid user ID format", www_authenticate=False)
        return Principal(tenant_id=tenant.strip(), kind="user", subject=user.strip(), provider="fake")


class FakePolicyEngine:
    name = "fake"
    transitional = False
    features: frozenset[str] = frozenset()

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        return Decision(allowed=self.allow, reason="fake:allow" if self.allow else "fake:deny")

    async def check_many(
        self, principal: Principal, action: str, resources: Sequence[ResourceRef]
    ) -> dict[str, Decision]:
        return {str(r.id): await self.check(principal, action, r) for r in resources}

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        return All()

    async def register_resource(
        self, resource: ResourceRef, creator: str | None = None, parent: ResourceRef | None = None
    ) -> None:
        return None  # nothing to store

    async def unregister_resource(self, resource: ResourceRef) -> None:
        return None


def register_fakes() -> None:
    register_local(GROUP_IDENTITY, "fake", lambda ctx: FakeIdentityProvider())
    register_local(GROUP_POLICY, "fake", lambda ctx: FakePolicyEngine())


__all__ = ["FakeIdentityProvider", "FakePolicyEngine", "PluginContext", "register_fakes"]
