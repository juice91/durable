"""Read-only views of a tenant's platform state, shared by every surface that answers "what is there?".

One implementation: the Architect's `platform.*` tools (eap_worker.platform_read_tools) call these; the
source and sync shapes are the very ones the API and the MCP control surface return (`source_view`,
`sync_view`). Every function takes a database engine and the tenant id, reads (never writes), and returns
plain JSON-able dicts that are bounded: lists at most `MAX_LIMIT` rows, strings at most `MAX_TEXT` characters.

What is never returned: secret values or credential references (a source's config loses any key that looks
like a credential), run input or output (only names, status and the error's first line), spec instructions.
Rows of another tenant are never visible: every query is `tenant_id = :t`.

All functions are blocking (SQL); call them from a thread.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_run_store.kb_source_launch import source_view
from eap_run_store.kb_sources import SourceStore, Sync, SyncStore
from eap_run_store.knowledge import KnowledgeStore

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
MAX_TEXT = 300
RUN_KINDS = ("agent", "workflow", "tool", "kb_source_sync", "kb_ingest")
_SECRETISH = re.compile(r"(secret|token|password|passwd|api[_-]?key|credential|authorization|private[_-]?key|bearer)", re.I)


def clamp_limit(limit: Any, default: int = DEFAULT_LIMIT) -> int:
    """`limit` as an int in 1..MAX_LIMIT (the default when it is not a number)."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        return default
    return max(1, min(limit, MAX_LIMIT))


def clip_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    """A string cut to `limit` characters (with an ellipsis); None stays None; other values are stringified."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + "…"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_config(value: Any, depth: int = 0) -> Any:
    """A config with credential-looking keys dropped, strings clipped and the nesting bounded."""
    if isinstance(value, dict):
        if depth >= 4:
            return "…"
        return {str(k): safe_config(v, depth + 1) for k, v in list(value.items())[:40] if not _SECRETISH.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [safe_config(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return clip_text(value, 200)
    return value


def sync_view(s: Sync) -> dict[str, Any]:
    """A `kb_source_syncs` row as the API and `kb.source.syncs` return it."""
    return {
        "run_id": s.run_id, "source_id": s.source_id, "parent_run_id": s.parent_run_id, "mode": s.mode,
        "status": s.status, "counters": s.counters, "errors": s.bounded_errors,
        "started_at": s.started_at.isoformat(),
        "completed_at": s.completed_at.isoformat() if s.completed_at is not None else None,
    }


# --------------------------------------------------------------------------------------- knowledge bases


def _kb_summary(kb: dict[str, Any], sources: int) -> dict[str, Any]:
    return {
        "id": kb["id"], "name": clip_text(kb["name"]), "documents": kb.get("document_count", 0),
        "chunks": kb.get("chunk_count", 0), "embedding_model": kb["embedding_model"], "sources": sources,
        "status": kb["status"],
    }


_SOURCE_COUNTS = text(
    "SELECT knowledge_base_id AS kb, count(*) AS n FROM kb_sources WHERE tenant_id = :t GROUP BY knowledge_base_id"
)


def list_knowledge_bases(engine: Engine, tenant_id: str, limit: int = MAX_LIMIT) -> dict[str, Any]:
    """The tenant's knowledge bases: name, id, documents, chunks, embedding model, number of sources."""
    kbs = KnowledgeStore(engine).list_knowledge_bases(tenant_id)
    with engine.connect() as conn:
        counts = {str(r.kb): r.n for r in conn.execute(_SOURCE_COUNTS, {"t": tenant_id})}
    limit = clamp_limit(limit, MAX_LIMIT)
    return {"items": [_kb_summary(kb, counts.get(kb["id"], 0)) for kb in kbs[:limit]], "total": len(kbs)}


_RECORD_COUNTS = text(
    "SELECT count(*) FROM kb_source_records WHERE tenant_id = :t AND source_id = :s AND status = 'active'"
)
_ENTITY_DOCS = text(
    "SELECT count(*) FROM kb_entity_documents d JOIN kb_entities e ON e.tenant_id = d.tenant_id AND e.id = d.entity_id "
    "WHERE d.tenant_id = :t AND e.source_id = :s AND d.status <> 'removed'"
)


