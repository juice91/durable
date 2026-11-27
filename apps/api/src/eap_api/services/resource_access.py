"""The Access tab of a resource (design 7.3, docs/iam.md "The Access tab and explain"): who holds which relation on an
agent, workflow, knowledge base, secret or MCP server, and an admin's changes to that.

Whoever may read the resource sees the list; only an admin (a person with the tenant's `manage_settings`) grants or
revokes. A grant names a member of the tenant (checked against the membership source) or a tenant role. Every change
leaves an audit row. With an engine that keeps no grants (`openaccess`) there is nothing to list and a write is 503."""

from __future__ import annotations

import logging
from typing import Any

from eap_iam import GRANT_SUBJECT_ROLE, GRANT_SUBJECT_USER, USE_RESOURCE_KINDS, USE_SUBJECT_KINDS, MembershipError, MembershipSource
from eap_run_store import AuditEntry, AuditStore
from eap_run_store.iam_catalog import RunStoreCatalog

from eap_api.services.access import NOT_FOUND, Access, AccessBound, keep, run_sync
from eap_api.services.errors import Forbidden, InvalidInput, NotFound, Unavailable

log = logging.getLogger("eap_api.resource_access")

# The kinds with an Access tab in Studio.
ACCESS_KINDS: tuple[str, ...] = ("agent", "workflow", "knowledge_base", "secret", "mcp_server")
ACTION_GRANT = "grant_access"
ACTION_REVOKE = "revoke_access"


class ResourceAccessService(AccessBound):
    def __init__(
        self, catalog: RunStoreCatalog, access: Access, membership: MembershipSource | None, audit: AuditStore | None = None
    ) -> None:
        self._catalog = catalog
        self._access = access
        self._membership = membership
        self._audit = audit

    def _readable(self, kind: str, resource_id: str) -> None:
        """NotFound unless the resource exists in the tenant and the caller may read it (one answer for both)."""
        if kind not in ACCESS_KINDS:
            raise InvalidInput(f"access is managed on: {', '.join(ACCESS_KINDS)}", code="invalid_resource_kind")
        self._access.require("read", kind, resource_id)
        if not self._catalog.exists(self._access.tenant_id, kind, resource_id):
            raise NotFound(NOT_FOUND[kind])

    def _is_admin(self) -> bool:
        return self._access.principal.kind == "user" and self._access.allowed("manage_settings", "tenant", None)

    def status(self, kind: str, resource_id: str) -> dict[str, Any]:
        self._readable(kind, resource_id)
        supported = self._access.supports_access_grants
        body: dict[str, Any] = {
            "resource": {"kind": kind, "id": resource_id}, "supported": supported, "can_manage": False,
            "relations": [], "grants": [], "used_by": [],
        }
        if not supported:
            return body
        body["can_manage"] = self._is_admin()
        body["relations"] = [{"relation": g.relation, "roles": list(g.roles)} for g in self._access.grantable(kind)]
        body["grants"] = [
            {"relation": g.relation, "subject_type": g.subject_type, "subject": g.subject}
            for g in self._access.list_grants(kind, resource_id)
        ]
        body["used_by"] = self._used_by(kind, resource_id)
        return body

    def _used_by(self, kind: str, resource_id: str) -> list[dict[str, str]]:
        """The agents and workflows granted this server or secret, as far as the caller may read them."""
        if kind not in USE_RESOURCE_KINDS or not self._access.supports_grants:
            return []
        grantees = self._access.grantees(kind, resource_id)
        out: list[dict[str, str]] = []
        for subject_kind in USE_SUBJECT_KINDS:
            ids = [str(g.id) for g in grantees if g.kind == subject_kind]
            if ids:
                out += [{"kind": subject_kind, "id": i} for i in keep(self._access.visible(subject_kind), ids)]
        return out

    # -- changes -----------------------------------------------------------------------------------------------------

    def _require_change(self, kind: str, resource_id: str) -> None:
        self._readable(kind, resource_id)
        if not self._access.supports_access_grants:
            raise Unavailable("the active policy engine keeps no per-resource grants", code="grants_unavailable")
        self._access.require("manage_settings", "tenant", None)
        if self._access.principal.kind != "user":
            raise Forbidden("only a person may change access", code="forbidden")

    def _member_subject(self, member_id: str) -> str:
        """The principal subject of a member of this tenant (`<source>:<member id>`)."""
        source = self._membership
        if source is None or not getattr(source, "configured", True):
            raise InvalidInput("there is no membership source to name a member from", code="no_membership")
        try:
            members = run_sync(source.list_members, self._access.tenant_id)
        except MembershipError as exc:
            raise Unavailable(exc.detail, code="membership_unavailable") from None
        if not any(m.id == member_id for m in members):
            raise InvalidInput("that is not a member of this tenant", code="unknown_member")
        return f"{source.name}:{member_id}"

    def grant(self, kind: str, resource_id: str, relation: str, member_id: str | None, role: str | None) -> dict[str, str]:
        """Give a member or a role `relation` on the resource. Admin only; idempotent."""
        self._require_change(kind, resource_id)
        if (member_id is None) == (role is None):
            raise InvalidInput("name a member or a role (one of the two)", code="invalid_subject")
        if role is not None:
            subject_type, subject = GRANT_SUBJECT_ROLE, role
        else:
            subject_type, subject = GRANT_SUBJECT_USER, self._member_subject(str(member_id))
        self._access.grant_access(kind, resource_id, relation, subject_type, subject)
        self._record(ACTION_GRANT, kind, resource_id, relation, subject_type, subject)
        return {"relation": relation, "subject_type": subject_type, "subject": subject}

    def revoke(self, kind: str, resource_id: str, relation: str, subject_type: str, subject: str) -> None:
        """Take a grant back, as the list names it (a subject who has left the tenant can still be removed)."""
        self._require_change(kind, resource_id)
        self._access.revoke_access(kind, resource_id, relation, subject_type, subject)
        self._record(ACTION_REVOKE, kind, resource_id, relation, subject_type, subject)

    def _record(self, action: str, kind: str, resource_id: str, relation: str, subject_type: str, subject: str) -> None:
        if self._audit is None:
            return
        principal = self._access.principal
        try:
            self._audit.record(AuditEntry(
                tenant_id=principal.tenant_id, actor_kind=principal.kind, actor_subject=principal.subject,
                on_behalf_of=principal.on_behalf_of, action=action, outcome="success",
                resource_kind=kind, resource_id=resource_id, detail={"name": f"{relation} {subject_type}:{subject}"},
            ))
        except Exception:  # noqa: BLE001 - the change itself is made; the row is best effort, like every audit write
            log.exception("audit_log write failed for %s", action)
