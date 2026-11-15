"""Postgres store for durable runs and event logging."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
import json
import re
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
RUN_KINDS = ("agent", "tool", "workflow", "kb_ingest", "eval")

# runs.error of a run whose hand-off to the orchestrator failed (the API could not enqueue it).
# Stable text: RunStore.requeue_after_enqueue_failure recognises the state by it.
ENQUEUE_FAILED_ERROR = (
    "EnqueueFailed: the run could not be started (enqueue_failed); retry the same request with the same run_id"
)

# An agent run names exactly one of its published agent version and a draft snapshot (a draft test
# run), and no tool or workflow; a tool run names its tool only and is never a conversation turn
# (it may name a knowledge base: the search tester); a workflow run names its published workflow
# version only; a kb_ingest run names its knowledge base only; an eval run (0015_evals) names its
# agent with exactly one of its version and a draft snapshot, and its test suite. Same text as
# migration 0015_evals.
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
    " OR (kind = 'eval' AND agent_id IS NOT NULL AND (agent_version IS NULL) <> (draft_spec IS NULL)"
    " AND eval_suite_id IS NOT NULL AND tool_id IS NULL AND workflow_id IS NULL AND workflow_version IS NULL"
    " AND knowledge_base_id IS NULL AND conversation_id IS NULL)"
)
# run_model_requests.operation (0011_knowledge_bases): a chat model request or an embedding call.
MODEL_REQUEST_OPERATIONS = ("chat", "embedding")
# A workflow's agent step runs as a child agent run linked to its workflow run and step (never a
# conversation turn). Same text as migration 0007_workflow_runs.
# Invariant: runs nest at most two levels. Only an agent run can have a parent, and its parent is a
# workflow run or an eval run (an eval case's child, 0015_evals), which never has one; so a child's parent is its root run. pending_approvals.root_run_id
# (0014_approvals) relies on this, and takes the parent's own parent if deeper nesting is ever added.
RUN_PARENT_LINK_CHECK = (
    "(parent_run_id IS NULL AND parent_step_id IS NULL)"
    " OR (parent_run_id IS NOT NULL AND parent_step_id IS NOT NULL AND kind = 'agent' AND conversation_id IS NULL)"
)
# runs.output_format (0014_approvals): `json` when runs.output is a structured output's canonical
# JSON text; NULL = text. Same text as migration 0014_approvals.
RUN_OUTPUT_FORMAT_CHECK = "output_format IN ('text', 'json')"
RUN_OUTPUT_FORMATS = ("text", "json")
# conversations.resource_id (0012_memory): the owner a conversation acts for, set once at creation.
RESOURCE_ID_PATTERN = r"^[A-Za-z0-9_.:@-]{1,128}$"
# Same text as migration 0012_memory.
CONVERSATION_RESOURCE_ID_CHECK = "resource_id IS NULL OR resource_id ~ '^[A-Za-z0-9_.:@-]{1,128}$'"


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
    # The resource (user or entity) the conversation belongs to (0012_memory): set once at creation,
    # never changed. NULL for conversations created before 0012 (resource-scoped memory off).
    resource_id: Mapped[StrAnnotated | None] = mapped_column("resource_id", String, nullable=True)

    __table_args__ = (
        # Lets runs reference (tenant_id, conversation_id), so a run can only join its own tenant's thread.
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        Index("ix_conversations_tenant_updated", "tenant_id", "updated_at"),
        # One agent's conversations in list order (0010_run_options).
        Index(
            "ix_conversations_tenant_agent_updated", "tenant_id", "agent_id", text("updated_at DESC"), text("id DESC")
        ),
        CheckConstraint(CONVERSATION_RESOURCE_ID_CHECK, name="ck_conversations_resource_id"),
        # A resource's conversations (forget-resource).
        Index(
            "ix_conversations_tenant_resource",
            "tenant_id",
            "resource_id",
            postgresql_where=text("resource_id IS NOT NULL"),
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
    # The format of `output` (0014_approvals): 'json' for a structured output (canonical JSON text),
    # 'text' or NULL for text.
    output_format: Mapped[str | None] = mapped_column("output_format", String, nullable=True)
    # The test suite an eval run executes (0015_evals; its snapshot is `input`). No FK: the run
    # outlives a deleted suite as its audit and cost record.
    eval_suite_id: Mapped[str | None] = mapped_column("eval_suite_id", PG_UUID(as_uuid=False), nullable=True)
    # The snapshot's suite_hash (sha256 hex), recorded once at creation: compare uses it, never
    # re-validating an old snapshot.
    eval_suite_hash: Mapped[str | None] = mapped_column("eval_suite_hash", String, nullable=True)

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
        CheckConstraint(RUN_OUTPUT_FORMAT_CHECK, name="ck_runs_output_format"),
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
        # A test suite's eval runs, newest first (0015_evals).
        Index(
            "ix_runs_tenant_eval_suite_created",
            "tenant_id",
            "eval_suite_id",
            "created_at",
            postgresql_where=text("eval_suite_id IS NOT NULL"),
        ),
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

    __table_args__ = (
        # Prefix deletes of a run's rows (`{run_id}:...`, `wm:{run_id}:...`) with LIKE (0012_memory).
        Index(
            "ix_side_effects_tenant_key_pattern",
            "tenant_id",
            "key",
            postgresql_ops={"key": "text_pattern_ops"},
        ),
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


class ConversationResourceConflict(ConversationIdConflict):
    """A client-supplied conversation id already exists in the tenant with another resource_id
    (resource ids are immutable; API: 409). A subclass of ConversationIdConflict, so a caller that
    only knows the parent still answers 409."""

    def __init__(self, conversation_id: UUID | str):
        super().__init__(conversation_id)  # pickle-safe, as RunNotFound

    def __str__(self) -> str:
        return f"Conversation {self.conversation_id} already exists with a different resource_id"


def validate_resource_id(resource_id: str | None) -> str | None:
    """Return `resource_id` if it is None or matches RESOURCE_ID_PATTERN and is not dot-only; raise
    ValueError otherwise. Dot-only ids (`.`, `..`, ...) are refused like dot-only document keys: a
    resource id appears in URL paths (`/resources/{resource_id}/...`), where path normalisation would
    turn them into another route. (The database CHECK keeps migration 0012's pattern.)"""
    if resource_id is None:
        return None
    if not isinstance(resource_id, str) or re.fullmatch(RESOURCE_ID_PATTERN, resource_id) is None:
        raise ValueError("resource_id must be 1 to 128 characters of A-Z, a-z, 0-9, '_', '.', ':', '@' or '-'")
    if not resource_id.strip("."):
        raise ValueError("resource_id must not consist of dots only")
    return resource_id


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


def _latest_turn_display_status(
    status: str | None, heartbeat_at: datetime | None, waiting_approval: bool = False
) -> str | None:
    """As _display_status, for the latest-turn columns of a conversation query."""
    if status == "running" and heartbeat_at is not None:
        if (datetime.now(timezone.utc) - heartbeat_at).total_seconds() > 20:
            return "recovering"
    if status == "running" and waiting_approval:
        return "waiting_approval"
    return status


def _iso_conversation(
    conv: ConversationModel,
    turn_count: int,
    last_turn: tuple[str | None, str | None, datetime | None] = (None, None, None),
    last_turn_waiting_approval: bool = False,
) -> dict[str, Any]:
    """`last_turn` is the latest turn's (run id, status, heartbeat_at), all None without turns;
    `last_turn_waiting_approval`: it has an open approval (0014_approvals)."""
    last_run_id, last_status, last_heartbeat = last_turn
    return {
        "id": conv.id,
        "tenant_id": conv.tenant_id,
        "agent_id": conv.agent_id,
        "resource_id": conv.resource_id,
        "title": conv.title,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "turn_count": turn_count,
        "last_turn_run_id": last_run_id,
        "last_turn_status": last_status,
        "last_turn_display_status": _latest_turn_display_status(
            last_status, last_heartbeat, last_turn_waiting_approval
        ),
    }


# A running run whose heartbeat is older than this shows as `recovering` (display status only).
RECOVERING_AFTER = timedelta(seconds=20)


# Display-only statuses (never stored): derived from the heartbeat and the approvals projection.
DISPLAY_ONLY_STATUSES = ("recovering", "waiting_approval")


def _display_status(run: RunModel, waiting_approval: bool = False) -> str:
    """The stored status, except for a running run: `recovering` while its heartbeat is stale (a
    worker restart, until recovery replays), else `waiting_approval` while it (or, for a workflow
    run, one of its child runs) has an open pending_approvals row (`waiting_approval`, computed by
    the read with eap_run_store.approvals.open_approval_exists)."""
    if run.status == "running" and run.heartbeat_at is not None:
        if datetime.now(timezone.utc) - run.heartbeat_at > RECOVERING_AFTER:
            return "recovering"
    if run.status == "running" and waiting_approval:
        return "waiting_approval"
    return run.status


def _stale_heartbeat() -> Any:
    return RunModel.heartbeat_at.is_not(None) & (
        RunModel.heartbeat_at < datetime.now(timezone.utc) - RECOVERING_AFTER
    )


def _status_condition(statuses: list[str]) -> Any:
    """`status IN (...)`; the display-only `recovering` matches running runs with a stale heartbeat
    and `waiting_approval` running runs with an open approval and a fresh heartbeat (as
    _display_status), so a list filter on them is not always empty. `running` keeps matching every
    running run, recovering and waiting ones included."""
    conditions = []
    stored = [s for s in statuses if s not in DISPLAY_ONLY_STATUSES]
    if stored:
        conditions.append(RunModel.status.in_(stored))
    if "recovering" in statuses:
        conditions.append((RunModel.status == "running") & _stale_heartbeat())
    if "waiting_approval" in statuses:
        from eap_run_store.approvals import open_approval_exists

        conditions.append((RunModel.status == "running") & ~_stale_heartbeat() & open_approval_exists())
    return or_(*conditions)


def _run_select() -> Any:
    """SELECT runs plus whether each has an open approval (for _run_dict's display status)."""
    from eap_run_store.approvals import open_approval_exists

    return select(RunModel, open_approval_exists().label("waiting_approval"))


def _run_dict(run: RunModel, waiting_approval: bool = False) -> dict[str, Any]:
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
        "display_status": _display_status(run, waiting_approval),
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
        "output_format": run.output_format,
        "eval_suite_id": run.eval_suite_id,
        "eval_suite_hash": run.eval_suite_hash,
    }


