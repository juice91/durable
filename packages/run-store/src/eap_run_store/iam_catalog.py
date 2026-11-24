"""Every platform resource with its tenant and creator, for a policy plug-in's reconcile and backfill (IAM design 5.6).

Read only: one SELECT per resource kind, no write. It implements `eap_iam.ResourceCatalog` structurally (this package does
not import the IAM interfaces). The creator of an agent or workflow is the `created_by` of its first version (a draft that
was never published has none); of a secret or an API key, the column on its row; the other kinds record none.

Object ids: agents, workflows, knowledge bases, schedules, MCP servers and API keys by their id; a secret by its name
(that is how every route and spec names it)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine

# `{w}` is the tenant filter (` WHERE <alias>.tenant_id = :t`) or nothing.
_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "agent",
        "SELECT a.tenant_id, a.id::text AS id, (SELECT v.created_by FROM agent_versions v WHERE v.tenant_id = a.tenant_id"
        " AND v.agent_id = a.id ORDER BY v.version ASC LIMIT 1) AS creator FROM agents a{w}",
    ),
    (
        "workflow",
        "SELECT a.tenant_id, a.id::text AS id, (SELECT v.created_by FROM workflow_versions v WHERE v.tenant_id = a.tenant_id"
        " AND v.workflow_id = a.id ORDER BY v.version ASC LIMIT 1) AS creator FROM workflows a{w}",
    ),
    ("knowledge_base", "SELECT a.tenant_id, a.id::text AS id, NULL AS creator FROM knowledge_bases a{w}"),
    ("secret", "SELECT a.tenant_id, a.name AS id, a.created_by AS creator FROM tenant_secrets a{w}"),
    ("schedule", "SELECT a.tenant_id, a.id::text AS id, NULL AS creator FROM workflow_schedules a{w}"),
    ("mcp_server", "SELECT a.tenant_id, a.id::text AS id, NULL AS creator FROM mcp_servers a{w}"),
    ("api_key", "SELECT a.tenant_id, a.id::text AS id, a.created_by AS creator FROM api_keys a{w}"),
)


@dataclass(frozen=True)
class CatalogRow:
    kind: str
    id: str
    tenant_id: str
    creator: str | None = None


class RunStoreCatalog:
    """`entries(tenant_id=None)`: the rows of every kind, archived and revoked ones included (their tuples stay)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def entries(self, tenant_id: str | None = None) -> list[CatalogRow]:
        rows: list[CatalogRow] = []
        with self._engine.connect() as conn:
            for kind, sql in _QUERIES:
                where = " WHERE a.tenant_id = :t" if tenant_id is not None else ""
                params = {"t": tenant_id} if tenant_id is not None else {}
                result = conn.execute(text(sql.format(w=where) + " ORDER BY 1, 2"), params)
                rows += [CatalogRow(kind, r.id, r.tenant_id, r.creator) for r in result]
        return rows
