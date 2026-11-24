"""The three plug-in interfaces. A plug-in distribution registers a factory `create(ctx) -> instance` under the
entry-point group of its interface; core never imports a plug-in."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from eap_iam.types import CatalogEntry, Decision, Grant, IdFilter, Member, Principal, RequestView, ResourceRef

GROUP_IDENTITY = "eap.iam.identity"
GROUP_POLICY = "eap.iam.policy"
GROUP_MEMBERSHIP = "eap.iam.membership"
GROUP_MIGRATIONS = "eap.db.migrations"


@runtime_checkable
class IdentityProvider(Protocol):
    name: str
    kinds: frozenset[str]  # principal kinds it can produce

    async def authenticate(self, request: RequestView) -> Principal | None:
        """A Principal, or None when this provider does not recognise the request. Raise `AuthError` only when
        the request presents THIS provider's credential and it is invalid: a bad token never falls through."""
        ...


@runtime_checkable
class PolicyEngine(Protocol):
    name: str
    transitional: bool  # refused in production unless EAP_IAM_ALLOW_TRANSITIONAL=1
    features: frozenset[str]  # "grants", "list_filter", "explain", "contextual_roles"

    async def check(self, principal: Principal, action: str, resource: ResourceRef) -> Decision: ...

    async def check_many(
        self, principal: Principal, action: str, resources: Sequence[ResourceRef]
    ) -> dict[str, Decision]: ...

    async def filter_ids(
        self, principal: Principal, action: str, kind: str, candidate_ids: Sequence[str] | None = None
    ) -> IdFilter: ...

    # Resource lifecycle (design 5.6): called by the service after the commit that creates or deletes a resource. An
    # engine with nothing to store (`openaccess`) does nothing. `creator` is the subject that becomes its owner; a run
    # has no owner and names its `parent` (the agent or workflow it belongs to) instead.
    async def register_resource(
        self, resource: ResourceRef, creator: str | None = None, parent: ResourceRef | None = None
    ) -> None: ...

    async def unregister_resource(self, resource: ResourceRef) -> None: ...


@runtime_checkable
class GrantingPolicyEngine(PolicyEngine, Protocol):
    """A policy engine that stores per-resource grants (features contains "grants"). Studio's Access tab uses it;
    only the relations in `eap_iam.GRANT_RELATIONS` are writable. Raises `GrantError` for a malformed grant and
    `PolicyUnavailable` when the engine cannot answer."""

    async def grant(self, grant: Grant) -> None: ...

    async def revoke(self, grant: Grant) -> None: ...

    async def list_grants(self, resource: ResourceRef) -> list[Grant]: ...


@runtime_checkable
class ResourceCatalog(Protocol):
    """Every platform resource with its tenant and creator, read from the platform's own stores (implemented in
    `eap_run_store`, never in a plug-in). A policy plug-in's reconcile job diffs it against its own tuples."""

    def entries(self, tenant_id: str | None = None) -> list[CatalogEntry]: ...


@runtime_checkable
class MembershipSource(Protocol):
    name: str
    mutable: bool

    async def roles_of(self, tenant_id: str, subject: str) -> frozenset[str]: ...

    # The member operations behind Studio's Members page (design 7.3). The change is made in the source (the IdP),
    # the single source of truth; a read-only source (`mutable` False) refuses the three writers with
    # `MembershipError("unavailable", ...)`. The last admin can never be demoted or removed (`last_admin`).
    async def list_members(self, tenant_id: str) -> list[Member]: ...

    async def invite(self, tenant_id: str, email: str, role: str) -> Member: ...

    async def set_role(self, tenant_id: str, member_id: str, role: str) -> Member: ...

    async def remove(self, tenant_id: str, member_id: str) -> None: ...