# ---------------------------------------------------------------------------- usage totals (0008)


# What mark_enqueue_failed records besides the status: the run is finished, without any model
# request. Cleared (all NULL) when a failed hand-off is revived (mark_running, requeue).
_ENQUEUE_FAILED_FINISH = ("finished_at", "input_tokens", "output_tokens", "cost_usd")


def _enqueue_failed_finish() -> dict[str, Any]:
    return {"finished_at": datetime.now(timezone.utc), "input_tokens": 0, "output_tokens": 0, "cost_usd": Decimal(0)}


def _status_values(
    tenant_id: str, run_id: UUID | str, status: str, output: str | None, error: str | None, messages: list[Any] | None,
    output_format: str | None = None,
) -> dict[str, Any]:
    """The columns RunStore.set_status writes (see there)."""
    values: dict[str, Any] = {"status": status, "updated_at": datetime.now(timezone.utc)}
    if output is not None:
        values["output"] = output
    if error is not None:
        values["error"] = error
    if messages is not None:
        values["messages"] = func.coalesce(RunModel.messages, func.cast(_json_param(messages), JSONB))
    if output_format is not None:
        if output_format not in RUN_OUTPUT_FORMATS:
            raise ValueError(f"output_format must be one of {RUN_OUTPUT_FORMATS}")
        values["output_format"] = output_format
    if status in TERMINAL_RUN_STATUSES:
        values.update(_finish_values(tenant_id, str(run_id), status))
    return values


