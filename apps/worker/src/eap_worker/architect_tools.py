"""The Architect's read and draft tools (Task 3): registry tools in the `architect.` namespace.

They run inside the Architect agent's run, in the worker, over the app database with the run's tenant
(`ctx.deps.tenant_id`); never through HTTP to our own API.

Read tools (no approval): `architect.list_approved_tools`, `list_created_agents`,
`list_agent_versions`, `list_created_workflows`, `list_workflow_versions`,
`list_workflow_step_kinds`. Draft tools (no approval, a draft is inert): `architect.save_agent`,
`architect.save_workflow`. Gated tools (listed in the Architect's `approvals.tools`, so every call
waits for a person): `architect.publish_agent`, `restore_agent_version`, `archive_agent`,
`publish_workflow`, `restore_workflow_version`, `archive_workflow`, `set_workflow_schedule`.

The gated tools hold no logic of their own: publish, restore and archive are the functions of
`eap_run_store.definitions`, the same ones the API routes call, run in one transaction with the
tenant of the run. Schedules go through `eap_run_store.ScheduleStore`. The approval summary of a gated
call (`approval_summary_text`) comes from a rolled-back dry run of that same function, so the card
shows exactly the warnings, versions, referencing workflows and schedules the real call would meet.

Rules every tool follows:

- Ids in tool inputs and outputs are slugs (`agents.slug`, `workflows.slug`), never row ids.
- A tool never raises for a problem the model can fix: it returns `{"error": {"code", "message"}}`.
- The Architect edits only what the Architect made: a definition is the Architect's when it has a
  slug (only these tools and the Architect page set one). It never edits a built-in agent or the
  reserved slugs (`architect`, `architect-drafter`, ...), and when the latest version of a
  definition has no `created_via` starting with `architect` (someone edited it by hand) a save
  returns `{"status": "needs_confirmation", "reason": "edited_by_hand"}` and writes nothing until
  it is called again with `confirm: true`.
- A save writes only the draft (`draft_spec`); the version with `created_via = architect:<run_id>`
  comes from the gated publish tool.

The functions with an `engine` argument hold the logic (and are what the tests call); the registered
tools are thin wrappers that take the engine from `get_runtime()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic_ai import RunContext
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from eap_run_store import ScheduleNameConflict, ScheduleStore, definitions
from eap_run_store.definition_errors import DefinitionError
from eap_runtime import RunDeps, ToolRegistry, run_side_effect
from eap_schemas import reserved_tool_name_conflicts
from eap_schemas.architect import (
    ARCHITECT_GATED_TOOL_IDS,
    RESERVED_AGENT_IDS,
    Allowlist,
    ArchitectAgentSpec,
    ArchitectWorkflowSpec,
    compile_agent,
    compile_workflow,
    slugify,
)

from eap_worker.architect_allowlist import approved_for_tenant

log = logging.getLogger("eap_worker.architect_tools")

ARCHITECT_MARKER_PREFIX = "architect"
TOOL_IDS = (
    "architect.list_approved_tools",
    "architect.list_created_agents",
    "architect.list_agent_versions",
    "architect.list_created_workflows",
    "architect.list_workflow_versions",
    "architect.list_workflow_step_kinds",
    "architect.save_agent",
    "architect.save_workflow",
    "architect.publish_agent",
    "architect.restore_agent_version",
    "architect.archive_agent",
    "architect.publish_workflow",
    "architect.restore_workflow_version",
    "architect.archive_workflow",
    "architect.set_workflow_schedule",
)
# The tools that go live: the seed lists them in the Architect's approvals.tools.
GATED_TOOL_IDS = ARCHITECT_GATED_TOOL_IDS
assert TOOL_IDS[8:] == GATED_TOOL_IDS
# `created_by` of a version made through a tool (the person who approved is in the approval record).
ARCHITECT_ACTOR = "architect"
SCHEDULE_ACTIONS = ("create", "pause", "resume", "delete")

STEP_KINDS: list[dict[str, Any]] = [
    {
        "kind": "agent",
        "fields": {"id": "step id", "agent": "id of an agent from list_approved_tools.agents", "prompt": "text"},
        "description": "Run one agent with the prompt and pass its answer on.",
    },
    {
        "kind": "parallel",
        "fields": {"id": "step id", "branches": "2 to 4 agent steps (id, agent, prompt) that run together"},
        "description": "Run agent steps at the same time. The step after it starts when all have finished, "
        "so a parallel step cannot be last; that step reads each branch as {{steps.<branch id>}}.",
    },
    {
        "kind": "tool",
        "fields": {"id": "step id", "tool": "id of a tool from list_approved_tools.tools", "params": "object"},
        "description": "Call one approved tool. A param value may be exactly one placeholder.",
    },
]
PLACEHOLDERS = {
    "{{input}}": "the text the workflow was started with",
    "{{previous}}": "the answer of the step before this one (not after a parallel step)",
    "{{steps.<id>}}": "the answer of an earlier step, or of a branch of an earlier parallel step",
}
LIMITS = {"steps": "1 to 8, run in order", "branches": "2 to 4", "loops_conditions_code": "not supported"}


def error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code(exc: ValueError) -> tuple[str, str]:
    """`code` and message of a compile ValueError that starts "code: ..."; other errors are invalid_spec."""
    message = str(exc)
    head, sep, _ = message.partition(":")
    if sep and re.fullmatch(r"[a-z_]+", head):
        return head, message
    return "invalid_spec", message


_SpecT = TypeVar("_SpecT", bound=BaseModel)


def coerce_spec(model: type[_SpecT], value: Any) -> tuple[_SpecT | None, dict[str, Any] | None]:
    """The spec as `model`: an instance, a dict, or a string that parses as a JSON object (thinking models
    sometimes JSON-encode the argument). Anything else is the usual `invalid_spec` error object, never a
    raw validation error, so the model reads one plain message."""
    if isinstance(value, model):
        return value, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, error("invalid_spec", "spec must be a JSON object (the definition itself), not text.")
    if not isinstance(value, dict):
        return None, error("invalid_spec", "spec must be a JSON object (the definition itself).")
    try:
        return model.model_validate(value), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'spec'}: {e['msg']}" for e in exc.errors()[:6]
        )
        return None, error("invalid_spec", problems)


def _marked(created_via: str | None) -> bool:
    return bool(created_via) and created_via.startswith(ARCHITECT_MARKER_PREFIX)


class _Kind:
    """The SQL of one definition kind (agents or workflows); `table` and `versions` are fixed names."""

    def __init__(self, table: str, versions: str, fk: str) -> None:
        self.table, self.versions, self.fk = table, versions, fk
        self.by_slug = text(
            f"SELECT id, name, draft_spec, archived, {'builtin' if table == 'agents' else 'false AS builtin'} "
            f"FROM {table} WHERE tenant_id = :t AND slug = :s"
        )
        self.latest = text(
            f"SELECT version, created_via, spec FROM {versions} WHERE {fk} = :d AND tenant_id = :t "
            "ORDER BY version DESC LIMIT 1"
        )
        self.listing = text(
            f"SELECT id, slug, name, draft_spec FROM {table} WHERE tenant_id = :t AND slug IS NOT NULL "
            f"AND NOT archived{' AND NOT builtin' if table == 'agents' else ''} ORDER BY slug"
        )
        self.history = text(
            f"SELECT version, created_via, created_at FROM {versions} WHERE {fk} = :d AND tenant_id = :t "
            "ORDER BY version DESC"
        )
        self.insert = text(
            f"INSERT INTO {table} (id, tenant_id, name, draft_spec, archived, slug, created_at, updated_at"
            f"{', builtin' if table == 'agents' else ''}) VALUES (:id, :t, :name, CAST(:spec AS jsonb), false, "
            f":s, :now, :now{', false' if table == 'agents' else ''})"
        )
        self.update = text(
            f"UPDATE {table} SET name = :name, draft_spec = CAST(:spec AS jsonb), updated_at = :now "
            "WHERE id = :id AND tenant_id = :t"
        )


AGENTS = _Kind("agents", "agent_versions", "agent_id")
WORKFLOWS = _Kind("workflows", "workflow_versions", "workflow_id")


def _list_created(engine: Engine, tenant_id: str, kind: _Kind) -> list[dict[str, Any]]:
    out = []
    with engine.connect() as conn:
        for row in conn.execute(kind.listing, {"t": tenant_id}).mappings().all():
            history = conn.execute(kind.history, {"d": row["id"], "t": tenant_id}).mappings().all()
            latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).mappings().first()
            out.append({
                "id": row["slug"],
                "name": row["name"],
                "versions": len(history),
                "latest_created_via": history[0]["created_via"] if history else None,
                "draft": latest is None or row["draft_spec"] != latest["spec"],
            })
    return out


def _list_versions(engine: Engine, tenant_id: str, kind: _Kind, slug: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
        if row is None or row["builtin"]:
            return error("not_found", f"No definition made by the Architect has the id {slug!r}.")
        history = conn.execute(kind.history, {"d": row["id"], "t": tenant_id}).mappings().all()
    return {
        "id": slug,
        "name": row["name"],
        "versions": [
            {"version": h["version"], "created_via": h["created_via"], "created_at": h["created_at"].isoformat()}
            for h in history
        ],
    }


def _save(
    engine: Engine, tenant_id: str, kind: _Kind, *, slug: str | None, derived_from: str, name: str,
    spec_json: dict[str, Any], confirm: bool,
) -> dict[str, Any]:
    """Write the draft of one definition (blocking). `slug` None derives one from the name."""
    label = "agent" if kind is AGENTS else "workflow"
    spec_text = json.dumps(spec_json)
    with engine.begin() as conn:
        row = None
        if slug is not None:
            if slug in RESERVED_AGENT_IDS:
                return error("reserved_id", f"{slug!r} is a built-in {label} id; choose another id.")
            row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
        else:
            base = slugify(derived_from)
            for n in range(1, 50):
                candidate = base if n == 1 else f"{base[:44]}-{n}"
                if candidate in RESERVED_AGENT_IDS:
                    continue
                found = conn.execute(kind.by_slug, {"t": tenant_id, "s": candidate}).mappings().first()
                if found is None:
                    slug = candidate
                    break
                if found["draft_spec"] == spec_json and found["name"] == name and not found["builtin"]:
                    slug, row = candidate, found  # the same draft again: a retried call
                    break
            if slug is None:
                return error("id_conflict", "Could not find a free id; give the definition an id.")
        if row is not None:
            if row["builtin"]:
                return error("builtin_agent", f"{slug!r} is a built-in agent; the Architect does not edit it.")
            if row["archived"]:
                return error("archived", f"The {label} {slug!r} is archived; it cannot be edited.")
            latest = conn.execute(kind.latest, {"d": row["id"], "t": tenant_id}).mappings().first()
            if latest is not None and not _marked(latest["created_via"]) and not confirm:
                return {
                    "status": "needs_confirmation",
                    "reason": "edited_by_hand",
                    "id": slug,
                    "message": f"The latest version of {slug!r} (v{latest['version']}) was made or edited by hand. "
                    "Tell the person, and call this tool again with confirm: true only after they say yes.",
                }
            conn.execute(kind.update, {"id": row["id"], "t": tenant_id, "name": name, "spec": spec_text, "now": _now()})
        else:
            try:
                with conn.begin_nested():
                    conn.execute(kind.insert, {
                        "id": str(uuid.uuid4()), "t": tenant_id, "name": name, "spec": spec_text, "s": slug,
                        "now": _now(),
                    })
            except IntegrityError:
                return error("id_conflict", f"The id {slug!r} was taken a moment ago; call again.")
    return {"id": slug, "version": None, "status": "draft"}


async def list_approved_tools(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return (await approved_for_tenant(engine, tenant_id)).model_dump(mode="json")


async def list_created_agents(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return {"agents": await asyncio.to_thread(_list_created, engine, tenant_id, AGENTS)}


async def list_agent_versions(engine: Engine, tenant_id: str, id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_list_versions, engine, tenant_id, AGENTS, id)


async def list_created_workflows(engine: Engine, tenant_id: str) -> dict[str, Any]:
    return {"workflows": await asyncio.to_thread(_list_created, engine, tenant_id, WORKFLOWS)}


async def list_workflow_versions(engine: Engine, tenant_id: str, id: str) -> dict[str, Any]:
    return await asyncio.to_thread(_list_versions, engine, tenant_id, WORKFLOWS, id)


async def list_workflow_step_kinds() -> dict[str, Any]:
    return {"step_kinds": STEP_KINDS, "placeholders": PLACEHOLDERS, "limits": LIMITS}


async def save_agent(
    engine: Engine, tenant_id: str, spec: ArchitectAgentSpec | dict[str, Any] | str, confirm: bool = False,
    *, allowlist: Allowlist | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    spec, problem = coerce_spec(ArchitectAgentSpec, spec)
    if problem is not None:
        return problem
    allowlist = allowlist or await approved_for_tenant(engine, tenant_id)
    try:
        compiled = compile_agent(spec, allowlist)
    except ValueError as exc:
        code, message = _code(exc)
        return error(code, message)
    result = await asyncio.to_thread(
        _save, engine, tenant_id, AGENTS, slug=spec.id, derived_from=spec.name, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), confirm=confirm,
    )
    if "error" in result or result.get("status") != "draft":
        return result
    warnings = [f"{name!r} is a reserved platform tool name" for name in reserved_tool_name_conflicts(compiled)]
    if compiled.approvals is not None:
        warnings.append("A person approves each call of: " + ", ".join(compiled.approvals.tools))
    log.info("architect.save_agent tenant=%s run=%s id=%s", tenant_id, run_id, result["id"])
    return {**result, "warnings": warnings}


async def save_workflow(
    engine: Engine, tenant_id: str, spec: ArchitectWorkflowSpec | dict[str, Any] | str, confirm: bool = False,
    *, allowlist: Allowlist | None = None, run_id: str | None = None,
) -> dict[str, Any]:
    spec, problem = coerce_spec(ArchitectWorkflowSpec, spec)
    if problem is not None:
        return problem
    allowlist = allowlist or await approved_for_tenant(engine, tenant_id)
    try:
        compiled = compile_workflow(spec, allowlist)
    except ValueError as exc:
        code, message = _code(exc)
        return error(code, message)
    result = await asyncio.to_thread(
        _save, engine, tenant_id, WORKFLOWS, slug=spec.id, derived_from=spec.name, name=compiled.name,
        spec_json=compiled.model_dump(mode="json"), confirm=confirm,
    )
    if "error" in result or result.get("status") != "draft":
        return result
    log.info("architect.save_workflow tenant=%s run=%s id=%s", tenant_id, run_id, result["id"])
    return {**result, "warnings": []}


# ----------------------------------------------------------------------------------- gated tools

T = TypeVar("T")
_REFUSAL_CODES = {400: "no_draft", 404: "not_found", 409: "conflict", 422: "publish_refused", 503: "unavailable"}


def _via(run_id: str | None) -> str:
    return f"{ARCHITECT_MARKER_PREFIX}:{run_id}" if run_id else ARCHITECT_MARKER_PREFIX


def _refusal(exc: DefinitionError) -> dict[str, Any]:
    """A refusal of a shared definition operation as the model reads it: `{error: {code, message, ...}}`."""
    return error(exc.code or _REFUSAL_CODES.get(exc.status_code, "refused"), exc.detail, **exc.extra)


def _resolve(conn: Connection, kind: _Kind, tenant_id: str, slug: str) -> tuple[Any, dict[str, Any] | None]:
    """The Architect's definition by slug, or an error: a built-in agent, a definition without a slug
    (someone else's: not reachable by slug) and another tenant's all refuse."""
    row = conn.execute(kind.by_slug, {"t": tenant_id, "s": slug}).mappings().first()
    label = "agent" if kind is AGENTS else "workflow"
    if row is None:
        return None, error("not_found", f"No {label} made by the Architect has the id {slug!r}.")
    if row["builtin"]:
        return None, error("builtin_agent", f"{slug!r} is a built-in agent; the Architect does not change it.")
    return row, None


def _apply(
    engine: Engine, kind: _Kind, tenant_id: str, slug: str, op: Callable[[Connection, Any], dict[str, Any]]
) -> dict[str, Any]:
    """Run `op(conn, row)` on the Architect's definition `slug` in ONE transaction: committed when it
    returns, rolled back (the definition untouched) when it raises a DefinitionError, which becomes an
    error result."""
    try:
        with engine.begin() as conn:
            row, err = _resolve(conn, kind, tenant_id, slug)
            if err is not None:
                return err
            return op(conn, row)
    except DefinitionError as exc:
        return _refusal(exc)


def _published(slug: str, result: definitions.Published) -> dict[str, Any]:
    return {
        "id": slug,
        "version": result.version["version"],
        "status": "published",
        "warnings": [w["message"] for w in result.warnings],
    }


def publish_agent_sync(engine: Engine, tenant_id: str, id: str, run_id: str | None = None) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.publish_agent(
            conn, tenant_id, str(row["id"]), created_by=ARCHITECT_ACTOR, created_via=_via(run_id)
        )
        return _published(id, result)

    return _apply(engine, AGENTS, tenant_id, id, op)


def restore_agent_version_sync(
    engine: Engine, tenant_id: str, id: str, version: int, discard_draft: bool = False, run_id: str | None = None
) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.restore_agent_version(
            conn, tenant_id, str(row["id"]), version, created_by=ARCHITECT_ACTOR, created_via=_via(run_id),
            discard_draft=discard_draft,
        )
        return {**_published(id, result), "status": "restored", "restored_from": version}

    return _apply(engine, AGENTS, tenant_id, id, op)


def archive_agent_sync(engine: Engine, tenant_id: str, id: str, run_id: str | None = None) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.set_agent_archived(conn, tenant_id, str(row["id"]), True)
        return {"id": id, "status": "archived", "archived": result.archived}

    return _apply(engine, AGENTS, tenant_id, id, op)


def publish_workflow_sync(engine: Engine, tenant_id: str, id: str, run_id: str | None = None) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.publish_workflow(
            conn, tenant_id, str(row["id"]), created_by=ARCHITECT_ACTOR, created_via=_via(run_id)
        )
        return _published(id, result)

    return _apply(engine, WORKFLOWS, tenant_id, id, op)


def restore_workflow_version_sync(
    engine: Engine, tenant_id: str, id: str, version: int, discard_draft: bool = False, run_id: str | None = None
) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.restore_workflow_version(
            conn, tenant_id, str(row["id"]), version, created_by=ARCHITECT_ACTOR, created_via=_via(run_id),
            discard_draft=discard_draft,
        )
        return {**_published(id, result), "status": "restored", "restored_from": version}

    return _apply(engine, WORKFLOWS, tenant_id, id, op)


def archive_workflow_sync(engine: Engine, tenant_id: str, id: str, run_id: str | None = None) -> dict[str, Any]:
    def op(conn: Connection, row: Any) -> dict[str, Any]:
        result = definitions.set_workflow_archived(conn, tenant_id, str(row["id"]), True)
        return {
            "id": id,
            "status": "archived",
            "archived": result.archived,
            "paused_schedules": len(result.paused_schedules),
        }

    return _apply(engine, WORKFLOWS, tenant_id, id, op)


# ---- schedules (eap_run_store.ScheduleStore; the checks are the API's, restated for the worker)


def describe_cron(cron: str) -> str:
    """The cron in words, by the rules of the Studio's schedule preview (cronPreview.ts): every minute,
    every N minutes, every hour at :MM, every day at HH:MM; any other pattern is returned as written."""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, day, month, weekday = parts
    if day != "*" or month != "*" or weekday != "*":
        return cron
    if minute == "*" and hour == "*":
        return "Every minute"
    step = re.fullmatch(r"\*/(\d+)", minute)
    if step and hour == "*":
        n = int(step.group(1))
        return "Every minute" if n == 1 else f"Every {n} minutes"
    if re.fullmatch(r"\d+", minute) and int(minute) < 60:
        if hour == "*":
            return f"Every hour at :{int(minute):02d}"
        if re.fullmatch(r"\d+", hour) and int(hour) < 24:
            return f"Every day at {int(hour):02d}:{int(minute):02d}"
    return cron


def _cron_problem(cron: str, tz: str) -> dict[str, Any] | None:
    from croniter import croniter

    if not (isinstance(cron, str) and len(cron.split()) == 5 and croniter.is_valid(cron)):
        return error("invalid_cron", "cron must be 5 fields: minute hour day month weekday")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return error("invalid_timezone", "timezone must be an IANA name such as UTC or Asia/Taipei")
    return None


def _schedule_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schedule_id": row["id"],
        "name": row["name"],
        "cron": row["cron"],
        "cron_in_words": describe_cron(row["cron"]),
        "timezone": row["timezone"],
        "active": row["active"],
        "next_fire_at": row["next_fire_at"].isoformat(),
    }


def set_workflow_schedule_sync(
    engine: Engine, tenant_id: str, workflow_id: str, action: str, *, name: str | None = None,
    cron: str | None = None, timezone: str = "UTC", input: Any = None, schedule_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """create | pause | resume | delete one schedule of a workflow the Architect made. A schedule fires
    the workflow's latest published version, so `create` and `resume` need one; pause and delete always
    work. `schedule_id` (or, for pause, resume and delete, the schedule's `name`) names the schedule."""
    from eap_worker.schedule_tick import input_problems, next_fire

    if action not in SCHEDULE_ACTIONS:
        return error("invalid_action", f"action must be one of: {', '.join(SCHEDULE_ACTIONS)}.")
    with engine.connect() as conn:
        row, err = _resolve(conn, WORKFLOWS, tenant_id, workflow_id)
        if err is not None:
            return err
        latest = conn.execute(
            text(
                "SELECT spec FROM workflow_versions WHERE workflow_id = :w AND tenant_id = :t"
                " ORDER BY version DESC LIMIT 1"
            ),
            {"w": row["id"], "t": tenant_id},
        ).first()
    if row["archived"]:
        return error("archived", f"The workflow {workflow_id!r} is archived; it takes no schedules.")
    store = ScheduleStore(engine)
    wf_row_id = str(row["id"])

    def find() -> dict[str, Any] | None:
        if schedule_id is not None:
            found = store.get(tenant_id, schedule_id)
            return found if found is not None and found["workflow_id"] == wf_row_id else None
        if name is not None:
            items, _ = store.list(tenant_id, wf_row_id, limit=200)
            return next((i for i in items if i["name"] == name), None)
        return None

    if action == "create":
        if not name or not cron:
            return error("missing_field", "create needs a name and a cron.")
        if (problem := _cron_problem(cron, timezone)) is not None:
            return problem
        if latest is None:
            return error("not_published", "Publish the workflow first: a schedule runs its latest published version.")
        value = {} if input is None else input
        if problems := input_problems(latest.spec.get("input_schema"), value):
            return error("input_schema_errors", "The input does not fit the workflow's input schema.", errors=problems)
        try:
            created = store.create(
                tenant_id, wf_row_id, name=name, cron=cron, timezone=timezone, input=value,
                next_fire_at=next_fire(cron, timezone, _now()),
            )
        except ScheduleNameConflict:
            return error("schedule_name_taken", f"The workflow already has a schedule named {name!r}.")
        log.info("architect.set_workflow_schedule tenant=%s run=%s create=%s", tenant_id, run_id, created["id"])
        return {"id": workflow_id, "status": "created", **_schedule_json(created)}

    current = find()
    if current is None:
        return error("not_found", "No schedule of this workflow has that schedule_id or name.")
    if action == "delete":
        store.delete(tenant_id, current["id"])
        return {"id": workflow_id, "status": "deleted", "schedule_id": current["id"], "name": current["name"]}
    if action == "pause":
        updated = store.update(tenant_id, current["id"], active=False, last_error=None)
        return {"id": workflow_id, "status": "paused", **_schedule_json(updated)}
    # resume: re-checked like the API's PATCH (the stored cron and input may no longer fit)
    if (problem := _cron_problem(current["cron"], current["timezone"])) is not None:
        return problem
    if latest is None:
        return error("not_published", "Publish the workflow first: a schedule runs its latest published version.")
    if problems := input_problems(latest.spec.get("input_schema"), current["input"]):
        return error("input_schema_errors", "The stored input no longer fits the workflow's input schema.", errors=problems)
    updated = store.update(
        tenant_id, current["id"], active=True, last_error=None,
        next_fire_at=next_fire(current["cron"], current["timezone"], _now()),
    )
    return {"id": workflow_id, "status": "resumed", **_schedule_json(updated)}


# ---- async entry points (what the tests and the registered tools call)


async def publish_agent(engine: Engine, tenant_id: str, id: str, *, run_id: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(publish_agent_sync, engine, tenant_id, id, run_id)


async def restore_agent_version(
    engine: Engine, tenant_id: str, id: str, version: int, *, discard_draft: bool = False, run_id: str | None = None
) -> dict[str, Any]:
    return await asyncio.to_thread(restore_agent_version_sync, engine, tenant_id, id, version, discard_draft, run_id)


async def archive_agent(engine: Engine, tenant_id: str, id: str, *, run_id: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(archive_agent_sync, engine, tenant_id, id, run_id)


async def publish_workflow(engine: Engine, tenant_id: str, id: str, *, run_id: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(publish_workflow_sync, engine, tenant_id, id, run_id)


async def restore_workflow_version(
    engine: Engine, tenant_id: str, id: str, version: int, *, discard_draft: bool = False, run_id: str | None = None
) -> dict[str, Any]:
    return await asyncio.to_thread(restore_workflow_version_sync, engine, tenant_id, id, version, discard_draft, run_id)


async def archive_workflow(engine: Engine, tenant_id: str, id: str, *, run_id: str | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(archive_workflow_sync, engine, tenant_id, id, run_id)


async def set_workflow_schedule(engine: Engine, tenant_id: str, workflow_id: str, action: str, **fields: Any) -> dict[str, Any]:
    return await asyncio.to_thread(set_workflow_schedule_sync, engine, tenant_id, workflow_id, action, **fields)


# ---- approval summaries


def _pure_summary(tool_id: str, args: dict[str, Any]) -> str:
    """The summary from the arguments alone (the registry summariser: pure and deterministic; the
    fallback when the facts below cannot be read)."""
    ident = args.get("id")
    version = args.get("version")
    if tool_id == "architect.publish_agent":
        return f"Publish the draft of agent {ident!r} as its next version, live for every run."
    if tool_id == "architect.restore_agent_version":
        return f"Restore agent {ident!r} to version {version}: published again as a new version."
    if tool_id == "architect.archive_agent":
        return f"Archive agent {ident!r}: new runs, conversations and publishes are refused."
    if tool_id == "architect.publish_workflow":
        return f"Publish the draft of workflow {ident!r} as its next version, live for every run."
    if tool_id == "architect.restore_workflow_version":
        return f"Restore workflow {ident!r} to version {version}: published again as a new version."
    if tool_id == "architect.archive_workflow":
        return f"Archive workflow {ident!r}: it takes no new runs and its schedules are paused."
    if tool_id == "architect.set_workflow_schedule":
        action = args.get("action")
        target = f"workflow {args.get('workflow_id')!r}"
        label = args.get("name") or args.get("schedule_id") or "a schedule"
        if action == "create":
            cron = str(args.get("cron") or "")
            return (
                f"Create schedule {label!r} on {target}: {describe_cron(cron)} ({args.get('timezone') or 'UTC'})"
                + (f", cron `{cron}`" if describe_cron(cron) != cron else "")
                + ". It then starts runs by itself."
            )
        verb = {"pause": "Pause", "resume": "Resume", "delete": "Delete"}.get(str(action), str(action))
        return f"{verb} schedule {label!r} on {target}."
    return tool_id


def _dry_run(engine: Engine, fn: Callable[[Connection], T]) -> T:
    """Run `fn` in a transaction that is always rolled back: the real shared function, nothing kept."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            return fn(conn)
        finally:
            trans.rollback()


def _warnings_text(warnings: list[dict[str, str]]) -> str:
    return "Warnings: " + " ".join(f"{w['message']}" for w in warnings) if warnings else "No publish warnings."


def approval_summary_text(engine: Engine, tenant_id: str, tool_id: str, args: dict[str, Any]) -> str | None:
    """The approval card's summary of one gated call, with facts read now (blocking): the publish
    warnings, the version a restore leaves and reaches, the workflows that run an agent, the schedules
    an archive pauses, the cron in words. The publish, restore and archive facts come from a dry run
    of the shared function (rolled back), so they are what the call itself would meet. None for a tool
    that is not gated here."""
    if tool_id not in GATED_TOOL_IDS:
        return None
    base = _pure_summary(tool_id, args)
    slug = args.get("id")
    version = args.get("version")
    if tool_id == "architect.set_workflow_schedule":
        return base
    if not isinstance(slug, str):
        return base
    kind = AGENTS if "agent" in tool_id else WORKFLOWS
    label = "agent" if kind is AGENTS else "workflow"
    facts: list[str] = []

    def dry(conn: Connection) -> None:
        row, err = _resolve(conn, kind, tenant_id, slug)
        if err is not None:
            facts.append(f"It would be refused: {err['error']['message']}")
            return
        row_id = str(row["id"])
        name = row["name"]
        try:
            if tool_id.endswith("publish_agent") or tool_id.endswith("publish_workflow"):
                fn = definitions.publish_agent if kind is AGENTS else definitions.publish_workflow
                result = fn(conn, tenant_id, row_id, created_by=ARCHITECT_ACTOR, created_via=ARCHITECT_MARKER_PREFIX)
                new = result.version["version"]
                facts.append(f"Publishes {label} {name!r} as version {new}.")
                facts.append(_warnings_text(result.warnings))
            elif "restore" in tool_id:
                fn = definitions.restore_agent_version if kind is AGENTS else definitions.restore_workflow_version
                if not isinstance(version, int):
                    return
                result = fn(
                    conn, tenant_id, row_id, version, created_by=ARCHITECT_ACTOR, created_via=ARCHITECT_MARKER_PREFIX,
                    discard_draft=bool(args.get("discard_draft")),
                )
                new = result.version["version"]
                facts.append(
                    f"Restores {label} {name!r} from version {new - 1} (live now) to version {version}, "
                    f"published as version {new}; the draft becomes version {version}."
                )
                facts.append(_warnings_text(result.warnings))
            elif tool_id == "architect.archive_agent":
                definitions.set_agent_archived(conn, tenant_id, row_id, True)
                facts.append(f"Archives agent {name!r}. No published workflow runs it.")
            elif tool_id == "architect.archive_workflow":
                active = [s for s in ScheduleStore(engine).list(tenant_id, row_id, limit=200)[0] if s["active"]]
                facts.append(f"Archives workflow {name!r}.")
                facts.append(
                    "It pauses "
                    + ", ".join(f"{s['name']!r} ({describe_cron(s['cron'])}, {s['timezone']})" for s in active)
                    + "." if active else "It has no active schedules."
                )
        except DefinitionError as exc:
            detail = exc.detail
            workflows = exc.extra.get("workflows")
            if workflows:
                names = ", ".join(w["workflow_name"] for w in workflows)
                detail = f"published workflows run it: {names}; archiving is refused while they do"
            facts.append(f"It would be refused now: {detail}.")

    try:
        _dry_run(engine, dry)
    except Exception:  # noqa: BLE001 - a summary is a courtesy; the pure one stands
        log.warning("architect approval summary failed tool=%s", tool_id, exc_info=True)
        return base
    return " ".join([base, *facts])


# ------------------------------------------------------------------------------------- registry


def _engine() -> Engine:
    from eap_worker.runtime import get_runtime  # runtime imports platform_tools, which registers these

    return get_runtime().engine


async def _list_approved_tools(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """What a created agent or workflow may use: tools (with requires_approval), MCP servers and their
    tools, knowledge bases, models, and agents a workflow step may run. Offer only these."""
    return await list_approved_tools(_engine(), ctx.deps.tenant_id)


async def _list_created_agents(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The agents the Architect made: id, name, number of versions, who made the latest version, draft."""
    return await list_created_agents(_engine(), ctx.deps.tenant_id)


async def _list_agent_versions(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """The versions of an agent the Architect made (by its id)."""
    return await list_agent_versions(_engine(), ctx.deps.tenant_id, id)


async def _list_created_workflows(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The workflows the Architect made: id, name, number of versions, who made the latest version, draft."""
    return await list_created_workflows(_engine(), ctx.deps.tenant_id)


async def _list_workflow_versions(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """The versions of a workflow the Architect made (by its id)."""
    return await list_workflow_versions(_engine(), ctx.deps.tenant_id, id)


async def _list_workflow_step_kinds(ctx: RunContext[RunDeps]) -> dict[str, Any]:
    """The step kinds a workflow may use, the prompt placeholders and the limits."""
    return await list_workflow_step_kinds()


async def _save_agent(
    ctx: RunContext[RunDeps], spec: ArchitectAgentSpec | str, confirm: bool = False
) -> dict[str, Any]:
    """Save an agent definition as a draft (not live). New agent when `id` is unused, otherwise the next
    draft of that Architect-made agent. If the result is needs_confirmation, ask the person first and call
    again with confirm: true. Grant a tool only when the description names a capability."""
    return await save_agent(_engine(), ctx.deps.tenant_id, spec, confirm, run_id=str(ctx.deps.run_id))


async def _save_workflow(
    ctx: RunContext[RunDeps], spec: ArchitectWorkflowSpec | str, confirm: bool = False
) -> dict[str, Any]:
    """Save a workflow definition as a draft (not live). Same rules as save_agent."""
    return await save_workflow(_engine(), ctx.deps.tenant_id, spec, confirm, run_id=str(ctx.deps.run_id))


async def _gated(ctx: RunContext[RunDeps], fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run one gated operation once for this tool call: the side-effect store keeps the result under
    the run and tool call id, so a crashed and replayed call returns it instead of repeating the change
    (and an unknown outcome is raised, never guessed)."""
    if ctx.tool_call_id is None:
        raise ValueError("tool_call_id is required for the Architect's gated tools")
    key = f"architect:{ctx.deps.run_id}:{ctx.tool_call_id}"
    tenant_id, run_id = ctx.deps.tenant_id, str(ctx.deps.run_id)

    def call() -> dict[str, Any]:
        return fn(_engine(), tenant_id, *args, run_id=run_id, **kwargs)

    result, _performed = await asyncio.to_thread(run_side_effect, ctx.deps.side_effects, key, call)
    return result


async def _publish_agent(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """Publish the draft of an agent you made as its next version (live). A person approves each call."""
    return await _gated(ctx, publish_agent_sync, id)


async def _restore_agent_version(
    ctx: RunContext[RunDeps], id: str, version: int, discard_draft: bool = False
) -> dict[str, Any]:
    """Restore an agent you made to an earlier version (published as a new version; the draft becomes it).
    Set discard_draft only after the person agreed to lose unpublished draft changes. A person approves each call."""
    return await _gated(ctx, restore_agent_version_sync, id, version, discard_draft)


async def _archive_agent(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """Archive an agent you made. Refused while a published workflow runs it. A person approves each call."""
    return await _gated(ctx, archive_agent_sync, id)


async def _publish_workflow(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """Publish the draft of a workflow you made as its next version (live). A person approves each call."""
    return await _gated(ctx, publish_workflow_sync, id)


async def _restore_workflow_version(
    ctx: RunContext[RunDeps], id: str, version: int, discard_draft: bool = False
) -> dict[str, Any]:
    """Restore a workflow you made to an earlier version (published as a new version; the draft becomes it).
    Set discard_draft only after the person agreed to lose unpublished draft changes. A person approves each call."""
    return await _gated(ctx, restore_workflow_version_sync, id, version, discard_draft)


async def _archive_workflow(ctx: RunContext[RunDeps], id: str) -> dict[str, Any]:
    """Archive a workflow you made; its active schedules are paused. A person approves each call."""
    return await _gated(ctx, archive_workflow_sync, id)


async def _set_workflow_schedule(
    ctx: RunContext[RunDeps], workflow_id: str, action: str, name: str | None = None, cron: str | None = None,
    timezone: str = "UTC", input: Any = None, schedule_id: str | None = None,
) -> dict[str, Any]:
    """Create, pause, resume or delete a schedule of a workflow you made (action: create | pause | resume |
    delete). create needs name and cron (5 fields, minute hour day month weekday), optional timezone (IANA) and
    input (an object; for a workflow you made: {"input": "<the text it starts with>"}); the others name the schedule by schedule_id or name. The workflow must have a published version. A
    person approves each call."""
    return await _gated(
        ctx, set_workflow_schedule_sync, workflow_id, action, name=name, cron=cron, timezone=timezone, input=input,
        schedule_id=schedule_id,
    )


_TOOLS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("architect.list_approved_tools", _list_approved_tools),
    ("architect.list_created_agents", _list_created_agents),
    ("architect.list_agent_versions", _list_agent_versions),
    ("architect.list_created_workflows", _list_created_workflows),
    ("architect.list_workflow_versions", _list_workflow_versions),
    ("architect.list_workflow_step_kinds", _list_workflow_step_kinds),
    ("architect.save_agent", _save_agent),
    ("architect.save_workflow", _save_workflow),
)
_GATED: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("architect.publish_agent", _publish_agent),
    ("architect.restore_agent_version", _restore_agent_version),
    ("architect.archive_agent", _archive_agent),
    ("architect.publish_workflow", _publish_workflow),
    ("architect.restore_workflow_version", _restore_workflow_version),
    ("architect.archive_workflow", _archive_workflow),
    ("architect.set_workflow_schedule", _set_workflow_schedule),
)


def register_architect_tools(registry: ToolRegistry) -> None:
    """Register the Architect's tools. The read and draft tools carry no approval; the gated ones are
    listed in the Architect's `approvals.tools` by the seed and carry a pure approval summary (the card
    shows `approval_summary_text` when the worker can read the facts)."""
    for tool_id, func in _TOOLS:
        registry.register(tool_id, func, description=(func.__doc__ or tool_id).strip(), side_effect=False)
    for tool_id, func in _GATED:
        registry.register(
            tool_id,
            func,
            description=(func.__doc__ or tool_id).strip(),
            side_effect=True,
            approval_summary=lambda args, _id=tool_id: _pure_summary(_id, args),
        )


__all__ = [
    "GATED_TOOL_IDS",
    "TOOL_IDS",
    "approval_summary_text",
    "archive_agent",
    "archive_workflow",
    "describe_cron",
    "publish_agent",
    "publish_workflow",
    "register_architect_tools",
    "restore_agent_version",
    "restore_workflow_version",
    "save_agent",
    "save_workflow",
    "set_workflow_schedule",
    "slugify",
]
