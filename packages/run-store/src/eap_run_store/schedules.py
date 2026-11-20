"""Workflow schedules (0022_parity_round_2): the ORM table and `ScheduleStore`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, cast, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, distinct_on
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.cursor import RunCursor
from eap_run_store.revisions import bump_row
from eap_run_store.store import Base


class WorkflowScheduleModel(Base):
    __tablename__ = "workflow_schedules"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    workflow_id: Mapped[str] = mapped_column("workflow_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    cron: Mapped[str] = mapped_column("cron", String, nullable=False)
    timezone: Mapped[str] = mapped_column("timezone", String, nullable=False, server_default=text("'UTC'"))
    input: Mapped[Any] = mapped_column("input", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active: Mapped[bool] = mapped_column("active", Boolean, nullable=False, server_default=text("true"))
    last_error: Mapped[str | None] = mapped_column("last_error", Text, nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column("last_fired_at", DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime] = mapped_column("next_fire_at", DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id", "name", name="uq_workflow_schedules_tenant_workflow_name"),
        Index("ix_workflow_schedules_active_next_fire", "active", "next_fire_at"),
    )


class ScheduleNameConflict(ValueError):
    """The workflow already has a schedule with this name."""

    def __init__(self, workflow_id: str, name: str):
        super().__init__(f"workflow {workflow_id} already has a schedule named {name!r}")
        self.workflow_id = workflow_id
        self.name = name


_EDITABLE = frozenset({"name", "cron", "timezone", "input", "active", "next_fire_at", "last_error"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _schedule_dict(row: WorkflowScheduleModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "workflow_id": row.workflow_id,
        "name": row.name,
        "cron": row.cron,
        "timezone": row.timezone,
        "input": row.input,
        "active": row.active,
        "last_error": row.last_error,
        "last_fired_at": row.last_fired_at,
        "next_fire_at": row.next_fire_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "revision": row.revision,
    }


class ScheduleStore:
    """Tenant-scoped workflow schedules. `claim_due`, `mark_fired` and `stop` are the worker's (any
    tenant, by schedule id); the rest are the API's and always filter by tenant."""

    def __init__(self, engine: Any):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def create(
        self, tenant_id: str, workflow_id: str, *, name: str, cron: str, timezone: str = "UTC",
        input: Any, next_fire_at: datetime,
    ) -> dict[str, Any]:
        row = WorkflowScheduleModel(
            id=str(uuid4()), tenant_id=tenant_id, workflow_id=str(workflow_id), name=name, cron=cron,
            timezone=timezone, input=input, active=True, next_fire_at=next_fire_at,
            created_at=_now(), updated_at=_now(),
        )
        with self.Session() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise ScheduleNameConflict(str(workflow_id), name) from None
            return _schedule_dict(row)

    def get(self, tenant_id: str, schedule_id: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = self._row(session, tenant_id, schedule_id)
            return None if row is None else _schedule_dict(row)

    def list(
        self, tenant_id: str, workflow_id: str | None = None, limit: int = 50, before: RunCursor | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first by (created_at, id). The second item is an opaque cursor when a full page
        came back (pass it as `before`), else None."""
        stmt = select(WorkflowScheduleModel).where(WorkflowScheduleModel.tenant_id == tenant_id)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowScheduleModel.workflow_id == str(workflow_id))
        if before is not None:
            stmt = stmt.where(
                tuple_(WorkflowScheduleModel.created_at, WorkflowScheduleModel.id)
                < tuple_(before.created_at, cast(before.id, PG_UUID(as_uuid=False)))
            )
        stmt = stmt.order_by(WorkflowScheduleModel.created_at.desc(), WorkflowScheduleModel.id.desc()).limit(limit)
        with self.Session() as session:
            items = [_schedule_dict(r) for r in session.scalars(stmt)]
        next_before = RunCursor.after(items[-1]).encode() if len(items) == limit and items else None
        return items, next_before

    def last_runs(self, tenant_id: str, schedule_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The newest run each schedule's ticks created: {schedule_id: {run_id, status, detail}}
        (absent for a schedule that never ran). `detail` is the `status` string of a finished run's JSON
        object output (a "nothing to do" exit such as `outside_slot`), else None."""
        if not schedule_ids:
            return {}
        from eap_run_store.store import RunModel

        stmt = (
            select(RunModel.schedule_id, RunModel.id, RunModel.status, RunModel.output)
            .where(RunModel.tenant_id == tenant_id, RunModel.schedule_id.in_(schedule_ids))
            .ext(distinct_on(RunModel.schedule_id))
            .order_by(RunModel.schedule_id, RunModel.created_at.desc(), RunModel.id.desc())
        )
        with self.Session() as session:
            rows = session.execute(stmt).all()
        out: dict[str, dict[str, Any]] = {}
        for schedule_id, run_id, status, output in rows:
            detail = None
            if status in ("completed", "skipped") and isinstance(output, str) and output.startswith("{"):
                try:
                    value = json.loads(output)
                except ValueError:
                    value = None
                if isinstance(value, dict) and isinstance(value.get("status"), str):
                    detail = value["status"][:64]
            out[schedule_id] = {"run_id": run_id, "status": status, "detail": detail}
        return out

    def update(
        self, tenant_id: str, schedule_id: str, *, expected_revision: int | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Sets the given columns (name, cron, timezone, input, active, next_fire_at, last_error), bumps
        updated_at and the revision; None when the tenant has no such schedule. RevisionMismatch when
        `expected_revision` is given and the row has moved on (nothing is written)."""
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise TypeError(f"not editable: {sorted(unknown)}")
        with self.Session() as session:
            row = self._row(session, tenant_id, schedule_id, lock=True)
            if row is None:
                return None
            bump_row(session, row, expected_revision)
            for key, value in fields.items():
                setattr(row, key, value)
            row.updated_at = _now()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise ScheduleNameConflict(row.workflow_id, fields.get("name", row.name)) from None
            return _schedule_dict(row)

    def delete(self, tenant_id: str, schedule_id: str) -> bool:
        with self.Session() as session:
            row = self._row(session, tenant_id, schedule_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def delete_for_workflow(self, tenant_id: str, workflow_id: str) -> int:
        with self.Session() as session:
            rows = session.scalars(select(WorkflowScheduleModel).where(
                WorkflowScheduleModel.tenant_id == tenant_id, WorkflowScheduleModel.workflow_id == str(workflow_id)))
            n = 0
            for row in rows:
                session.delete(row)
                n += 1
            session.commit()
            return n

    # ------------------------------------------------------------------ the worker's (any tenant)

    def claim_due(self, now: datetime, limit: int = 100, *, session: Any = None) -> list[dict[str, Any]]:
        """Active schedules with next_fire_at <= now, oldest first, `FOR UPDATE SKIP LOCKED`.

        The row locks last until the transaction ends. Pass the worker's own `session` (inside its
        transaction) to keep them while it creates runs and calls `mark_fired`/`stop` with the same
        session; without one the claim is its own short transaction (locks released on return)."""
        stmt = (
            select(WorkflowScheduleModel)
            .where(WorkflowScheduleModel.active.is_(True), WorkflowScheduleModel.next_fire_at <= now)
            .order_by(WorkflowScheduleModel.next_fire_at, WorkflowScheduleModel.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        if session is not None:
            return [_schedule_dict(r) for r in session.scalars(stmt)]
        with self.Session() as own:
            rows = [_schedule_dict(r) for r in own.scalars(stmt)]
            own.commit()
            return rows

    def mark_fired(self, schedule_id: str, fired_at: datetime, next_fire_at: datetime, *, session: Any = None) -> None:
        self._worker_update(
            schedule_id, session, last_fired_at=fired_at, next_fire_at=next_fire_at, updated_at=_now())

    def stop(self, schedule_id: str, last_error: str, *, session: Any = None) -> None:
        """Deactivate with a reason (the tick found the input or workflow no longer runnable)."""
        self._worker_update(
            schedule_id, session, bump=True, active=False, last_error=last_error, updated_at=_now())

    # ------------------------------------------------------------------ internals

    def _worker_update(self, schedule_id: str, session: Any, *, bump: bool = False, **values: Any) -> None:
        def apply(s: Any) -> None:
            row = s.get(WorkflowScheduleModel, str(schedule_id))
            if row is not None:
                if bump:  # a stop changes what the editor shows (`active`, `last_error`); a fire does not
                    row.revision = row.revision + 1
                for key, value in values.items():
                    setattr(row, key, value)

        if session is not None:
            apply(session)
            return
        with self.Session() as own:
            apply(own)
            own.commit()

    @staticmethod
    def _row(session: Any, tenant_id: str, schedule_id: str, *, lock: bool = False) -> WorkflowScheduleModel | None:
        try:
            sid = str(UUID(str(schedule_id)))
        except ValueError:
            return None
        stmt = select(WorkflowScheduleModel).where(
            WorkflowScheduleModel.id == sid, WorkflowScheduleModel.tenant_id == tenant_id)
        if lock:
            stmt = stmt.with_for_update()
        return session.scalars(stmt).first()
