"""Agents: drafts, versions, publish, restore, archive and compare (the business logic behind the agent routes).

Every lookup is tenant-scoped: another tenant's agent is reported exactly like an unknown one (`NotFound`).
A write takes the `If-Match` revision as a plain `expected_revision` (None: no comparison); a stale one is
the revision-mismatch `Conflict` (see eap_api.services.revision_conflict). Refusals answer the bodies the
routes always had (`{"detail": <message>}`, see `plain_refusals`); the publish-time policy refuses with the
run store's `DefinitionError`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.agent_grants import AgentGrantService, PublishGrants
from eap_api.services.errors import InvalidInput, NotFound
from eap_api.services.revision_conflict import stale_revision
from eap_api.services.run_filters import plain_refusals
from eap_api.version_diff import compare_specs
from eap_run_store.agent_checks import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    PRICE_UNUSABLE,
    WARNING_COST_CAP_UNPRICED,
    WARNING_REDACTION_FIXED_VALUES,
    _fixed_string_paths,
    _price_status,
    check_publishable,
    cost_cap_warnings as _cost_cap_warnings,
    cost_cap_zero_reason,
    draft_hash,
    publish_warnings,
    validate_stored_draft,
)
from eap_run_store import AuditStore
from eap_run_store.definitions import publish_agent, restore_agent_version, set_agent_archived
from eap_run_store.revisions import RevisionMismatch, bump_row
from eap_schemas import AgentSpec
from eap_schemas.architect import BUILTIN_ONLY_TOOL_PREFIXES
from eap_schemas.tools import ungated_always_gated_tools

# Tool ids of the Architect's management tools: valid registry ids, but only an agent whose row is
# `builtin` may list them (the check is here, on create and patch; the worker re-checks per run).
ARCHITECT_TOOL_PREFIX = BUILTIN_ONLY_TOOL_PREFIXES  # `architect.*` and `platform.*`
TOOL_NOT_ALLOWED = "tool_not_allowed"
BUILTIN_AGENT = "builtin_agent"
APPROVAL_REQUIRED = "approval_required"
DRAFT = "draft"
AGENT_NOT_FOUND = "Agent not found"


class ToolRefusal(InvalidInput):
    """422 for a spec that lists a tool its agent may not (`tool_not_allowed`) or lists an always-gated tool
    without its approval entry (`approval_required`). `detail_object` is the whole body, with `tools`."""

    def __init__(self, message: str, code: str, tools: list[str]) -> None:
        super().__init__(message, code=code, detail_object={"detail": message, "code": code, "tools": tools})
        self.tools = tools


def refuse_ungated(spec: AgentSpec) -> None:
    """`approval_required` when the spec lists an always-gated tool (the Architect's publish, restore, archive
    and schedule tools; `secrets.request`) without its `approvals.tools` entry."""
    ungated = ungated_always_gated_tools(spec.tools, spec.approvals.tools if spec.approvals else None)
    if ungated:
        raise ToolRefusal(f"tools {', '.join(ungated)} must be listed in approvals.tools", APPROVAL_REQUIRED, ungated)


def refuse_architect_tools(spec: AgentSpec, builtin: bool) -> None:
    """`tool_not_allowed` when a non-builtin agent's spec lists an `architect.*` tool; `approval_required` for
    any agent that lists an always-gated tool without its approval entry."""
    if not builtin:
        denied = [t for t in spec.tools if t.startswith(ARCHITECT_TOOL_PREFIX)]
        if denied:
            raise ToolRefusal(f"tools {', '.join(denied)} are reserved for built-in agents", TOOL_NOT_ALLOWED, denied)
    refuse_ungated(spec)


def builtin_role(agent: AgentModel) -> str | None:
    """`architect` for the Architect, `drafter` for its two helpers (slug `*-drafter`), else None."""
    if not agent.builtin:
        return None
    slug = agent.slug or ""
    if slug == "architect":
        return "architect"
    if slug.endswith("-drafter") or slug in ("scorer-suggester", "workflow-explainer"):  # helpers the page hides by default
        return "drafter"
    return None


def latest_spec_summary(latest: AgentVersionModel | None) -> dict | None:
    """What the latest published version runs: version, model, tool count and spec hash (None: draft-only)."""
    if latest is None:
        return None
    spec = latest.spec or {}
    model = spec.get("model") or {}
    return {
        "version": latest.version,
        "model": {"provider": model.get("provider"), "name": model.get("name")},
        "tool_count": len(spec.get("tools") or []) + len(spec.get("mcp_tools") or []),
        "spec_hash": latest.spec_hash,
    }


def agent_to_response(agent: AgentModel, latest: AgentVersionModel | None) -> dict:
    """The agent as the API shows it. `latest` is the newest published version (None: draft-only);
    `draft_dirty` is true only when a version exists and the draft's canonical hash differs from it."""
    return {
        "builtin_role": builtin_role(agent),
        "id": str(agent.id),
        "name": agent.name,
        "draft_spec": agent.draft_spec,
        "latest_version": latest.version if latest is not None else None,
        "latest_spec_summary": latest_spec_summary(latest),
        "draft_dirty": latest is not None and draft_hash(agent.draft_spec) != latest.spec_hash,
        "archived": bool(agent.archived),
        "builtin": bool(agent.builtin),
        "revision": agent.revision,
    }


