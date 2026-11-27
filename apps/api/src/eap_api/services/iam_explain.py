""""Why can't I?" (design 7.2): the policy engine's decision on the caller's own request, and what would allow it.

It answers about the caller only: their roles, and the roles or grants the model says the action needs. It never names who
else holds a right, and it does not say whether a resource exists (a resource the caller may not read and one that is not
there get the same answer). An engine that cannot answer is a decision too (`engine_unavailable`), not an error."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from eap_iam import PolicyUnavailable, Principal, ResourceRef

from eap_api.services.authz import AuthzService
from eap_api.services.errors import InvalidInput

log = logging.getLogger("eap_api.iam_explain")

ALLOWED = "allowed"
NOT_A_MEMBER = "not_a_member"
NO_PERMISSION = "no_permission"
ENGINE_UNAVAILABLE = "engine_unavailable"


class IamExplainService:
    def __init__(self, authz: AuthzService) -> None:
        self._authz = authz

    async def explain(self, principal: Principal, kind: str, resource_id: str | None, actions: Sequence[str]) -> dict[str, Any]:
        resource_id = None if kind == "tenant" else resource_id
        supported = self._authz.supports_explain
        decisions: list[dict[str, Any]] = []
        for action in dict.fromkeys(actions):
            needed = self._authz.requirements(action, kind, resource_id is not None)
            if supported and needed is None:
                raise InvalidInput(f"there is no action {action!r} on a {kind}", code="unknown_action")
            try:
                decision = await self._authz.check(principal, action, ResourceRef(kind, resource_id, principal.tenant_id))
                allowed = decision.allowed
                reason = ALLOWED if allowed else NO_PERMISSION if principal.roles else NOT_A_MEMBER
            except PolicyUnavailable as exc:
                log.error("policy engine unavailable: %s", exc.detail)
                allowed, reason = False, ENGINE_UNAVAILABLE
            decisions.append({
                "action": action, "allowed": allowed, "reason": reason,
                "needed": None if needed is None else {"roles": list(needed.roles), "relations": list(needed.relations)},
            })
        return {
            "supported": supported, "kind": kind, "id": resource_id, "roles": sorted(principal.roles), "decisions": decisions,
        }
