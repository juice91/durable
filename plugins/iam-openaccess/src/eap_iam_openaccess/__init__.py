"""The throw-away policy engine: every authenticated user is admin (design 5.1, 2.2)."""

from __future__ import annotations

from collections.abc import Sequence

from eap_iam import All, Decision, IdFilter, PluginContext, Principal, ResourceRef


class OpenAccessPolicy:
    name = "openaccess"
    transitional = True
    features: frozenset[str] = frozenset()

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision:
        if principal.kind == "user":
            return Decision(allowed=True, reason="role:admin", explain={"rule": "openaccess"})
        # Machines are bounded by the constraints their principal carries (eap_iam.permits); the engine abstains.
        return Decision(allowed=True, reason="abstain:constraints", explain={"rule": "openaccess"})

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


def create(ctx: PluginContext) -> OpenAccessPolicy:
    return OpenAccessPolicy()
