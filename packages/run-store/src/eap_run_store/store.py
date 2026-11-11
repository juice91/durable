"""Postgres store for durable runs and event logging."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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


class Base(DeclarativeBase):
    """Base class for all database models."""


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


class ExecutionEventModel(Base):
    """Model for execution_events table."""

    __tablename__ = "execution_events"

    run_id: Mapped[UUIDAnnotated] = mapped_column(
        "run_id",
        String,
        ForeignKey("runs.id"),
        primary_key=True,
    )
    seq: Mapped[int] = mapped_column("seq", Integer, primary_key=True)
    event_key: Mapped[StrAnnotated] = mapped_column("event_key", String, nullable=False)
    type: Mapped[StrAnnotated] = mapped_column("type", String, nullable=False)
    data: Mapped[dict] = mapped_column("data", JSONB, nullable=False)
    ts: Mapped[datetime] = mapped_column("ts", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint("run_id", "event_key", name="uq_run_event_key"),
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


def create_tables(engine):
    """Create all tables in the database."""
    Base.metadata.create_all(engine)


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


class RunStore:
    """Store for durable runs."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def create_run(self, tenant_id: str, agent_id: UUID, agent_version: int, input: str, run_id: UUID) -> tuple[UUID, bool]:
        """Create a new run. Idempotent: same run_id returns existing without creating second row.
        
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

    def set_status(self, run_id: UUID, status: str, output: str | None = None, error: str | None = None) -> None:
        """Update run status and optionally output or error."""
        with self.Session() as session:
            run = session.execute(
                select(RunModel).where(RunModel.id == str(run_id))
            ).scalar_one_or_none()
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            run.status = status
            if output is not None:
                run.output = output
            if error is not None:
                run.error = error
            session.commit()

    def heartbeat(self, run_id: UUID) -> None:
        """Update the heartbeat timestamp for a run."""
        with self.Session() as session:
            run = session.execute(
                select(RunModel).where(RunModel.id == str(run_id))
            ).scalar_one_or_none()
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            run.heartbeat_at = datetime.now(timezone.utc)
            session.commit()

    def get_run(self, tenant_id: str, run_id: UUID) -> dict | None:
        """Get a run by ID. Returns None if not found or belongs to another tenant."""
        with self.Session() as session:
            run = session.execute(
                select(RunModel).where(RunModel.id == str(run_id), RunModel.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if run is None:
                return None
            
            # Calculate display_status
            status = run.status
            display_status = status
            if status == "running" and run.heartbeat_at is not None:
                # Check if heartbeat is older than 20 seconds
                now = datetime.now(timezone.utc)
                delta = (now - run.heartbeat_at).total_seconds()
                if delta > 20:
                    display_status = "recovering"
            
            return {
                "id": run.id,
                "tenant_id": run.tenant_id,
                "agent_id": run.agent_id,
                "agent_version": run.agent_version,
                "status": status,
                "display_status": display_status,
                "input": run.input,
                "output": run.output,
                "error": run.error,
                "heartbeat_at": run.heartbeat_at,
                "created_at": run.created_at,
                "updated_at": run.updated_at,
            }


class EventLog:
    """Event log for a run."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def append(self, run_id: UUID, event_key: str, type: RunEventType, data: dict | None = None) -> tuple[RunEvent, bool]:
        """Append an event to a run. Seq is assigned atomically.
        
        Returns (RunEvent, created: bool) where created=False if the event_key already exists.
        A repeated key never consumes a seq number.
        """
        with self.Session() as session:
            # Lock the runs row to ensure seq uniqueness
            session.execute(
                text("SELECT id FROM runs WHERE id = :run_id FOR UPDATE"),
                {"run_id": str(run_id)}
            )

            # Check if event with this event_key already exists
            existing_event = session.execute(
                select(ExecutionEventModel).where(
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
                .where(ExecutionEventModel.run_id == str(run_id), ExecutionEventModel.seq > after_seq)
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
