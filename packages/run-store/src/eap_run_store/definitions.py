"""Publish, restore and archive of agents and workflows: ONE implementation.

The API routes (eap_api.routes, routes/workflows.py) and the Architect's gated tools
(eap_worker.architect_tools) both call these functions, so what a person can do from Studio and what
the Architect can do after an approval cannot drift apart.

Contract of every function:

- It takes a SQLAlchemy `Connection` (a Session's `connection()`, or `engine.begin()` in a tool),
  the `tenant_id` and the row id, and never commits or rolls back: the caller owns the transaction
  (an exception leaves nothing written once the caller rolls back).
- The definition row is locked first with FOR NO KEY UPDATE (it conflicts with itself, so publish,
  restore and archive run one after the other, but not with the KEY SHARE locks foreign-key checks
  take, so inserts that reference the row are not blocked). A workflow publish or restore then locks
  the agents its steps run FOR SHARE in id order (see workflow_checks.reference_problems), so an
  archive of one of them (FOR NO KEY UPDATE) waits for the publish.
- Another tenant's row reads exactly like a missing one (`DefinitionNotFound`).
- A refusal is a typed `DefinitionError` (eap_run_store.definition_errors) carrying the status and
  body the API has always answered with.
- `created_via` (`architect:<run_id>` or `architect-page`, else None) is stored on the new version.
- `after_lock`, when given, runs once the row lock is held and the archived check passed; the API's
  race tests use it to pause a request inside the lock.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text, update
from sqlalchemy.engine import Connection, Row
from sqlalchemy.exc import IntegrityError

from eap_run_store.agent_checks import check_publishable, draft_hash, publish_warnings, validate_stored_draft
from eap_run_store.definition_errors import (
    AGENT_ARCHIVED,
    BUILTIN_AGENT,
    DefinitionBadRequest,
    DefinitionConflict,
    DefinitionInvalid,
    DefinitionNotFound,
)
from eap_run_store.schedules import WorkflowScheduleModel
from eap_run_store.workflow_checks import (
    agent_step_cost_cap_warnings,
    judge_cost_cap_warnings,
    loop_agent_approval_warnings,
    output_ref_checks,
    reference_problems,
    unpublished_agents,
)
from eap_schemas import AgentSpec, canonical_spec_hash
from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash

AGENT_NOT_PUBLISHED = "agent_not_published"
WORKFLOW_ARCHIVED = "Workflow is archived; unarchive it to publish or restore versions"


@dataclass(frozen=True)
class Published:
    """The new version row (`agent_id` or `workflow_id`, `version`, `spec`, `spec_hash`, `created_at`
    as a datetime, `created_by`, `created_via`, and `restored_from` for an agent restore) and the
    publish warnings `[{code, path, message}]`."""

    version: dict[str, Any]
    warnings: list[dict[str, str]]


@dataclass(frozen=True)
class ArchiveResult:
    """The row after an archive or unarchive; `changed` is False for an idempotent repeat, and
    `paused_schedules` lists the schedule ids a workflow archive switched off."""

    id: str
    name: str
    archived: bool
    builtin: bool
    changed: bool
    paused_schedules: tuple[str, ...] = ()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(spec: Any) -> str:
    return json.dumps(spec if isinstance(spec, dict) else spec.model_dump(mode="json"))


# ---------------------------------------------------------------------------------------- agents


def lock_agent(conn: Connection, tenant_id: str, agent_id: str) -> Row:
    """The tenant's agent row (id, name, draft_spec, archived, builtin, slug) locked FOR NO KEY
    UPDATE, or DefinitionNotFound."""
    row = conn.execute(
        text(
            "SELECT id, name, draft_spec, archived, builtin, slug FROM agents"
            " WHERE id = :id AND tenant_id = :t FOR NO KEY UPDATE"
        ),
        {"id": str(agent_id), "t": tenant_id},
    ).first()
    if row is None:
        raise DefinitionNotFound("Agent not found")
    return row


def _refuse_if_archived_agent(row: Row) -> None:
    if row.archived:
        raise DefinitionConflict(AGENT_ARCHIVED)


def latest_agent_version(conn: Connection, agent_id: str) -> Row | None:
    return conn.execute(
        text("SELECT version, spec_hash FROM agent_versions WHERE agent_id = :id ORDER BY version DESC LIMIT 1"),
        {"id": str(agent_id)},
    ).first()


def _insert_agent_version(
    conn: Connection, tenant_id: str, agent_id: str, version: int, spec: dict[str, Any], spec_hash: str,
    created_by: str, restored_from: int | None, created_via: str | None,
) -> dict[str, Any]:
    try:
        with conn.begin_nested():
            row = conn.execute(
                text(
                    "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at,"
                    " created_by, restored_from, created_via)"
                    " VALUES (:id, :a, :t, :v, CAST(:spec AS jsonb), :h, :now, :by, :rf, :via)"
                    " RETURNING agent_id, version, spec, spec_hash, created_at, created_by, restored_from, created_via"
                ),
                {
                    "id": str(uuid.uuid4()), "a": str(agent_id), "t": tenant_id, "v": version, "spec": _dump(spec),
                    "h": spec_hash, "now": _now(), "by": created_by, "rf": restored_from, "via": created_via,
                },
            ).mappings().one()
    except IntegrityError:
        raise DefinitionConflict("concurrent publish, retry") from None
    return dict(row)


def publish_agent(
    conn: Connection, tenant_id: str, agent_id: str, *, created_by: str, created_via: str | None = None,
    after_lock: Callable[[], None] | None = None,
) -> Published:
    """Publish the agent's draft as the next version (the checks and warnings of
    agent_checks.check_publishable and publish_warnings). 404 unknown, 409 archived, 400 no draft,
    422 a draft that no longer validates or the platform refuses, 409 when the draft equals the
    latest version or a concurrent publish won the number."""
    # Lock the agent row before reading the draft and the latest version, like restore does: a
    # publish and a restore (which may discard the draft) then run one after the other, so a draft a
    # restore discarded can never be published, and "no changes since latest" is judged on the state
    # the other request committed.
    agent = lock_agent(conn, tenant_id, agent_id)
    _refuse_if_archived_agent(agent)
    if after_lock is not None:
        after_lock()
    if agent.draft_spec is None:
        raise DefinitionBadRequest("Agent has no draft spec")

    latest_version = latest_agent_version(conn, agent_id)
    latest = latest_version.version if latest_version is not None else None

    spec = validate_stored_draft(agent.draft_spec)
    check_publishable(conn, tenant_id, spec)
    warnings = publish_warnings(spec)
    new_hash = canonical_spec_hash(spec)
    if latest_version is not None and latest_version.spec_hash == new_hash:
        raise DefinitionConflict(f"no changes since version {latest}")

    version = _insert_agent_version(
        conn, tenant_id, agent_id, (latest or 0) + 1, spec.model_dump(mode="json"), new_hash, created_by, None,
        created_via,
    )
    return Published(version, warnings)


def restore_agent_version(
    conn: Connection, tenant_id: str, agent_id: str, version_num: int, *, created_by: str,
    created_via: str | None = None, discard_draft: bool = False, after_lock: Callable[[], None] | None = None,
) -> Published:
    """Roll back by publishing version `latest + 1` whose spec equals version `version_num`.

    History is never rewritten. The new version carries `restored_from = version_num` and the same
    `spec_hash`, passes the same checks as a publish under today's policy, and the draft becomes the
    restored spec. 409 when the source already is (or equals) the latest version, when the draft has
    unpublished changes and `discard_draft` is not set, or when a concurrent publish won the number.
    """
    # Locked: a concurrent draft edit cannot slip between the dirty-draft check and the overwrite.
    agent = lock_agent(conn, tenant_id, agent_id)
    _refuse_if_archived_agent(agent)
    if after_lock is not None:
        after_lock()

    source = conn.execute(
        text("SELECT version, spec, spec_hash FROM agent_versions WHERE agent_id = :id AND version = :v"),
        {"id": str(agent_id), "v": version_num},
    ).first()
    if source is None:
        raise DefinitionNotFound("Version not found")

    latest = latest_agent_version(conn, agent_id)
    assert latest is not None  # the source exists
    if latest.version == source.version:
        raise DefinitionConflict(f"version {version_num} is already the latest version")
    if latest.spec_hash == source.spec_hash:
        raise DefinitionConflict(
            f"version {version_num} has the same spec as the latest version {latest.version}"
        )
    if not discard_draft and agent.draft_spec is not None and draft_hash(agent.draft_spec) != latest.spec_hash:
        raise DefinitionConflict(
            f"the draft has unpublished changes since version {latest.version}; publish them, "
            "or restore with discard_draft: true to replace the draft"
        )

    try:
        spec = AgentSpec.model_validate(source.spec)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "spec"
        raise DefinitionInvalid(
            f"version {version_num} is no longer a valid agent spec ({where}: {first.get('msg')})"
        ) from None
    check_publishable(conn, tenant_id, spec)

    restored = _insert_agent_version(
        conn, tenant_id, agent_id, latest.version + 1, dict(source.spec), source.spec_hash, created_by,
        source.version, created_via,
    )
    conn.execute(
        text(
            "UPDATE agents SET draft_spec = CAST(:spec AS jsonb), name = :name, updated_at = :now"
            " WHERE id = :id AND tenant_id = :t"
        ),
        {"spec": _dump(dict(source.spec)), "name": spec.name, "now": _now(), "id": str(agent_id), "t": tenant_id},
    )
    return Published(restored, publish_warnings(spec))


def referencing_workflows(conn: Connection, tenant_id: str, agent_id: uuid.UUID | str) -> list[dict[str, Any]]:
    """The tenant's non-archived workflows with a published version that runs `agent_id` as an
    agent step, one entry per workflow: `{workflow_id, workflow_name, versions (ascending),
    latest_version}` (`latest_version` is the workflow's newest version, which a run without
    `version` uses). Steps are a flat list (parallel branches name step ids), so a JSONB
    containment test on `spec->'steps'` finds every agent step. One query."""
    needle = json.dumps([{"type": "agent", "agent_id": str(agent_id)}])
    rows = conn.execute(
        text(
            "SELECT w.id, w.name, array_agg(v.version ORDER BY v.version) AS versions,"
            " (SELECT max(lv.version) FROM workflow_versions lv"
            "   WHERE lv.workflow_id = w.id AND lv.tenant_id = w.tenant_id) AS latest"
            " FROM workflows w JOIN workflow_versions v ON v.workflow_id = w.id AND v.tenant_id = w.tenant_id"
            " WHERE w.tenant_id = :t AND w.archived IS NOT TRUE"
            "   AND v.spec -> 'steps' @> CAST(:needle AS jsonb)"
            " GROUP BY w.id, w.name, w.tenant_id ORDER BY w.name, w.id"
        ),
        {"t": tenant_id, "needle": needle},
    ).fetchall()
    return [
        {"workflow_id": str(r.id), "workflow_name": r.name, "versions": list(r.versions), "latest_version": r.latest}
        for r in rows
    ]


def set_agent_archived(
    conn: Connection, tenant_id: str, agent_id: str, archived: bool, *, force: bool = False
) -> ArchiveResult:
    """Archive or unarchive the agent (idempotent). Locked like publish and restore: a publish that
    already holds the row finishes first, and one that starts after sees the new flag. 409
    `builtin_agent` for a built-in agent; 409 with `workflows` when a published version of a
    non-archived workflow runs it as a step, unless `force`."""
    agent = lock_agent(conn, tenant_id, agent_id)
    if archived and agent.builtin:
        raise DefinitionConflict("a built-in agent cannot be archived", code=BUILTIN_AGENT)
    if archived and not agent.archived and not force:
        workflows = referencing_workflows(conn, tenant_id, agent.id)
        if workflows:
            names = ", ".join(w["workflow_name"] for w in workflows[:5]) + (" …" if len(workflows) > 5 else "")
            raise DefinitionConflict(
                f"agent is a step of {len(workflows)} published workflow(s) ({names}); their runs "
                "would be refused. Remove the step, or archive with force=true",
                workflows=workflows,
            )
    changed = bool(agent.archived) != archived
    if changed:
        conn.execute(
            text("UPDATE agents SET archived = :a, updated_at = :now WHERE id = :id AND tenant_id = :t"),
            {"a": archived, "now": _now(), "id": str(agent_id), "t": tenant_id},
        )
    return ArchiveResult(str(agent.id), agent.name, archived, bool(agent.builtin), changed)


# ------------------------------------------------------------------------------------- workflows


def lock_workflow(conn: Connection, tenant_id: str, workflow_id: str) -> Row:
    """The tenant's workflow row (id, name, draft_spec, archived, slug) locked FOR NO KEY UPDATE, or
    DefinitionNotFound."""
    row = conn.execute(
        text(
            "SELECT id, name, draft_spec, archived, slug FROM workflows"
            " WHERE id = :id AND tenant_id = :t FOR NO KEY UPDATE"
        ),
        {"id": str(workflow_id), "t": tenant_id},
    ).first()
    if row is None:
        raise DefinitionNotFound("Workflow not found")
    return row


def latest_workflow_version(conn: Connection, tenant_id: str, workflow_id: str) -> Row | None:
    return conn.execute(
        text(
            "SELECT version, spec_hash FROM workflow_versions WHERE workflow_id = :id AND tenant_id = :t"
            " ORDER BY version DESC LIMIT 1"
        ),
        {"id": str(workflow_id), "t": tenant_id},
    ).first()


def _insert_workflow_version(
    conn: Connection, tenant_id: str, workflow_id: str, version: int, spec: dict[str, Any], spec_hash: str,
    created_by: str, created_via: str | None,
) -> dict[str, Any]:
    try:
        with conn.begin_nested():
            row = conn.execute(
                text(
                    "INSERT INTO workflow_versions (id, workflow_id, tenant_id, version, spec, spec_hash,"
                    " created_at, created_by, created_via)"
                    " VALUES (:id, :w, :t, :v, CAST(:spec AS jsonb), :h, :now, :by, :via)"
                    " RETURNING workflow_id, version, spec, spec_hash, created_at, created_by, created_via"
                ),
                {
                    "id": str(uuid.uuid4()), "w": str(workflow_id), "t": tenant_id, "v": version,
                    "spec": _dump(spec), "h": spec_hash, "now": _now(), "by": created_by, "via": created_via,
                },
            ).mappings().one()
    except IntegrityError:
        raise DefinitionConflict("concurrent publish, retry") from None
    return dict(row)


def _workflow_problems_and_warnings(
    conn: Connection, tenant_id: str, spec: WorkflowSpec
) -> list[dict[str, str]]:
    """The publish checks (422 naming every problem) and the publish warnings of a workflow spec.
    Locks the agents the spec runs FOR SHARE in id order (see reference_problems)."""
    problems = reference_problems(conn, spec, tenant_id, lock=True)
    ref_problems, warnings = output_ref_checks(conn, spec, tenant_id)
    problems += ref_problems
    warnings += loop_agent_approval_warnings(conn, spec, tenant_id)
    warnings += judge_cost_cap_warnings(spec)
    warnings += agent_step_cost_cap_warnings(conn, spec, tenant_id)
    if problems:
        unpublished = unpublished_agents(conn, spec, tenant_id)
        if unpublished:
            # A draft may name an agent that is still a draft; a published workflow runs the latest published
            # version of each agent, so every one needs a version first.
            raise DefinitionInvalid("; ".join(problems), code=AGENT_NOT_PUBLISHED, agents=unpublished)
        raise DefinitionInvalid("; ".join(problems))
    return warnings


def publish_workflow(
    conn: Connection, tenant_id: str, workflow_id: str, *, created_by: str, created_via: str | None = None,
    after_lock: Callable[[], None] | None = None,
) -> Published:
    """Publish the draft as the next immutable version (409 when it equals the latest one; 409 for an
    archived workflow). The warnings are AC.9 (refs into unpinned or text agents' output), LP.5
    (pinned loop body agents with an approval policy) and LP.16 (`cost_cap_unpriced_model`)."""
    workflow = lock_workflow(conn, tenant_id, workflow_id)
    if workflow.archived:
        raise DefinitionConflict(WORKFLOW_ARCHIVED)
    if after_lock is not None:
        after_lock()
    if workflow.draft_spec is None:
        raise DefinitionBadRequest("Workflow has no draft spec")
    try:
        spec = WorkflowSpec.model_validate(workflow.draft_spec)
    except ValidationError as exc:
        messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
        raise DefinitionInvalid(f"draft spec is invalid: {messages}") from None
    warnings = _workflow_problems_and_warnings(conn, tenant_id, spec)

    spec_hash = canonical_workflow_hash(spec)
    latest = latest_workflow_version(conn, tenant_id, workflow_id)
    if latest is not None and latest.spec_hash == spec_hash:
        raise DefinitionConflict(f"no changes since version {latest.version}")

    version = _insert_workflow_version(
        conn, tenant_id, workflow_id, (latest.version if latest else 0) + 1, spec.model_dump(mode="json"),
        spec_hash, created_by, created_via,
    )
    return Published(version, warnings)


def _workflow_draft_hash(draft_spec: dict | None) -> str | None:
    if draft_spec is None:
        return None
    try:
        return canonical_workflow_hash(WorkflowSpec.model_validate(draft_spec))
    except ValidationError:
        return None


def restore_workflow_version(
    conn: Connection, tenant_id: str, workflow_id: str, version_num: int, *, created_by: str,
    created_via: str | None = None, discard_draft: bool = False, after_lock: Callable[[], None] | None = None,
) -> Published:
    """Roll back by publishing version `latest + 1` whose spec equals version `version_num`, like an
    agent restore: no version row is changed, the spec is re-validated under today's policy (what it
    points at, loop caps), the draft becomes the restored spec. 409 when the source is (or equals)
    the latest version or the draft has unpublished changes and `discard_draft` is not set. Workflow
    versions have no `restored_from` column, so the link is not stored."""
    workflow = lock_workflow(conn, tenant_id, workflow_id)
    if workflow.archived:
        raise DefinitionConflict(WORKFLOW_ARCHIVED)
    if after_lock is not None:
        after_lock()
    source = conn.execute(
        text(
            "SELECT version, spec, spec_hash FROM workflow_versions"
            " WHERE workflow_id = :id AND tenant_id = :t AND version = :v"
        ),
        {"id": str(workflow_id), "t": tenant_id, "v": version_num},
    ).first()
    if source is None:
        raise DefinitionNotFound("Version not found")
    latest = latest_workflow_version(conn, tenant_id, workflow_id)
    assert latest is not None  # the source exists
    if latest.version == source.version:
        raise DefinitionConflict(f"version {version_num} is already the latest version")
    if latest.spec_hash == source.spec_hash:
        raise DefinitionConflict(f"version {version_num} has the same spec as the latest version {latest.version}")
    if (
        not discard_draft
        and workflow.draft_spec is not None
        and _workflow_draft_hash(workflow.draft_spec) != latest.spec_hash
    ):
        raise DefinitionConflict(
            f"the draft has unpublished changes since version {latest.version}; publish them, "
            "or restore with discard_draft: true to replace the draft"
        )
    try:
        spec = WorkflowSpec.model_validate(source.spec)
    except ValidationError as exc:
        messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
        raise DefinitionInvalid(f"version {version_num} is no longer a valid workflow spec: {messages}") from None
    warnings = _workflow_problems_and_warnings(conn, tenant_id, spec)

    restored = _insert_workflow_version(
        conn, tenant_id, workflow_id, latest.version + 1, dict(source.spec), source.spec_hash, created_by,
        created_via,
    )
    conn.execute(
        text(
            "UPDATE workflows SET draft_spec = CAST(:spec AS jsonb), name = :name, updated_at = :now"
            " WHERE id = :id AND tenant_id = :t"
        ),
        {"spec": _dump(dict(source.spec)), "name": spec.name, "now": _now(), "id": str(workflow_id), "t": tenant_id},
    )
    return Published(restored, warnings)


def pause_workflow_schedules(conn: Connection, tenant_id: str, workflow_id: str) -> list[str]:
    """Switch off the workflow's active schedules in the caller's transaction (a pause: `active`
    false, nothing deleted; resume with PATCH active=true). Returns the schedule ids paused."""
    table = WorkflowScheduleModel.__table__
    rows = conn.execute(
        update(table)
        .where(table.c.tenant_id == tenant_id, table.c.workflow_id == str(workflow_id), table.c.active.is_(True))
        .values(active=False, updated_at=_now())
        .returning(table.c.id)
    ).all()
    return sorted(str(r.id) for r in rows)


def set_workflow_archived(
    conn: Connection, tenant_id: str, workflow_id: str, archived: bool
) -> ArchiveResult:
    """Archive or unarchive the workflow (idempotent). An archive pauses the workflow's active
    schedules (listed in `paused_schedules`); an unarchive does not resume them. An archived workflow
    is hidden from the list, refuses new runs and schedules, and cannot be published or restored."""
    workflow = lock_workflow(conn, tenant_id, workflow_id)
    changed = bool(workflow.archived) != archived
    paused: list[str] = []
    if changed:
        conn.execute(
            text("UPDATE workflows SET archived = :a, updated_at = :now WHERE id = :id AND tenant_id = :t"),
            {"a": archived, "now": _now(), "id": str(workflow_id), "t": tenant_id},
        )
    if archived:
        paused = pause_workflow_schedules(conn, tenant_id, workflow_id)
    return ArchiveResult(str(workflow.id), workflow.name, archived, False, changed, tuple(paused))


__all__ = [
    "ArchiveResult",
    "Published",
    "WORKFLOW_ARCHIVED",
    "latest_agent_version",
    "latest_workflow_version",
    "lock_agent",
    "lock_workflow",
    "pause_workflow_schedules",
    "publish_agent",
    "publish_workflow",
    "referencing_workflows",
    "restore_agent_version",
    "restore_workflow_version",
    "set_agent_archived",
    "set_workflow_archived",
]
