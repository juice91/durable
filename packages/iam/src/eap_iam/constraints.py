"""Constraints carried by a principal, evaluated before the policy engine is asked (design 1.5, 2.3).

The mapping from action to required scope is data here, not code in a plug-in."""

from __future__ import annotations

from eap_iam.types import (
    ACTION_INGEST_OTLP,
    ACTION_MCP_MANAGE,
    ACTION_MCP_READ,
    ACTION_RUN_AGUI,
    ROLE_ADMIN,
    ROLE_EDITOR,
    ROLE_VIEWER,
    Principal,
    ResourceRef,
)

# Scopes that satisfy an action; any one of them is enough, the first is the one named in a refusal.
ACTION_SCOPES: dict[str, tuple[str, ...]] = {
    ACTION_RUN_AGUI: ("agui:run",),
    ACTION_INGEST_OTLP: ("otlp",),
    ACTION_MCP_READ: ("mcp", "manage"),
    ACTION_MCP_MANAGE: ("manage",),
}


# The scopes a personal API key may carry, by the role of its owner (the union over the owner's roles). A viewer reads
# only (`mcp` is the read scope of the MCP control endpoint); `agui:run` starts runs, `manage` and `otlp` write.
# `otlp` stays with admins: it feeds the tenant's telemetry store.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset({"agui:run", "mcp", "manage", "otlp"}),
    ROLE_EDITOR: frozenset({"agui:run", "mcp", "manage"}),
    ROLE_VIEWER: frozenset({"mcp"}),
}


def scopes_for_roles(roles: "frozenset[str] | set[str] | tuple[str, ...]") -> frozenset[str]:
    """The scopes a key owned by someone with `roles` may carry; an unknown role grants none."""
    allowed: frozenset[str] = frozenset()
    for role in roles:
        allowed |= ROLE_SCOPES.get(role, frozenset())
    return allowed


def has_required_scope(principal: Principal, action: str) -> bool:
    """True when `action` needs no scope, or the principal carries one that satisfies it."""
    needed = ACTION_SCOPES.get(action)
    return needed is None or any(scope in principal.scopes for scope in needed)


def _allows(allowlist: tuple[str, ...] | None, other: tuple[str, ...] | None, resource_id: str | None) -> bool:
    # Fail closed: a NULL list allows everything only when the other list is NULL too.
    if allowlist is None and other is None:
        return True
    return allowlist is not None and resource_id is not None and str(resource_id) in allowlist


def permits(principal: Principal, action: str, resource: ResourceRef | None = None) -> bool:
    """The pure constraint check: the scope the action needs, then the agent / workflow allowlists.

    Only API-key principals carry constraints; every other kind passes here and is decided by the policy engine."""
    if principal.kind != "api_key":
        return True
    if not has_required_scope(principal, action):
        return False
    if resource is not None and resource.kind == "agent":
        return _allows(principal.agent_ids, principal.workflow_ids, resource.id)
    if resource is not None and resource.kind == "workflow":
        return _allows(principal.workflow_ids, principal.agent_ids, resource.id)
    return True