def version_dict_to_response(version: dict) -> dict:
    """A version row as the shared definition operations return it (see eap_run_store.definitions)."""
    return {
        "agent_id": str(version["agent_id"]),
        "version": version["version"],
        "spec": version["spec"],
        "spec_hash": version["spec_hash"],
        "created_at": version["created_at"].isoformat(),
        "created_by": version["created_by"],
        "published_by": version.get("published_by"),
        "restored_from": version["restored_from"],
        "created_via": version["created_via"],
    }


def version_to_response(version: AgentVersionModel) -> dict:
    return {
        "agent_id": str(version.agent_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "published_by": version.published_by,
        "restored_from": version.restored_from,
        "created_via": version.created_via,
    }


def _latest_version(db: Session, agent_id: str) -> AgentVersionModel | None:
    return (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == agent_id)
        .order_by(AgentVersionModel.version.desc())
        .first()
    )


def _compare_side(db: Session, agent: AgentModel, side: int | str) -> dict:
    if side == DRAFT:
        if agent.draft_spec is None:
            raise NotFound("Agent has no draft spec")
        return {
            "version": DRAFT,
            "spec": agent.draft_spec,
            "spec_hash": draft_hash(agent.draft_spec),
            "created_at": None,
            "created_by": None,
            "restored_from": None,
        }
    version = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent.id), AgentVersionModel.version == side)
        .first()
    )
    if version is None:
        raise NotFound(f"Version {side} not found")
    return {
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "restored_from": version.restored_from,
    }


