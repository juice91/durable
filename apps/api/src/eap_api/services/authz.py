"""Authorisation in the service layer (design 2.5): the only caller of the policy engine.

The decision order is: constraints carried by the principal (key scopes, allowlists) -> policy engine -> default deny.
P0a wires the service and its call shape; resource services start calling `require` / `filter_ids` with the real engine
in IAM P2 (the transitional `openaccess` engine allows every user)."""

from __future__ import annotations

from collections.abc import Sequence

from eap_iam import Decision, IdFilter, PolicyEngine, Principal, ResourceRef, permits

from eap_api.services.errors import Forbidden, NotFound

# What the capabilities answer reports: (resource kind, action) pairs the caller may exercise, as `kind.action`.
CAPABILITY_ACTIONS: tuple[tuple[str, str], ...] = (
    ("agent", "read"), ("agent", "run"), ("agent", "edit"), ("agent", "publish"), ("agent", "archive"),
    ("workflow", "read"), ("workflow", "run"), ("workflow", "edit"), ("workflow", "publish"), ("workflow", "archive"),
    ("knowledge_base", "read"), ("knowledge_base", "edit"),
    ("secret", "read"), ("secret", "edit"),
    ("mcp_server", "read"), ("mcp_server", "edit"),
    ("api_key", "read"), ("api_key", "revoke"),
    ("tenant", "manage_members"), ("tenant", "read_audit"), ("tenant", "read_permissions"),
)


class AuthzService:
    def __init__(self, engine: PolicyEngine) -> None:
        self._engine = engine

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        if not permits(principal, action, resource):
            return Decision(allowed=False, reason="deny:constraint")
        return await self._engine.check(principal, action, resource)

    async def require(self, principal: Principal, action: str, resource: ResourceRef) -> None:
        """Return when allowed. A principal that may not even read the resource gets NotFound (the same answer as
        "does not exist"); one that can read but not act gets Forbidden with a stable code."""
        decision = await self.check(principal, action, resource)
        if decision.allowed:
            return
        if action != "read":
            readable = await self.check(principal, "read", resource)
            if readable.allowed:
                raise Forbidden("not allowed", code="forbidden")
        raise NotFound("not found")

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        return await self._engine.filter_ids(principal, action, kind, candidate_ids)

    async def allowed_actions(self, principal: Principal) -> list[str]:
        """`kind.action` strings the principal may exercise on the tenant as a whole (for Studio to hide pages)."""
        allowed: list[str] = []
        for kind, action in CAPABILITY_ACTIONS:
            decision = await self.check(principal, action, ResourceRef(kind, None, principal.tenant_id))
            if decision.allowed:
                allowed.append(f"{kind}.{action}")
        return allowed
