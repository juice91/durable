"""Authorisation in the service layer (design 2.5): the only caller of the policy engine.

The decision order is: constraints carried by the principal (key scopes, allowlists) -> policy engine -> default deny.
P0a wires the service and its call shape; resource services start calling `require` / `filter_ids` with the real engine
in IAM P2 (the transitional `openaccess` engine allows every user)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from eap_iam import (
    AccessRequirement,
    Decision,
    ExplainingPolicyEngine,
    Grant,
    GrantableRelation,
    GrantingPolicyEngine,
    IdFilter,
    Only,
    PolicyEngine,
    Principal,
    ResourceRef,
    UseGrant,
    permits,
    supports_grants,
)

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
    ("tenant", "manage_external_access"), ("tenant", "read_external_access"),
)


class GrantsUnsupported(Exception):
    """The selected policy engine keeps no grants (the transitional `openaccess`)."""


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
        refused = {"action": action, "kind": resource.kind, "id": resource.id}
        if resource.id is None and action == "create":
            # A collection (`create`): there is nothing to hide, and "may not create" is the useful answer.
            raise Forbidden("not allowed", code="forbidden", refused=refused)
        if action != "read":
            readable = await self.check(principal, "read", resource)
            if readable.allowed:
                raise Forbidden("not allowed", code="forbidden", refused=refused)
        raise NotFound("not found")

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter:
        if principal.kind == "api_key" and kind in ("agent", "workflow"):
            # A key's allowlists are constraints (permits), not engine data: a restricted key sees only its own ids.
            own, other = (principal.agent_ids, principal.workflow_ids) if kind == "agent" else (principal.workflow_ids, principal.agent_ids)
            if own is not None or other is not None:
                return Only(tuple(own or ()))
        return await self._engine.filter_ids(principal, action, kind, candidate_ids)

    async def register(self, resource: ResourceRef, creator: str | None, parent: ResourceRef | None = None) -> None:
        """Tell the engine about a resource the caller just created (tenant link, owner), before the response."""
        await self._engine.register_resource(resource, creator, parent)

    async def unregister(self, resource: ResourceRef) -> None:
        await self._engine.unregister_resource(resource)

    # -- agent-bound grants (docs/iam.md, "Agent and workflow grants") ---------------------------------------------------

    @property
    def supports_grants(self) -> bool:
        """Whether the selected engine keeps agent-bound grants (`openfga`; not `openaccess`)."""
        return supports_grants(self._engine)

    def _grant_engine(self) -> Any:
        if not supports_grants(self._engine):
            raise GrantsUnsupported()
        return self._engine

    async def grant_use(self, grant: UseGrant) -> None:
        await self._grant_engine().grant_use(grant)

    async def revoke_use(self, grant: UseGrant) -> None:
        await self._grant_engine().revoke_use(grant)

    async def used_by(self, subject: ResourceRef, kind: str) -> list[str]:
        return await self._grant_engine().used_by(subject, kind)

    async def grantees(self, resource: ResourceRef) -> list[ResourceRef]:
        return await self._grant_engine().grantees(resource)

    # -- per-resource grants (the Access tab) and explanations ----------------------------------------------------------

    @property
    def supports_access_grants(self) -> bool:
        """Whether the selected engine keeps per-resource grants for people and roles (`openfga`; not `openaccess`)."""
        return isinstance(self._engine, GrantingPolicyEngine) and "grants" in self._engine.features

    def _access_engine(self) -> Any:
        if not self.supports_access_grants:
            raise GrantsUnsupported()
        return self._engine

    def grantable(self, kind: str) -> list[GrantableRelation]:
        return self._access_engine().grantable(kind)

    async def list_grants(self, resource: ResourceRef) -> list[Grant]:
        return await self._access_engine().list_grants(resource)

    async def grant(self, grant: Grant) -> None:
        await self._access_engine().grant(grant)

    async def revoke(self, grant: Grant) -> None:
        await self._access_engine().revoke(grant)

    @property
    def supports_explain(self) -> bool:
        return isinstance(self._engine, ExplainingPolicyEngine) and "explain" in self._engine.features

    def requirements(self, action: str, kind: str, has_id: bool) -> AccessRequirement | None:
        """What the engine says `action` needs; None when it has no such action, or cannot explain."""
        return self._engine.requirements(action, kind, has_id) if self.supports_explain else None  # type: ignore[attr-defined]

    async def allowed_actions(self, principal: Principal) -> list[str]:
        """`kind.action` strings the principal may exercise on the tenant as a whole (for Studio to hide pages)."""
        allowed: list[str] = []
        for kind, action in CAPABILITY_ACTIONS:
            decision = await self.check(principal, action, ResourceRef(kind, None, principal.tenant_id))
            if decision.allowed:
                allowed.append(f"{kind}.{action}")
        return allowed