def _source_stats(conn: Any, tenant_id: str, source_id: str) -> tuple[int, int]:
    """(records, documents) of a source: its active records and the documents its entities carry."""
    params = {"t": tenant_id, "s": source_id}
    return int(conn.execute(_RECORD_COUNTS, params).scalar() or 0), int(conn.execute(_ENTITY_DOCS, params).scalar() or 0)


def get_knowledge_base(engine: Engine, tenant_id: str, kb_id: str) -> dict[str, Any] | None:
    """One knowledge base with its sources (name, provider, kind, non-secret config, last sync status and time,
    records, documents). None when the id is not this tenant's."""
    store = KnowledgeStore(engine)
    kb = store.get_knowledge_base(tenant_id, kb_id)
    if kb is None:
        return None
    sources = SourceStore(engine).list_for_kb(tenant_id, kb["id"])
    syncs = SyncStore(engine)
    out_sources = []
    with engine.connect() as conn:
        for s in sources[:MAX_LIMIT]:
            view = source_view(s)
            last = syncs.list_for_source(tenant_id, s.id, 1)
            records, documents = _source_stats(conn, tenant_id, s.id)
            out_sources.append({
                "id": view["id"], "name": clip_text(view["name"]), "provider": view["provider"], "kind": view["kind"],
                "enabled": view["enabled"], "config": safe_config(view["config"]),
                "last_successful_sync_at": view["last_successful_sync_at"],
                "last_sync_status": last[0].status if last else None,
                "last_sync_at": last[0].started_at.isoformat() if last else None,
                "last_sync_mode": last[0].mode if last else None,
                "records": records, "documents": documents,
            })
    return {**_kb_summary(kb, len(sources)), "description": clip_text(kb.get("description")), "sources_list": out_sources}


# ----------------------------------------------------------------------------------------------- syncs


def _report(run: dict[str, Any] | None) -> dict[str, Any]:
    """The bounded report a finished sync run stored as its output (budget, stop reason, diagnostics), or {}."""
    if run is None or not run.get("output") or run.get("status") not in ("completed", "failed", "needs_attention"):
        return {}
    try:
        report = json.loads(run["output"])
    except (TypeError, ValueError):
        return {}
    return report if isinstance(report, dict) else {}


def list_source_syncs(engine: Engine, tenant_id: str, source_id: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any] | None:
    """The last `limit` syncs of a source: mode, status, discovered/inserted/updated/failed, budget used, errors
    count, whether diagnostics exist. None when the source is not this tenant's."""
    from eap_run_store import RunStore

    source = SourceStore(engine).get(tenant_id, source_id) if _is_uuid(source_id) else None
    if source is None:
        return None
    runs = RunStore(engine)
    items = []
    for sync in SyncStore(engine).list_for_source(tenant_id, source.id, clamp_limit(limit)):
        view = sync_view(sync)
        c = view["counters"] or {}
        run = runs.get_run(tenant_id, _uuid(sync.run_id))
        report = _report(run)
        budget = report.get("budget")
        items.append({
            "run_id": view["run_id"], "mode": view["mode"], "status": view["status"],
            "started_at": view["started_at"], "completed_at": view["completed_at"],
            "discovered": c.get("discovered", report.get("discovered")),
            "inserted": c.get("inserted", report.get("inserted")),
            "updated": c.get("updated", report.get("updated")),
            "failed": c.get("failed", report.get("failed")),
            "calls_used": report.get("calls_used"),
            "budget_used": safe_config(budget) if isinstance(budget, dict) else budget,
            "stop_reason": report.get("stop_reason"),
            "errors_count": len(view["errors"] or []),
            "has_diagnostics": bool(report.get("diagnostics")),
        })
    return {"source_id": source.id, "source_name": clip_text(source.name), "items": items}


# --------------------------------------------------------------------------------- agents and workflows


def _is_uuid(value: Any) -> bool:
    return _uuid(value) is not None


def _uuid(value: Any) -> Any:
    import uuid

    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


