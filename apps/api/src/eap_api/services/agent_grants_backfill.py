"""The one-off migration to agent-bound grants (docs/iam.md, "Agent and workflow grants", operator steps).

Until now every member could name any `secret:<name>` and every agent could call any registered MCP server; no grant exists.
This reads what the PUBLISHED versions of every agent and workflow use today (all versions: a pinned schedule may run an old
one) with the same extractor the publish check uses, and reports per agent and per workflow what it would be granted. The latest
draft is listed too, marked as a draft, and never granted (a draft is not live; an admin's publish approval grants it).
`apply_report` writes the grants (audit `granted_by = backfill:<operator>`), re-reads them and diffs; running it twice is a no-op.

Names that resolve to nothing (a server or secret that is not registered) are reported as dangling and not granted. A spec that
computes a reference at run time is granted only its statically visible part and is flagged `dynamic`."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from eap_iam import ResourceRef, UseGrant
from eap_run_store import AuditEntry, AuditStore
from eap_schemas.permissions import ImpliedReport, implied_permissions
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_api.services.authz import AuthzService

ACTION_GRANT = "grant_agent_use"
BACKFILL_REASON = "backfill"

_AGENTS = text("SELECT id, name, archived, builtin, draft_spec FROM agents WHERE tenant_id = :t ORDER BY name, id")
_AGENT_VERSIONS = text("SELECT agent_id AS owner, version, spec FROM agent_versions WHERE tenant_id = :t ORDER BY agent_id, version")
_WORKFLOWS = text("SELECT id, name, archived, draft_spec FROM workflows WHERE tenant_id = :t ORDER BY name, id")
_WORKFLOW_VERSIONS = text("SELECT workflow_id AS owner, version, spec FROM workflow_versions WHERE tenant_id = :t ORDER BY workflow_id, version")
_SERVERS = text("SELECT id, name FROM mcp_servers WHERE tenant_id = :t")
_SECRETS = text("SELECT name FROM tenant_secrets WHERE tenant_id = :t")


def _uses(report: ImpliedReport) -> tuple[list[str], list[str]]:
    return sorted({m.server for m in report.permissions.mcp}), list(report.permissions.secrets)


def _collect(
    kind: str, spec_rows: Iterable[Any], draft: Any
) -> tuple[dict[tuple[str, str], list[int]], set[tuple[str, str]], bool, list[str], list[dict[str, str]]]:
    """(published uses -> versions, draft-only uses, dynamic, dynamic paths, agent steps) of one definition's specs."""
    published: dict[tuple[str, str], list[int]] = {}
    dynamic: list[str] = []
    agents: dict[str, None] = {}
    for row in spec_rows:
        spec = row.spec if isinstance(row.spec, dict) else {}
        report = implied_permissions(spec, kind)  # type: ignore[arg-type]
        servers, secrets = _uses(report)
        for name in servers:
            published.setdefault(("mcp_server", name), []).append(int(row.version))
        for name in secrets:
            published.setdefault(("secret", name), []).append(int(row.version))
        dynamic += [p for p in report.dynamic_paths if p not in dynamic]
        for a in report.permissions.agents:
            agents.setdefault(a.agent_id)
    draft_only: set[tuple[str, str]] = set()
    if isinstance(draft, dict):
        report = implied_permissions(draft, kind)  # type: ignore[arg-type]
        servers, secrets = _uses(report)
        found = {("mcp_server", n) for n in servers} | {("secret", n) for n in secrets}
        draft_only = found - set(published)
        dynamic += [p for p in report.dynamic_paths if p not in dynamic]
    return published, draft_only, bool(dynamic), dynamic, [{"id": a} for a in agents]


