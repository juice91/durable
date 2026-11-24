"""Reading the audit log (design 9): the tenant's rows, filtered, newest first, with a cursor.

Admin only (except `actor=me`, the caller's own rows): the policy engine decides `tenant.read_audit` (the transitional `openaccess` engine allows every user).
Reading the log is itself recorded (`audit.read`, no filters in the row: an admin looking is the event)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from eap_iam import Principal, ResourceRef
from eap_run_store import AuditEntry, AuditStore, InvalidRunCursor, RunCursor
from eap_run_store.audit import DEFAULT_RETENTION_DAYS, MAX_LIMIT, retention_days_from_env  # noqa: F401 (the route's bounds)

from eap_api.services.authz import AuthzService
from eap_api.services.errors import InvalidInput

ACTION_READ_AUDIT = "read_audit"
ACTOR_ME = "me"  # `actor=me`: the caller's own rows (any signed-in person, not only an admin)


class AuditService:
    def __init__(self, store: AuditStore, authz: AuthzService) -> None:
        self._store = store
        self._authz = authz

    @property
    def retention_days(self) -> int | None:
        try:
            return retention_days_from_env(os.environ)
        except ValueError:
            return None

    async def list(
        self,
        principal: Principal,
        *,
        actor: str | None,
        action: str | None,
        resource_kind: str | None,
        resource_id: str | None,
        outcome: str | None,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        own = actor == ACTOR_ME
        if own:
            # A person's own activity is theirs to read whatever their role: pinned to the caller's subject (the
            # filter also matches rows made on their behalf), still tenant-scoped, and not itself recorded.
            actor = principal.subject
        else:
            await self._authz.require(principal, ACTION_READ_AUDIT, ResourceRef("tenant", None, principal.tenant_id))
        try:
            position = RunCursor.decode(cursor) if cursor else None
        except InvalidRunCursor as exc:
            raise InvalidInput(str(exc), code="invalid_cursor") from None
        items, next_cursor = self._store.list(
            principal.tenant_id, actor=actor, action=action, resource_kind=resource_kind, resource_id=resource_id,
            outcome=outcome, since=since, until=until, limit=limit, cursor=position,
        )
        if not own:
            self._record_read(principal, len(items))
        return {"items": [{**i, "at": i["at"].isoformat()} for i in items],
                "next_cursor": next_cursor.encode() if next_cursor else None}

    def _record_read(self, principal: Principal, count: int) -> None:
        self._store.record(
            AuditEntry(
                tenant_id=principal.tenant_id, actor_kind=principal.kind, actor_subject=principal.subject,
                on_behalf_of=principal.on_behalf_of, action="audit.read", outcome="success",
                resource_kind="audit", detail={"count": count},
            )
        )