@plain_refusals
class AgentService(AccessBound):
    """Agents and their versions. Reads return the API shapes; writes return `(shape, revision)` for the ETag."""

    def __init__(self, db: Session, access: Access | None = None) -> None:
        self.db = db
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _require(self, action: str, agent_id: uuid.UUID | str | None) -> None:
        acl.require(self._access, action, "agent", None if agent_id is None else str(agent_id))

    # -- helpers -----------------------------------------------------------------------------------

    def _grants(self) -> AgentGrantService:
        return AgentGrantService(self.db, self._access, AuditStore(self.db.get_bind()) if self._access is not None else None)

    def _run_definition_op(self, op: Callable[[Connection], Any]) -> Any:
        """Run one shared definition operation (eap_run_store.definitions) in the request's transaction:
        commit on success, roll back (releasing the row locks) on a refusal."""
        try:
            result = op(self.db.connection())
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()
        return result

    def tenant_agent(self, tenant_id: str, agent_id: uuid.UUID, *, lock: bool = False) -> AgentModel:
        """The tenant's agent or NotFound (another tenant's agent is reported exactly like an unknown one)."""
        query = self.db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
        if lock:
            # FOR NO KEY UPDATE: conflicts with itself (publish and restore run one after the other) but
            # not with the KEY SHARE locks foreign-key checks take, so run inserts referencing the agent
            # are not blocked for the length of a publish or restore.
            query = query.with_for_update(key_share=True)
        agent = query.first()
        if agent is None:
            raise NotFound(AGENT_NOT_FOUND)
        return agent

    def _shape(self, agent: AgentModel) -> dict:
        return agent_to_response(agent, _latest_version(self.db, str(agent.id)))

    # -- agents ------------------------------------------------------------------------------------

    def create(self, tenant_id: str, spec: AgentSpec) -> tuple[dict, int]:
        """Create an agent with `spec` as its draft."""
        self._require("create", None)
        refuse_architect_tools(spec, builtin=False)
        agent = AgentModel(tenant_id=tenant_id, name=spec.name, draft_spec=spec.model_dump(mode="json"))
        self.db.add(agent)
        self.db.commit()
        self.db.refresh(agent)
        acl.register(self._access, "agent", str(agent.id))  # before the response: the creator owns what it made
        return {**agent_to_response(agent, None), "builtin_role": None}, agent.revision

    def list(self, tenant_id: str, *, archived: bool, include_system: bool) -> list[dict]:
        """The tenant's agents: the active ones, or with `archived` the archived ones only. The Architect's
        drafter helpers are left out unless `include_system`; they stay reachable by id."""
        agents = (
            self.db.query(AgentModel)
            .filter(AgentModel.tenant_id == tenant_id, AgentModel.archived.is_(archived))
            .all()
        )
        agents = [a for a in agents if include_system or builtin_role(a) != "drafter"]
        shown = set(acl.keep(acl.visible(self._access, "agent"), [str(a.id) for a in agents]))  # one engine call
        agents = [a for a in agents if str(a.id) in shown]
        # One query for every agent's newest version (spec included), not one per agent.
        newest = (
            self.db.query(AgentVersionModel.agent_id, func.max(AgentVersionModel.version).label("v"))
            .filter(AgentVersionModel.agent_id.in_([str(a.id) for a in agents]))
            .group_by(AgentVersionModel.agent_id)
            .subquery()
        )
        latest_by_agent = {
            str(row.agent_id): row
            for row in self.db.query(AgentVersionModel).join(
                newest, (AgentVersionModel.agent_id == newest.c.agent_id) & (AgentVersionModel.version == newest.c.v)
            )
        }
        return [agent_to_response(agent, latest_by_agent.get(str(agent.id))) for agent in agents]

    def get(self, tenant_id: str, agent_id: uuid.UUID) -> tuple[dict, int]:
        self._require("read", agent_id)
        agent = self.tenant_agent(tenant_id, agent_id)
        return self._shape(agent), agent.revision

    def patch(
        self, tenant_id: str, agent_id: uuid.UUID, spec: AgentSpec, expected_revision: int | None
    ) -> tuple[dict, int]:
        """Replace the draft spec. A stale `expected_revision` is the revision-mismatch Conflict with the
        current agent."""
        self._require("edit", agent_id)
        agent = self.tenant_agent(tenant_id, agent_id)

        def current() -> tuple[dict, int]:
            self.db.refresh(agent)
            return self._shape(agent), agent.revision

        if expected_revision is not None and agent.revision != expected_revision:
            raise stale_revision(expected_revision, *current())
        refuse_architect_tools(spec, builtin=bool(agent.builtin))
        try:
            bump_row(self.db, agent, expected_revision)
            agent.name = spec.name
            agent.draft_spec = spec.model_dump(mode="json")
            self.db.commit()
        except RevisionMismatch:
            self.db.rollback()
            raise stale_revision(expected_revision, *current()) from None
        self.db.refresh(agent)
        return self._shape(agent), agent.revision

    def set_archived(
        self, tenant_id: str, agent_id: uuid.UUID, archived: bool, force: bool = False
    ) -> tuple[dict, int]:
        """eap_run_store.definitions.set_agent_archived: locked like publish and restore, 409 for a built-in
        agent or when published workflows run it (unless force)."""
        self._require("archive", agent_id)
        self._run_definition_op(
            lambda conn: set_agent_archived(conn, tenant_id, str(agent_id), archived, force=force)
        )
        agent = self.tenant_agent(tenant_id, agent_id)
        return self._shape(agent), agent.revision

    # -- versions ----------------------------------------------------------------------------------

    def publish(self, tenant_id: str, user_id: str, agent_id: uuid.UUID) -> tuple[dict, int]:
        """Publish the draft as a new version: the version and `warnings` (publish-time advice that does not
        block). The work is eap_run_store.definitions.publish_agent, shared with the Architect's publish tool."""
        self._require("publish", agent_id)
        grants = self._grants()
        plan = grants.plan("agent", str(agent_id), self.tenant_agent(tenant_id, agent_id).draft_spec)
        grants.require_approvable(plan)  # newly used servers and secrets: an admin approves, and approving grants them
        result = self._run_definition_op(
            lambda conn: publish_agent(
                conn, tenant_id, str(agent_id), created_by=user_id,
                after_lock=lambda: _latest_version(self.db, str(agent_id)),
            )
        )
        grants.apply(plan, f"published version {result.version['version']}")
        return {**version_dict_to_response(result.version), "warnings": result.warnings}, result.revision

    def restore(
        self, tenant_id: str, user_id: str, agent_id: uuid.UUID, version_num: int, discard_draft: bool
    ) -> tuple[dict, int]:
        """Roll back by publishing version `latest + 1` whose spec equals version `version_num` (history is
        never rewritten). The work is eap_run_store.definitions.restore_agent_version."""
        self._require("publish", agent_id)  # a restore publishes a new version
        grants = self._grants()
        old = (
            self.db.query(AgentVersionModel)
            .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version_num).first()
        )
        plan = grants.plan("agent", str(agent_id), None if old is None else old.spec)
        grants.require_approvable(plan)
        result = self._run_definition_op(
            lambda conn: restore_agent_version(
                conn, tenant_id, str(agent_id), version_num, created_by=user_id,
                discard_draft=discard_draft, after_lock=lambda: _latest_version(self.db, str(agent_id)),
            )
        )
        grants.apply(plan, f"restored version {version_num} as version {result.version['version']}")
        return {**version_dict_to_response(result.version), "warnings": result.warnings}, result.revision

    def publish_grants(self, tenant_id: str, agent_id: uuid.UUID) -> dict:
        """What publishing the draft would newly grant (the publish card's line), and whether the caller may approve it."""
        self._require("publish", agent_id)
        plan = self._grants().plan("agent", str(agent_id), self.tenant_agent(tenant_id, agent_id).draft_spec)
        return (plan or PublishGrants("agent", str(agent_id))).to_json()

    def access_grants(self, tenant_id: str) -> AgentGrantService:
        return self._grants()

    def access_status(self, tenant_id: str, agent_id: uuid.UUID) -> dict:
        """The Access section: what this agent is authorised to use (docs/iam.md, "Agent and workflow grants")."""
        self._require("read", agent_id)
        self.tenant_agent(tenant_id, agent_id)
        latest = _latest_version(self.db, str(agent_id))
        return self._grants().status("agent", str(agent_id), None if latest is None else latest.spec)

    def list_versions(self, tenant_id: str, agent_id: uuid.UUID) -> list[dict]:
        """Versions of an agent, newest first."""
        self._require("read", agent_id)
        self.tenant_agent(tenant_id, agent_id)
        versions = (
            self.db.query(AgentVersionModel)
            .filter(AgentVersionModel.agent_id == str(agent_id))
            .order_by(AgentVersionModel.version.desc())
            .all()
        )
        return [version_to_response(v) for v in versions]

    def get_version(self, tenant_id: str, agent_id: uuid.UUID, version_num: int) -> dict:
        self._require("read", agent_id)
        self.tenant_agent(tenant_id, agent_id)
        version = (
            self.db.query(AgentVersionModel)
            .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version_num)
            .first()
        )
        if version is None:
            raise NotFound("Version not found")
        return version_to_response(version)

    def compare(self, tenant_id: str, agent_id: uuid.UUID, base: int | str, target: int | str) -> dict:
        """Two versions, or a version and the draft (a side is a version number or `"draft"`), with the
        field-level `changes` and the instructions' line diff. Read-only."""
        self._require("read", agent_id)
        agent = self.tenant_agent(tenant_id, agent_id)
        base_side = _compare_side(self.db, agent, base)
        target_side = _compare_side(self.db, agent, target)
        diff = compare_specs(base_side["spec"], target_side["spec"])
        hashes = (base_side["spec_hash"], target_side["spec_hash"])
        identical = hashes[0] == hashes[1] if None not in hashes else not diff["changes"]
        return {
            "agent_id": str(agent.id),
            "base": base_side,
            "target": target_side,
            "identical": identical,
            **diff,
        }