async def build_report(db: Engine, authz: AuthzService, tenant_id: str) -> dict[str, Any]:
    """The dry run: what each agent and workflow of the tenant would be granted, and what is already granted."""
    with db.connect() as conn:
        params = {"t": tenant_id}
        agents = conn.execute(_AGENTS, params).all()
        workflows = conn.execute(_WORKFLOWS, params).all()
        versions: dict[tuple[str, str], list[Any]] = {}
        for kind, query in (("agent", _AGENT_VERSIONS), ("workflow", _WORKFLOW_VERSIONS)):
            for row in conn.execute(query, params):
                versions.setdefault((kind, str(row.owner)), []).append(row)
        servers = {r.name: str(r.id) for r in conn.execute(_SERVERS, params)}
        secrets = {r.name for r in conn.execute(_SECRETS, params)}
    agent_names = {str(a.id): a.name for a in agents}

    items: list[dict[str, Any]] = []
    used_servers: set[str] = set()
    used_secrets: set[str] = set()
    for kind, rows in (("agent", agents), ("workflow", workflows)):
        for row in rows:
            subject_id = str(row.id)
            published, draft_only, dynamic, dynamic_paths, steps = _collect(kind, versions.get((kind, subject_id), []), row.draft_spec)
            granted: dict[str, set[str]] = {"mcp_server": set(), "secret": set()}
            if authz.supports_grants:
                subject = ResourceRef(kind, subject_id, tenant_id)
                for resource_kind in granted:
                    granted[resource_kind] = set(await authz.used_by(subject, resource_kind))
            grants, dangling, draft = [], [], []
            for (resource_kind, name), vs in sorted(published.items()):
                exists = name in servers if resource_kind == "mcp_server" else name in secrets
                (used_servers if resource_kind == "mcp_server" else used_secrets).add(name)
                if not exists:
                    dangling.append({"kind": resource_kind, "name": name, "versions": vs})
                    continue
                resource_id = servers[name] if resource_kind == "mcp_server" else name
                grants.append({
                    "kind": resource_kind, "id": resource_id, "name": name, "versions": vs, "source": "published",
                    "status": "granted" if resource_id in granted[resource_kind] else "to_grant",
                })
            for resource_kind, name in sorted(draft_only):
                exists = name in servers if resource_kind == "mcp_server" else name in secrets
                (used_servers if resource_kind == "mcp_server" else used_secrets).add(name)
                draft.append({"kind": resource_kind, "name": name, "source": "draft", "exists": exists})
            items.append({
                "kind": kind, "id": subject_id, "name": row.name, "archived": bool(row.archived),
                "builtin": bool(getattr(row, "builtin", False)),
                "published_versions": [int(v.version) for v in versions.get((kind, subject_id), [])],
                "grants": grants, "draft_only": draft, "dangling": dangling,
                "dynamic": dynamic, "dynamic_paths": dynamic_paths,
                "agents": [{"id": s["id"], "name": agent_names.get(s["id"])} for s in steps] if kind == "workflow" else [],
            })
    nothing = [f"{i['kind']} {i['name']}" for i in items if not i["grants"]]
    return {
        "tenant": tenant_id, "supported": authz.supports_grants, "items": items,
        "unused": {"mcp_servers": sorted(set(servers) - used_servers), "secrets": sorted(secrets - used_secrets)},
        "summary": {
            "agents": len(agents), "workflows": len(workflows),
            "to_grant": sum(1 for i in items for g in i["grants"] if g["status"] == "to_grant"),
            "already_granted": sum(1 for i in items for g in i["grants"] if g["status"] == "granted"),
            "dangling": sum(len(i["dangling"]) for i in items), "dynamic": sum(1 for i in items if i["dynamic"]),
            "draft_only": sum(len(i["draft_only"]) for i in items),
            "nothing_to_grant": nothing,
        },
    }


