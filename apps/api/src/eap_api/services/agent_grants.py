"""Agent-bound grants (docs/iam.md, "Agent and workflow grants"): an admin authorises an agent or a workflow to use an
MCP server or a named secret. The grant is a stored tuple in the policy engine; every grant and revoke leaves an audit row.

Two entry points use it:

* the Access section of an agent or workflow (`status`, `grant`, `revoke`; reading is open to whoever may read the
  subject, writing is for admins only, the model's `can_grant_use`);
* the publish of a new version (`plan`, `require_approvable`, `apply`): what the version newly uses is listed on the
  publish card, an admin's approval is the grant, and a person who is not an admin cannot approve such a publish.

With an engine that keeps no grants (`openaccess`) there is nothing to list or write: `plan` is None and `status` says
the section is not available. Whether the worker enforces the grants at run time is a separate switch
(`EAP_AGENT_GRANTS_ENFORCE`, docs/iam.md)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from eap_iam import USE_RESOURCE_KINDS, USE_SUBJECT_KINDS, enforcement_requested
from eap_run_store import AuditEntry, AuditStore
from eap_run_store.secrets import TenantSecretModel
from eap_schemas.permissions import grant_uses
from eap_schemas.secrets import is_secret_name
from sqlalchemy import select
from sqlalchemy.orm import Session

from eap_api.mcp_servers.models import McpServerModel, McpServerSecretModel
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Forbidden, InvalidInput, NotFound, Unavailable

log = logging.getLogger("eap_api.agent_grants")

ACTION_GRANT = "grant_agent_use"
ACTION_REVOKE = "revoke_agent_use"
APPROVAL_REQUIRED = "grant_approval_required"
GRANTS_UNAVAILABLE = "grants_unavailable"


@dataclass(frozen=True)
class PublishGrants:
    """What a version newly uses and no grant covers yet: the servers (id and name) and the secret names."""

    subject_kind: str
    subject_id: str
    mcp_servers: tuple[dict[str, str], ...] = ()
    secrets: tuple[str, ...] = ()
    can_approve: bool = True
    dynamic: bool = False

    def __bool__(self) -> bool:
        return bool(self.mcp_servers or self.secrets)

    def describe(self) -> str:
        parts = [f"MCP server `{m['name']}`" for m in self.mcp_servers] + [f"secret `{n}`" for n in self.secrets]
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "newly_uses": {"mcp_servers": [m["name"] for m in self.mcp_servers], "secrets": list(self.secrets)},
            "can_approve": self.can_approve,
            "dynamic": self.dynamic,
            "message": (
                f"This version newly uses {self.describe()}. Approving grants them."
                if self
                else None
            ),
        }


class AgentGrantService(AccessBound):
    def __init__(self, db: Session, access: Access | None = None, audit: AuditStore | None = None) -> None:
        self.db = db
        self._access = access
        self._audit = audit

    # -- the subject and the resources ----------------------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self._access is not None and self._access.supports_grants

    def _tenant(self) -> str:
        assert self._access is not None
        return self._access.tenant_id

    def _check_subject(self, kind: str, subject_id: str, action: str = "read") -> None:
        if kind not in USE_SUBJECT_KINDS:
            raise InvalidInput(f"grants belong to an agent or a workflow, not a {kind}", code="invalid_subject")
        if self._access is not None:
            self._access.require(action, kind, subject_id)

    def _servers(self) -> dict[str, dict[str, str]]:
        """name -> {id, name} of the tenant's registered MCP servers."""
        rows = self.db.execute(
            select(McpServerModel.id, McpServerModel.name).where(McpServerModel.tenant_id == self._tenant())
        ).all()
        return {r.name: {"id": str(r.id), "name": r.name} for r in rows}

    def _secret_names(self) -> set[str]:
        return set(self.db.scalars(select(TenantSecretModel.name).where(TenantSecretModel.tenant_id == self._tenant())).all())

    def _bound_credentials(self, server_ids: list[str]) -> dict[str, list[str]]:
        """The names of the credentials each server carries (header and env names, never a value)."""
        if not server_ids:
            return {}
        rows = self.db.execute(
            select(McpServerSecretModel.server_id, McpServerSecretModel.header_name).where(
                McpServerSecretModel.tenant_id == self._tenant(), McpServerSecretModel.server_id.in_(server_ids)
            )
        ).all()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(str(r.server_id), []).append(r.header_name)
        return {k: sorted(v) for k, v in out.items()}

    def _resolve(self, kind: str, name: str) -> tuple[str, str]:
        """(object id, display name) of a server (by name or id) or a secret (by name); NotFound when there is none."""
        if kind == "mcp_server":
            servers = self._servers()
            if name in servers:
                return servers[name]["id"], name
            for s in servers.values():
                if s["id"] == name:
                    return s["id"], s["name"]
            raise NotFound("MCP server not found")
        if kind == "secret":
            if not is_secret_name(name) or name not in self._secret_names():
                raise NotFound("secret not found")
            return name, name
        raise InvalidInput("a grant names an MCP server or a secret", code="invalid_resource_kind")

    # -- audit ----------------------------------------------------------------------------------------------------

    def _record(self, action: str, subject_kind: str, subject_id: str, kind: str, name: str, reason: str | None) -> None:
        if self._audit is None or self._access is None:
            return
        principal = self._access.principal
        detail: dict[str, Any] = {"name": f"{kind}:{name}"}
        if reason:
            detail["reason"] = reason
        try:
            self._audit.record(AuditEntry(
                tenant_id=principal.tenant_id, actor_kind=principal.kind, actor_subject=principal.subject,
                on_behalf_of=principal.on_behalf_of, action=action, outcome="success",
                resource_kind=subject_kind, resource_id=subject_id, detail=detail,
            ))
        except Exception:  # noqa: BLE001 - the grant itself is made; the row is best effort, like every audit write
            log.exception("audit_log write failed for %s", action)

    def _granted_when(self, subject_kind: str, subject_id: str) -> dict[str, dict[str, Any]]:
        """`kind:name` -> who granted it last and when, from the audit rows (the tuple itself has no author)."""
        if self._audit is None:
            return {}
        rows, _ = self._audit.list(
            self._tenant(), action=ACTION_GRANT, resource_kind=subject_kind, resource_id=subject_id, limit=200
        )
        out: dict[str, dict[str, Any]] = {}
        for row in rows:  # newest first
            key = (row.get("detail") or {}).get("name")
            if isinstance(key, str) and key not in out:
                out[key] = {"granted_by": row["actor_subject"], "granted_at": row["at"].isoformat()}
        return out

    # -- the Access section ---------------------------------------------------------------------------------------

    def status(self, subject_kind: str, subject_id: str, latest_spec: dict[str, Any] | None = None) -> dict[str, Any]:
        """The grants of an agent or workflow, for whoever may read it. `latest_spec`: the newest published version's spec
        (a grant it no longer uses reads "unused by the latest version")."""
        self._check_subject(subject_kind, subject_id)
        can_manage = self._can_manage()
        base: dict[str, Any] = {
            "subject": {"kind": subject_kind, "id": subject_id},
            "supported": self.supported, "enforced": self.supported and enforcement_requested(),
            "can_manage": can_manage, "grants": [], "options": None,
        }
        if not self.supported:
            return base
        assert self._access is not None
        server_ids = self._access.used_by(subject_kind, subject_id, "mcp_server")
        secret_ids = self._access.used_by(subject_kind, subject_id, "secret")
        servers_by_id = {s["id"]: s["name"] for s in self._servers().values()}
        creds = self._bound_credentials([i for i in server_ids if i in servers_by_id])
        used = grant_uses(latest_spec, subject_kind) if latest_spec is not None else None  # type: ignore[arg-type]
        when = self._granted_when(subject_kind, subject_id)
        grants: list[dict[str, Any]] = []
        for sid in server_ids:
            if sid not in servers_by_id:
                continue  # a server deleted since: its tuples go with it; never show an id with no name
            name = servers_by_id[sid]
            grants.append({
                "kind": "mcp_server", "id": sid, "name": name, "credentials": creds.get(sid, []),
                "used_by_latest": None if used is None else name in used.mcp_servers, **when.get(f"mcp_server:{name}", {}),
            })
        for name in secret_ids:
            grants.append({
                "kind": "secret", "id": name, "name": name, "credentials": [],
                "used_by_latest": None if used is None else name in used.secrets, **when.get(f"secret:{name}", {}),
            })
        base["grants"] = sorted(grants, key=lambda g: (g["kind"], g["name"]))
        if can_manage:
            base["options"] = {
                "mcp_servers": sorted(servers_by_id.values()), "secrets": sorted(self._secret_names()),
            }
        return base

    def _can_manage(self) -> bool:
        return self._access is None or self._access.allowed("manage_settings", "tenant", None)

    def _require_supported(self) -> Access:
        if not self.supported:
            raise Unavailable("the active policy engine keeps no per-agent grants", code=GRANTS_UNAVAILABLE)
        assert self._access is not None
        return self._access

    def grant(self, subject_kind: str, subject_id: str, kind: str, name: str, reason: str | None = None) -> dict[str, Any]:
        """Authorise the agent or workflow to use the server or secret. Admin only; idempotent."""
        access = self._require_supported()
        self._check_subject(subject_kind, subject_id)
        resource_id, display = self._resolve(kind, name)
        access.require("grant_use", kind, resource_id)  # NotFound / Forbidden: only an admin grants
        access.grant_use(subject_kind, subject_id, kind, resource_id)
        self._record(ACTION_GRANT, subject_kind, subject_id, kind, display, reason)
        return {"kind": kind, "id": resource_id, "name": display}

    def revoke(self, subject_kind: str, subject_id: str, kind: str, name: str, reason: str | None = None) -> None:
        access = self._require_supported()
        self._check_subject(subject_kind, subject_id)
        resource_id, display = self._resolve(kind, name)
        access.require("grant_use", kind, resource_id)
        access.revoke_use(subject_kind, subject_id, kind, resource_id)
        self._record(ACTION_REVOKE, subject_kind, subject_id, kind, display, reason)

    # -- publish --------------------------------------------------------------------------------------------------

    def plan(self, subject_kind: str, subject_id: str, spec: dict[str, Any] | None) -> PublishGrants | None:
        """What `spec` newly uses (no grant yet), or None when the engine keeps no grants or there is no caller to check."""
        if not self.supported or self._access is None or spec is None:
            return None
        uses = grant_uses(spec, subject_kind)  # type: ignore[arg-type]
        if not uses:
            return PublishGrants(subject_kind, subject_id, dynamic=uses.dynamic)
        servers = self._servers()
        granted_servers = set(self._access.used_by(subject_kind, subject_id, "mcp_server")) if uses.mcp_servers else set()
        granted_secrets = set(self._access.used_by(subject_kind, subject_id, "secret")) if uses.secrets else set()
        new_servers = tuple(
            servers[n] for n in uses.mcp_servers if n in servers and servers[n]["id"] not in granted_servers
        )
        known_secrets = self._secret_names() if uses.secrets else set()
        new_secrets = tuple(n for n in uses.secrets if n in known_secrets and n not in granted_secrets)
        can = all(self._access.allowed("grant_use", "mcp_server", s["id"]) for s in new_servers) and all(
            self._access.allowed("grant_use", "secret", n) for n in new_secrets
        )
        return PublishGrants(subject_kind, subject_id, new_servers, new_secrets, can_approve=can, dynamic=uses.dynamic)

    def require_approvable(self, plan: PublishGrants | None) -> None:
        """A publish that newly uses an ungranted server or secret can be approved by an admin only."""
        if plan and not plan.can_approve:
            raise Forbidden(
                f"This version newly uses {plan.describe()}. Approving the publish grants them, so an admin must approve it.",
                code=APPROVAL_REQUIRED,
            )

    def apply(self, plan: PublishGrants | None, reason: str) -> None:
        """Write the grants of an approved publish (the caller checked `require_approvable`)."""
        if not plan or self._access is None:
            return
        for server in plan.mcp_servers:
            self._access.grant_use(plan.subject_kind, plan.subject_id, "mcp_server", server["id"])
            self._record(ACTION_GRANT, plan.subject_kind, plan.subject_id, "mcp_server", server["name"], reason)
        for name in plan.secrets:
            self._access.grant_use(plan.subject_kind, plan.subject_id, "secret", name)
            self._record(ACTION_GRANT, plan.subject_kind, plan.subject_id, "secret", name, reason)

    def undo(self, plan: PublishGrants | None) -> None:
        """Take back what `apply` wrote (the approval it belonged to was not recorded). Best effort: a failure is logged."""
        if not plan or self._access is None:
            return
        try:
            for server in plan.mcp_servers:
                self._access.revoke_use(plan.subject_kind, plan.subject_id, "mcp_server", server["id"])
                self._record(ACTION_REVOKE, plan.subject_kind, plan.subject_id, "mcp_server", server["name"], "approval not recorded")
            for name in plan.secrets:
                self._access.revoke_use(plan.subject_kind, plan.subject_id, "secret", name)
                self._record(ACTION_REVOKE, plan.subject_kind, plan.subject_id, "secret", name, "approval not recorded")
        except Exception:  # noqa: BLE001
            log.exception("could not take back grants of an approval that was not recorded")
