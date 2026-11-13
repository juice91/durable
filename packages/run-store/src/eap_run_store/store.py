"""Postgres store for durable runs and event logging."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import json
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text,
    func,
    case,
    null,
    or_,
    tuple_,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

from eap_run_store.cursor import ConversationCursor, RunCursor
from sqlalchemy.orm import (
    DeclarativeBase,
    aliased,
    Mapped,
    mapped_column,
    sessionmaker,
)

from eap_schemas import RunEvent, RunEventType

# SQLAlchemy 2.x style type annotations
StrAnnotated = str
UUIDAnnotated = UUID


def _json_param(value: Any):
    from sqlalchemy import literal

    return literal(json.dumps(value), type_=Text)


class Base(DeclarativeBase):
    """Base class for all database models. Alembic (packages/db) creates and migrates these tables."""


TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "needs_attention"})
RUN_KINDS = ("agent", "tool", "workflow", "kb_ingest")

# runs.error of a run whose hand-off to the orchestrator failed (the API could not enqueue it).
# Stable text: RunStore.requeue_after_enqueue_failure recognises the state by it.
ENQUEUE_FAILED_ERROR = (
    "EnqueueFailed: the run could not be started (enqueue_failed); retry the same request with the same run_id"
)

# An agent run names exactly one of its published agent version and a draft snapshot (a draft test
# run), and no tool or workflow; a tool run names its tool only and is never a conversation turn
# (it may name a knowledge base: the search tester); a workflow run names its published workflow
# version only; a kb_ingest run names its knowledge base only. Same text as migration
# 0011_knowledge_bases.
RUN_KIND_TARGET_CHECK = (
    "(kind = 'agent' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'tool' AND tool_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND conversation_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL)"
    " OR (kind = 'workflow' AND workflow_id IS NOT NULL AND workflow_version IS NOT NULL"
    " AND agent_id IS NULL AND agent_version IS NULL AND draft_spec IS NULL AND tool_id IS NULL"
    " AND conversation_id IS NULL AND knowledge_base_id IS NULL)"
    " OR (kind = 'kb_ingest' AND knowledge_base_id IS NOT NULL AND agent_id IS NULL AND agent_version IS NULL"
    " AND draft_spec IS NULL AND tool_id IS NULL AND conversation_id IS NULL AND workflow_id IS NULL"
    " AND workflow_version IS NULL)"
)
# run_model_requests.operation (0011_knowledge_bases): a chat model request or an embedding call.
MODEL_REQUEST_OPERATIONS = ("chat", "embedding")
# A workflow's agent step runs as a child agent run linked to its workflow run and step (never a
# conversation turn). Same text as migration 0007_workflow_runs.
RUN_PARENT_LINK_CHECK = (
    "(parent_run_id IS NULL AND parent_step_id IS NULL)"
    " OR (parent_run_id IS NOT NULL AND parent_step_id IS NOT NULL AND kind = 'agent' AND conversation_id IS NULL)"
)


class ConversationModel(Base):
    """A tenant-owned thread with one agent. Its turns are runs (runs.conversation_id, runs.turn)."""

    __tablename__ = "conversations"

    id: Mapped[StrAnnotated] = mapped_column("id", String, primary_key=True)
    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    # Like runs.agent_id: no foreign key, because agents belong to the API's own metadata.
    agent_id: Mapped[StrAnnotated] = mapped_column("agent_id", String, nullable=False)
    title: Mapped[StrAnnotated | None] = mapped_column("title", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        # Lets runs reference (tenant_id, conversation_id), so a run can only join its own tenant's thread.
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        Index("ix_conversations_tenant_updated", "tenant_id", "updated_at"),
        # One agent's conversations in list order (0010_run_options).
        Index(
            "ix_conversations_tenant_agent_updated", "tenant_id", "agent_id", text("updated_at DESC"), text("id DESC")
        ),
    )


class RunModel(Base):
    """Model for runs table."""

    __tablename__ = "runs"

    id: Mapped[UUIDAnnotated] = mapped_column(
        "id",
        String,
        primary_key=True,
    )
    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    # What the run executes: 'agent' (a published agent version: agent_id + agent_version) or
    # 'tool' (one platform tool call from the tool test console: tool_id). ck_runs_kind_target
    # keeps the target columns consistent with the kind.
    kind: Mapped[StrAnnotated] = mapped_column("kind", String, nullable=False, server_default=text("'agent'"))
    agent_id: Mapped[UUIDAnnotated | None] = mapped_column("agent_id", String, nullable=True)
    agent_version: Mapped[int | None] = mapped_column("agent_version", Integer, nullable=True)
    tool_id: Mapped[StrAnnotated | None] = mapped_column("tool_id", String, nullable=True)
    # 'workflow' runs: the published workflow version executed (workflows.id, workflow_versions.version).
    workflow_id: Mapped[StrAnnotated | None] = mapped_column("workflow_id", String, nullable=True)
    workflow_version: Mapped[int | None] = mapped_column("workflow_version", Integer, nullable=True)
    # An agent step's child run: the workflow run and step it executes for (both NULL otherwise).
    parent_run_id: Mapped[StrAnnotated | None] = mapped_column("parent_run_id", String, nullable=True)
    parent_step_id: Mapped[StrAnnotated | None] = mapped_column("parent_step_id", String, nullable=True)
    status: Mapped[StrAnnotated] = mapped_column("status", String, nullable=False)
    input: Mapped[StrAnnotated | None] = mapped_column("input", Text, nullable=True)
    output: Mapped[StrAnnotated | None] = mapped_column("output", Text, nullable=True)
    error: Mapped[StrAnnotated | None] = mapped_column("error", Text, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        "heartbeat_at", DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )
    # Conversation turn: both null for a standalone run, both set for a turn (turn numbers start at 1).
    conversation_id: Mapped[StrAnnotated | None] = mapped_column("conversation_id", String, nullable=True)
    turn: Mapped[int | None] = mapped_column("turn", Integer, nullable=True)
    # The run's own new Pydantic AI messages (ModelMessagesTypeAdapter JSON), written once when it
    # completes. A later turn's history is built from these.
    messages: Mapped[list | None] = mapped_column("messages", JSONB, nullable=True)
    # The creating API request's trace context (NULL when telemetry was off): the worker opens the
    # run's span as a child of `traceparent`, and the Studio links to the trace by `trace_id`.
    trace_id: Mapped[StrAnnotated | None] = mapped_column("trace_id", String, nullable=True)
    traceparent: Mapped[StrAnnotated | None] = mapped_column("traceparent", String, nullable=True)
    # Usage and cost (0008_run_usage). model_id: the effective model `provider:name`;
    # model_overridden: it differs from the version's model at the caller's request. Totals are set
    # by set_status when the run reaches a terminal status (SUM over run_model_requests, also over
    # the child runs' rows for a workflow run; cost NULL if any request is unpriced). started_at is
    # set once by mark_running. All NULL for runs recorded before 0008 ("not recorded").
    model_id: Mapped[StrAnnotated | None] = mapped_column("model_id", String, nullable=True)
    model_overridden: Mapped[bool] = mapped_column(
        "model_overridden", Boolean, nullable=False, default=False, server_default=text("false")
    )
    input_tokens: Mapped[int | None] = mapped_column("input_tokens", BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column("output_tokens", BigInteger, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column("cost_usd", Numeric(20, 10), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column("started_at", DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column("finished_at", DateTime(timezone=True), nullable=True)
    # Run options (0010_run_options). request_context: the validated request context (declared
    # fields only; NULL = none). draft_spec: the agent draft snapshot of a draft test run
    # (agent_version NULL); a retry relaunches from it, never from the live draft.
    request_context: Mapped[dict | None] = mapped_column("request_context", JSONB, nullable=True)
    draft_spec: Mapped[dict | None] = mapped_column("draft_spec", JSONB, nullable=True)
    # The knowledge base a kb_ingest run ingests into (0011_knowledge_bases; a tool run of the search
    # tester may name one too). No FK: the run outlives a deleted knowledge base as its audit and
    # cost record.
    knowledge_base_id: Mapped[str | None] = mapped_column(
        "knowledge_base_id", PG_UUID(as_uuid=False), nullable=True
    )

    __table_args__ = (
        # Redundant with the primary key, but lets tenant-owned children reference (tenant_id, id).
        UniqueConstraint("tenant_id", "id", name="uq_runs_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_runs_tenant_conversation",
        ),
        UniqueConstraint("conversation_id", "turn", name="uq_runs_conversation_turn"),
        CheckConstraint("(conversation_id IS NULL) = (turn IS NULL)", name="ck_runs_conversation_turn"),
        CheckConstraint(RUN_KIND_TARGET_CHECK, name="ck_runs_kind_target"),
        CheckConstraint(RUN_PARENT_LINK_CHECK, name="ck_runs_parent_link"),
        ForeignKeyConstraint(
            ["tenant_id", "parent_run_id"], ["runs.tenant_id", "runs.id"], name="fk_runs_tenant_parent_run"
        ),
        # The tenant's runs, newest first (GET /runs), also filtered by kind or by tool.
        Index("ix_runs_tenant_created", "tenant_id", "created_at"),
        Index("ix_runs_tenant_kind_created", "tenant_id", "kind", "created_at"),
        Index("ix_runs_tenant_tool_created", "tenant_id", "tool_id", "created_at"),
        Index("ix_runs_tenant_workflow_created", "tenant_id", "workflow_id", "created_at"),
        Index("ix_runs_parent_run", "parent_run_id"),
        # A knowledge base's runs (ingest progress, summed cost), newest first.
        Index("ix_runs_tenant_kb_created", "tenant_id", "knowledge_base_id", "created_at"),
    )


class ExecutionEventModel(Base):
    """Model for execution_events table. Owned by the run's tenant (composite FK to runs)."""

    __tablename__ = "execution_events"

    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[UUIDAnnotated] = mapped_column(
        "run_id",
        String,
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column("seq", Integer, primary_key=True)
    event_key: Mapped[StrAnnotated] = mapped_column("event_key", String, nullable=False)
    type: Mapped[StrAnnotated] = mapped_column("type", String, nullable=False)
    data: Mapped[dict] = mapped_column("data", JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column("ts", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("run_id", "event_key", name="uq_run_event_key"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_execution_events_tenant_run",
        ),
        Index("ix_execution_events_tenant_run", "tenant_id", "run_id"),
        # One event type across the tenant's runs, by time (processor results).
        Index("ix_execution_events_tenant_type_ts", "tenant_id", "type", "ts"),
    )


class RunModelRequestModel(Base):
    """One model request of a run, with its usage and cost (0008_run_usage).

    Written only by EventLog.append, in the transaction of the request's `model_message` event
    (`request_key` = that event's key without `:final`, e.g. `model:0`), first write wins. The cost
    is computed once, at record time, and never recomputed. Owned by the run's tenant (composite FK).
    """

    __tablename__ = "run_model_requests"

    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[StrAnnotated] = mapped_column("run_id", String, primary_key=True)
    request_key: Mapped[StrAnnotated] = mapped_column("request_key", String, primary_key=True)
    model_id: Mapped[StrAnnotated] = mapped_column("model_id", String, nullable=False)
    provider_model_name: Mapped[StrAnnotated | None] = mapped_column("provider_model_name", String, nullable=True)
    input_tokens: Mapped[int] = mapped_column("input_tokens", BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column("output_tokens", BigInteger, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column("cache_read_tokens", BigInteger, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column("cache_write_tokens", BigInteger, nullable=False)
    usage_measured: Mapped[bool] = mapped_column("usage_measured", Boolean, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column("cost_usd", Numeric(20, 10), nullable=True)
    price_source: Mapped[StrAnnotated | None] = mapped_column("price_source", String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    # 'chat' (a model request) or 'embedding' (an embedding call: output_tokens 0). 0011.
    operation: Mapped[StrAnnotated] = mapped_column(
        "operation", String, nullable=False, default="chat", server_default=text("'chat'")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], name="fk_run_model_requests_tenant_run"
        ),
        CheckConstraint("operation IN ('chat', 'embedding')", name="ck_run_model_requests_operation"),
        Index("ix_run_model_requests_tenant_created", "tenant_id", "created_at"),
        Index("ix_run_model_requests_tenant_model_created", "tenant_id", "model_id", "created_at"),
    )


class SideEffectModel(Base):
    """Model for side_effects table."""

    __tablename__ = "side_effects"

    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, primary_key=True)
    key: Mapped[StrAnnotated] = mapped_column("key", String, primary_key=True)
    state: Mapped[StrAnnotated] = mapped_column("state", String, nullable=False)
    result: Mapped[dict | None] = mapped_column("result", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        "completed_at", DateTime(timezone=True), nullable=True
    )


class RunNotFound(ValueError):
    """No run with this id belongs to the given tenant (it may not exist, or belong to another tenant).

    Raised by every mutation that takes (tenant_id, run_id); nothing is changed when it is raised.
    """

    def __init__(self, tenant_id: str, run_id: UUID | str):
        self.tenant_id = tenant_id
        self.run_id = run_id
        # args are the constructor arguments: raised inside DBOS steps, whose errors are pickled
        # (a message-only args made unpickling fail with TypeError).
        super().__init__(tenant_id, run_id)

    def __str__(self) -> str:
        return f"Run not found for tenant {self.tenant_id!r}: {self.run_id}"


def _run_event_from_model(model: ExecutionEventModel) -> RunEvent:
    """Convert ExecutionEventModel to RunEvent schema."""
    return RunEvent(
        run_id=UUID(model.run_id),
        seq=model.seq,
        type=model.type,  # type is already a string from the database
        data=model.data,
        ts=model.ts,
    )


class RunIdConflict(Exception):
    """Raised when a run_id exists under a different tenant."""

    def __init__(self, run_id: UUID):
        self.run_id = run_id
        super().__init__(run_id)  # pickle-safe, as RunNotFound

    def __str__(self) -> str:
        return f"Run ID {self.run_id} already exists under a different tenant"


class ConversationNotFound(ValueError):
    """No conversation with this id belongs to the tenant (unknown, or another tenant's)."""

    def __init__(self, tenant_id: str, conversation_id: UUID | str):
        self.tenant_id = tenant_id
        self.conversation_id = str(conversation_id)
        super().__init__(tenant_id, conversation_id)  # pickle-safe, as RunNotFound

    def __str__(self) -> str:
        return f"Conversation not found for tenant {self.tenant_id!r}: {self.conversation_id}"


class ConversationIdConflict(Exception):
    """A client-supplied conversation id already exists under a different tenant."""

    def __init__(self, conversation_id: UUID | str):
        self.conversation_id = str(conversation_id)
        super().__init__(conversation_id)  # pickle-safe, as RunNotFound

    def __str__(self) -> str:
        return f"Conversation ID {self.conversation_id} already exists under a different tenant"


class TurnInProgress(Exception):
    """The conversation's latest turn has not reached a terminal status yet."""

    def __init__(self, conversation_id: str, run_id: str, status: str):
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.status = status
        super().__init__(conversation_id, run_id, status)  # pickle-safe, as RunNotFound

    def __str__(self) -> str:
        return f"Conversation {self.conversation_id} is busy: turn run {self.run_id} is {self.status}"


CONVERSATION_TITLE_MAX_LENGTH = 200
AUTO_TITLE_LENGTH = 60


def auto_title(text: str) -> str | None:
    """The title a conversation gets from its first turn's input: every run of whitespace (newlines
    included) and every control character collapsed into one space, trimmed, then the first 60
    characters (trailing space trimmed again). None when nothing printable is left."""
    cleaned = "".join(" " if (ch.isspace() or not ch.isprintable()) else ch for ch in text)
    title = " ".join(cleaned.split())[:AUTO_TITLE_LENGTH].rstrip()
    return title or None


def _latest_turn_display_status(status: str | None, heartbeat_at: datetime | None) -> str | None:
    """As _display_status, for the latest-turn columns of a conversation query."""
    if status == "running" and heartbeat_at is not None:
        if (datetime.now(timezone.utc) - heartbeat_at).total_seconds() > 20:
            return "recovering"
    return status


def _iso_conversation(
    conv: ConversationModel,
    turn_count: int,
    last_turn: tuple[str | None, str | None, datetime | None] = (None, None, None),
) -> dict[str, Any]:
    """`last_turn` is the latest turn's (run id, status, heartbeat_at), all None without turns."""
    last_run_id, last_status, last_heartbeat = last_turn
    return {
        "id": conv.id,
        "tenant_id": conv.tenant_id,
        "agent_id": conv.agent_id,
        "title": conv.title,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "turn_count": turn_count,
        "last_turn_run_id": last_run_id,
        "last_turn_status": last_status,
        "last_turn_display_status": _latest_turn_display_status(last_status, last_heartbeat),
    }


def _display_status(run: RunModel) -> str:
    if run.status == "running" and run.heartbeat_at is not None:
        if (datetime.now(timezone.utc) - run.heartbeat_at).total_seconds() > 20:
            return "recovering"
    return run.status


def _run_dict(run: RunModel) -> dict[str, Any]:
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "kind": run.kind,
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
        "tool_id": run.tool_id,
        "workflow_id": run.workflow_id,
        "workflow_version": run.workflow_version,
        "parent_run_id": run.parent_run_id,
        "parent_step_id": run.parent_step_id,
        "status": run.status,
        "display_status": _display_status(run),
        "input": run.input,
        "output": run.output,
        "error": run.error,
        "heartbeat_at": run.heartbeat_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "conversation_id": run.conversation_id,
        "turn": run.turn,
        "trace_id": run.trace_id,
        "traceparent": run.traceparent,
        "model_id": run.model_id,
        "model_overridden": bool(run.model_overridden),
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost_usd": run.cost_usd,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "request_context": run.request_context,
        "draft_spec": run.draft_spec,
        "knowledge_base_id": run.knowledge_base_id,
    }


# ---------------------------------------------------------------------------- usage totals (0008)


# What mark_enqueue_failed records besides the status: the run is finished, without any model
# request. Cleared (all NULL) when a failed hand-off is revived (mark_running, requeue).
_ENQUEUE_FAILED_FINISH = ("finished_at", "input_tokens", "output_tokens", "cost_usd")


def _enqueue_failed_finish() -> dict[str, Any]:
    return {"finished_at": datetime.now(timezone.utc), "input_tokens": 0, "output_tokens": 0, "cost_usd": Decimal(0)}


def _finish_values(tenant_id: str, run_id: str, status: str) -> dict[str, Any]:
    """The columns a terminal set_status writes besides the status, as SQL expressions evaluated in
    the same UPDATE: `finished_at` and the usage totals.

    Totals are the SUM over the run's `run_model_requests` rows and, for a workflow run, over its
    child runs' rows (a workflow run makes no model request itself). Tokens are 0 for a run without
    requests; `cost_usd` is NULL if any request is unpriced, else the exact sum (0 without requests).
    Computed from durable rows, so a re-executed finish step writes the same totals.
    `finished_at` is kept when the run already has this terminal status (a re-executed finish
    step), and set to now() otherwise."""
    children = aliased(RunModel)
    child_ids = select(children.id).where(children.tenant_id == tenant_id, children.parent_run_id == run_id)
    rows = (
        select(
            func.coalesce(func.sum(RunModelRequestModel.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(RunModelRequestModel.output_tokens), 0).label("output_tokens"),
            case(
                (func.count() == func.count(RunModelRequestModel.cost_usd),
                 func.coalesce(func.sum(RunModelRequestModel.cost_usd), 0)),
                else_=None,
            ).label("cost_usd"),
        )
        .where(
            RunModelRequestModel.tenant_id == tenant_id,
            or_(RunModelRequestModel.run_id == run_id, RunModelRequestModel.run_id.in_(child_ids)),
        )
        .subquery()
    )
    return {
        "input_tokens": select(rows.c.input_tokens).scalar_subquery(),
        "output_tokens": select(rows.c.output_tokens).scalar_subquery(),
        "cost_usd": select(rows.c.cost_usd).scalar_subquery(),
        "finished_at": case(
            ((RunModel.status == status) & RunModel.finished_at.is_not(None), RunModel.finished_at),
            else_=datetime.now(timezone.utc),
        ),
    }


def _run_option_values(
    request_context: dict[str, Any] | None,
    draft_spec: dict[str, Any] | None,
    model_id: str | None,
    model_overridden: bool,
) -> dict[str, Any]:
    """Insert values for the run options of an agent run (0010_run_options; model_id from 0008).
    An empty context is stored as NULL, like a run recorded before request context existed. SQL
    NULL explicitly: a JSONB column would bind Python None as the JSON value `null`."""
    return {
        "request_context": request_context or null(),
        "draft_spec": null() if draft_spec is None else draft_spec,
        "model_id": model_id,
        "model_overridden": bool(model_overridden),
    }


class RunStore:
    """Store for durable runs."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def create_run(
        self,
        tenant_id: str,
        agent_id: UUID,
        agent_version: int | None,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
        *,
        request_context: dict[str, Any] | None = None,
        draft_spec: dict[str, Any] | None = None,
        model_id: str | None = None,
        model_overridden: bool = False,
    ) -> tuple[UUID, bool]:
        """Create a new agent run. Idempotent: same run_id returns existing without creating second row.

        `trace_id`/`traceparent` are the creating API request's trace context (None when telemetry
        is off); an existing row keeps the values it was created with, and so do the run options:
        `request_context` (validated; None or empty is stored as NULL), `draft_spec` (a draft test
        run's snapshot, with `agent_version` None), `model_id` (the effective model) and
        `model_overridden`.

        Returns (UUID, created: bool) where created=False if the run already exists for this tenant
        (whatever its kind: the caller checks that it is the same request).
        Raises RunIdConflict if the run_id exists under a different tenant.
        """
        return self._insert_run(
            tenant_id,
            run_id,
            kind="agent",
            agent_id=str(agent_id),
            agent_version=agent_version,
            input=input,
            trace_id=trace_id,
            traceparent=traceparent,
            **_run_option_values(request_context, draft_spec, model_id, model_overridden),
        )

    def create_tool_run(
        self,
        tenant_id: str,
        tool_id: str,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
        *,
        knowledge_base_id: UUID | str | None = None,
    ) -> tuple[UUID, bool]:
        """Create a tool run (kind 'tool': one call of `tool_id`, `input` = its JSON arguments as text).

        `knowledge_base_id`: the knowledge base the run searches (the knowledge base search tester),
        so its cost is listed with the knowledge base's runs; the caller has checked the tenant.
        Same idempotency and tenant rules as create_run: (run_id, created), RunIdConflict for
        another tenant's run_id, and an existing row of this tenant is returned as is.
        """
        return self._insert_run(
            tenant_id,
            run_id,
            kind="tool",
            tool_id=tool_id,
            input=input,
            trace_id=trace_id,
            traceparent=traceparent,
            knowledge_base_id=None if knowledge_base_id is None else str(knowledge_base_id),
        )

    def create_workflow_run(
        self,
        tenant_id: str,
        workflow_id: UUID | str,
        workflow_version: int,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Create a workflow run (kind 'workflow': published version `workflow_version` of
        `workflow_id`, `input` = the workflow input as canonical JSON text).

        Same idempotency and tenant rules as create_run."""
        return self._insert_run(
            tenant_id,
            run_id,
            kind="workflow",
            workflow_id=str(workflow_id),
            workflow_version=workflow_version,
            input=input,
            trace_id=trace_id,
            traceparent=traceparent,
        )

    def create_kb_ingest_run(
        self,
        tenant_id: str,
        knowledge_base_id: UUID | str,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Create a knowledge base ingest run (kind 'kb_ingest': `input` = the canonical JSON of
        `{document_id, doc_version}` or `{reindex: generation}`). The caller has checked that the
        knowledge base belongs to `tenant_id` (KnowledgeStore). Same idempotency and tenant rules as
        create_run."""
        return self._insert_run(
            tenant_id,
            run_id,
            kind="kb_ingest",
            knowledge_base_id=str(knowledge_base_id),
            input=input,
            trace_id=trace_id,
            traceparent=traceparent,
        )

    def create_child_agent_run(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        agent_version: int,
        input: str,
        run_id: UUID,
        parent_run_id: UUID | str,
        parent_step_id: str,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Create the agent run that executes agent step `parent_step_id` of workflow run
        `parent_run_id` (same tenant: enforced by fk_runs_tenant_parent_run). Same idempotency
        rules as create_run; `eap.agent_run` then drives it like any agent run."""
        return self._insert_run(
            tenant_id,
            run_id,
            kind="agent",
            agent_id=str(agent_id),
            agent_version=agent_version,
            input=input,
            parent_run_id=str(parent_run_id),
            parent_step_id=parent_step_id,
            traceparent=traceparent,
        )

    def _insert_run(self, tenant_id: str, run_id: UUID, **values: Any) -> tuple[UUID, bool]:
        from sqlalchemy.dialects.postgresql import insert

        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.Session() as session:
            # INSERT ... ON CONFLICT DO NOTHING ... RETURNING: atomic against a concurrent retry.
            stmt = (
                insert(RunModel)
                .values(id=str(run_id), tenant_id=tenant_id, status="queued", **values)
                .on_conflict_do_nothing(constraint="runs_pkey")
                .returning(RunModel.id, RunModel.tenant_id)
            )
            result = session.execute(stmt).fetchone()
            if result is not None:
                session.commit()
                return UUID(result.id), True

            existing = session.execute(select(RunModel).where(RunModel.id == str(run_id))).scalar_one_or_none()
            session.commit()
            if existing is None:
                # Deleted between the conflict and this read; report it as existing, like before.
                return UUID(str(run_id)), False
            if existing.tenant_id != tenant_id:
                raise RunIdConflict(run_id)
            return UUID(existing.id), False

    def set_status(
        self,
        tenant_id: str,
        run_id: UUID,
        status: str,
        output: str | None = None,
        error: str | None = None,
        messages: list[Any] | None = None,
    ) -> None:
        """Update run status and optionally output, error or messages, only for a run owned by `tenant_id`.

        `messages` is written once: a repeated call (a re-executed finish step) keeps the first value,
        so the history later turns read never changes after it was first recorded.
        A terminal status (completed, failed, needs_attention) also sets `finished_at` and the usage
        totals in the same statement (see _finish_values).
        Raises RunNotFound (and changes nothing) if the run does not exist or belongs to another tenant.
        """
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if output is not None:
            values["output"] = output
        if error is not None:
            values["error"] = error
        if messages is not None:
            values["messages"] = func.coalesce(RunModel.messages, func.cast(_json_param(messages), JSONB))
        if status in TERMINAL_RUN_STATUSES:
            values.update(_finish_values(tenant_id, str(run_id), status))
        self._update_owned(tenant_id, run_id, values)

    def mark_enqueue_failed(self, tenant_id: str, run_id: UUID) -> bool:
        """The run could not be handed to the orchestrator: queued -> failed (ENQUEUE_FAILED_ERROR).

        Only a run still `queued` changes, so a workflow that did start (an enqueue that raised
        after DBOS accepted it) is never overwritten. Returns whether the row changed.

        Like any terminal status it sets `finished_at` and the usage totals (zero: no model request
        was made). mark_running and requeue_after_enqueue_failure clear them again when the run is
        revived."""
        return self._conditional_update(
            tenant_id, run_id, RunModel.status == "queued",
            {"status": "failed", "error": ENQUEUE_FAILED_ERROR, **_enqueue_failed_finish()},
        )

    def mark_running(self, tenant_id: str, run_id: UUID) -> bool:
        """A workflow has started the run: status -> running, and an ENQUEUE_FAILED_ERROR left by a
        hand-off that raised although DBOS accepted it is cleared in the same statement (any other
        error is kept). RunNotFound for another tenant's or an unknown run. Returns True.

        A conversation turn is checked under the conversation's row lock (the lock create_turn
        takes). If a later turn exists, this turn was marked failed by its hand-off and the
        conversation has moved on: it is left unchanged (still failed) and False is returned, so
        the workflow must stop without running it. A run without a conversation (agent, tool or
        workflow run) never takes the lock and always returns True."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        values = {
            "status": "running",
            "updated_at": datetime.now(timezone.utc),
            "error": case((RunModel.error == ENQUEUE_FAILED_ERROR, None), else_=RunModel.error),
            # The first time a worker starts the run; a recovery or replay keeps it.
            "started_at": func.coalesce(RunModel.started_at, datetime.now(timezone.utc)),
            # A run revived from a failed hand-off is no longer finished: drop what
            # mark_enqueue_failed recorded (same condition as the error above).
            **{
                column: case((RunModel.error == ENQUEUE_FAILED_ERROR, None), else_=getattr(RunModel, column))
                for column in _ENQUEUE_FAILED_FINISH
            },
        }
        with self.Session() as session:
            run = session.execute(
                select(RunModel.conversation_id, RunModel.turn)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).first()
            if run is None:
                raise RunNotFound(tenant_id, run_id)
            if run.conversation_id is not None:
                session.execute(
                    select(ConversationModel.id).where(ConversationModel.id == run.conversation_id).with_for_update()
                )
                later = session.execute(
                    select(RunModel.id)
                    .where(RunModel.conversation_id == run.conversation_id, RunModel.turn > run.turn)
                    .limit(1)
                ).first()
                if later is not None:
                    session.rollback()
                    return False
            session.execute(
                update(RunModel).where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id).values(**values)
            )
            session.commit()
            return True

    def requeue_after_enqueue_failure(self, tenant_id: str, run_id: UUID) -> bool:
        """A retry of a run left failed by mark_enqueue_failed: failed -> queued, error cleared.

        Any other run is left alone. A conversation turn is re-queued only while it is still the
        conversation's latest turn, checked under the conversation's row lock (the lock create_turn
        takes): a failed turn is terminal, so a later turn may have started since. Returns whether
        the row changed."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        failed_handoff = (RunModel.status == "failed") & (RunModel.error == ENQUEUE_FAILED_ERROR)
        with self.Session() as session:
            run = session.execute(
                select(RunModel.conversation_id, RunModel.turn)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id, failed_handoff)
            ).first()
            if run is None:
                return False
            if run.conversation_id is not None:
                session.execute(
                    select(ConversationModel.id).where(ConversationModel.id == run.conversation_id).with_for_update()
                )
                later = session.execute(
                    select(RunModel.id)
                    .where(RunModel.conversation_id == run.conversation_id, RunModel.turn > run.turn)
                    .limit(1)
                ).first()
                if later is not None:
                    session.rollback()
                    return False
            updated = session.execute(
                update(RunModel)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id, failed_handoff)
                .values(
                    status="queued",
                    error=None,
                    updated_at=datetime.now(timezone.utc),
                    **{column: None for column in _ENQUEUE_FAILED_FINISH},
                )
                .returning(RunModel.id)
            ).fetchone()
            session.commit()
            return updated is not None

    def _conditional_update(self, tenant_id: str, run_id: UUID, condition: Any, values: dict[str, Any]) -> bool:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.Session() as session:
            updated = session.execute(
                update(RunModel)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id, condition)
                .values(**values, updated_at=datetime.now(timezone.utc))
                .returning(RunModel.id)
            ).fetchone()
            session.commit()
            return updated is not None

    def heartbeat(self, tenant_id: str, run_id: UUID) -> None:
        """Update the heartbeat timestamp for a run owned by `tenant_id` (RunNotFound otherwise)."""
        self._update_owned(tenant_id, run_id, {"heartbeat_at": datetime.now(timezone.utc)})

    def _update_owned(self, tenant_id: str, run_id: UUID, values: dict[str, Any]) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.Session() as session:
            updated = session.execute(
                update(RunModel)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
                .values(**values)
                .returning(RunModel.id)
            ).fetchone()
            if updated is None:
                session.rollback()
                raise RunNotFound(tenant_id, run_id)
            session.commit()

    def get_run(self, tenant_id: str, run_id: UUID) -> dict | None:
        """Get a run by ID. Returns None if not found or belongs to another tenant.

        display_status is `recovering` for a running run whose heartbeat is older than 20 s.
        """
        with self.Session() as session:
            run = session.execute(
                select(RunModel).where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).scalar_one_or_none()
            return None if run is None else _run_dict(run)

    def list_runs(
        self,
        tenant_id: str,
        *,
        limit: int = 50,
        before: RunCursor | None = None,
        agent_id: UUID | str | None = None,
        status: str | list[str] | tuple[str, ...] | None = None,
        kind: str | None = None,
        tool_id: str | None = None,
        workflow_id: UUID | str | None = None,
        knowledge_base_id: UUID | str | None = None,
        parent_run_id: UUID | str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        statement_timeout_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """The tenant's runs ordered (created_at DESC, id DESC): at most `limit`, strictly after the
        `before` position in that order (RunCursor.after(last row of the previous page)),
        optionally only one agent's, one status's, one kind's ('agent' | 'tool') and/or one tool's.

        The cursor is the pair, not the timestamp alone, so rows sharing a created_at are never
        skipped or repeated across pages.

        Child runs (a workflow's agent steps) are listed only when `parent_run_id` names their
        workflow run; otherwise only top-level runs are returned. `workflow_id` filters workflow runs,
        `knowledge_base_id` a knowledge base's runs (its kb_ingest and search tester runs).

        Drill-down from metrics (P0.6): `status` may be several statuses (any of them),
        `created_from` / `created_to` bound `created_at` to `[from, to)`, and `model` / `provider`
        keep runs that used that model (eap_run_store.metrics.run_model_condition). That filter is
        two EXISTS lookups per run and may walk far back in history to fill a page, so the query
        then runs under the metrics `statement_timeout` (`statement_timeout_ms`, default 5 s) and
        raises MetricsQueryTimeout when it fires."""
        query = select(RunModel).where(RunModel.tenant_id == tenant_id)
        if parent_run_id is not None:
            query = query.where(RunModel.parent_run_id == str(parent_run_id))
        else:
            query = query.where(RunModel.parent_run_id.is_(None))
        if workflow_id is not None:
            query = query.where(RunModel.workflow_id == str(workflow_id))
        if knowledge_base_id is not None:
            query = query.where(RunModel.knowledge_base_id == str(knowledge_base_id))
        if before is not None:
            query = query.where(tuple_(RunModel.created_at, RunModel.id) < tuple_(before.created_at, before.id))
        if agent_id is not None:
            query = query.where(RunModel.agent_id == str(agent_id))
        if isinstance(status, str):
            query = query.where(RunModel.status == status)
        elif status is not None:
            query = query.where(RunModel.status.in_(list(status)))
        if created_from is not None:
            query = query.where(RunModel.created_at >= created_from)
        if created_to is not None:
            query = query.where(RunModel.created_at < created_to)
        timeout_ms = None
        if model is not None or provider is not None:
            from eap_run_store.metrics import DEFAULT_STATEMENT_TIMEOUT_MS, run_model_condition

            query = query.where(run_model_condition(RunModel, tenant_id, model, provider))
            timeout_ms = int(statement_timeout_ms or DEFAULT_STATEMENT_TIMEOUT_MS)
        if kind is not None:
            query = query.where(RunModel.kind == kind)
        if tool_id is not None:
            query = query.where(RunModel.tool_id == tool_id)
        query = query.order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit)
        with self.Session() as session:
            if timeout_ms is None:
                return [_run_dict(r) for r in session.execute(query).scalars()]
            from sqlalchemy.exc import DBAPIError

            from eap_run_store.metrics import MetricsQueryTimeout

            try:
                # SET LOCAL: only this read's transaction, reset when the session ends it.
                session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                return [_run_dict(r) for r in session.execute(query).scalars()]
            except DBAPIError as exc:
                if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014":  # query_canceled
                    raise MetricsQueryTimeout(timeout_ms) from None
                raise

    # ------------------------------------------------------------------ conversations

    def create_conversation(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        title: str | None = None,
        conversation_id: UUID | str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a conversation; idempotent on a client-supplied `conversation_id`.

        Returns (conversation, created). Raises ConversationIdConflict if the id belongs to another tenant.
        """
        from sqlalchemy.dialects.postgresql import insert

        if not tenant_id:
            raise ValueError("tenant_id is required")
        conv_id = str(conversation_id or uuid4())
        now = datetime.now(timezone.utc)
        with self.Session() as session:
            inserted = session.execute(
                insert(ConversationModel)
                .values(id=conv_id, tenant_id=tenant_id, agent_id=str(agent_id), title=title, created_at=now, updated_at=now)
                .on_conflict_do_nothing(constraint="conversations_pkey")
                .returning(ConversationModel.id)
            ).fetchone()
            session.commit()
            existing = session.get(ConversationModel, conv_id)
            if existing is None or existing.tenant_id != tenant_id:
                raise ConversationIdConflict(conv_id)
            row = session.execute(self._conversation_select().where(ConversationModel.id == conv_id)).one()
            return self._conversation_from_row(row), inserted is not None

    @staticmethod
    def _conversation_select():
        """Conversations joined to their latest turn (LATERAL, one index probe on
        uq_runs_conversation_turn per conversation). Turns are numbered gap-free from 1 under the
        conversation lock, so the latest turn number is the turn count."""
        latest = (
            select(RunModel.turn, RunModel.id, RunModel.status, RunModel.heartbeat_at)
            .where(RunModel.conversation_id == ConversationModel.id)
            .order_by(RunModel.turn.desc())
            .limit(1)
            .lateral("latest_turn")
        )
        return select(
            ConversationModel,
            func.coalesce(latest.c.turn, 0),
            latest.c.id,
            latest.c.status,
            latest.c.heartbeat_at,
        ).outerjoin(latest, true())

    @staticmethod
    def _conversation_from_row(row) -> dict[str, Any]:
        conv, turn_count, run_id, run_status, heartbeat_at = row
        return _iso_conversation(conv, turn_count, (run_id, run_status, heartbeat_at))

    def get_conversation(self, tenant_id: str, conversation_id: UUID | str) -> dict[str, Any] | None:
        """The tenant's conversation, or None (unknown, or another tenant's)."""
        query = self._conversation_select().where(
            ConversationModel.id == str(conversation_id), ConversationModel.tenant_id == tenant_id
        )
        with self.Session() as session:
            row = session.execute(query).one_or_none()
            return None if row is None else self._conversation_from_row(row)

    def list_conversations(
        self,
        tenant_id: str,
        agent_id: UUID | str | None = None,
        limit: int = 100,
        before: ConversationCursor | None = None,
    ) -> list[dict[str, Any]]:
        """The tenant's conversations ordered (updated_at DESC, id DESC), at most `limit`, strictly
        after the `before` position (ConversationCursor.after(last row of the previous page)).

        updated_at is the last activity: creation, then each turn start (create_turn). A rename does
        not move a conversation. Each row carries turn_count and the latest turn's run id, status
        and display status (None without turns)."""
        query = self._conversation_select().where(ConversationModel.tenant_id == tenant_id)
        if agent_id is not None:
            query = query.where(ConversationModel.agent_id == str(agent_id))
        if before is not None:
            query = query.where(
                tuple_(ConversationModel.updated_at, ConversationModel.id) < tuple_(before.updated_at, before.id)
            )
        query = query.order_by(ConversationModel.updated_at.desc(), ConversationModel.id.desc()).limit(limit)
        with self.Session() as session:
            return [self._conversation_from_row(row) for row in session.execute(query).all()]

    def rename_conversation(self, tenant_id: str, conversation_id: UUID | str, title: str) -> dict[str, Any]:
        """Set the title (the caller validates it). Under the conversation's row lock, the lock
        create_turn's auto-title takes, so a user title is never overwritten by a concurrent first
        turn. updated_at is not changed: renaming does not reorder the list. Raises
        ConversationNotFound for another tenant's or an unknown conversation."""
        conv_id = str(conversation_id)
        with self.Session() as session:
            conv = session.execute(
                select(ConversationModel)
                .where(ConversationModel.id == conv_id, ConversationModel.tenant_id == tenant_id)
                .with_for_update()
            ).scalar_one_or_none()
            if conv is None:
                session.rollback()
                raise ConversationNotFound(tenant_id, conv_id)
            conv.title = title
            session.commit()
        renamed = self.get_conversation(tenant_id, conv_id)
        if renamed is None:  # deleted in between; there is no delete today
            raise ConversationNotFound(tenant_id, conv_id)
        return renamed

    def list_turns(self, tenant_id: str, conversation_id: UUID | str) -> list[dict[str, Any]]:
        """The conversation's runs in turn order ([] for another tenant's conversation)."""
        with self.Session() as session:
            runs = session.execute(
                select(RunModel)
                .where(RunModel.tenant_id == tenant_id, RunModel.conversation_id == str(conversation_id))
                .order_by(RunModel.turn)
            ).scalars().all()
            return [_run_dict(r) for r in runs]

    def create_turn(
        self,
        tenant_id: str,
        conversation_id: UUID | str,
        agent_version: int | None,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
        *,
        request_context: dict[str, Any] | None = None,
        draft_spec: dict[str, Any] | None = None,
        model_id: str | None = None,
        model_overridden: bool = False,
    ) -> tuple[UUID, bool]:
        """Insert the conversation's next turn as a queued run; idempotent on `run_id`.

        Under the conversation's row lock: if `run_id` already exists for this tenant it is returned
        as (run_id, False) whatever it is (the caller checks that it is the same request); otherwise
        the latest turn must be terminal (else TurnInProgress) and the new run gets turn = latest + 1.
        Raises ConversationNotFound for another tenant's or an unknown conversation, RunIdConflict
        if `run_id` belongs to another tenant.
        `trace_id`/`traceparent` and the run options (`request_context`, `draft_spec` with
        `agent_version` None for a draft turn, `model_id`, `model_overridden`) as in create_run.
        """
        from sqlalchemy.dialects.postgresql import insert

        conv_id = str(conversation_id)
        with self.Session() as session:
            conv = session.execute(
                select(ConversationModel)
                .where(ConversationModel.id == conv_id, ConversationModel.tenant_id == tenant_id)
                .with_for_update()
            ).scalar_one_or_none()
            if conv is None:
                session.rollback()
                raise ConversationNotFound(tenant_id, conv_id)

            existing = session.get(RunModel, str(run_id))
            if existing is not None:
                session.rollback()
                if existing.tenant_id != tenant_id:
                    raise RunIdConflict(run_id)
                return UUID(existing.id), False

            latest = session.execute(
                select(RunModel).where(RunModel.conversation_id == conv_id).order_by(RunModel.turn.desc()).limit(1)
            ).scalar_one_or_none()
            if latest is not None and latest.status not in TERMINAL_RUN_STATUSES:
                session.rollback()
                raise TurnInProgress(conv_id, latest.id, latest.status)

            now = datetime.now(timezone.utc)
            inserted = session.execute(
                insert(RunModel)
                .values(
                    id=str(run_id),
                    tenant_id=tenant_id,
                    agent_id=conv.agent_id,
                    agent_version=agent_version,
                    status="queued",
                    input=input,
                    conversation_id=conv_id,
                    turn=(latest.turn if latest is not None else 0) + 1,
                    created_at=now,
                    updated_at=now,
                    trace_id=trace_id,
                    traceparent=traceparent,
                    **_run_option_values(request_context, draft_spec, model_id, model_overridden),
                )
                .on_conflict_do_nothing(constraint="runs_pkey")
                .returning(RunModel.id)
            ).fetchone()
            if inserted is None:
                # The same run_id was inserted concurrently by a path that does not take this lock
                # (a standalone run): report it like any existing run.
                session.rollback()
                raced = self.get_run(tenant_id, run_id)
                if raced is None:
                    raise RunIdConflict(run_id)
                return UUID(raced["id"]), False
            conv.updated_at = now
            # Auto-title, once: only the first turn, only when the conversation has no title (one the
            # creator or a rename gave it), under the same row lock rename_conversation takes.
            if latest is None and conv.title is None:
                conv.title = auto_title(input)
            session.commit()
            return run_id, True

    def completed_turns_before(self, tenant_id: str, conversation_id: UUID | str, turn: int) -> list[dict[str, Any]]:
        """The COMPLETED turns numbered below `turn`, in order: what a turn's message history is built from.

        Each item: turn, run_id, input, output, messages (the run's stored new messages, or None for a
        run completed without them), created_at.
        """
        with self.Session() as session:
            runs = session.execute(
                select(RunModel)
                .where(
                    RunModel.tenant_id == tenant_id,
                    RunModel.conversation_id == str(conversation_id),
                    RunModel.turn < turn,
                    RunModel.status == "completed",
                )
                .order_by(RunModel.turn)
            ).scalars().all()
            return [
                {
                    "turn": r.turn,
                    "run_id": r.id,
                    "input": r.input,
                    "output": r.output,
                    "messages": r.messages,
                    "created_at": r.created_at,
                }
                for r in runs
            ]


def append_event_in_session(
    session,
    tenant_id: str,
    run_id: UUID | str,
    event_key: str,
    type: RunEventType | str,
    data: dict | None = None,
    usage: dict[str, Any] | None = None,
) -> tuple[RunEvent, bool]:
    """EventLog.append's work inside the caller's open ORM session, which commits it: a store that
    must write its own rows, an event and a usage row in one transaction (KnowledgeStore) uses this.
    Same rules as EventLog.append (seq under the runs row lock, repeated key = no write).
    Raises RunNotFound if the run does not exist or belongs to another tenant; the caller rolls back."""
    from sqlalchemy.dialects.postgresql import insert

    if not tenant_id:
        raise ValueError("tenant_id is required")
    # Lock the tenant's runs row to ensure seq uniqueness (and prove ownership).
    owned = session.execute(
        text("SELECT id FROM runs WHERE id = :run_id AND tenant_id = :tenant_id FOR UPDATE"),
        {"run_id": str(run_id), "tenant_id": tenant_id},
    ).fetchone()
    if owned is None:
        raise RunNotFound(tenant_id, run_id)

    existing_event = session.execute(
        select(ExecutionEventModel).where(
            ExecutionEventModel.tenant_id == tenant_id,
            ExecutionEventModel.run_id == str(run_id),
            ExecutionEventModel.event_key == event_key,
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        # Event already exists, return it without consuming seq
        return _run_event_from_model(existing_event), False

    new_seq = session.execute(
        text("SELECT COALESCE(MAX(seq), -1) FROM execution_events WHERE run_id = :run_id"),
        {"run_id": str(run_id)},
    ).scalar_one() + 1
    event_model = ExecutionEventModel(
        tenant_id=tenant_id,
        run_id=str(run_id),
        seq=new_seq,
        event_key=event_key,
        type=type,
        data=data or {},
        ts=datetime.now(timezone.utc),
    )
    session.add(event_model)
    session.flush()
    if usage is not None:
        row = {"created_at": event_model.ts, **usage, "tenant_id": tenant_id, "run_id": str(run_id)}
        session.execute(
            insert(RunModelRequestModel).values(**row).on_conflict_do_nothing(constraint="run_model_requests_pkey")
        )
    return _run_event_from_model(event_model), True


class EventLog:
    """Event log for a run."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def append(
        self,
        tenant_id: str,
        run_id: UUID,
        event_key: str,
        type: RunEventType,
        data: dict | None = None,
        usage: dict[str, Any] | None = None,
    ) -> tuple[RunEvent, bool]:
        """Append an event to a run owned by `tenant_id`. Seq is assigned atomically.

        Returns (RunEvent, created: bool) where created=False if the event_key already exists.
        A repeated key never consumes a seq number.
        `usage`: a model request's usage row (the run_model_requests columns except tenant_id and
        run_id: request_key, model_id, provider_model_name, input_tokens, output_tokens,
        cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, and
        optionally operation ('chat' by default, 'embedding' for an embedding call) and
        created_at), inserted in the event's transaction with ON CONFLICT DO NOTHING. A
        repeated key writes no row: the first event's row stands, so usage and cost are recorded
        exactly once per request and never recomputed.
        Raises RunNotFound (and writes nothing) if the run does not exist or belongs to another tenant.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.Session() as session:
            try:
                event, created = append_event_in_session(session, tenant_id, run_id, event_key, type, data, usage)
            except RunNotFound:
                session.rollback()
                raise
            session.commit()
            return event, created

    def get_event(self, tenant_id: str, run_id: UUID | str, event_key: str) -> RunEvent | None:
        """The run's event with this key, or None (no such event, or another tenant's run)."""
        with self.Session() as session:
            event = session.execute(
                select(ExecutionEventModel).where(
                    ExecutionEventModel.tenant_id == tenant_id,
                    ExecutionEventModel.run_id == str(run_id),
                    ExecutionEventModel.event_key == event_key,
                )
            ).scalar_one_or_none()
            return None if event is None else _run_event_from_model(event)

    def append_step_decision(
        self, tenant_id: str, run_id: UUID | str, step_id: str, type: RunEventType, data: dict | None = None
    ) -> tuple[str, RunEvent | None]:
        """Record the one decision of a workflow approval step, atomically under the run-row lock.

        The decision is the event `step:{step_id}:decision` (type step_approved | step_rejected |
        step_timed_out). It is written only while the step is waiting: the run is not terminal, its
        `step:{step_id}:waiting` event exists and no decision exists yet. Returns
        ("created", event), ("exists", the earlier decision) or ("not_waiting", None); exactly one
        caller ever gets "created" (the API's approve/reject and the worker's timeout race on it).
        Raises RunNotFound for an unknown run or another tenant's run. Same seq rules as append.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        decision_key = f"step:{step_id}:decision"
        with self.Session() as session:
            owned = session.execute(
                text("SELECT status FROM runs WHERE id = :run_id AND tenant_id = :tenant_id FOR UPDATE"),
                {"run_id": str(run_id), "tenant_id": tenant_id},
            ).fetchone()
            if owned is None:
                session.rollback()
                raise RunNotFound(tenant_id, run_id)
            keys = set(
                session.execute(
                    select(ExecutionEventModel.event_key).where(
                        ExecutionEventModel.run_id == str(run_id),
                        ExecutionEventModel.event_key.in_([f"step:{step_id}:waiting", decision_key, "end"]),
                    )
                ).scalars()
            )
            if decision_key in keys:
                existing = session.execute(
                    select(ExecutionEventModel).where(
                        ExecutionEventModel.run_id == str(run_id), ExecutionEventModel.event_key == decision_key
                    )
                ).scalar_one()
                session.commit()
                return "exists", _run_event_from_model(existing)
            if (
                owned.status in TERMINAL_RUN_STATUSES
                or "end" in keys
                or f"step:{step_id}:waiting" not in keys
            ):
                session.rollback()
                return "not_waiting", None
            new_seq = session.execute(
                text("SELECT COALESCE(MAX(seq), -1) FROM execution_events WHERE run_id = :run_id"),
                {"run_id": str(run_id)},
            ).scalar_one() + 1
            event_model = ExecutionEventModel(
                tenant_id=tenant_id,
                run_id=str(run_id),
                seq=new_seq,
                event_key=decision_key,
                type=type,
                data=data or {},
                ts=datetime.now(timezone.utc),
            )
            session.add(event_model)
            session.commit()
            return "created", _run_event_from_model(event_model)

    def list_events(self, tenant_id: str, run_id: UUID, after_seq: int = -1) -> list[RunEvent]:
        """List events for a run after a given seq. Returns empty for another tenant's run."""
        with self.Session() as session:
            # First check if run belongs to this tenant
            run = session.execute(
                select(RunModel).where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if run is None:
                return []

            # Get events
            events = session.execute(
                select(ExecutionEventModel)
                .where(
                    ExecutionEventModel.tenant_id == tenant_id,
                    ExecutionEventModel.run_id == str(run_id),
                    ExecutionEventModel.seq > after_seq,
                )
                .order_by(ExecutionEventModel.seq)
            ).scalars().all()

            return [_run_event_from_model(e) for e in events]


class PostgresSideEffectStore:
    """Postgres implementation of side-effect store."""

    def __init__(self, engine, tenant_id: str):
        self.engine = engine
        self.tenant_id = tenant_id
        self.Session = sessionmaker(bind=engine)

    def reserve(self, key: str) -> tuple[str, dict | None]:
        """Reserve a side effect.
        
        Returns ('new', None) if this call inserted the reservation.
        Returns ('reserved', None) if the key was already reserved.
        Returns ('completed', result) if the key was already completed.
        """
        from sqlalchemy.dialects.postgresql import insert
        
        with self.Session() as session:
            # First, try to check if the key exists
            existing = session.execute(
                select(SideEffectModel).where(
                    SideEffectModel.tenant_id == self.tenant_id,
                    SideEffectModel.key == key
                )
            ).scalar_one_or_none()
            
            if existing is not None:
                # Key exists, return its state
                session.commit()
                return existing.state, existing.result
            
            # Key doesn't exist, try to insert
            stmt = insert(SideEffectModel).values(
                tenant_id=self.tenant_id,
                key=key,
                state="reserved",
                result=None,
                created_at=datetime.now(timezone.utc),
                completed_at=None
            ).on_conflict_do_nothing(
                constraint="side_effects_pkey"
            ).returning(
                SideEffectModel.state,
                SideEffectModel.result
            )
            
            result = session.execute(stmt).fetchone()
            
            if result is not None:
                # We inserted a new row - return 'new' not the stored state
                session.commit()
                return ("new", None)
            
            # Shouldn't get here, but if we do, try to fetch again
            existing = session.execute(
                select(SideEffectModel).where(
                    SideEffectModel.tenant_id == self.tenant_id,
                    SideEffectModel.key == key
                )
            ).scalar_one_or_none()
            
            session.commit()
            
            if existing is None:
                # This shouldn't happen
                return ("reserved", None)
            
            return existing.state, existing.result

    def complete(self, key: str, result: dict) -> None:
        """Complete a side effect.
        
        Raises ValueError if the key is not reserved.
        """
        with self.Session() as session:
            # First check if the key exists and is reserved
            existing = session.execute(
                select(SideEffectModel).where(
                    SideEffectModel.tenant_id == self.tenant_id,
                    SideEffectModel.key == key
                )
            ).scalar_one_or_none()
            
            if existing is None:
                raise ValueError(f"Side effect key '{key}' not found (not reserved)")
            
            if existing.state != "reserved":
                raise ValueError(f"Side effect key '{key}' is not reserved (state: {existing.state})")
            
            # Update to completed
            existing.state = "completed"
            existing.result = result
            existing.completed_at = datetime.now(timezone.utc)
            session.commit()


def get_side_effect_store(engine, tenant_id: str) -> PostgresSideEffectStore:
    """Factory function to create a side effect store."""
    return PostgresSideEffectStore(engine, tenant_id)
