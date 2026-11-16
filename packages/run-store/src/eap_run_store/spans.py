"""Materialised spans (0022_parity_round_2): the ORM table and `SpanStore`.

Filled by a worker step when a top-level run finishes (`replace_for_run`, idempotent: delete then
insert), from the same derivation `GET /runs/{id}/trace` uses (`trace.flat_spans`). Runs finished
before the table existed have no rows. Read newest first, paged by a `(start, span_id)` cursor.

`run_filter_conditions` (store.py, shared with `RunStore.list_runs`) builds the WHERE clause of the `GET /runs` filters on `runs` (the same meaning as
`RunStore.list_runs`); the spans list applies it through the span's run and the runs histogram on
`runs` directly.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, Index, PrimaryKeyConstraint, String, cast, delete, exists, func, or_, select, text, tuple_
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.cursor import MAX_CURSOR_LENGTH
from eap_run_store.store import Base, RunModel, run_filter_conditions


class SpanModel(Base):
    __tablename__ = "spans"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", PG_UUID(as_uuid=False), nullable=False)
    span_id: Mapped[str] = mapped_column("span_id", String, nullable=False)
    parent_id: Mapped[str | None] = mapped_column("parent_id", String, nullable=True)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    start: Mapped[datetime] = mapped_column("start", DateTime(timezone=True), nullable=False)
    end: Mapped[datetime | None] = mapped_column("end", DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column("status", String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column("agent_id", String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column("workflow_id", String, nullable=True)
    model: Mapped[str | None] = mapped_column("model", String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("run_id", "span_id", name="spans_pkey"),
        Index("ix_spans_tenant_start", "tenant_id", text("start DESC")),
    )


class InvalidSpanCursor(ValueError):
    """The token is not a cursor `SpanStore.list` issued."""


class SpanCursor:
    """A position in the (start DESC, span_id DESC) order, as an opaque URL-safe base64 token."""

    def __init__(self, start: datetime, span_id: str):
        self.start = start
        self.span_id = span_id

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SpanCursor) and (self.start, self.span_id) == (other.start, other.span_id)

    def __hash__(self) -> int:
        return hash((self.start, self.span_id))

    @classmethod
    def after(cls, span: dict[str, Any]) -> SpanCursor:
        return cls(span["start"], span["span_id"])

    def encode(self) -> str:
        raw = json.dumps({"t": self.start.isoformat(), "id": self.span_id}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> SpanCursor:
        if not token or len(token) > MAX_CURSOR_LENGTH:
            raise InvalidSpanCursor("cursor is empty or too long")
        try:
            data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        except (binascii.Error, ValueError) as exc:
            raise InvalidSpanCursor("cursor is not valid") from exc
        if not isinstance(data, dict) or set(data) != {"t", "id"}:
            raise InvalidSpanCursor("cursor is not valid")
        if not isinstance(data["t"], str) or not isinstance(data["id"], str):
            raise InvalidSpanCursor("cursor is not valid")
        try:
            start = datetime.fromisoformat(data["t"])
        except ValueError as exc:
            raise InvalidSpanCursor("cursor is not valid") from exc
        if start.tzinfo is None:
            raise InvalidSpanCursor("cursor is not valid")
        return cls(start, data["id"])


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _execute(session: Any, statement: Any, slow: bool, timeout_ms: int | None) -> Any:
    if not slow:
        return session.execute(statement)
    from eap_run_store.metrics import DEFAULT_STATEMENT_TIMEOUT_MS, MetricsQueryTimeout

    ms = int(timeout_ms or DEFAULT_STATEMENT_TIMEOUT_MS)
    try:
        session.execute(text(f"SET LOCAL statement_timeout = {ms}"))
        return session.execute(statement)
    except DBAPIError as exc:
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014":  # query_canceled
            raise MetricsQueryTimeout(ms) from None
        raise


SPAN_RUN_KINDS = frozenset({"agent", "tool", "workflow"})
MAX_LIST_LIMIT = 200
MAX_NAME_QUERY = 200
_WORKFLOW_SPEC = text(
    "SELECT spec FROM workflow_versions WHERE tenant_id = :t AND workflow_id = :w AND version = :v"
)


def materialise_spans(engine: Any, tenant_id: str, run_id: UUID | str) -> int:
    """Replace the run's `spans` rows from its trace; returns the rows written. Writes nothing (0)
    for an unknown run, one that has not finished, a child run (its spans are part of its parent's
    trace) or a run of another kind than agent, tool and workflow. Idempotent. Called by the worker
    when a run ends, and by the cancel paths (the API's cancel and the worker's reconciler), where
    the run is ended from outside and its own workflow never gets to record."""
    from eap_run_store.store import TERMINAL_RUN_STATUSES
    from eap_run_store.trace import TraceReader, flat_spans

    def workflow_spec(workflow_id: str, version: int) -> dict[str, Any] | None:
        with engine.connect() as conn:
            row = conn.execute(_WORKFLOW_SPEC, {"t": tenant_id, "w": str(workflow_id), "v": version}).first()
        return dict(row[0]) if row is not None else None

    trace = TraceReader(engine).read(tenant_id, run_id, workflow_spec=workflow_spec)
    if trace is None:
        return 0
    run = trace["run"]
    if (
        run["status"] not in TERMINAL_RUN_STATUSES
        or run["kind"] not in SPAN_RUN_KINDS
        or run.get("parent_run_id") is not None
    ):
        return 0
    return SpanStore(engine).replace_for_run(tenant_id, run_id, flat_spans(trace))


_SPAN_COLUMNS = ("run_id", "span_id", "parent_id", "kind", "name", "start", "end", "status",
                 "agent_id", "workflow_id", "model")
# `list` keys that are conditions on the span's run (see run_filter_conditions).
_RUN_FILTERS = ("status", "kind", "tool_id", "workflow_id", "model", "provider", "tags", "metadata")


def _span_dict(row: SpanModel) -> dict[str, Any]:
    return {c: getattr(row, c) for c in _SPAN_COLUMNS}


class SpanStore:
    """Reads and writes the `spans` table; every call is tenant-scoped."""

    def __init__(self, engine: Any):
        self.engine = engine
        self.Session = sessionmaker(engine, expire_on_commit=False)

    def replace_for_run(self, tenant_id: str, run_id: UUID | str, spans: list[dict[str, Any]]) -> int:
        """Delete the run's rows and insert `spans` (trace.flat_spans rows) in one transaction, so a
        repeat leaves the same rows. Returns the rows written."""
        rid = str(run_id)
        rows = [{"tenant_id": tenant_id, "run_id": rid, **{k: s.get(k) for k in _SPAN_COLUMNS if k != "run_id"}}
                for s in spans]
        with self.Session() as session, session.begin():
            session.execute(delete(SpanModel).where(SpanModel.tenant_id == tenant_id, SpanModel.run_id == rid))
            if rows:
                session.execute(pg_insert(SpanModel).values(rows).on_conflict_do_nothing())
        return len(rows)

    def list(
        self,
        tenant_id: str,
        filters: dict[str, Any] | None,
        limit: int = 50,
        before: SpanCursor | None = None,
        statement_timeout_ms: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Newest first by (start, span_id). `filters`: `from` / `to` (span start, `[from, to)`),
        `span_kind`, `q` (case-insensitive substring of the span name), `agent_id`, `workflow_id`,
        `model` and `provider` (as `GET /runs`: the run used that model), and `status`, `kind`,
        `tool_id`, `tags`, `metadata` of the span's run. The second item is an opaque cursor when
        a full page came back, else None. `limit` is clamped to 1..200 and `q` to its first 200
        characters (the store holds the bounds, not only the route).

        `model` and `provider` match through the span's RUN, as `GET /runs` does (the run, or one of
        its child runs, made a request on that model), not the span's own `model` column; so every
        span of such a run is listed, including its tool calls."""
        limit = min(max(int(limit), 1), MAX_LIST_LIMIT)
        f = dict(filters or {})
        if f.get("q"):
            f["q"] = str(f["q"])[:MAX_NAME_QUERY]
        stmt = select(SpanModel).where(SpanModel.tenant_id == tenant_id)
        if f.get("from") is not None:
            stmt = stmt.where(SpanModel.start >= f["from"])
        if f.get("to") is not None:
            stmt = stmt.where(SpanModel.start < f["to"])
        if f.get("span_kind"):
            stmt = stmt.where(SpanModel.kind == f["span_kind"])
        if f.get("q"):
            stmt = stmt.where(SpanModel.name.ilike(_like(f["q"]), escape="\\"))
        if f.get("agent_id"):
            stmt = stmt.where(SpanModel.agent_id == str(f["agent_id"]))
        slow = False
        if any(f.get(k) for k in _RUN_FILTERS):
            conds, slow = run_filter_conditions(
                tenant_id, **{k: f[k] for k in _RUN_FILTERS if f.get(k)}, parent_run_id=None
            )
            stmt = stmt.where(exists().where(cast(RunModel.id, PG_UUID(as_uuid=False)) == SpanModel.run_id, *conds))
        if before is not None:
            stmt = stmt.where(tuple_(SpanModel.start, SpanModel.span_id) < tuple_(before.start, before.span_id))
        stmt = stmt.order_by(SpanModel.start.desc(), SpanModel.span_id.desc()).limit(limit)
        with self.Session() as session:
            items = [_span_dict(r) for r in _execute(session, stmt, slow, statement_timeout_ms).scalars()]
        next_before = SpanCursor.after(items[-1]).encode() if len(items) == limit and items else None
        return items, next_before

    def runs_histogram(
        self,
        tenant_id: str,
        start: datetime,
        end: datetime,
        interval: str,
        filters: dict[str, Any] | None = None,
        statement_timeout_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Runs per UTC `interval` ('hour' | 'day') bucket over `[start, end)`, zero-filled: one
        `{start, runs, failed}` per bucket from the one holding `start` to the one holding `end`.
        `filters` are the `GET /runs` filters (run_filter_conditions keys, `created_from` / `created_to`
        replaced by the range)."""
        if interval not in ("hour", "day"):
            raise ValueError("interval must be hour or day")
        conds, slow = run_filter_conditions(
            tenant_id,
            **{k: v for k, v in (filters or {}).items() if k not in ("created_from", "created_to")},
            created_from=start, created_to=end,
        )
        bucket = func.date_trunc(interval, func.timezone("UTC", RunModel.created_at)).label("bucket")
        stmt = (
            select(bucket, func.count().label("runs"), func.count().filter(RunModel.status == "failed").label("failed"))
            .where(*conds)
            .group_by(bucket)
        )
        with self.Session() as session:
            found = {
                row.bucket.replace(tzinfo=timezone.utc): (row.runs, row.failed)
                for row in _execute(session, stmt, slow, statement_timeout_ms)
            }
        step = timedelta(hours=1) if interval == "hour" else timedelta(days=1)
        first = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if interval == "day":
            first = first.replace(hour=0)
        buckets = []
        at = first
        while at < end:
            runs, failed = found.get(at, (0, 0))
            buckets.append({"start": at, "runs": runs, "failed": failed})
            at += step
        return buckets


__all__ = ["MAX_LIST_LIMIT", "MAX_NAME_QUERY", "SPAN_RUN_KINDS", "materialise_spans", "InvalidSpanCursor", "SpanCursor", "SpanModel", "SpanStore", "run_filter_conditions"]
