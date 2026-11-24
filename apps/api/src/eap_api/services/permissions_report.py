"""The implied-permissions report (design 3.4): for every published agent and workflow, the `permissions` block its
references imply and the places that are computed at run time. READ ONLY: nothing is stored, granted or enforced; the
report exists so the later migration (P2/P3) holds no surprise. Admin only (`tenant.read_permissions`)."""

from __future__ import annotations

from typing import Any

from eap_iam import Principal, ResourceRef
from eap_schemas.permissions import implied_permissions
from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.services.authz import AuthzService

ACTION_READ_PERMISSIONS = "read_permissions"
MAX_ITEMS = 500

_AGENTS = text(
    "SELECT DISTINCT ON (a.id) a.id AS id, a.name AS name, v.version AS version, v.spec AS spec"
    " FROM agents a JOIN agent_versions v ON v.agent_id = a.id AND v.tenant_id = a.tenant_id"
    " WHERE a.tenant_id = :t AND NOT COALESCE(a.archived, false) ORDER BY a.id, v.version DESC LIMIT :n"
)
_WORKFLOWS = text(
    "SELECT DISTINCT ON (w.id) w.id AS id, w.name AS name, v.version AS version, v.spec AS spec"
    " FROM workflows w JOIN workflow_versions v ON v.workflow_id = w.id AND v.tenant_id = w.tenant_id"
    " WHERE w.tenant_id = :t AND NOT COALESCE(w.archived, false) ORDER BY w.id, v.version DESC LIMIT :n"
)


class PermissionsReportService:
    def __init__(self, db: Session, authz: AuthzService) -> None:
        self._db = db
        self._authz = authz

    async def report(self, principal: Principal) -> dict[str, Any]:
        await self._authz.require(principal, ACTION_READ_PERMISSIONS, ResourceRef("tenant", None, principal.tenant_id))
        items: list[dict[str, Any]] = []
        for kind, query in (("agent", _AGENTS), ("workflow", _WORKFLOWS)):
            for row in self._db.execute(query, {"t": principal.tenant_id, "n": MAX_ITEMS + 1}).mappings():
                spec = row["spec"] if isinstance(row["spec"], dict) else {}
                implied = implied_permissions(spec, kind)
                items.append(
                    {
                        "kind": kind, "id": str(row["id"]), "name": row["name"], "version": row["version"],
                        "declared": spec.get("permissions") is not None,
                        "implied": implied.permissions.model_dump(mode="json"),
                        "dynamic": implied.dynamic, "dynamic_paths": list(implied.dynamic_paths),
                    }
                )
        items.sort(key=lambda i: (i["kind"], i["name"] or "", i["id"]))
        truncated = len(items) > MAX_ITEMS
        return {
            "items": items[:MAX_ITEMS],
            "truncated": truncated,
            "summary": {
                "total": min(len(items), MAX_ITEMS),
                "dynamic": sum(1 for i in items[:MAX_ITEMS] if i["dynamic"]),
                "declared": sum(1 for i in items[:MAX_ITEMS] if i["declared"]),
            },
            "enforced": False,
        }
