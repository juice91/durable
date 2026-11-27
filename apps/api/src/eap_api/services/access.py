"""Per-request access to the policy engine for the (synchronous) resource services (IAM design 2.5, 5.6).

A resource service is built once per request with an `Access`: the caller's core `Principal` plus the `AuthzService`
(the only code that asks the policy engine). The service calls `require` before it acts, `visible` to narrow a list and
`register` / `unregister` when it creates or deletes a resource; it never sees the engine.

`access=None` means "no caller to check" (a script or test that builds a service directly, a worker): every check
passes. The providers in `service_deps/` and the MCP / AG-UI entry points always pass one, so a request never runs
unchecked by omission of a header; with the deployed `openaccess` policy every check allows.

The services run in a worker thread (FastAPI sync routes, `run_in_threadpool`); the engine is async, so each call is
handed back to the event loop with `anyio.from_thread.run`. A thread with no event loop (a unit test) runs it with
`asyncio.run`. A failing engine is `Unavailable` (503): nothing is allowed while the policy cannot answer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

import anyio
from eap_iam import (
    All,
    Except,
    Grant,
    GrantableRelation,
    GrantError,
    IdFilter,
    Only,
    PolicyUnavailable,
    Principal,
    ResourceRef,
    UseGrant,
)

from eap_api.services.authz import AuthzService, GrantsUnsupported
from eap_api.services.errors import Forbidden, InvalidInput, NotFound, Unavailable

log = logging.getLogger("eap_api.access")

T = TypeVar("T")

UNAVAILABLE = "authorization is unavailable"
# What an unknown resource answers today, so a resource the caller may not read is indistinguishable from one that is
# not there (design 2.5).
NOT_FOUND = {
    "agent": "Agent not found", "workflow": "Workflow not found", "schedule": "Schedule not found",
    "run": "Run not found", "knowledge_base": "Knowledge base not found", "secret": "secret not found",
    "mcp_server": "MCP server not found", "api_key": "API key not found",
}


def run_sync(call: Callable[..., Awaitable[T]], *args: Any) -> T:
    """Run an async call from sync code: on the event loop when this is a worker thread of one, else on a new loop."""
    try:
        return anyio.from_thread.run(call, *args)
    except RuntimeError:  # not an anyio worker thread: there is no loop to hand the call to
        return asyncio.run(call(*args))


class Access:
    def __init__(self, authz: AuthzService, principal: Principal) -> None:
        self._authz = authz
        self.principal = principal
        self._allowed: set[tuple[str, str, str | None]] = set()  # decisions of this request: allows only

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    def _ref(self, kind: str, resource_id: str | None) -> ResourceRef:
        return ResourceRef(kind, None if resource_id is None else str(resource_id), self.principal.tenant_id)

    def _run(self, call: Callable[..., Awaitable[T]], *args: Any) -> T:
        try:
            return run_sync(call, self.principal, *args)
        except PolicyUnavailable as exc:
            log.error("policy engine unavailable: %s", exc.detail)
            raise Unavailable(UNAVAILABLE, code="authz_unavailable") from None

    # -- decisions -------------------------------------------------------------------------------------------------

    def require(self, action: str, kind: str, resource_id: str | None) -> None:
        """Return when the caller may `action` the resource. NotFound when it may not even read it, Forbidden when it can
        read but not act (AuthzService.require). `resource_id` None is the collection (`create`, answered by the tenant)."""
        key = (action, kind, None if resource_id is None else str(resource_id))
        if key in self._allowed:
            return
        try:
            self._run(self._authz.require, action, self._ref(kind, resource_id))
        except NotFound:
            raise NotFound(NOT_FOUND.get(kind, "not found")) from None
        self._allowed.add(key)

    def allowed(self, action: str, kind: str, resource_id: str | None) -> bool:
        """`require` as a boolean (no refusal raised)."""
        try:
            self.require(action, kind, resource_id)
        except (NotFound, Forbidden):
            return False
        return True

    def visible(self, kind: str, action: str = "read") -> IdFilter:
        """Which ids of `kind` the caller may `action`: one engine call, whatever the page size."""
        return self._run(self._authz.filter_ids, action, kind)

    # -- resource lifecycle ----------------------------------------------------------------------------------------

    def register(self, kind: str, resource_id: str, parent: tuple[str, str] | None = None) -> None:
        """Write the tenant link and the owner tuple of a resource the caller just created, before the response. One
        retry: a transient failure must not leave the creator unable to see what it made (the reconcile job heals the
        rest). The creator is the principal's subject (for a run principal, the user it acts for)."""
        creator = self.principal.on_behalf_of if self.principal.kind == "run" else self.principal.subject
        parent_ref = None if parent is None else self._ref(*parent)
        for attempt in (1, 2):
            try:
                run_sync(self._authz.register, self._ref(kind, resource_id), creator, parent_ref)
                return
            except PolicyUnavailable as exc:
                log.error("policy register failed (attempt %s): %s", attempt, exc.detail)
        raise Unavailable(UNAVAILABLE, code="authz_unavailable")

    # -- agent-bound grants ----------------------------------------------------------------------------------------

    @property
    def supports_grants(self) -> bool:
        return self._authz.supports_grants

    def _grants(self, call: Callable[..., Awaitable[T]], *args: Any) -> T:
        try:
            return run_sync(call, *args)
        except PolicyUnavailable as exc:
            log.error("policy engine unavailable: %s", exc.detail)
            raise Unavailable(UNAVAILABLE, code="authz_unavailable") from None
        except GrantsUnsupported:
            raise Unavailable(
                "the active policy engine keeps no per-agent grants", code="grants_unavailable"
            ) from None
        except GrantError as exc:
            raise InvalidInput(exc.detail, code=exc.code) from None

    def used_by(self, subject_kind: str, subject_id: str, kind: str) -> list[str]:
        return self._grants(self._authz.used_by, self._ref(subject_kind, subject_id), kind)

    def grantees(self, kind: str, resource_id: str) -> list[ResourceRef]:
        return self._grants(self._authz.grantees, self._ref(kind, resource_id))

    def grant_use(self, subject_kind: str, subject_id: str, kind: str, resource_id: str) -> None:
        self._grants(self._authz.grant_use, UseGrant(self._ref(subject_kind, subject_id), self._ref(kind, resource_id)))

    def revoke_use(self, subject_kind: str, subject_id: str, kind: str, resource_id: str) -> None:
        self._grants(self._authz.revoke_use, UseGrant(self._ref(subject_kind, subject_id), self._ref(kind, resource_id)))

    # -- per-resource grants (the Access tab) ----------------------------------------------------------------------

    @property
    def supports_access_grants(self) -> bool:
        return self._authz.supports_access_grants

    def grantable(self, kind: str) -> list[GrantableRelation]:
        return self._authz.grantable(kind)

    def list_grants(self, kind: str, resource_id: str) -> list[Grant]:
        return self._grants(self._authz.list_grants, self._ref(kind, resource_id))

    def grant_access(self, kind: str, resource_id: str, relation: str, subject_type: str, subject: str) -> None:
        self._grants(self._authz.grant, Grant(subject, relation, self._ref(kind, resource_id), subject_type=subject_type))

    def revoke_access(self, kind: str, resource_id: str, relation: str, subject_type: str, subject: str) -> None:
        self._grants(self._authz.revoke, Grant(subject, relation, self._ref(kind, resource_id), subject_type=subject_type))

    def unregister(self, kind: str, resource_id: str) -> None:
        try:
            run_sync(self._authz.unregister, self._ref(kind, resource_id))
        except PolicyUnavailable as exc:
            log.error("policy unregister failed: %s", exc.detail)
            raise Unavailable(UNAVAILABLE, code="authz_unavailable") from None


