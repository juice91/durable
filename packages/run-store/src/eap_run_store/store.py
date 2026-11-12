"""Postgres store for durable runs and event logging."""

from datetime import datetime, timezone
from typing import Any
import json
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text,
    func,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB

from eap_run_store.cursor import RunCursor
from sqlalchemy.orm import (
    DeclarativeBase,
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
    agent_id: Mapped[UUIDAnnotated] = mapped_column("agent_id", String, nullable=False)
    agent_version: Mapped[int] = mapped_column("agent_version", Integer, nullable=False)
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
        # The tenant's runs, newest first (GET /runs).
        Index("ix_runs_tenant_created", "tenant_id", "created_at"),
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
        super().__init__(f"Run not found for tenant {tenant_id!r}: {run_id}")


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
        super().__init__(f"Run ID {run_id} already exists under a different tenant")


class ConversationNotFound(ValueError):
    """No conversation with this id belongs to the tenant (unknown, or another tenant's)."""

    def __init__(self, tenant_id: str, conversation_id: UUID | str):
        self.tenant_id = tenant_id
        self.conversation_id = str(conversation_id)
        super().__init__(f"Conversation not found for tenant {tenant_id!r}: {conversation_id}")


class ConversationIdConflict(Exception):
    """A client-supplied conversation id already exists under a different tenant."""

    def __init__(self, conversation_id: UUID | str):
        self.conversation_id = str(conversation_id)
        super().__init__(f"Conversation ID {conversation_id} already exists under a different tenant")


class TurnInProgress(Exception):
    """The conversation's latest turn has not reached a terminal status yet."""

    def __init__(self, conversation_id: str, run_id: str, status: str):
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.status = status
        super().__init__(f"Conversation {conversation_id} is busy: turn run {run_id} is {status}")


def _iso_conversation(conv: ConversationModel, turn_count: int) -> dict[str, Any]:
    return {
        "id": conv.id,
        "tenant_id": conv.tenant_id,
        "agent_id": conv.agent_id,
        "title": conv.title,
        "created_at": conv.created_at,
        "updated_at": conv.updated_at,
        "turn_count": turn_count,
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
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
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
        agent_version: int,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Create a new run. Idempotent: same run_id returns existing without creating second row.

        `trace_id`/`traceparent` are the creating API request's trace context (None when telemetry
        is off); an existing row keeps the values it was created with.

        Returns (UUID, created: bool) where created=False if the run already exists for this tenant.
        Raises RunIdConflict if the run_id exists under a different tenant.
        """
        from sqlalchemy.dialects.postgresql import insert

        with self.Session() as session:
            # Use PostgreSQL INSERT ... ON CONFLICT ... RETURNING for atomic operation
            stmt = insert(RunModel).values(
                id=str(run_id),
                tenant_id=tenant_id,
                agent_id=str(agent_id),
                agent_version=agent_version,
                status="queued",
                input=input,
                trace_id=trace_id,
                traceparent=traceparent,
            ).on_conflict_do_nothing(
                constraint="runs_pkey"
            ).returning(
                RunModel.id,
                RunModel.tenant_id
            )
            
            result = session.execute(stmt).fetchone()
            
            if result is not None:
                # We inserted a new row
                session.commit()
                return UUID(result.id), True
            
            # Check if the run exists under this tenant or another tenant
            existing = session.execute(
                select(RunModel).where(RunModel.id == str(run_id))
            ).scalar_one_or_none()
            
            if existing is None:
                # This shouldn't happen but handle gracefully
                session.commit()
                return UUID(run_id), False
            
            if existing.tenant_id != tenant_id:
                # Different tenant - raise conflict
                session.commit()
                raise RunIdConflict(run_id)
            
            # Same tenant, run already exists
            session.commit()
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
        Raises RunNotFound (and changes nothing) if the run does not exist or belongs to another tenant.
        """
        values: dict[str, Any] = {"status": status, "updated_at": datetime.now(timezone.utc)}
        if output is not None:
            values["output"] = output
        if error is not None:
            values["error"] = error
        if messages is not None:
            values["messages"] = func.coalesce(RunModel.messages, func.cast(_json_param(messages), JSONB))
        self._update_owned(tenant_id, run_id, values)

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
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """The tenant's runs ordered (created_at DESC, id DESC): at most `limit`, strictly after the
        `before` position in that order (RunCursor.after(last row of the previous page)),
        optionally only one agent's and/or one status's.

        The cursor is the pair, not the timestamp alone, so rows sharing a created_at are never
        skipped or repeated across pages."""
        query = select(RunModel).where(RunModel.tenant_id == tenant_id)
        if before is not None:
            query = query.where(tuple_(RunModel.created_at, RunModel.id) < tuple_(before.created_at, before.id))
        if agent_id is not None:
            query = query.where(RunModel.agent_id == str(agent_id))
        if status is not None:
            query = query.where(RunModel.status == status)
        query = query.order_by(RunModel.created_at.desc(), RunModel.id.desc()).limit(limit)
        with self.Session() as session:
            return [_run_dict(r) for r in session.execute(query).scalars()]

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
            return _iso_conversation(existing, self._turn_count(session, conv_id)), inserted is not None

    @staticmethod
    def _turn_count(session, conversation_id: str) -> int:
        return session.execute(
            select(func.count()).select_from(RunModel).where(RunModel.conversation_id == conversation_id)
        ).scalar_one()

    def get_conversation(self, tenant_id: str, conversation_id: UUID | str) -> dict[str, Any] | None:
        """The tenant's conversation, or None (unknown, or another tenant's)."""
        with self.Session() as session:
            conv = session.execute(
                select(ConversationModel).where(
                    ConversationModel.id == str(conversation_id), ConversationModel.tenant_id == tenant_id
                )
            ).scalar_one_or_none()
            return None if conv is None else _iso_conversation(conv, self._turn_count(session, conv.id))

    def list_conversations(self, tenant_id: str, agent_id: UUID | str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """The tenant's conversations, most recently active first."""
        counts = (
            select(RunModel.conversation_id, func.count().label("n"))
            .where(RunModel.tenant_id == tenant_id, RunModel.conversation_id.is_not(None))
            .group_by(RunModel.conversation_id)
            .subquery()
        )
        query = (
            select(ConversationModel, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.conversation_id == ConversationModel.id)
            .where(ConversationModel.tenant_id == tenant_id)
            .order_by(ConversationModel.updated_at.desc(), ConversationModel.id)
            .limit(limit)
        )
        if agent_id is not None:
            query = query.where(ConversationModel.agent_id == str(agent_id))
        with self.Session() as session:
            return [_iso_conversation(conv, n) for conv, n in session.execute(query).all()]

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
        agent_version: int,
        input: str,
        run_id: UUID,
        trace_id: str | None = None,
        traceparent: str | None = None,
    ) -> tuple[UUID, bool]:
        """Insert the conversation's next turn as a queued run; idempotent on `run_id`.

        Under the conversation's row lock: if `run_id` already exists for this tenant it is returned
        as (run_id, False) whatever it is (the caller checks that it is the same request); otherwise
        the latest turn must be terminal (else TurnInProgress) and the new run gets turn = latest + 1.
        Raises ConversationNotFound for another tenant's or an unknown conversation, RunIdConflict
        if `run_id` belongs to another tenant.
        `trace_id`/`traceparent` as in create_run.
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


class EventLog:
    """Event log for a run."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def append(
        self, tenant_id: str, run_id: UUID, event_key: str, type: RunEventType, data: dict | None = None
    ) -> tuple[RunEvent, bool]:
        """Append an event to a run owned by `tenant_id`. Seq is assigned atomically.

        Returns (RunEvent, created: bool) where created=False if the event_key already exists.
        A repeated key never consumes a seq number.
        Raises RunNotFound (and writes nothing) if the run does not exist or belongs to another tenant.
        """
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.Session() as session:
            # Lock the tenant's runs row to ensure seq uniqueness (and prove ownership).
            owned = session.execute(
                text("SELECT id FROM runs WHERE id = :run_id AND tenant_id = :tenant_id FOR UPDATE"),
                {"run_id": str(run_id), "tenant_id": tenant_id},
            ).fetchone()
            if owned is None:
                session.rollback()
                raise RunNotFound(tenant_id, run_id)

            # Check if event with this event_key already exists
            existing_event = session.execute(
                select(ExecutionEventModel).where(
                    ExecutionEventModel.tenant_id == tenant_id,
                    ExecutionEventModel.run_id == str(run_id),
                    ExecutionEventModel.event_key == event_key
                )
            ).scalar_one_or_none()
            
            if existing_event is not None:
                # Event already exists, return it without consuming seq
                session.commit()
                return _run_event_from_model(existing_event), False

            # Get max seq for this run
            result = session.execute(
                text("SELECT COALESCE(MAX(seq), -1) FROM execution_events WHERE run_id = :run_id"),
                {"run_id": str(run_id)}
            ).scalar_one()
            new_seq = result + 1

            # Create the event
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
            session.commit()

            return _run_event_from_model(event_model), True

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