def set_status_in_session(
    session, tenant_id: str, run_id: UUID | str, status: str, output: str | None = None, error: str | None = None,
) -> None:
    """RunStore.set_status's work inside the caller's open session (which commits it): a store that
    must check its own rows and finish a run in one transaction (KnowledgeStore.write_search_result).
    Raises RunNotFound (the caller rolls back)."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    updated = session.execute(
        update(RunModel)
        .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
        .values(**_status_values(tenant_id, run_id, status, output, error, None))
        .returning(RunModel.id)
    ).fetchone()
    if updated is None:
        raise RunNotFound(tenant_id, run_id)


def _finish_values(tenant_id: str, run_id: str, status: str) -> dict[str, Any]:
    """The columns a terminal set_status writes besides the status, as SQL expressions evaluated in
    the same UPDATE: `finished_at` and the usage totals.

    Totals are the SUM over the run's `run_model_requests` rows and, for a workflow run, over its
    child runs' rows (a workflow run makes no model request itself). Tokens are 0 for a run without
    requests; `cost_usd` is NULL if any request is unpriced, else the exact sum (0 without requests).
    Computed from durable rows, so a re-executed finish step writes the same totals.
    `finished_at` is kept when the run already has this terminal status (a re-executed finish
    step), and set to now() otherwise."""
    return {
        **_totals_values(tenant_id, run_id),
        "finished_at": case(
            ((RunModel.status == status) & RunModel.finished_at.is_not(None), RunModel.finished_at),
            else_=datetime.now(timezone.utc),
        ),
    }


def _totals_values(tenant_id: str, run_id: str) -> dict[str, Any]:
    """The usage totals columns (`input_tokens`, `output_tokens`, `cost_usd`) as SQL expressions
    over the run's (and its child runs') `run_model_requests` rows; see _finish_values."""
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
    }


def insert_usage_in_session(session, tenant_id: str, run_id: UUID | str, usage: dict[str, Any], at: datetime | None = None) -> None:
    """Insert one `run_model_requests` row (first write wins per request key) inside the caller's
    session, then, if the run is already finished (`finished_at` set: a post-turn step such as the
    observer or the memory index runs after eap.finish_run), recompute its usage totals from its
    rows in the same transaction. `status` and `finished_at` never change here. The caller holds (or
    takes) the run row lock: append_event_in_session locks it first."""
    from sqlalchemy.dialects.postgresql import insert

    row = {"created_at": at or datetime.now(timezone.utc), **usage, "tenant_id": tenant_id, "run_id": str(run_id)}
    session.execute(
        insert(RunModelRequestModel).values(**row).on_conflict_do_nothing(constraint="run_model_requests_pkey")
    )
    session.execute(
        update(RunModel)
        .where(RunModel.tenant_id == tenant_id, RunModel.id == str(run_id), RunModel.finished_at.is_not(None))
        .values(**_totals_values(tenant_id, str(run_id)))
        .execution_options(synchronize_session=False)
    )


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

    def create_eval_run(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        agent_version: int | None,
        eval_suite_id: UUID | str,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
        *,
        draft_spec: dict[str, Any] | None = None,
    ) -> tuple[UUID, bool]:
        """Create an eval run (kind 'eval', 0015_evals): `input` = the suite snapshot's canonical JSON
        (`EvalSuiteSnapshot.canonical_json()`, built by EvalStore.snapshot), against published version
        `agent_version` of `agent_id` or, with `agent_version` None, the draft snapshot `draft_spec`
        (exactly one: ck_runs_kind_target). The caller has checked that the agent and the suite belong
        to `tenant_id`. `input` is validated once here (ValueError if it is not a snapshot of
        `eval_suite_id`) and its `suite_hash()` is recorded in `eval_suite_hash`. Same idempotency
        and tenant rules as create_run."""
        from pydantic import ValidationError

        from eap_schemas.evals import EvalSuiteSnapshot

        try:
            snapshot = EvalSuiteSnapshot.model_validate_json(input)
        except ValidationError as exc:
            raise ValueError(f"input is not a valid suite snapshot: {exc.errors()[0]['msg']}") from None
        if str(snapshot.suite_id) != str(eval_suite_id):
            raise ValueError("input is a snapshot of another suite")
        return self._insert_run(
            tenant_id,
            run_id,
            kind="eval",
            agent_id=str(agent_id),
            agent_version=agent_version,
            eval_suite_id=str(eval_suite_id),
            eval_suite_hash=snapshot.suite_hash(),
            input=input,
            trace_id=trace_id,
            traceparent=traceparent,
            draft_spec=null() if draft_spec is None else draft_spec,
        )

    def create_eval_child_run(
        self,
        tenant_id: str,
        eval_run_id: UUID | str,
        case_id: UUID | str,
        agent_id: UUID | str,
        agent_version: int | None,
        input: str,
        run_id: UUID,
        *,
        draft_spec: dict[str, Any] | None = None,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Create the agent run that executes case `case_id` of eval run `eval_run_id` (same tenant:
        fk_runs_tenant_parent_run): kind 'agent', no conversation, `parent_step_id = case:{case_id}`,
        against published version `agent_version` or, with `agent_version` None, the draft snapshot
        `draft_spec` (exactly one, else ValueError). Same idempotency rules as create_run."""
        if (agent_version is None) == (draft_spec is None):
            raise ValueError("exactly one of agent_version and draft_spec is required")
        return self._insert_run(
            tenant_id,
            run_id,
            kind="agent",
            agent_id=str(agent_id),
            agent_version=agent_version,
            draft_spec=null() if draft_spec is None else draft_spec,
            input=input,
            parent_run_id=str(eval_run_id),
            parent_step_id=f"case:{case_id}",
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
        output_format: str | None = None,
    ) -> None:
        """Update run status and optionally output, error or messages, only for a run owned by `tenant_id`.

        `output_format` (AC.4) is `json` when `output` is a structured output's canonical JSON text
        (None leaves the column unchanged: NULL means text).

        `messages` is written once: a repeated call (a re-executed finish step) keeps the first value,
        so the history later turns read never changes after it was first recorded.
        A terminal status (completed, failed, needs_attention) also sets `finished_at` and the usage
        totals in the same statement (see _finish_values).
        Raises RunNotFound (and changes nothing) if the run does not exist or belongs to another tenant.
        """
        self._update_owned(
            tenant_id, run_id, _status_values(tenant_id, run_id, status, output, error, messages, output_format)
        )

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

    def mark_running(self, tenant_id: str, run_id: UUID, model_id: str | None = None) -> bool:
        """A workflow has started the run: status -> running, and an ENQUEUE_FAILED_ERROR left by a
        hand-off that raised although DBOS accepted it is cleared in the same statement (any other
        error is kept). RunNotFound for another tenant's or an unknown run. Returns True.

        A conversation turn is checked under the conversation's row lock (the lock create_turn
        takes). If a later turn exists, this turn was marked failed by its hand-off and the
        conversation has moved on: it is left unchanged (still failed) and False is returned, so
        the workflow must stop without running it. A run without a conversation (agent, tool or
        workflow run) never takes the lock and returns True, except a DETACHED turn: its
        conversation was deleted (delete_conversation set conversation_id, turn and input to NULL
        on a terminal run, e.g. a failed hand-off that DBOS accepted after all). It is left
        unchanged and False is returned, so it can never run and write output again.

        `model_id` (AC.17 review 2026-11-15): the spec's model, recorded as the run's effective model
        when the row has none (workflow-step and eval-case child runs are created without run
        options), so metrics can tell a fallback answer (a usage row of another model) on every run.
        A recorded model (an API-created run, or an override) is never replaced."""
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
        if model_id is not None:
            values["model_id"] = func.coalesce(RunModel.model_id, model_id)
        with self.Session() as session:
            run = session.execute(
                select(RunModel.conversation_id, RunModel.turn, RunModel.input, RunModel.status)
                .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).first()
            if run is None:
                raise RunNotFound(tenant_id, run_id)
            if run.conversation_id is not None:
                session.execute(
                    select(ConversationModel.id).where(ConversationModel.id == run.conversation_id).with_for_update()
                )
                # Re-read under the lock (the lock order of create_turn and delete_conversation:
                # conversation, then runs): a delete that held it has detached this run meanwhile.
                run = session.execute(
                    select(RunModel.conversation_id, RunModel.turn, RunModel.input, RunModel.status)
                    .where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
                ).one()
            if run.conversation_id is None and run.input is None and run.status in TERMINAL_RUN_STATUSES:
                # Detached by delete_conversation (every run is created with an input; only that
                # deletion clears it): never revive it.
                session.rollback()
                return False
            if run.conversation_id is not None:
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

    def recorded_cost_usd(self, tenant_id: str, run_id: UUID | str) -> Decimal:
        """The sum of `cost_usd` over the run's own `run_model_requests` rows recorded so far
        (AC.5 `eap.check_budget`): chat requests, failed fallback attempts that billed, moderation
        and embedding rows alike; an unpriced row counts as 0. Another tenant's run, or a run
        without rows, gives 0. Never the child runs' rows (a cap is per run)."""
        with self.Session() as session:
            total = session.execute(
                select(func.coalesce(func.sum(RunModelRequestModel.cost_usd), 0)).where(
                    RunModelRequestModel.tenant_id == tenant_id,
                    RunModelRequestModel.run_id == str(run_id),
                )
            ).scalar_one()
            return Decimal(total)

    def get_run(self, tenant_id: str, run_id: UUID) -> dict | None:
        """Get a run by ID. Returns None if not found or belongs to another tenant.

        display_status is `recovering` for a running run whose heartbeat is older than 20 s, else
        `waiting_approval` while it (or a child run of a workflow run) has an open approval.
        """
        with self.Session() as session:
            row = session.execute(
                _run_select().where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).one_or_none()
            return None if row is None else _run_dict(row[0], row[1])

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
        query = _run_select().where(RunModel.tenant_id == tenant_id)
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
        if status is not None:
            query = query.where(_status_condition([status] if isinstance(status, str) else list(status)))
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
                return [_run_dict(r, waiting) for r, waiting in session.execute(query).all()]
            from sqlalchemy.exc import DBAPIError

            from eap_run_store.metrics import MetricsQueryTimeout

            try:
                # SET LOCAL: only this read's transaction, reset when the session ends it.
                session.execute(text(f"SET LOCAL statement_timeout = {timeout_ms}"))
                return [_run_dict(r, waiting) for r, waiting in session.execute(query).all()]
            except DBAPIError as exc:
                if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014":  # query_canceled
                    raise MetricsQueryTimeout(timeout_ms) from None
                raise

    def chat_usage(self, tenant_id: str, run_ids: list[UUID | str]) -> dict[str, dict[str, Any]]:
        """The chat model's own usage per run (its own requests, not its child runs'), with the
        null rules of the trace's `chat_*` split (eap_run_store.trace._UsageSplit): one statement
        for the whole list, so a page of runs costs one query.

        A request is "chat" as in eap_run_store.trace.request_operation: not an embedding, a
        moderation check, an observer or a reflector request. The run's requests are its usage rows
        plus its `model_message` events without one (reserved, not completed: the trace counts them
        too). For each run with any usage row or such event: `chat_input_tokens`,
        `chat_output_tokens` (null when the run had chat requests but none was recorded; 0 when it
        had none) and `chat_cost_usd` (a decimal string; null when a chat request had no price or
        no usage row). Runs with neither are absent (the caller reports null: not recorded)."""
        ids = [str(r) for r in run_ids]
        if not ids:
            return {}
        not_chat_key = (
            "({k} LIKE 'moderation:%' OR {k} LIKE 'memory:observe:%' OR {k} LIKE 'memory:reflect:%')"
        )
        sql = text(
            "WITH rows AS ("
            "  SELECT run_id,"
            "   sum(input_tokens) FILTER (WHERE chat) AS inp, sum(output_tokens) FILTER (WHERE chat) AS outp,"
            "   sum(cost_usd) FILTER (WHERE chat) AS cost, count(*) FILTER (WHERE chat) AS recorded,"
            "   count(*) FILTER (WHERE chat AND cost_usd IS NULL) AS unpriced"
            "  FROM (SELECT q.*, (q.operation = 'chat' AND NOT " + not_chat_key.format(k="q.request_key") + ") AS chat"
            "        FROM run_model_requests q WHERE q.tenant_id = :t AND q.run_id = ANY(:ids)) r"
            "  GROUP BY run_id),"
            " unrecorded AS ("
            "  SELECT e.run_id, count(DISTINCT k.key) AS n"
            "  FROM execution_events e"
            "  CROSS JOIN LATERAL (SELECT CASE WHEN e.event_key LIKE '%\\:final'"
            "                      THEN left(e.event_key, -6) ELSE e.event_key END AS key) k"
            "  WHERE e.tenant_id = :t AND e.run_id = ANY(:ids) AND e.type = 'model_message'"
            "   AND NOT " + not_chat_key.format(k="k.key") + ""
            "   AND NOT EXISTS (SELECT 1 FROM run_model_requests q"
            "                   WHERE q.tenant_id = e.tenant_id AND q.run_id = e.run_id AND q.request_key = k.key)"
            "  GROUP BY e.run_id)"
            " SELECT coalesce(rows.run_id, unrecorded.run_id) AS run_id, rows.inp, rows.outp, rows.cost,"
            "  coalesce(rows.recorded, 0) AS recorded, coalesce(rows.unpriced, 0) AS unpriced,"
            "  coalesce(unrecorded.n, 0) AS unrecorded"
            " FROM rows FULL JOIN unrecorded ON unrecorded.run_id = rows.run_id"
        )
        out: dict[str, dict[str, Any]] = {}
        with self.Session() as session:
            for row in session.execute(sql, {"t": tenant_id, "ids": ids}):
                requests = row.recorded + row.unrecorded
                no_tokens = requests > 0 and row.recorded == 0
                missing_cost = row.unpriced or row.unrecorded
                out[str(row.run_id)] = {
                    "chat_input_tokens": None if no_tokens else int(row.inp or 0),
                    "chat_output_tokens": None if no_tokens else int(row.outp or 0),
                    "chat_cost_usd": None
                    if missing_cost
                    else format(Decimal(row.cost) if row.cost is not None else Decimal(0), "f"),
                }
        return out

    def agent_run_ids(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> tuple[list[str], bool]:
        """The ids of the tenant's runs of `agent_id` that may have logged in `[start, end]`: created
        by `end` and not finished before `start` (child runs of workflows included), newest first, at
        most `limit`. Returns (ids, more): `more` is True when older matching runs were left out.
        For GET /logs?agent_id= (log records carry the run id, not the agent id).

        Known gap (review finding 3, P1, needs a migration): no index on (tenant_id, agent_id,
        created_at), so this filters ix_runs_tenant_created's rows by agent; fine at today's sizes,
        slow for a tenant with many runs of other agents in the window."""
        query = (
            select(RunModel.id)
            .where(
                RunModel.tenant_id == tenant_id,
                RunModel.agent_id == str(agent_id),
                RunModel.created_at <= end,
                or_(RunModel.finished_at.is_(None), RunModel.finished_at >= start),
            )
            .order_by(RunModel.created_at.desc(), RunModel.id.desc())
            .limit(limit + 1)
        )
        with self.Session() as session:
            ids = [str(r) for r in session.execute(query).scalars()]
        return ids[:limit], len(ids) > limit

    # ------------------------------------------------------------------ conversations

    def create_conversation(
        self,
        tenant_id: str,
        agent_id: UUID | str,
        title: str | None = None,
        conversation_id: UUID | str | None = None,
        *,
        resource_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a conversation; idempotent on a client-supplied `conversation_id`.

        `resource_id` (RESOURCE_ID_PATTERN, else ValueError) is set once and never changed; the API
        defaults it to the principal's user id. Returns (conversation, created). Raises
        ConversationIdConflict if the id belongs to another tenant, and its subclass
        ConversationResourceConflict if the tenant's existing conversation has another resource_id
        (None and a value differ: a conversation created before 0012 cannot gain one).
        """
        from sqlalchemy.dialects.postgresql import insert

        if not tenant_id:
            raise ValueError("tenant_id is required")
        validate_resource_id(resource_id)
        conv_id = str(conversation_id or uuid4())
        now = datetime.now(timezone.utc)
        with self.Session() as session:
            inserted = session.execute(
                insert(ConversationModel)
                .values(
                    id=conv_id,
                    tenant_id=tenant_id,
                    agent_id=str(agent_id),
                    title=title,
                    resource_id=resource_id,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="conversations_pkey")
                .returning(ConversationModel.id)
            ).fetchone()
            session.commit()
            existing = session.get(ConversationModel, conv_id)
            if existing is None or existing.tenant_id != tenant_id:
                raise ConversationIdConflict(conv_id)
            if existing.resource_id != resource_id:
                raise ConversationResourceConflict(conv_id)
            row = session.execute(self._conversation_select().where(ConversationModel.id == conv_id)).one()
            return self._conversation_from_row(row), inserted is not None

    @staticmethod
    def _conversation_select():
        """Conversations joined to their latest turn (LATERAL, one index probe on
        uq_runs_conversation_turn per conversation). Turns are numbered gap-free from 1 under the
        conversation lock, so the latest turn number is the turn count. The latest turn's open
        approval (0014_approvals) feeds its display status."""
        from eap_run_store.approvals import open_approval_exists

        latest = (
            select(
                RunModel.turn,
                RunModel.id,
                RunModel.status,
                RunModel.heartbeat_at,
                open_approval_exists().label("waiting_approval"),
            )
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
            func.coalesce(latest.c.waiting_approval, False),
        ).outerjoin(latest, true())

    @staticmethod
    def _conversation_from_row(row) -> dict[str, Any]:
        conv, turn_count, run_id, run_status, heartbeat_at, waiting_approval = row
        return _iso_conversation(conv, turn_count, (run_id, run_status, heartbeat_at), bool(waiting_approval))

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
            rows = session.execute(
                _run_select()
                .where(RunModel.tenant_id == tenant_id, RunModel.conversation_id == str(conversation_id))
                .order_by(RunModel.turn)
            ).all()
            return [_run_dict(r, waiting) for r, waiting in rows]

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
    usage: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> tuple[RunEvent, bool]:
    """EventLog.append's work inside the caller's open ORM session, which commits it: a store that
    must write its own rows, an event and a usage row in one transaction (KnowledgeStore) uses this.
    Same rules as EventLog.append (seq under the runs row lock, repeated key = no write).
    `usage` is one row or a list of rows (AC.5: a model request answered by a fallback model plus
    its failed attempts that reported usage), all written in the event's transaction.
    A usage row inserted on a FINISHED run also refreshes the run's totals (insert_usage_in_session).
    Raises RunNotFound if the run does not exist or belongs to another tenant; the caller rolls back."""
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
    for row in usage_rows(usage):
        # A usage row on a finished run (post-turn steps) also refreshes the run's totals.
        insert_usage_in_session(session, tenant_id, run_id, row, event_model.ts)
    return _run_event_from_model(event_model), True


def usage_rows(usage: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """`EventLog.append`'s `usage` argument as a list of rows: None -> [], one row -> [row]."""
    if usage is None:
        return []
    if isinstance(usage, dict):
        return [usage]
    return list(usage)


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
        usage: dict[str, Any] | list[dict[str, Any]] | None = None,
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
        exactly once per request and never recomputed. `usage` may also be a list of such rows
        (AC.5: the answering model's row plus `model:{n}:failed:{k}` rows of failed fallback
        attempts that reported usage), all in the event's transaction; a single row still works.
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

    def append_approval_waiting(
        self,
        tenant_id: str,
        run_id: UUID | str,
        kind: str,
        approval_key: str,
        data: dict[str, Any],
    ) -> tuple[RunEvent, bool]:
        """Record that the run waits for an approval: the waiting event and its open
        pending_approvals row, in ONE transaction under the run-row lock (0014_approvals).

        kind `tool`: event `tool:{tool_call_id}:approval:waiting`, type tool_approval_waiting, data
        `{tool_call_id, tool_name, args, deadline, workflow_id}`. kind `step`: event
        `step:{step_id}:waiting`, type step_waiting, data `{step_id, prompt, timeout_hours,
        workflow_id, deadline}`. `data["deadline"]` (an aware datetime or ISO text; stored as ISO
        text) is required, and a tool's `data["tool_name"]` too. The row copies tool_name, args
        (≤ 8 KiB canonical JSON, else {"truncated": true}), prompt and the deadline from the data,
        and the root run, agent and workflow from the run.

        Returns (event, created). A repeated call (a replayed step) writes no event and returns the
        first event, whose deadline stands; it only adds the open row when it is missing and the
        approval is undecided (an event written by a worker without the projection, during a
        rolling deploy), copied from that first event. Raises RunNotFound (writing nothing) for an unknown run
        or another tenant's; any failure of the row write rolls the event back.
        """
        from eap_run_store import approvals

        if kind not in approvals.APPROVAL_KINDS:
            raise ValueError("kind must be 'tool' or 'step'")
        if not approval_key:
            raise ValueError("approval_key is required")
        payload = dict(data or {})
        deadline = payload.get("deadline")
        if isinstance(deadline, datetime):
            payload["deadline"] = deadline.isoformat()
        if kind == "tool" and not payload.get("tool_name"):
            raise ValueError("a tool approval needs data['tool_name']")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        event_key = approvals.waiting_event_key(kind, approval_key)
        with self.Session() as session:
            try:
                event, created = append_event_in_session(
                    session, tenant_id, run_id, event_key, approvals.waiting_event_type(kind), payload
                )
                # A replay also writes a missing row (ON CONFLICT DO NOTHING): during a rolling
                # deploy a worker without the projection may have written the event alone. The
                # row then copies the FIRST event (its deadline and time), and is never inserted
                # once a decision exists, so a decided approval never reopens.
                replayed = None if created else dict(event.data or {})
                decided = replayed is not None and session.execute(
                    select(ExecutionEventModel.seq).where(
                        ExecutionEventModel.run_id == str(run_id),
                        ExecutionEventModel.event_key == approvals.decision_event_key(kind, approval_key),
                    )
                ).first() is not None
                if not decided and (replayed is None or replayed.get("deadline") is not None):
                    approvals.insert_waiting_in_session(
                        session,
                        tenant_id=tenant_id,
                        run_id=str(run_id),
                        kind=kind,
                        approval_key=approval_key,
                        data=payload if replayed is None else replayed,
                        requested_at=event.ts,
                    )
            except BaseException:
                session.rollback()
                raise
            session.commit()
            return event, created

    def append_step_decision(
        self, tenant_id: str, run_id: UUID | str, step_id: str, type: RunEventType, data: dict | None = None
    ) -> tuple[str, RunEvent | None]:
        """Record the one decision of a workflow approval step, atomically under the run-row lock.

        The decision is the event `step:{step_id}:decision` (type step_approved | step_rejected |
        step_timed_out). It is written only while the step is waiting: the run is not terminal, its
        `step:{step_id}:waiting` event exists and no decision exists yet. Returns
        ("created", event), ("exists", the earlier decision) or ("not_waiting", None); exactly one
        caller ever gets "created" (the API's approve/reject and the worker's timeout race on it).
        The step's pending_approvals row is closed in the same transaction (0014_approvals).
        Raises RunNotFound for an unknown run or another tenant's run. Same seq rules as append.
        """
        from eap_run_store import approvals

        if type not in approvals.STEP_DECISION_TYPES:
            raise ValueError(f"a step decision is one of {approvals.STEP_DECISION_TYPES}")
        return self._append_decision(tenant_id, run_id, "step", step_id, type, data)

    def append_tool_decision(
        self, tenant_id: str, run_id: UUID | str, tool_call_id: str, type: RunEventType, data: dict | None = None
    ) -> tuple[str, RunEvent | None]:
        """Record the one decision of a gated tool call, atomically under the run-row lock.

        The decision is the event `tool:{tool_call_id}:approval:decision` (type tool_approved |
        tool_denied | tool_approval_timed_out; data `{tool_call_id, decided_by, comment}`, decided_by
        absent or None for a timeout). Same rules and results as append_step_decision: written only
        while `tool:{tool_call_id}:approval:waiting` exists, no decision exists and the run is not
        terminal (no `end` event); ("created", event) | ("exists", the earlier decision) |
        ("not_waiting", None). The call's pending_approvals row is closed in the same transaction.
        Raises RunNotFound for an unknown run or another tenant's run.
        """
        from eap_run_store import approvals

        if type not in approvals.TOOL_DECISION_TYPES:
            raise ValueError(f"a tool decision is one of {approvals.TOOL_DECISION_TYPES}")
        return self._append_decision(tenant_id, run_id, "tool", tool_call_id, type, data)

    def _append_decision(
        self, tenant_id: str, run_id: UUID | str, kind: str, approval_key: str, type: str, data: dict | None
    ) -> tuple[str, RunEvent | None]:
        from eap_run_store import approvals

        if not tenant_id:
            raise ValueError("tenant_id is required")
        waiting_key = approvals.waiting_event_key(kind, approval_key)
        decision_key = approvals.decision_event_key(kind, approval_key)
        with self.Session() as session:
            try:
                owned = session.execute(
                    text("SELECT status FROM runs WHERE id = :run_id AND tenant_id = :tenant_id FOR UPDATE"),
                    {"run_id": str(run_id), "tenant_id": tenant_id},
                ).fetchone()
                if owned is None:
                    raise RunNotFound(tenant_id, run_id)
                keys = set(
                    session.execute(
                        select(ExecutionEventModel.event_key).where(
                            ExecutionEventModel.run_id == str(run_id),
                            ExecutionEventModel.event_key.in_([waiting_key, decision_key, "end"]),
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
                if owned.status in TERMINAL_RUN_STATUSES or "end" in keys or waiting_key not in keys:
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
                session.flush()
                approvals.record_decision_in_session(
                    session,
                    tenant_id=tenant_id,
                    run_id=str(run_id),
                    kind=kind,
                    approval_key=approval_key,
                    event_type=type,
                    data=data or {},
                    decided_at=event_model.ts,
                )
                event = _run_event_from_model(event_model)
            except BaseException:
                session.rollback()
                raise
            session.commit()
            return "created", event

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
