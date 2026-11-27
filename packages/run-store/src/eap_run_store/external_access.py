"""External agents, inbound (0043_external_access): external clients, workflow exports, A2A endpoints, the external run
claims and `ExternalAccessStore`; plus a workflow version's external contract (what a caller sends and what happens
outside), which the publish operations check.

All SQL of the feature lives here. Every read and write is scoped by `tenant_id` except the sweep reads
(`overdue_runs`, `unstarted_runs`, `close_ended_claims`, `delete_dead_claims`), which the worker runs across tenants,
and `find_endpoint`, which reads an A2A endpoint by its random public id (the public Agent Card has no caller).
Design: an internal design note; operator doc: docs/external-access.md.

A claim (`external_runs`) is written BEFORE its run row, so a crash between the two is repaired by the caller's retry:
the same idempotency key finds the same claim, hence the same run id and the same workflow version. `closed_at` takes a
claim whose run has ended out of the quota count and the sweep; the count joins `runs` as well, so a run that ended and is
not closed yet does not hold a slot.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflows import AgentStep, ApprovalStep, InputStep, ToolStep, WorkflowSpec, walk_steps
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from eap_run_store.cursor import RunCursor
from eap_run_store.revisions import bump_row
from eap_run_store.store import (
    CANCEL_EVENT_KEY,
    END_EVENT_KEY,
    ENQUEUE_FAILED_ERROR,
    RUN_CANCELLED_CODE,
    TERMINAL_RUN_STATUSES,
    Base,
    ExecutionEventModel,
    RunModel,
    append_event_in_session,
)
from eap_run_store.approvals import open_approval_exists

PROTOCOLS = ("mcp", "a2a")
RESULT_MODES = ("output", "status_only")
RUN_STATES = ("queued", "running", "input_required", "completed", "failed", "cancelled")
DEADLINE_REASON = "deadline_exceeded"
# Who the cancel request of an overdue run names; a client's own cancel names its key.
DEADLINE_CANCELLER = "system:external-deadline"
CONTRACT_CHANGED = "export_contract_changed"
WARNING_SIDE_EFFECTS = "external_side_effects_without_approval"
WARNING_PROMPT_INPUT = "external_input_into_agent_prompt"
_MAX_LIST = 100

_AGENT_VERSION = text(
    "SELECT v.spec FROM agent_versions v JOIN agents a ON a.id = v.agent_id AND a.tenant_id = v.tenant_id"
    " WHERE v.tenant_id = :t AND v.agent_id = :a AND (CAST(:v AS integer) IS NULL OR v.version = :v)"
    " ORDER BY v.version DESC LIMIT 1"
)


# ------------------------------------------------------------------------------------------------ the contract


@dataclass(frozen=True)
class WorkflowEffects:
    """What running a workflow version does outside the platform, and whether a person stands in the way."""

    side_effects: tuple[str, ...]  # sorted, e.g. "tool:http.request", "agent:<id>:mcp:<spec>"
    has_gate: bool  # an approval or input step
    input_into_prompt: bool  # an agent step's input references the run input


@dataclass(frozen=True)
class WorkflowContract(WorkflowEffects):
    input_schema: dict[str, Any]
    hash: str  # sha256 of the input schema and the side-effect set


def _agent_side_effects(conn: Connection, tenant_id: str, agent_id: str, version: int | None) -> list[str]:
    row = conn.execute(_AGENT_VERSION, {"t": tenant_id, "a": agent_id, "v": version}).first()
    spec = (row[0] if row is not None else None) or {}
    found = [f"agent:{agent_id}:mcp:{json.dumps(t, sort_keys=True)}" for t in spec.get("mcp_tools") or ()]
    for tool_id in spec.get("tools") or ():
        declared = TOOL_DECLARATIONS.get(tool_id) if isinstance(tool_id, str) else None
        if declared is not None and declared.side_effect:
            found.append(f"agent:{agent_id}:tool:{tool_id}")
    return found


def workflow_effects(conn: Connection, tenant_id: str, spec: WorkflowSpec) -> WorkflowEffects:
    """The effects of `spec` at every depth, agent steps by their pinned (else latest) published version."""
    side_effects: set[str] = set()
    has_gate = input_into_prompt = False
    for _scope, step in walk_steps(spec.steps):
        if isinstance(step, (ApprovalStep, InputStep)):
            has_gate = True
        elif isinstance(step, ToolStep):
            declared = TOOL_DECLARATIONS.get(step.tool_id)
            if declared is not None and declared.side_effect:
                side_effects.add(f"tool:{step.tool_id}")
        elif isinstance(step, AgentStep):
            side_effects.update(_agent_side_effects(conn, tenant_id, str(step.agent_id), step.version))
            if "input." in json.dumps(step.input):
                input_into_prompt = True
    return WorkflowEffects(tuple(sorted(side_effects)), has_gate, input_into_prompt)


def workflow_contract(conn: Connection, tenant_id: str, spec: WorkflowSpec) -> WorkflowContract:
    effects = workflow_effects(conn, tenant_id, spec)
    material = json.dumps(
        {"input_schema": spec.input_schema, "side_effects": list(effects.side_effects)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return WorkflowContract(
        effects.side_effects, effects.has_gate, effects.input_into_prompt, dict(spec.input_schema),
        hashlib.sha256(material.encode()).hexdigest(),
    )


def external_warnings(effects: WorkflowEffects) -> list[dict[str, str]]:
    """The warnings of an exported workflow: the inbound webhook warnings, worded for an external agent."""
    out: list[dict[str, str]] = []
    if effects.side_effects and not effects.has_gate:
        out.append({"code": WARNING_SIDE_EFFECTS, "message": (
            "this workflow can be started by an external agent and has side effects without a person approving them; "
            "the agent's input can steer an agent step (prompt injection)"
        )})
    if effects.input_into_prompt:
        out.append({"code": WARNING_PROMPT_INPUT, "message": (
            "an external agent's input reaches an agent step's prompt: untrusted text can steer a model"
        )})
    return out


def on_workflow_published(conn: Connection, tenant_id: str, workflow_id: str, spec: WorkflowSpec) -> list[dict[str, str]]:
    """Run inside a publish (or restore) transaction. An enabled export that follows the latest version is disabled
    (`export_contract_changed`) when the new version's input schema or side-effect set differs from the approved one.
    Returns the publish warnings of an exported workflow; none for a workflow nothing exports."""
    exported = conn.execute(
        text("SELECT 1 FROM workflow_exports WHERE tenant_id = :t AND workflow_id = :w LIMIT 1"),
        {"t": tenant_id, "w": str(workflow_id)},
    ).first()
    if exported is None:
        return []
    contract = workflow_contract(conn, tenant_id, spec)
    conn.execute(
        text(
            "UPDATE workflow_exports SET enabled = false, disabled_reason = :r, approved_hash = NULL,"
            " approved_contract_hash = NULL, enabled_by = NULL, enabled_at = NULL, updated_at = now(),"
            " revision = revision + 1"
            " WHERE tenant_id = :t AND workflow_id = :w AND enabled AND workflow_version IS NULL"
            " AND approved_contract_hash <> :h"
        ),
        {"t": tenant_id, "w": str(workflow_id), "h": contract.hash, "r": CONTRACT_CHANGED},
    )
    return external_warnings(contract)


# ------------------------------------------------------------------------------------------------ tables


class ExternalClientModel(Base):
    __tablename__ = "external_clients"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    description: Mapped[str] = mapped_column("description", String, nullable=False, server_default=text("''"))
    enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, server_default=text("true"))
    max_active_runs: Mapped[int | None] = mapped_column("max_active_runs", Integer, nullable=True)
    max_runs_per_day: Mapped[int | None] = mapped_column("max_runs_per_day", Integer, nullable=True)
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_external_clients_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_external_clients_tenant_name"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="ck_external_clients_name"),
    )


class WorkflowExportModel(Base):
    __tablename__ = "workflow_exports"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    title: Mapped[str] = mapped_column("title", String, nullable=False)
    description: Mapped[str] = mapped_column("description", String, nullable=False, server_default=text("''"))
    tags: Mapped[Any] = mapped_column("tags", JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    examples: Mapped[Any] = mapped_column("examples", JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    workflow_id: Mapped[str] = mapped_column("workflow_id", String, nullable=False)
    workflow_version: Mapped[int | None] = mapped_column("workflow_version", Integer, nullable=True)
    protocols: Mapped[Any] = mapped_column("protocols", JSONB, nullable=False)
    result_mode: Mapped[str] = mapped_column("result_mode", String, nullable=False, server_default=text("'output'"))
    client_inputs: Mapped[bool] = mapped_column("client_inputs", Boolean, nullable=False, server_default=text("false"))
    max_runtime_seconds: Mapped[int] = mapped_column("max_runtime_seconds", Integer, nullable=False, server_default=text("3600"))
    require_idempotency_key: Mapped[bool] = mapped_column("require_idempotency_key", Boolean, nullable=False, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, default=False, server_default=text("false"))
    disabled_reason: Mapped[str | None] = mapped_column("disabled_reason", String, nullable=True)
    config_hash: Mapped[str] = mapped_column("config_hash", String, nullable=False)
    approved_hash: Mapped[str | None] = mapped_column("approved_hash", String, nullable=True)
    approved_contract_hash: Mapped[str | None] = mapped_column("approved_contract_hash", String, nullable=True)
    enabled_by: Mapped[str | None] = mapped_column("enabled_by", String, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column("enabled_at", DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_workflow_exports_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_workflow_exports_tenant_name"),
        # The composite foreign key to `workflows` (fk_workflow_exports_workflow, migration 0043) is added by
        # eap_db.metadata: that table's model belongs to the API's metadata, which this package does not import.
        CheckConstraint("name ~ '^[a-z][a-z0-9_]{0,47}$'", name="ck_workflow_exports_name"),
        CheckConstraint("result_mode IN ('output', 'status_only')", name="ck_workflow_exports_result_mode"),
        Index("ix_workflow_exports_workflow", "tenant_id", "workflow_id"),
    )


class ExternalEndpointModel(Base):
    """The A2A endpoint of a tenant: the random public id in its URL, what its public Agent Card says, and whether it is
    served."""

    __tablename__ = "external_endpoints"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, primary_key=True)
    endpoint_id: Mapped[str] = mapped_column("endpoint_id", String, nullable=False)
    card_name: Mapped[str] = mapped_column("card_name", String, nullable=False, server_default=text("''"))
    card_description: Mapped[str] = mapped_column("card_description", String, nullable=False, server_default=text("''"))
    enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (UniqueConstraint("endpoint_id", name="uq_external_endpoints_endpoint_id"),)


class ExternalRunModel(Base):
    __tablename__ = "external_runs"

    run_id: Mapped[str] = mapped_column("run_id", String, primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    client_id: Mapped[str] = mapped_column("client_id", PG_UUID(as_uuid=False), nullable=False)
    key_id: Mapped[str] = mapped_column("key_id", String, nullable=False)
    export_id: Mapped[str] = mapped_column("export_id", PG_UUID(as_uuid=False), nullable=False)
    protocol: Mapped[str] = mapped_column("protocol", String, nullable=False)
    idempotency_hash: Mapped[str | None] = mapped_column("idempotency_hash", String, nullable=True)
    input_hash: Mapped[str] = mapped_column("input_hash", String, nullable=False)
    workflow_version: Mapped[int] = mapped_column("workflow_version", Integer, nullable=False)
    context_label: Mapped[str | None] = mapped_column("context_label", String, nullable=True)
    deadline_at: Mapped[datetime] = mapped_column("deadline_at", DateTime(timezone=True), nullable=False)
    cancel_reason: Mapped[str | None] = mapped_column("cancel_reason", String, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column("closed_at", DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "client_id"], ["external_clients.tenant_id", "external_clients.id"], name="fk_external_runs_client"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "export_id"], ["workflow_exports.tenant_id", "workflow_exports.id"], name="fk_external_runs_export"
        ),
        CheckConstraint("protocol IN ('mcp', 'a2a')", name="ck_external_runs_protocol"),
        Index(
            "uq_external_runs_idempotency", "tenant_id", "client_id", "export_id", "idempotency_hash", unique=True,
            postgresql_where=text("idempotency_hash IS NOT NULL"),
        ),
        Index("ix_external_runs_client_created", "tenant_id", "client_id", "created_at"),
        Index("ix_external_runs_open", "tenant_id", "client_id", postgresql_where=text("closed_at IS NULL")),
        Index("ix_external_runs_deadline", "deadline_at", postgresql_where=text("closed_at IS NULL")),
    )


# ------------------------------------------------------------------------------------------------ refusals


class ExternalNameConflict(Exception):
    """The tenant already has a client (or an export) with this name."""


class IdempotencyConflict(Exception):
    """The idempotency key was used with another input."""


class ExternalQuotaExceeded(Exception):
    """A run would exceed a quota: `quota` is `client_active`, `tenant_active` or `client_per_day`."""

    def __init__(self, quota: str, limit: int, retry_after: int):
        super().__init__(quota, limit, retry_after)
        self.quota = quota
        self.limit = limit
        self.retry_after = retry_after


@dataclass(frozen=True)
class ClaimLimits:
    client_active: int
    tenant_active: int
    client_per_day: int


@dataclass(frozen=True)
class Claim:
    row: dict[str, Any]
    created: bool  # False: a replay of this key


def _dict(model: Any) -> dict[str, Any]:
    return {c.key: getattr(model, c.key) for c in model.__table__.columns}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _terminal(column: Any) -> Any:
    return column.in_(TERMINAL_RUN_STATUSES)


def _run_join() -> Any:
    return and_(RunModel.tenant_id == ExternalRunModel.tenant_id, RunModel.id == ExternalRunModel.run_id)


def _end_error_code() -> Any:
    return (
        select(ExecutionEventModel.data["error_code"].astext)
        .where(
            ExecutionEventModel.tenant_id == RunModel.tenant_id,
            ExecutionEventModel.run_id == RunModel.id,
            ExecutionEventModel.event_key == END_EVENT_KEY,
        )
        .correlate(RunModel)
        .scalar_subquery()
    )


def _state_clause(state: str) -> Any:
    """The SQL of one external state (RUN_STATES), over external_runs left-joined to runs."""
    waiting = open_approval_exists()
    cancelled = and_(
        RunModel.status == "failed",
        func.coalesce(_end_error_code(), "") == RUN_CANCELLED_CODE,
        ExternalRunModel.cancel_reason.is_(None),
    )
    if state == "queued":
        return or_(RunModel.id.is_(None), RunModel.status == "queued")
    if state == "running":
        return and_(RunModel.status == "running", ~waiting)
    if state == "input_required":
        return and_(RunModel.status == "running", waiting)
    if state == "completed":
        return RunModel.status.in_(("completed", "skipped"))
    if state == "cancelled":
        return cancelled
    if state == "failed":
        return or_(RunModel.status == "needs_attention", and_(RunModel.status == "failed", ~cancelled))
    raise ValueError(f"unknown state {state!r}")


class ExternalAccessStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    # -------------------------------------------------------------------------------------------- clients

    def create_client(
        self, tenant_id: str, actor: str, *, name: str, description: str = "",
        max_active_runs: int | None = None, max_runs_per_day: int | None = None,
    ) -> dict[str, Any]:
        row = ExternalClientModel(
            id=str(uuid.uuid4()), tenant_id=tenant_id, name=name, description=description, enabled=True,
            max_active_runs=max_active_runs, max_runs_per_day=max_runs_per_day, created_by=actor,
        )
        return self._insert(row)

    def _insert(self, row: Any) -> dict[str, Any]:
        with self.Session() as session:
            try:
                session.add(row)
                session.flush()
                session.refresh(row)
                found = _dict(row)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "tenant_name" in str(exc.orig):
                    raise ExternalNameConflict() from None
                raise
        return found

    def _get(self, model: Any, tenant_id: str, row_id: str) -> dict[str, Any] | None:
        try:
            row_id = str(uuid.UUID(str(row_id)))
        except ValueError:
            return None
        with self.Session() as session:
            row = session.execute(select(model).where(model.tenant_id == tenant_id, model.id == row_id)).scalar_one_or_none()
            return None if row is None else _dict(row)

    def _list(self, model: Any, tenant_id: str) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.execute(select(model).where(model.tenant_id == tenant_id).order_by(model.name)).scalars()
            return [_dict(r) for r in rows]

    def _update(
        self, model: Any, tenant_id: str, row_id: str, changes: dict[str, Any], expected_revision: int | None,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        """RevisionMismatch when `expected_revision` is stale; None when the tenant has no such row."""
        with self.Session() as session:
            row = session.execute(
                select(model).where(model.tenant_id == tenant_id, model.id == str(row_id)).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return None
            bump_row(session, row, expected_revision)
            for key, value in {**changes, **state}.items():
                setattr(row, key, value)
            row.updated_at = _now()
            try:
                session.flush()
                found = _dict(row)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "tenant_name" in str(exc.orig):
                    raise ExternalNameConflict() from None
                raise
        return found

    def get_client(self, tenant_id: str, client_id: str) -> dict[str, Any] | None:
        return self._get(ExternalClientModel, tenant_id, client_id)

    def list_clients(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._list(ExternalClientModel, tenant_id)

    def update_client(
        self, tenant_id: str, client_id: str, changes: dict[str, Any], expected_revision: int | None
    ) -> dict[str, Any] | None:
        return self._update(ExternalClientModel, tenant_id, client_id, changes, expected_revision, {})

    # -------------------------------------------------------------------------------------------- exports

    def create_export(self, tenant_id: str, actor: str, values: dict[str, Any]) -> dict[str, Any]:
        return self._insert(WorkflowExportModel(id=str(uuid.uuid4()), tenant_id=tenant_id, created_by=actor, enabled=False, **values))

    def get_export(self, tenant_id: str, export_id: str) -> dict[str, Any] | None:
        return self._get(WorkflowExportModel, tenant_id, export_id)

    def get_export_by_name(self, tenant_id: str, name: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.execute(
                select(WorkflowExportModel).where(WorkflowExportModel.tenant_id == tenant_id, WorkflowExportModel.name == name)
            ).scalar_one_or_none()
            return None if row is None else _dict(row)

    def list_exports(self, tenant_id: str) -> list[dict[str, Any]]:
        return self._list(WorkflowExportModel, tenant_id)

    def update_export(
        self, tenant_id: str, export_id: str, changes: dict[str, Any], expected_revision: int | None, **state: Any
    ) -> dict[str, Any] | None:
        return self._update(WorkflowExportModel, tenant_id, export_id, changes, expected_revision, state)

    # -------------------------------------------------------------------------------------------- A2A endpoints

    def get_endpoint(self, tenant_id: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.get(ExternalEndpointModel, tenant_id)
            return None if row is None else _dict(row)

    def find_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        """The endpoint of a public id, whichever tenant's: for the unauthenticated Agent Card only."""
        with self.Session() as session:
            row = session.execute(
                select(ExternalEndpointModel).where(ExternalEndpointModel.endpoint_id == endpoint_id)
            ).scalar_one_or_none()
            return None if row is None else _dict(row)

    def save_endpoint(
        self, tenant_id: str, actor: str, *, card_name: str, card_description: str, enabled: bool
    ) -> dict[str, Any]:
        """Create the tenant's endpoint (with a new random 128-bit id) or change what it says; the id stays."""
        values = {"card_name": card_name, "card_description": card_description, "enabled": enabled}
        with self.Session() as session, session.begin():
            session.execute(
                insert(ExternalEndpointModel)
                .values(tenant_id=tenant_id, endpoint_id=secrets.token_hex(16), created_by=actor, **values)
                .on_conflict_do_update(index_elements=["tenant_id"], set_={**values, "updated_at": func.now()})
            )
            return _dict(session.get(ExternalEndpointModel, tenant_id))

    def rotate_endpoint(self, tenant_id: str) -> dict[str, Any] | None:
        """A new public id: the old URL stops answering. None when the tenant has no endpoint."""
        with self.Session() as session, session.begin():
            row = session.get(ExternalEndpointModel, tenant_id, with_for_update=True)
            if row is None:
                return None
            row.endpoint_id = secrets.token_hex(16)
            row.updated_at = _now()
            session.flush()
            return _dict(row)

    # -------------------------------------------------------------------------------------------- claims

    @staticmethod
    def _active_counts(session: Session, tenant_id: str, client_id: str, exclude: str) -> tuple[int, int]:
        """(this client's, the tenant's) claims that hold a slot: the run has not ended, or no run exists yet and the
        claim's deadline is ahead. `exclude` is the claim being (re)started."""
        row = session.execute(
            select(
                func.count().filter(ExternalRunModel.client_id == client_id),
                func.count(),
            )
            .select_from(ExternalRunModel)
            .outerjoin(RunModel, _run_join())
            .where(
                ExternalRunModel.tenant_id == tenant_id,
                ExternalRunModel.closed_at.is_(None),
                ExternalRunModel.run_id != exclude,
                or_(
                    and_(RunModel.id.is_(None), ExternalRunModel.deadline_at > func.now()),
                    and_(RunModel.id.is_not(None), ~_terminal(RunModel.status)),
                ),
            )
        ).one()
        return int(row[0]), int(row[1])

    def _require_slot(self, session: Session, tenant_id: str, client_id: str, run_id: str, limits: ClaimLimits) -> None:
        mine, tenant = self._active_counts(session, tenant_id, client_id, run_id)
        if mine >= limits.client_active:
            raise ExternalQuotaExceeded("client_active", limits.client_active, 30)
        if tenant >= limits.tenant_active:
            raise ExternalQuotaExceeded("tenant_active", limits.tenant_active, 30)

    def find_claim(
        self, tenant_id: str, client_id: str, export_id: str, idempotency_hash: str
    ) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.execute(
                select(ExternalRunModel).where(
                    ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id,
                    ExternalRunModel.export_id == export_id, ExternalRunModel.idempotency_hash == idempotency_hash,
                )
            ).scalar_one_or_none()
            return None if row is None else _dict(row)

    def claimed_exports(self, tenant_id: str, client_id: str, protocol: str, idempotency_hash: str) -> list[str]:
        """The names of the exports this client started over `protocol` with this idempotency key."""
        with self.Session() as session:
            return list(session.execute(
                select(WorkflowExportModel.name)
                .join(ExternalRunModel, and_(
                    ExternalRunModel.tenant_id == WorkflowExportModel.tenant_id,
                    ExternalRunModel.export_id == WorkflowExportModel.id,
                ))
                .where(
                    ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id,
                    ExternalRunModel.protocol == protocol, ExternalRunModel.idempotency_hash == idempotency_hash,
                )
                .order_by(WorkflowExportModel.name)
            ).scalars())

    def claim_run(
        self, tenant_id: str, *, client_id: str, key_id: str, export_id: str, protocol: str,
        idempotency_hash: str | None, input_hash: str, run_id: str, workflow_version: int, deadline_at: datetime,
        context_label: str | None, limits: ClaimLimits,
    ) -> Claim:
        """One transaction under a per-tenant advisory lock: the claim of (client, export, key), made new when the key
        is unknown (or absent) and the quotas allow it. A known key returns its claim (IdempotencyConflict for another
        input); when its run never started (no run row, or the hand-off to the worker failed) it takes a slot again,
        with a new deadline if the old one passed. ExternalQuotaExceeded writes nothing."""
        with self.Session() as session, session.begin():
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"external_runs:{tenant_id}"}
            )
            existing = None
            if idempotency_hash is not None:
                existing = session.execute(
                    select(ExternalRunModel).where(
                        ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id,
                        ExternalRunModel.export_id == export_id,
                        ExternalRunModel.idempotency_hash == idempotency_hash,
                    )
                ).scalar_one_or_none()
            if existing is not None:
                if existing.input_hash != input_hash:
                    raise IdempotencyConflict()
                run = session.execute(
                    select(RunModel.status, RunModel.error).where(
                        RunModel.tenant_id == tenant_id, RunModel.id == existing.run_id
                    )
                ).first()
                if run is None or (run.status == "failed" and run.error == ENQUEUE_FAILED_ERROR):
                    self._require_slot(session, tenant_id, client_id, existing.run_id, limits)
                    existing.closed_at = None
                    if existing.deadline_at <= _now():
                        existing.deadline_at = deadline_at
                    session.flush()
                return Claim(_dict(existing), False)
            self._require_slot(session, tenant_id, client_id, run_id, limits)
            today = session.execute(
                select(func.count()).select_from(ExternalRunModel).where(
                    ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id,
                    ExternalRunModel.created_at > func.now() - text("interval '24 hours'"),
                )
            ).scalar_one()
            if today >= limits.client_per_day:
                raise ExternalQuotaExceeded("client_per_day", limits.client_per_day, 3600)
            row = ExternalRunModel(
                run_id=run_id, tenant_id=tenant_id, client_id=client_id, key_id=key_id, export_id=export_id,
                protocol=protocol, idempotency_hash=idempotency_hash, input_hash=input_hash,
                workflow_version=workflow_version, deadline_at=deadline_at, context_label=context_label,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            return Claim(_dict(row), True)

    def release_claim(self, tenant_id: str, run_id: str) -> bool:
        """Delete a claim whose run row was never written (the start was refused). False when the run exists."""
        with self.Session() as session, session.begin():
            deleted = session.execute(
                text(
                    "DELETE FROM external_runs e WHERE e.tenant_id = :t AND e.run_id = :r"
                    " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.tenant_id = e.tenant_id AND r.id = e.run_id)"
                ),
                {"t": tenant_id, "r": run_id},
            )
            return deleted.rowcount > 0

    # -------------------------------------------------------------------------------------------- one client's runs

    @staticmethod
    def _run_query(tenant_id: str, client_id: str) -> Any:
        changed = func.coalesce(RunModel.updated_at, ExternalRunModel.created_at)
        return (
            select(
                ExternalRunModel,
                WorkflowExportModel.name.label("export_name"),
                WorkflowExportModel.result_mode,
                WorkflowExportModel.workflow_id,
                RunModel.status,
                RunModel.error,
                RunModel.output,
                RunModel.output_format,
                changed.label("changed_at"),
                open_approval_exists().label("waiting_approval"),
                _end_error_code().label("error_code"),
            )
            .join(
                WorkflowExportModel,
                and_(WorkflowExportModel.tenant_id == ExternalRunModel.tenant_id,
                     WorkflowExportModel.id == ExternalRunModel.export_id),
            )
            .outerjoin(RunModel, _run_join())
            .where(ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id)
        )

    @staticmethod
    def _run_row(found: Any) -> dict[str, Any]:
        return {
            **_dict(found[0]),
            "export_name": found.export_name, "result_mode": found.result_mode, "workflow_id": found.workflow_id,
            "status": found.status, "error": found.error, "output": found.output, "output_format": found.output_format,
            "changed_at": found.changed_at,
            "waiting_approval": bool(found.waiting_approval), "error_code": found.error_code,
        }

    def get_external_run(self, tenant_id: str, client_id: str, run_id: str) -> dict[str, Any] | None:
        """The client's run, or None for any other id (another client's, a person's run, another tenant's): the claim
        is read before anything of the run."""
        with self.Session() as session:
            found = session.execute(
                self._run_query(tenant_id, client_id).where(ExternalRunModel.run_id == str(run_id))
            ).first()
            return None if found is None else self._run_row(found)

    def _filtered(
        self, tenant_id: str, client_id: str, state: str | None, context_id: str | None,
        changed_after: datetime | None,
    ) -> Any:
        query = self._run_query(tenant_id, client_id)
        if state is not None:
            query = query.where(_state_clause(state))
        if context_id is not None:
            # A claim without a label (a run started over MCP) has its run id as its context.
            query = query.where(or_(
                ExternalRunModel.context_label == context_id,
                and_(ExternalRunModel.context_label.is_(None), ExternalRunModel.run_id == context_id),
            ))
        if changed_after is not None:
            query = query.where(func.coalesce(RunModel.updated_at, ExternalRunModel.created_at) >= changed_after)
        return query

    def list_runs(
        self, tenant_id: str, client_id: str, *, state: str | None = None, limit: int = 50,
        cursor: RunCursor | None = None, context_id: str | None = None, changed_after: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], RunCursor | None]:
        """The client's runs, the latest status change first, keyset paged."""
        limit = max(1, min(limit, _MAX_LIST))
        changed = func.coalesce(RunModel.updated_at, ExternalRunModel.created_at)
        query = self._filtered(tenant_id, client_id, state, context_id, changed_after)
        if cursor is not None:
            query = query.where(or_(
                changed < cursor.created_at,
                and_(changed == cursor.created_at, ExternalRunModel.run_id < cursor.id),
            ))
        with self.Session() as session:
            found = session.execute(query.order_by(changed.desc(), ExternalRunModel.run_id.desc()).limit(limit + 1)).all()
            rows = [self._run_row(f) for f in found[:limit]]
        next_cursor = RunCursor(rows[-1]["changed_at"], rows[-1]["run_id"]) if len(found) > limit else None
        return rows, next_cursor

    def count_runs(
        self, tenant_id: str, client_id: str, *, state: str | None = None, context_id: str | None = None,
        changed_after: datetime | None = None,
    ) -> int:
        """How many runs `list_runs` would page through with the same filters."""
        query = self._filtered(tenant_id, client_id, state, context_id, changed_after)
        with self.Session() as session:
            return int(session.execute(query.with_only_columns(func.count())).scalar_one())

    def active_run_ids(self, tenant_id: str, client_id: str, limit: int = 1000) -> list[str]:
        with self.Session() as session:
            return list(session.execute(
                select(ExternalRunModel.run_id)
                .join(RunModel, _run_join())
                .where(
                    ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.client_id == client_id,
                    ~_terminal(RunModel.status),
                )
                .order_by(ExternalRunModel.created_at).limit(limit)
            ).scalars())

    def waiting_kind(self, tenant_id: str, run_id: str) -> str | None:
        """What the run (or a child of it) waits on: `input` (an input step) or `approval` (an approval step or a tool
        approval), the oldest open one; None when nothing is open."""
        with self.engine.connect() as conn:
            open_row = conn.execute(
                text(
                    "SELECT run_id, kind, approval_key FROM pending_approvals"
                    " WHERE tenant_id = :t AND root_run_id = :r AND decided_at IS NULL"
                    " ORDER BY requested_at, approval_key LIMIT 1"
                ),
                {"t": tenant_id, "r": run_id},
            ).first()
            if open_row is None:
                return None
            if open_row.kind != "step":
                return "approval"
            takes_data = conn.execute(
                text(
                    "SELECT 1 FROM execution_events WHERE tenant_id = :t AND run_id = :r AND event_key = :k"
                    " AND jsonb_typeof(data -> 'resume_schema') = 'object'"
                ),
                {"t": tenant_id, "r": open_row.run_id, "k": f"step:{open_row.approval_key}:waiting"},
            ).first()
        return "input" if takes_data is not None else "approval"

    def run_source(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        """`run_started.source` of a run an external client started (never its input or idempotency key)."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT e.protocol, e.client_id::text AS client_id, c.name AS client_name, e.key_id, x.name AS export"
                    " FROM external_runs e"
                    " JOIN external_clients c ON c.tenant_id = e.tenant_id AND c.id = e.client_id"
                    " JOIN workflow_exports x ON x.tenant_id = e.tenant_id AND x.id = e.export_id"
                    " WHERE e.tenant_id = :t AND e.run_id = :r"
                ),
                {"t": tenant_id, "r": str(run_id)},
            ).first()
        if row is None:
            return None
        return {
            "kind": f"external_{row.protocol}", "client_id": row.client_id, "client_name": row.client_name,
            "key_id": row.key_id, "export": row.export,
        }

    # -------------------------------------------------------------------------------------------- the sweep (all tenants)

    def overdue_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Runs past their deadline that have not ended, the longest overdue first (parked on an approval included)."""
        with self.Session() as session:
            rows = session.execute(
                select(ExternalRunModel.tenant_id, ExternalRunModel.run_id)
                .join(RunModel, _run_join())
                .where(
                    ExternalRunModel.closed_at.is_(None), ExternalRunModel.deadline_at < func.now(),
                    ~_terminal(RunModel.status),
                )
                .order_by(ExternalRunModel.deadline_at).limit(limit)
            ).all()
        return [{"tenant_id": r.tenant_id, "run_id": r.run_id} for r in rows]

    def request_deadline_cancel(self, tenant_id: str, run_id: str) -> bool:
        """The durable cancel request of an overdue run and its cancel reason, in one transaction (the run-row lock of
        the event append). True when this cancel is the deadline's (now or on an earlier tick); False when the run has
        ended or somebody else asked to cancel it first, whose reason then stands."""
        with self.Session() as session, session.begin():
            result = append_event_in_session(
                session, tenant_id, uuid.UUID(str(run_id)), CANCEL_EVENT_KEY, "cancel_requested",
                {"requested_by": DEADLINE_CANCELLER},
            )
            ours = not result.dropped and (result.event.data or {}).get("requested_by") == DEADLINE_CANCELLER
            if ours:
                session.execute(
                    update(ExternalRunModel)
                    .where(ExternalRunModel.tenant_id == tenant_id, ExternalRunModel.run_id == str(run_id))
                    .values(cancel_reason=DEADLINE_REASON)
                )
        return ours

    def unstarted_runs(self, older_than: timedelta, limit: int = 100) -> list[dict[str, Any]]:
        """Runs still `queued` after `older_than` whose claim is open: candidates for the worker to start again (it
        skips the ones DBOS already knows). Oldest first."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT e.tenant_id, e.run_id, v.id AS version_id, r.input AS input_json FROM external_runs e"
                    " JOIN runs r ON r.tenant_id = e.tenant_id AND r.id = e.run_id"
                    " JOIN workflow_versions v ON v.tenant_id = r.tenant_id AND v.workflow_id = r.workflow_id"
                    " AND v.version = r.workflow_version"
                    " WHERE e.closed_at IS NULL AND r.status = 'queued' AND r.created_at < :before"
                    " ORDER BY r.created_at, e.run_id LIMIT :n"
                ),
                {"before": _now() - older_than, "n": limit},
            ).mappings().all()
        return [{"tenant_id": r["tenant_id"], "run_id": r["run_id"], "version_id": str(r["version_id"]),
                 "input_json": r["input_json"]} for r in rows]

    def close_ended_claims(self, limit: int = 500) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "UPDATE external_runs SET closed_at = now() WHERE run_id IN ("
                    " SELECT e.run_id FROM external_runs e JOIN runs r ON r.tenant_id = e.tenant_id AND r.id = e.run_id"
                    " WHERE e.closed_at IS NULL AND r.status = ANY(:terminal) LIMIT :n)"
                ),
                {"terminal": list(TERMINAL_RUN_STATUSES), "n": limit},
            ).rowcount

    def delete_dead_claims(self, grace: timedelta, limit: int = 500) -> int:
        """Claims with no run row whose deadline passed `grace` ago: a start that never happened, or a run that was
        deleted (its idempotency key is forgotten with it)."""
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    "DELETE FROM external_runs WHERE run_id IN ("
                    " SELECT e.run_id FROM external_runs e WHERE e.deadline_at < :before AND NOT EXISTS ("
                    "  SELECT 1 FROM runs r WHERE r.tenant_id = e.tenant_id AND r.id = e.run_id) LIMIT :n)"
                ),
                {"before": _now() - grace, "n": limit},
            ).rowcount


__all__ = [
    "CONTRACT_CHANGED", "DEADLINE_CANCELLER", "DEADLINE_REASON", "PROTOCOLS", "RESULT_MODES", "RUN_STATES",
    "WARNING_PROMPT_INPUT", "WARNING_SIDE_EFFECTS", "Claim", "ClaimLimits", "ExternalAccessStore",
    "ExternalClientModel", "ExternalEndpointModel", "ExternalNameConflict", "ExternalQuotaExceeded",
    "ExternalRunModel", "IdempotencyConflict", "WorkflowContract", "WorkflowEffects", "WorkflowExportModel",
    "external_warnings", "on_workflow_published", "workflow_contract", "workflow_effects",
]