_LATEST = (
    "SELECT DISTINCT ON ({fk}) {fk} AS d, version, spec, created_via FROM {versions} WHERE tenant_id = :t ORDER BY {fk}, version DESC"
)
_WORKFLOWS = text(
    "SELECT w.id, w.slug, w.name, w.archived, w.draft_spec, v.version AS published_version, v.spec AS published_spec, "
    "v.created_via FROM workflows w LEFT JOIN (" + _LATEST.format(fk="workflow_id", versions="workflow_versions")
    + ") v ON v.d = w.id WHERE w.tenant_id = :t AND (:all OR NOT w.archived) ORDER BY w.archived, w.name, w.id"
)
_AGENTS = text(
    "SELECT a.id, a.slug, a.name, a.archived, a.builtin, a.draft_spec, v.version AS published_version, "
    "v.spec AS published_spec FROM agents a LEFT JOIN (" + _LATEST.format(fk="agent_id", versions="agent_versions")
    + ") v ON v.d = a.id WHERE a.tenant_id = :t AND (:all OR NOT a.archived) ORDER BY a.archived, a.name, a.id"
)
_WORKFLOW_ONE = text(
    "SELECT w.id, w.slug, w.name, w.archived, w.draft_spec, v.version AS published_version, v.spec AS published_spec, "
    "v.created_via FROM workflows w LEFT JOIN (" + _LATEST.format(fk="workflow_id", versions="workflow_versions")
    + ") v ON v.d = w.id WHERE w.tenant_id = :t AND (CAST(w.id AS text) = :ref OR w.slug = :ref)"
)


def _model_of(spec: Any) -> str | None:
    model = spec.get("model") if isinstance(spec, dict) else None
    if isinstance(model, dict):
        model = f"{model.get('provider', '')}:{model.get('name', '')}".strip(":") or None
    return clip_text(model, 100)


def _by_architect(created_via: Any, slug: Any) -> bool:
    return bool(slug) or (isinstance(created_via, str) and created_via.startswith("architect"))


def list_workflows(engine: Engine, tenant_id: str, limit: int = MAX_LIMIT, include_archived: bool = True) -> dict[str, Any]:
    """Every workflow of the tenant: id, name, archived, published version (None when only a draft exists),
    whether the draft differs from it, and whether the Architect made it (a slug, or a version it published)."""
    with engine.connect() as conn:
        rows = conn.execute(_WORKFLOWS, {"t": tenant_id, "all": include_archived}).mappings().all()
    limit = clamp_limit(limit, MAX_LIMIT)
    return {"total": len(rows), "items": [{
        "id": str(r["id"]), "name": clip_text(r["name"]), "archived": bool(r["archived"]),
        "published_version": r["published_version"],
        "draft_pending": r["published_version"] is not None and r["draft_spec"] != r["published_spec"],
        "created_by_architect": _by_architect(r["created_via"], r["slug"]),
    } for r in rows[:limit]]}


def _render_condition(cond: Any) -> str:
    """Render a branch condition to human-readable text."""
    if not isinstance(cond, dict):
        return str(cond)[:100]
    if "op" in cond:
        op = cond.get("op", "eq")
        left = _render_operand(cond.get("left"))
        if op == "exists":
            return f"{left} exists"
        right = _render_operand(cond.get("right"))
        symbol = {"eq": "==", "ne": "!=", "gt": ">", "lt": "<", "contains": "contains"}.get(op, op)
        return f"{left} {symbol} {right}"
    if "all" in cond:
        parts = [_render_condition(c) for c in cond.get("all", [])[:4]]
        return " and ".join(parts[:3]) + ("…" if len(parts) > 3 else "")
    if "any" in cond:
        parts = [_render_condition(c) for c in cond.get("any", [])[:4]]
        return " or ".join(parts[:3]) + ("…" if len(parts) > 3 else "")
    if "not" in cond:
        inner = _render_condition(cond.get("not"))
        return f"not ({inner})"
    return str(cond)[:100]


def _render_operand(value: Any) -> str:
    """Render a condition operand (literal or $ref) to text."""
    if isinstance(value, dict):
        if "$ref" in value:
            ref = value["$ref"]
            if isinstance(ref, str):
                return f"${ref}"  # $steps.feed.output.ok, $input.dry
        if "$template" in value:
            return "template(...)"
    # Literal value
    if isinstance(value, str):
        return value[:40]
    return json.dumps(value)[:60]