class AccessBound:
    """Mixin of a service that carries an optional Access in `_access` (see `bind_access`)."""

    _access: Access | None = None

    def bind_access(self, access: Access | None) -> Any:
        """Attach the caller's access to a service built without one (the MCP control endpoint builds its services in
        one place and binds them there); returns the service."""
        self._access = access
        return self


# -- helpers for services that take an optional Access -----------------------------------------------------------------


def require(access: Access | None, action: str, kind: str, resource_id: str | None) -> None:
    if access is not None:
        access.require(action, kind, resource_id)


def visible(access: Access | None, kind: str, action: str = "read") -> IdFilter:
    return All() if access is None else access.visible(kind, action)


def register(access: Access | None, kind: str, resource_id: str, parent: tuple[str, str] | None = None) -> None:
    if access is not None:
        access.register(kind, resource_id, parent)


def unregister(access: Access | None, kind: str, resource_id: str) -> None:
    if access is not None:
        access.unregister(kind, resource_id)


def keep(flt: IdFilter, ids: Sequence[str]) -> list[str]:
    """`ids` narrowed by an engine answer (for lists that are filtered in Python, not in SQL)."""
    if isinstance(flt, All):
        return list(ids)
    if isinstance(flt, Only):
        allowed = set(flt.ids)
        return [i for i in ids if i in allowed]
    assert isinstance(flt, Except)
    denied = set(flt.ids)
    return [i for i in ids if i not in denied]


def sql_ids(flt: IdFilter) -> tuple[str, ...] | None:
    """The id list a store takes as a restriction: None for no restriction (All). A list that cannot be written as
    "only these ids" (Except) is narrowed to nothing: fail closed. The engines in the tree answer All or Only."""
    if isinstance(flt, All):
        return None
    if isinstance(flt, Only):
        return tuple(flt.ids)
    log.warning("an Except filter cannot restrict a paged list; showing nothing")
    return ()