async def apply_report(
    db: Engine, authz: AuthzService, tenant_id: str, report: dict[str, Any], operator: str, audit: AuditStore | None = None
) -> dict[str, Any]:
    """Write every `to_grant` entry of the report (published uses only), then re-read the engine and diff. Idempotent."""
    if not authz.supports_grants:
        raise RuntimeError("the selected policy engine keeps no agent grants; nothing can be applied")
    written = 0
    for item in report["items"]:
        if item["archived"]:
            continue  # an archived definition runs nowhere; unarchiving and publishing grants what it needs
        subject = ResourceRef(item["kind"], item["id"], tenant_id)
        for g in item["grants"]:
            if g["status"] != "to_grant":
                continue
            await authz.grant_use(UseGrant(subject, ResourceRef(g["kind"], g["id"], tenant_id)))
            written += 1
            if audit is not None:
                audit.record(AuditEntry(
                    tenant_id=tenant_id, actor_kind="user", actor_subject=f"backfill:{operator}", action=ACTION_GRANT,
                    outcome="success", resource_kind=item["kind"], resource_id=item["id"],
                    detail={"name": f"{g['kind']}:{g['name']}", "reason": BACKFILL_REASON},
                ))
    after = await build_report(db, authz, tenant_id)
    missing = [
        {"subject": f"{i['kind']} {i['name']}", "kind": g["kind"], "name": g["name"]}
        for i in after["items"] if not i["archived"] for g in i["grants"] if g["status"] == "to_grant"
    ]
    return {"written": written, "verified": not missing, "missing": missing, "report": after}


# -- rendering --------------------------------------------------------------------------------------------------------------


def render_table(report: dict[str, Any], *, applied: dict[str, Any] | None = None) -> str:
    """A readable report: one block per agent and workflow, then the dangling, dynamic and unused lists."""
    out: list[str] = []
    s = report["summary"]
    out.append(f"Agent-bound grants for tenant {report['tenant']}: " + ("APPLIED" if applied else "dry run, nothing written"))
    if not report["supported"]:
        out.append("(the selected policy engine keeps no grants: every grant below reads as to_grant)")
    out.append("")
    for kind, title in (("agent", "AGENTS"), ("workflow", "WORKFLOWS")):
        out.append(title)
        rows = [i for i in report["items"] if i["kind"] == kind]
        if not rows:
            out.append("  (none)")
        for i in rows:
            tags = [t for t, on in (("archived", i["archived"]), ("built-in", i["builtin"]), ("dynamic refs", i["dynamic"])) if on]
            versions = ",".join(f"v{v}" for v in i["published_versions"]) or "no published version"
            out.append(f"  {i['name']}  [{i['id']}]  {versions}" + (f"  ({', '.join(tags)})" if tags else ""))
            for g in i["grants"]:
                what = "MCP server" if g["kind"] == "mcp_server" else "secret"
                vs = ",".join(f"v{v}" for v in g["versions"])
                out.append(f"      {g['status']:<9} {what:<10} {g['name']}  (used by {vs})")
            for d in i["draft_only"]:
                what = "MCP server" if d["kind"] == "mcp_server" else "secret"
                out.append(f"      draft     {what:<10} {d['name']}  (only in the latest draft: granted when an admin publishes it)")
            for d in i["dangling"]:
                out.append(f"      DANGLING  {d['kind']:<10} {d['name']}  (not registered: reported, not granted)")
            for a in i["agents"]:
                out.append(f"      agent step -> {a['name'] or a['id']} (the agent's own grants apply)")
            if not i["grants"] and not i["draft_only"] and not i["dangling"]:
                out.append("      nothing to grant")
    out.append("")
    out.append(
        f"summary: {s['agents']} agents, {s['workflows']} workflows; {s['to_grant']} to grant, {s['already_granted']} already granted, "
        f"{s['dangling']} dangling, {s['dynamic']} with dynamic refs, {s['draft_only']} draft-only"
    )
    if report["unused"]["mcp_servers"] or report["unused"]["secrets"]:
        out.append(
            "unused (named by no spec; a managed server's env references are not counted): "
            f"servers {report['unused']['mcp_servers'] or '-'}, secrets {report['unused']['secrets'] or '-'}"
        )
    if applied:
        out.append(f"applied: {applied['written']} grants written; re-read and diffed: " + ("OK" if applied["verified"] else f"MISSING {applied['missing']}"))
    return "\n".join(out) + "\n"