def _step_does(step: dict[str, Any]) -> dict[str, Any] | None:
    """Build a `does` object describing what a step does, bounded to 600 graphemes."""
    # Published specs say type/input/tool_id/agent_id; the Architect's compact form says kind/params/tool/agent.
    kind = step.get("type") or step.get("kind")
    if not isinstance(kind, str):
        return None

    # Bounding helper
    def bounded(s: str) -> str:
        s = s[:600]
        return s + "…" if len(s) > 600 else s

    # Tool summary helpers
    def _summarize_tool_input(tool: str, params: Any) -> str:
        if not isinstance(params, dict):
            return ""
        # http.request: method + url + reducer name (and details)
        if tool == "http.request":
            method = params.get("method", "GET")
            url = params.get("url", "")
            summary = f"{method} {url}"
            # Reducers are top-level keys of the http.request input: extract_html, parse_feed, response_query, body_from_url.
            extra = []
            html = params.get("extract_html")
            if isinstance(html, dict):
                piece = f"extract_html {html.get('mode', '')}".strip()
                details = [f"limit {html['limit']}" for _ in [0] if html.get("limit")]
                for key in ("exclude_urls", "exclude_terms"):
                    if html.get(key):
                        details.append(f"{len(html[key])} {key}")
                extra.append(piece + (f" ({', '.join(details)})" if details else ""))
            if isinstance(params.get("parse_feed"), dict):
                extra.append("parse_feed")
            rq = params.get("response_query")
            if isinstance(rq, dict) and rq.get("expression"):
                extra.append(f"response_query {str(rq['expression'])[:80]}")
            if isinstance(params.get("body_from_url"), dict):
                extra.append("body_from_url")
            if extra:
                summary += " → " + " · ".join(extra)
            return summary[:200]
        # json.query: the expression cut to 120 graphemes
        if tool == "json.query":
            expr = params.get("expression", "")
            return expr[:120] if isinstance(expr, str) else str(expr)[:120]
        # workflow.return: the value's keys and outcome
        if tool == "workflow.return":
            value = params.get("value", {})
            if isinstance(value, dict) and "$template" in value and isinstance(value["$template"], dict):
                value = value["$template"]
            keys = list(value.keys()) if isinstance(value, dict) else []
            text = f"returns {{{', '.join(keys[:5])}{'…' if len(keys) > 5 else ''}}}" if keys else "returns a value"
            if params.get("outcome"):
                text += f" · outcome {params['outcome']}"
            return text
        # text.check: rule names
        if tool == "text.check":
            rules = params.get("rules", [])
            if isinstance(rules, list):
                names = [r for r in rules if isinstance(r, str)]
                return ", ".join(names[:5])[:120]
            return "text check"
        # feed.parse / html.extract: mode and url
        if tool in ("feed.parse", "html.extract"):
            mode = params.get("mode", "feed")
            url = params.get("url", "")
            return f"{mode} {url}"[:120]
        # default: the input's top-level keys
        keys = list(params.keys())
        if keys:
            return f"{', '.join(keys[:5])}"[:120]
        return ""

    # Step-specific summaries
    if kind == "tool":
        tool_id = step.get("tool_id") or step.get("tool")
        if not tool_id:
            return None
        params = step.get("input") if step.get("input") is not None else step.get("params", {})
        input_summary = _summarize_tool_input(tool_id, params)
        does: dict[str, Any] = {"tool": clip_text(str(tool_id), 120)}
        if input_summary:
            does["input_summary"] = bounded(input_summary)
        on_error = step.get("on_error")
        if on_error:
            does["on_error"] = str(on_error)[:50]
        return does

    if kind == "agent":
        agent_id = step.get("agent_id") or step.get("agent")
        agent_name = step.get("name")
        if not agent_id:
            return None
        does = {"agent_id": clip_text(str(agent_id), 120)}
        if agent_name:
            does["agent_name"] = clip_text(str(agent_name), 120)
        version = step.get("version")
        if version is not None:
            does["version"] = version if isinstance(version, int) else str(version)
        else:
            does["version"] = "latest"
        agent_input = step.get("input") if step.get("input") is not None else step.get("params")
        if isinstance(agent_input, dict):
            # The step's input mapping (published form) or, in the compact form, an input schema's properties
            keys = list((agent_input.get("properties") or {}).keys()) if "properties" in agent_input and "type" in agent_input else list(agent_input.keys())
            if keys:
                does["input_keys"] = keys[:10]
        elif isinstance(agent_input, str):
            does["input_keys"] = ["template"]
        return does

    if kind == "branch":
        cases = step.get("cases", [])
        if not isinstance(cases, list) or not cases:
            return None
        rendered_cases = []
        for c in cases[:8]:
            if isinstance(c, dict):
                when_text = _render_condition(c.get("when"))
                to = c.get("next")
                if when_text:
                    case_item: dict[str, str] = {"to": clip_text(str(to), 80) if to else "", "when": bounded(when_text)}
                    rendered_cases.append(case_item)
        default = step.get("default")
        does = {"cases": rendered_cases}
        if default:
            does["default"] = clip_text(str(default), 80)
        return does

    if kind == "loop":
        max_iter = step.get("max_iterations")
        if max_iter is not None:
            return {"max_iterations": max_iter}
        return None

    if kind == "foreach":
        items_ref = step.get("items")
        if items_ref:
            return {"items": clip_text(str(items_ref), 200)}
        return None

    if kind == "parallel":
        branches = step.get("branches", [])
        return {"branches": len(branches)}

    if kind == "approval":
        prompt = step.get("prompt", "")
        return {"prompt": clip_text(str(prompt), 200)}

    if kind == "input":
        schema = step.get("schema", {})
        if isinstance(schema, dict):
            props = (schema.get("properties") or {}).keys()
            keys = list(props)[:10]
            if keys:
                return {"fields": keys}
        return None

    if kind == "evaluate":
        evals = step.get("evaluators", [])
        if isinstance(evals, list):
            names = [e.get("id", "") if isinstance(e, dict) else str(e) for e in evals]
            return {"evaluators": names[:5]}
        return None

    return None


def _steps(steps: Any, depth: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in (steps if isinstance(steps, list) else [])[:60]:
        if not isinstance(step, dict):
            continue
        item = {"id": clip_text(step.get("id"), 80), "type": (step.get("type") or step.get("kind"))}
        # Keep kind for backwards compatibility, but drop it if null
        if (step.get("type") or step.get("kind")) is not None:
            item["kind"] = (step.get("type") or step.get("kind"))
        for key in ("tool", "agent"):
            if step.get(key):
                item[key] = clip_text(step[key], 120)
        next_step = step.get("next")
        if next_step:
            item["next"] = clip_text(str(next_step), 80)
        does = _step_does(step)
        if does:
            item["does"] = does
        for key in ("branches", "steps", "body"):
            if depth < 2 and isinstance(step.get(key), list):
                item[key] = _steps(step[key], depth + 1)
        out.append(item)
    return out


def get_workflow(engine: Engine, tenant_id: str, workflow_ref: str) -> dict[str, Any] | None:
    """One workflow (by id or slug): the published spec's summary, its steps with kind and tool or agent.
    None when it is not this tenant's."""
    with engine.connect() as conn:
        row = conn.execute(_WORKFLOW_ONE, {"t": tenant_id, "ref": str(workflow_ref)}).mappings().first()
    if row is None:
        return None
    spec = row["published_spec"] if isinstance(row["published_spec"], dict) else None
    shown = spec if spec is not None else (row["draft_spec"] if isinstance(row["draft_spec"], dict) else {})
    return {
        "id": str(row["id"]), "name": clip_text(row["name"]), "archived": bool(row["archived"]),
        "published_version": row["published_version"],
        "draft_pending": row["published_version"] is not None and row["draft_spec"] != row["published_spec"],
        "created_by_architect": _by_architect(row["created_via"], row["slug"]),
        "spec_of": "published" if spec is not None else "draft",
        "description": clip_text(shown.get("description"), 500),
        "start": shown.get("start"),
        "input_fields": sorted((shown.get("input_schema") or {}).get("properties", {}))[:30]
        if isinstance(shown.get("input_schema"), dict) else [],
        "steps": _steps(shown.get("steps")),
    }


_RECENT = {
    "agents": text(
        "SELECT id, name FROM agents WHERE tenant_id = :t AND NOT COALESCE(archived, false) AND NOT COALESCE(builtin, false) "
        "ORDER BY updated_at DESC NULLS LAST, id LIMIT :n"
    ),
    "workflows": text(
        "SELECT id, name FROM workflows WHERE tenant_id = :t AND NOT COALESCE(archived, false) "
        "ORDER BY updated_at DESC NULLS LAST, id LIMIT :n"
    ),
}
_RECENT_COUNT = {
    "agents": text("SELECT count(*) FROM agents WHERE tenant_id = :t AND NOT COALESCE(archived, false) AND NOT COALESCE(builtin, false)"),
    "workflows": text("SELECT count(*) FROM workflows WHERE tenant_id = :t AND NOT COALESCE(archived, false)"),
}


def recent_definitions(engine: Engine, tenant_id: str, kind: str, limit: int = 200) -> dict[str, Any]:
    """The tenant's live agents (`kind="agents"`, built-ins left out) or workflows, most recently changed first:
    `{total, items: [{id, name}]}` with at most `limit` items (the system context caps and filters them)."""
    with engine.connect() as conn:
        rows = conn.execute(_RECENT[kind], {"t": tenant_id, "n": max(1, min(int(limit), 200))}).mappings().all()
        total = conn.execute(_RECENT_COUNT[kind], {"t": tenant_id}).scalar_one()
    return {"total": int(total), "items": [{"id": str(r["id"]), "name": clip_text(r["name"])} for r in rows]}


def list_agents(engine: Engine, tenant_id: str, limit: int = MAX_LIMIT, include_archived: bool = False) -> dict[str, Any]:
    """The tenant's agents (the Architect's own drafter helpers left out): id, name, published version, model."""
    with engine.connect() as conn:
        rows = [
            r for r in conn.execute(_AGENTS, {"t": tenant_id, "all": include_archived}).mappings().all()
            if not (r["builtin"] and str(r["slug"] or "").endswith("-drafter"))
        ]
    limit = clamp_limit(limit, MAX_LIMIT)
    return {"total": len(rows), "items": [{
        "id": str(r["id"]), "name": clip_text(r["name"]), "archived": bool(r["archived"]), "builtin": bool(r["builtin"]),
        "published_version": r["published_version"],
        "draft_pending": r["published_version"] is not None and r["draft_spec"] != r["published_spec"],
        "model": _model_of(r["published_spec"] if r["published_spec"] is not None else r["draft_spec"]),
    } for r in rows[:limit]]}


# --------------------------------------------------------------------------------------- schedules, runs

_SCHEDULES = text(
    "SELECT s.id, s.workflow_id, s.name, s.cron, s.timezone, s.active, s.next_fire_at, s.last_fired_at, s.last_error, "
    "w.name AS workflow_name FROM workflow_schedules s LEFT JOIN workflows w ON w.id = s.workflow_id AND w.tenant_id = s.tenant_id "
    "WHERE s.tenant_id = :t AND (CAST(:w AS text) IS NULL OR s.workflow_id = CAST(:w AS text)) "
    "ORDER BY s.created_at DESC, s.id DESC LIMIT :n"
)


def list_schedules(engine: Engine, tenant_id: str, workflow_id: str | None = None, limit: int = MAX_LIMIT) -> dict[str, Any]:
    """The tenant's schedules: workflow, name, cron, timezone, active, next and last fire, last error."""
    with engine.connect() as conn:
        rows = conn.execute(_SCHEDULES, {"t": tenant_id, "w": workflow_id, "n": clamp_limit(limit, MAX_LIMIT)}).mappings().all()
    return {"items": [{
        "id": str(r["id"]), "workflow_id": str(r["workflow_id"]), "workflow_name": clip_text(r["workflow_name"]),
        "name": clip_text(r["name"]), "cron": r["cron"], "timezone": r["timezone"], "active": bool(r["active"]),
        "next_fire_at": _iso(r["next_fire_at"]), "last_fired_at": _iso(r["last_fired_at"]),
        "last_error": clip_text(r["last_error"], 200),
    } for r in rows]}


_RUNS = text(
    "SELECT r.id, r.kind, r.status, r.agent_id, r.workflow_id, r.created_at, r.started_at, r.finished_at, r.error, "
    "r.model_id, a.name AS agent_name, w.name AS workflow_name "
    "FROM runs r LEFT JOIN agents a ON a.id = r.agent_id AND a.tenant_id = r.tenant_id "
    "LEFT JOIN workflows w ON w.id = r.workflow_id AND w.tenant_id = r.tenant_id "
    "WHERE r.tenant_id = :t AND r.parent_run_id IS NULL "
    "AND (CAST(:wf AS text) IS NULL OR CAST(r.workflow_id AS text) = :wf) "
    "AND (CAST(:ag AS text) IS NULL OR CAST(r.agent_id AS text) = :ag) "
    "AND (CAST(:kind AS text) IS NULL OR r.kind = :kind) AND (CAST(:st AS text) IS NULL OR r.status = :st) "
    "ORDER BY r.created_at DESC, r.id DESC LIMIT :n"
)


def list_recent_runs(
    engine: Engine, tenant_id: str, *, limit: int = DEFAULT_LIMIT, workflow_id: str | None = None,
    agent_id: str | None = None, kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    """The last `limit` top-level runs, newest first: kind, status, start, end, the error's first line (no run
    input or output) and the workflow or agent name. Filters: workflow_id, agent_id, kind, status."""
    if kind is not None and kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {', '.join(RUN_KINDS)}")
    for name, value in (("workflow_id", workflow_id), ("agent_id", agent_id)):
        if value is not None and not _is_uuid(value):
            raise ValueError(f"{name} must be a UUID")
    params = {"t": tenant_id, "wf": str(_uuid(workflow_id)) if workflow_id else None,
              "ag": str(_uuid(agent_id)) if agent_id else None, "kind": kind, "st": status, "n": clamp_limit(limit)}
    with engine.connect() as conn:
        rows = conn.execute(_RUNS, params).mappings().all()
    return {"items": [{
        "run_id": str(r["id"]), "kind": r["kind"], "status": r["status"], "workflow_id": _s(r["workflow_id"]),
        "agent_id": _s(r["agent_id"]), "name": clip_text(r["workflow_name"] or r["agent_name"]),
        "model": r["model_id"], "created_at": _iso(r["created_at"]), "started_at": _iso(r["started_at"]),
        "finished_at": _iso(r["finished_at"]),
        "error": clip_text((r["error"] or "").splitlines()[0], 160) if r["error"] else None,
    } for r in rows]}


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


# Mask helper - copy of what's in workflow_eval.py for run input/output
# Extend to mask any sensitive key at any depth
def _sensitive_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in SENSITIVE_FIELDS or "api" in lowered and "key" in lowered or "jwt" in lowered


SENSITIVE_FIELDS = frozenset({
    "authorization", "cookie", "proxy-authorization", "x-api-key",
    "token", "secret", "password", "passwd", "access_key", "private_key",
})


def _mask_run_data(value: Any) -> Any:
    """Mask sensitive fields in run input/output at any depth, bounded to 4 KiB."""
    if isinstance(value, str):
        return clip_text(value, 4096)
    if isinstance(value, dict):
        return {k: _mask_sensitive_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_run_data(v) for v in value]
    return value


def _mask_sensitive_value(key: str, value: Any) -> Any:
    """Mask a value if its key matches a sensitive pattern, otherwise recurse."""
    if _sensitive_field(str(key)):
        return "<REDACTED>"
    if isinstance(value, dict):
        return {k: _mask_sensitive_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_run_data(v) for v in value]
    return value


def _render_run_input_output(value: Any) -> str:
    """Render run input/output as a bounded preview string."""
    if isinstance(value, str):
        return clip_text(value, 4096)
    if isinstance(value, dict):
        items = []
        for k, v in list(value.items())[:20]:
            if isinstance(v, str):
                items.append(f"{k}={clip_text(v, 60)}")
            elif isinstance(v, dict):
                items.append(f"{k}={{...}}")
            elif isinstance(v, list):
                items.append(f"{k}=[...]")
            else:
                items.append(f"{k}={json.dumps(v)[:40]}")
        return ", ".join(items)
    if isinstance(value, list):
        return f"[...{len(value)} items]"
    return json.dumps(value)[:400]


def get_run(engine: Engine, tenant_id: str, run_id: str) -> dict[str, Any] | None:
    """One run by ID: status, display_status, input (bounded 4 KiB, secrets masked), output (bounded 4 KiB),
    error, and the step list with status and a 200-grapheme output preview per step.
    None when it is not this tenant's."""
    from eap_run_store import RunStore

    rs = RunStore(engine)
    run = rs.get_run(tenant_id, _uuid(run_id))
    if run is None:
        return None

    # Bounded and masked input/output
    input_val = run.get("input")
    output_val = run.get("output")
    input_preview = _render_run_input_output(input_val) if input_val is not None else None
    output_preview = _render_run_input_output(output_val) if output_val is not None else None

    # Step list from events
    steps: list[dict[str, Any]] = []
    try:
        events = rs.list_root_events(tenant_id, _uuid(run_id))
        # Group events by step id
        step_events: dict[str, list[Any]] = {}
        for ord_ev in events:
            ev = ord_ev.event
            data = ev.data or {}
            step_id = data.get("step_id")
            if step_id:
                if step_id not in step_events:
                    step_events[step_id] = []
                step_events[step_id].append(ev)

        # Build step summaries
        for step_id, evts in step_events.items():
            step_summary: dict[str, Any] = {"id": clip_text(step_id, 80)}
            # Look for step status events
            for ev in evts:
                if ev.type == "step_start":
                    step_summary["status"] = "running"
                elif ev.type == "step_end":
                    step_summary["status"] = "completed"
                elif ev.type == "step_error":
                    step_summary["status"] = "failed"
                elif ev.type == "step_skip":
                    step_summary["status"] = "skipped"

            # Output preview from the step's output event
            for ev in evts:
                if ev.type == "step_end" and ev.data:
                    output_data = ev.data.get("output")
                    if output_data is not None:
                        # Mask sensitive fields in the output data before rendering
                        masked_data = _mask_run_data(output_data)
                        preview = json.dumps(masked_data)[:200]
                        step_summary["output_preview"] = clip_text(preview, 200)
                        break

            # Add to steps list if we have meaningful info
            steps.append(step_summary)
    except Exception:
        # If we can't get events, return an empty steps list
        steps = []

    return {
        "run_id": run.get("id"),
        "kind": run.get("kind"),
        "status": run.get("status"),
        "display_status": run.get("display_status"),
        "input": _mask_run_data(input_val),
        "input_preview": input_preview,
        "output": _mask_run_data(output_val),
        "output_preview": output_preview,
        "error": clip_text(run.get("error"), 500),
        "steps": steps,
    }


def list_runs_with_previews(
    engine: Engine, tenant_id: str, *, limit: int = DEFAULT_LIMIT, workflow_id: str | None = None,
    agent_id: str | None = None, kind: str | None = None, status: str | None = None,
) -> dict[str, Any]:
    """The last `limit` top-level runs, newest first: kind, status, start, end, the error's first line,
    output_preview (120 graphemes), and the workflow or agent name. No run input or output.
    Filters: workflow_id, agent_id, kind, status."""
    if kind is not None and kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {', '.join(RUN_KINDS)}")
    for name, value in (("workflow_id", workflow_id), ("agent_id", agent_id)):
        if value is not None and not _is_uuid(value):
            raise ValueError(f"{name} must be a UUID")
    params = {"t": tenant_id, "wf": str(_uuid(workflow_id)) if workflow_id else None,
              "ag": str(_uuid(agent_id)) if agent_id else None, "kind": kind, "st": status, "n": clamp_limit(limit)}
    with engine.connect() as conn:
        rows = conn.execute(_RUNS, params).mappings().all()

    from eap_run_store import RunStore

    rs = RunStore(engine)
    items = []
    for r in rows:
        run_id = str(r["id"])
        # output_preview comes from the run row's output (as get_run reads it), masked and cut to 120 graphemes.
        output_preview = None
        stored = rs.get_run(tenant_id, _uuid(run_id))
        output = stored.get("output") if stored else None
        if output is not None:
            output_preview = clip_text(_render_run_input_output(_mask_run_data(output)), 120)

        items.append({
            "run_id": run_id, "kind": r["kind"], "status": r["status"],
            "workflow_id": _s(r["workflow_id"]), "agent_id": _s(r["agent_id"]),
            "name": clip_text(r["workflow_name"] or r["agent_name"]),
            "model": r["model_id"], "created_at": _iso(r["created_at"]),
            "started_at": _iso(r["started_at"]), "finished_at": _iso(r["finished_at"]),
            "error": clip_text((r["error"] or "").splitlines()[0], 160) if r["error"] else None,
            "output_preview": output_preview,
        })
    return {"items": items}
