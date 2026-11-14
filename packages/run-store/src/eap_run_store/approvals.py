"""The approvals projection (0014_approvals): every human decision a run waits for, in one inbox.

`pending_approvals` holds one row per tool approval (an agent run's gated tool call, kind `tool`)
or workflow approval step (kind `step`). The event log stays the source of truth: the row is written
in the transaction of the waiting event and updated in the transaction of the decision event, by
EventLog.append_approval_waiting, EventLog.append_tool_decision and EventLog.append_step_decision
(through `insert_waiting_in_session` and `record_decision_in_session` here), so the inbox never
disagrees with the events.

ApprovalStore reads the inbox, tenant-scoped, with keyset cursor pagination.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    and_,
    exists,
    literal,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.orm import Mapped, aliased, mapped_column, sessionmaker

from eap_run_store.cursor import InvalidRunCursor, MAX_CURSOR_LENGTH
from eap_run_store.store import TERMINAL_RUN_STATUSES, Base, RunModel

ApprovalKind = Literal["tool", "step"]
ApprovalStatus = Literal["pending", "decided"]
ApprovalDecision = Literal["approved", "denied", "timed_out"]

APPROVAL_KINDS = ("tool", "step")
APPROVAL_DECISIONS = ("approved", "denied", "timed_out")
# The projection's copy of a tool call's arguments: at most this many bytes of canonical JSON,
# else {"truncated": true} (the waiting event keeps the full arguments).
APPROVAL_ARGS_MAX_BYTES = 8 * 1024
TRUNCATED_ARGS: dict[str, Any] = {"truncated": True}

# The decision event type -> the projection's decision.
DECISION_BY_EVENT_TYPE: dict[str, str] = {
    "tool_approved": "approved",
    "tool_denied": "denied",
    "tool_approval_timed_out": "timed_out",
    "step_approved": "approved",
    "step_rejected": "denied",
    "step_timed_out": "timed_out",
}
TOOL_DECISION_TYPES = ("tool_approved", "tool_denied", "tool_approval_timed_out")
STEP_DECISION_TYPES = ("step_approved", "step_rejected", "step_timed_out")

# Same texts as migration 0014_approvals.
PA_KIND_CHECK = "kind IN ('tool', 'step')"
PA_TOOL_NAME_CHECK = "(kind = 'tool' AND tool_name IS NOT NULL) OR (kind = 'step' AND tool_name IS NULL)"
PA_DECISION_CHECK = "decision IS NULL OR decision IN ('approved', 'denied', 'timed_out')"
PA_DECIDED_CHECK = "(decided_at IS NULL) = (decision IS NULL)"


def waiting_event_key(kind: ApprovalKind, approval_key: str) -> str:
    """The waiting event's key: `tool:{tool_call_id}:approval:waiting` or `step:{step_id}:waiting`."""
    return f"tool:{approval_key}:approval:waiting" if kind == "tool" else f"step:{approval_key}:waiting"


def decision_event_key(kind: ApprovalKind, approval_key: str) -> str:
    """The decision event's key: `tool:{tool_call_id}:approval:decision` or `step:{step_id}:decision`."""
    return f"tool:{approval_key}:approval:decision" if kind == "tool" else f"step:{approval_key}:decision"


def tool_approval_topic(run_id: str) -> str:
    """The DBOS message topic an agent run's gated tool calls wait on: `tool_approval:{run_id}`.
    One topic per run (all the gated calls of one model response wait in one loop). Shared by the
    API's wake-up (eap_api.launcher) and the worker's wait (AC.6), so both always agree."""
    return f"tool_approval:{run_id}"


def waiting_event_type(kind: ApprovalKind) -> str:
    return "tool_approval_waiting" if kind == "tool" else "step_waiting"


class PendingApprovalModel(Base):
    """One approval a run waits (or waited) for. See the module docstring and 0014_approvals."""

    __tablename__ = "pending_approvals"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    # The tool call id (kind tool) or the step id (kind step).
    approval_key: Mapped[str] = mapped_column("approval_key", String, nullable=False)
    # The top-level run: a workflow agent step's child run names its workflow run, else the run itself.
    root_run_id: Mapped[str] = mapped_column("root_run_id", String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column("agent_id", String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column("workflow_id", String, nullable=True)
    tool_name: Mapped[str | None] = mapped_column("tool_name", String, nullable=True)
    prompt: Mapped[str | None] = mapped_column("prompt", Text, nullable=True)
    args: Mapped[dict | None] = mapped_column("args", JSONB, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        "requested_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    deadline: Mapped[datetime] = mapped_column("deadline", DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column("decided_at", DateTime(timezone=True), nullable=True)
    decision: Mapped[str | None] = mapped_column("decision", String, nullable=True)
    decided_by: Mapped[str | None] = mapped_column("decided_by", String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "run_id", "kind", "approval_key", name="pending_approvals_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_pending_approvals_tenant_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "root_run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_pending_approvals_tenant_root_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(PA_KIND_CHECK, name="ck_pending_approvals_kind"),
        CheckConstraint(PA_TOOL_NAME_CHECK, name="ck_pending_approvals_tool_name"),
        CheckConstraint(PA_DECISION_CHECK, name="ck_pending_approvals_decision"),
        CheckConstraint(PA_DECIDED_CHECK, name="ck_pending_approvals_decided"),
        Index(
            "ix_pending_approvals_tenant_open",
            "tenant_id",
            "requested_at",
            postgresql_where=text("decided_at IS NULL"),
        ),
        Index(
            "ix_pending_approvals_tenant_decided",
            "tenant_id",
            "decided_at",
            postgresql_where=text("decided_at IS NOT NULL"),
        ),
        Index("ix_pending_approvals_tenant_root_run", "tenant_id", "root_run_id"),
    )


def projection_args(args: Any) -> dict[str, Any] | None:
    """The arguments as the projection stores them: None stays None, a dict whose canonical JSON is
    at most 8 KiB is kept, anything larger (or not a JSON object) becomes {"truncated": true}."""
    if args is None:
        return None
    if not isinstance(args, dict):
        return dict(TRUNCATED_ARGS)
    try:
        encoded = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    except (TypeError, ValueError):
        return dict(TRUNCATED_ARGS)
    return args if len(encoded) <= APPROVAL_ARGS_MAX_BYTES else dict(TRUNCATED_ARGS)


def _parse_deadline(value: Any) -> datetime:
    if isinstance(value, datetime):
        deadline = value
    elif isinstance(value, str):
        deadline = datetime.fromisoformat(value)
    else:
        raise ValueError("the waiting event's data needs a `deadline` (datetime or ISO 8601 text)")
    if deadline.tzinfo is None:
        raise ValueError("the approval deadline must be timezone-aware")
    return deadline


def insert_waiting_in_session(
    session,
    *,
    tenant_id: str,
    run_id: str,
    kind: ApprovalKind,
    approval_key: str,
    data: dict[str, Any],
    requested_at: datetime,
) -> None:
    """Insert the open projection row of a waiting event just written in `session` (the caller's
    transaction, which holds the run-row lock). Fields come from the event data (`deadline`, and
    `tool_name` + `args` for a tool, `prompt` for a step) and from the run row (root run, agent,
    workflow). A row already there is kept (ON CONFLICT DO NOTHING)."""
    run = session.execute(
        select(RunModel.parent_run_id, RunModel.agent_id, RunModel.workflow_id).where(
            RunModel.tenant_id == tenant_id, RunModel.id == run_id
        )
    ).one()
    # Runs nest at most two levels (store.RUN_PARENT_LINK_CHECK: only an agent run has a parent, and
    # its parent is a workflow run, which has none), so the parent is the root. Taking the parent's
    # own parent when it has one keeps root_run_id the top-level run should deeper nesting appear.
    root_run_id, workflow_id = run_id, run.workflow_id
    if run.parent_run_id is not None:
        parent = session.execute(
            select(RunModel.parent_run_id, RunModel.workflow_id).where(
                RunModel.tenant_id == tenant_id, RunModel.id == run.parent_run_id
            )
        ).one_or_none()
        root_run_id = (parent.parent_run_id if parent is not None else None) or run.parent_run_id
        if workflow_id is None and parent is not None:
            workflow_id = parent.workflow_id
    values = {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "kind": kind,
        "approval_key": approval_key,
        "root_run_id": root_run_id,
        "agent_id": run.agent_id,
        "workflow_id": workflow_id,
        "tool_name": data.get("tool_name") if kind == "tool" else None,
        "prompt": data.get("prompt") if kind == "step" else None,
        "args": projection_args(data.get("args")) if kind == "tool" else None,
        "requested_at": requested_at,
        "deadline": _parse_deadline(data.get("deadline")),
    }
    session.execute(
        pg_insert(PendingApprovalModel.__table__).values(**values).on_conflict_do_nothing(
            index_elements=["tenant_id", "run_id", "kind", "approval_key"]
        )
    )


def record_decision_in_session(
    session,
    *,
    tenant_id: str,
    run_id: str,
    kind: ApprovalKind,
    approval_key: str,
    event_type: str,
    data: dict[str, Any],
    decided_at: datetime,
) -> bool:
    """Close the open projection row in the decision event's transaction: decision (from the event
    type), decided_at (the event's ts) and decided_by (`data["decided_by"]`, None for a timeout).
    Returns whether a row was closed (False when there is no open row, e.g. a wait written before
    the projection existed and not backfilled)."""
    closed = session.execute(
        update(PendingApprovalModel)
        .where(
            PendingApprovalModel.tenant_id == tenant_id,
            PendingApprovalModel.run_id == run_id,
            PendingApprovalModel.kind == kind,
            PendingApprovalModel.approval_key == approval_key,
            PendingApprovalModel.decided_at.is_(None),
        )
        .values(
            decision=DECISION_BY_EVENT_TYPE[event_type],
            decided_at=decided_at,
            decided_by=data.get("decided_by"),
        )
        .returning(PendingApprovalModel.run_id)
    ).fetchone()
    return closed is not None


def open_approval_exists(run_table=RunModel) -> Any:
    """SQL: the run (or, for a workflow run, one of its child runs) has an open approval row (the
    `waiting_approval` display status). Correlated to `run_table`.

    Two EXISTS, each on its own index: the run's own rows by the primary key prefix
    (tenant_id, run_id), and its child runs' rows by ix_pending_approvals_tenant_root_run.

    A row stays open when its run ends without a decision (failed, needs_attention, or completed
    while waiting): the decision CHECK has no value for "abandoned", and the event log has no
    decision to project. Such rows are ignored here instead of being closed: a child row counts only
    while the child run is not terminal (the run's own rows need no such test, since only a running
    run shows waiting_approval), and ApprovalStore.list hides them from the pending inbox."""
    pa = PendingApprovalModel
    child = aliased(RunModel, name="approval_child_run")
    own = exists().where(
        pa.tenant_id == run_table.tenant_id,
        pa.run_id == run_table.id,
        pa.decided_at.is_(None),
    )
    descendants = (
        select(literal(1))
        .select_from(pa.__table__.join(child, and_(child.tenant_id == pa.tenant_id, child.id == pa.run_id)))
        .where(
            pa.tenant_id == run_table.tenant_id,
            pa.root_run_id == run_table.id,
            pa.run_id != run_table.id,
            pa.decided_at.is_(None),
            child.status.not_in(sorted(TERMINAL_RUN_STATUSES)),
        )
        .exists()
    )
    return or_(own, descendants)


# ---------------------------------------------------------------------------- the inbox


@dataclass(frozen=True)
class ApprovalCursor:
    """A position in the inbox order, as an opaque token (URL-safe base64 JSON, like RunCursor).

    Pending rows are ordered (requested_at DESC, run_id DESC, kind DESC, approval_key DESC), decided
    rows (decided_at DESC, ...): `at` is that timestamp. `status` names the list the token belongs
    to: a pending token's `at` is a request time and a decided token's a decision time, so a token
    is only valid for its own list (ApprovalStore.list raises InvalidRunCursor on a mismatch). A
    token only names a position; every query stays tenant-scoped, so a token never reveals another
    tenant's rows."""

    at: datetime
    run_id: str
    kind: str
    approval_key: str
    status: str = "pending"

    @classmethod
    def after(cls, row: dict[str, Any], status: ApprovalStatus) -> ApprovalCursor:
        """The cursor at `row` (an ApprovalStore.list row) for the list with that `status`."""
        at = row["requested_at"] if status == "pending" else row["decided_at"]
        return cls(at, str(row["run_id"]), str(row["kind"]), str(row["approval_key"]), status)

    def encode(self) -> str:
        raw = json.dumps(
            {"t": self.at.isoformat(), "r": self.run_id, "k": self.kind, "a": self.approval_key, "s": self.status},
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> ApprovalCursor:
        # Tool call ids are provider-chosen strings, so the token may run longer than a run cursor.
        if not token or len(token) > 4 * MAX_CURSOR_LENGTH:
            raise InvalidRunCursor("cursor is empty or too long")
        try:
            data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        except (binascii.Error, ValueError) as exc:
            raise InvalidRunCursor("cursor is not valid") from exc
        if not isinstance(data, dict) or set(data) != {"t", "r", "k", "a", "s"}:
            raise InvalidRunCursor("cursor is not valid")
        if not all(isinstance(data[k], str) for k in ("t", "r", "k", "a", "s")) or data["k"] not in APPROVAL_KINDS:
            raise InvalidRunCursor("cursor is not valid")
        if data["s"] not in ("pending", "decided"):
            raise InvalidRunCursor("cursor is not valid")
        try:
            at = datetime.fromisoformat(data["t"])
        except ValueError as exc:
            raise InvalidRunCursor("cursor is not valid") from exc
        if at.tzinfo is None:
            raise InvalidRunCursor("cursor is not valid")
        return cls(at, data["r"], data["k"], data["a"], data["s"])


def _approval_dict(row: PendingApprovalModel, run_status: str | None = None) -> dict[str, Any]:
    return {
        "tenant_id": row.tenant_id,
        "run_id": row.run_id,
        "kind": row.kind,
        "approval_key": row.approval_key,
        "root_run_id": row.root_run_id,
        "agent_id": row.agent_id,
        "workflow_id": row.workflow_id,
        "tool_name": row.tool_name,
        "prompt": row.prompt,
        "args": row.args,
        "requested_at": row.requested_at,
        "deadline": row.deadline,
        "decided_at": row.decided_at,
        "decision": row.decision,
        "decided_by": row.decided_by,
        "status": "pending" if row.decided_at is None else "decided",
        "run_status": run_status,
    }


class ApprovalStore:
    """Reads of the approvals projection (writes go through EventLog, with their events)."""

    MAX_LIMIT = 200

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def list(
        self,
        tenant_id: str,
        status: ApprovalStatus = "pending",
        kind: ApprovalKind | None = None,
        agent_id: str | None = None,
        cursor: ApprovalCursor | str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of the tenant's inbox and the next page's cursor (None on the last page).

        `pending`: open rows of runs that are not terminal (a run that ended while waiting no longer
        waits), newest request first. `decided`: rows with a decision, newest decision first.
        `kind` and `agent_id` filter. `cursor` is the previous page's token (or an ApprovalCursor);
        a malformed token raises InvalidRunCursor. `limit` is clamped to 1..200."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if status not in ("pending", "decided"):
            raise ValueError("status must be 'pending' or 'decided'")
        if kind is not None and kind not in APPROVAL_KINDS:
            raise ValueError("kind must be 'tool' or 'step'")
        if isinstance(cursor, str):
            cursor = ApprovalCursor.decode(cursor)
        if cursor is not None and cursor.status != status:
            raise InvalidRunCursor(f"this cursor belongs to the {cursor.status} list, not the {status} list")
        limit = max(1, min(int(limit), self.MAX_LIMIT))
        pa = PendingApprovalModel
        order_at = pa.requested_at if status == "pending" else pa.decided_at
        query = (
            select(pa, RunModel.status)
            .join(RunModel, and_(RunModel.tenant_id == pa.tenant_id, RunModel.id == pa.run_id))
            .where(pa.tenant_id == tenant_id)
        )
        if status == "pending":
            query = query.where(pa.decided_at.is_(None), RunModel.status.not_in(sorted(TERMINAL_RUN_STATUSES)))
        else:
            query = query.where(pa.decided_at.is_not(None))
        if kind is not None:
            query = query.where(pa.kind == kind)
        if agent_id is not None:
            query = query.where(pa.agent_id == str(agent_id))
        if cursor is not None:
            query = query.where(
                tuple_(order_at, pa.run_id, pa.kind, pa.approval_key)
                < tuple_(cursor.at, cursor.run_id, cursor.kind, cursor.approval_key)
            )
        query = query.order_by(
            order_at.desc(), pa.run_id.desc(), pa.kind.desc(), pa.approval_key.desc()
        ).limit(limit + 1)
        with self.Session() as session:
            rows = [_approval_dict(row, run_status) for row, run_status in session.execute(query).all()]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = ApprovalCursor.after(rows[-1], status).encode()
        return rows, next_cursor

    def get(self, tenant_id: str, run_id: str, kind: ApprovalKind, approval_key: str) -> dict[str, Any] | None:
        """One projection row, or None (unknown, or another tenant's)."""
        pa = PendingApprovalModel
        with self.Session() as session:
            found = session.execute(
                select(pa, RunModel.status)
                .join(RunModel, and_(RunModel.tenant_id == pa.tenant_id, RunModel.id == pa.run_id))
                .where(
                    pa.tenant_id == tenant_id,
                    pa.run_id == str(run_id),
                    pa.kind == kind,
                    pa.approval_key == approval_key,
                )
            ).one_or_none()
            return None if found is None else _approval_dict(found[0], found[1])

    def list_for_run(self, tenant_id: str, run_id: str, *, open_only: bool = False) -> list[dict[str, Any]]:
        """The rows of a run and, for a workflow run, of its child runs (root_run_id), oldest first."""
        pa = PendingApprovalModel
        query = (
            select(pa, RunModel.status)
            .join(RunModel, and_(RunModel.tenant_id == pa.tenant_id, RunModel.id == pa.run_id))
            .where(pa.tenant_id == tenant_id, or_(pa.run_id == str(run_id), pa.root_run_id == str(run_id)))
        )
        if open_only:
            query = query.where(pa.decided_at.is_(None))
        query = query.order_by(pa.requested_at, pa.run_id, pa.kind, pa.approval_key)
        with self.Session() as session:
            return [_approval_dict(row, run_status) for row, run_status in session.execute(query).all()]


__all__ = [
    "APPROVAL_ARGS_MAX_BYTES",
    "APPROVAL_DECISIONS",
    "APPROVAL_KINDS",
    "ApprovalCursor",
    "ApprovalStore",
    "DECISION_BY_EVENT_TYPE",
    "PendingApprovalModel",
    "STEP_DECISION_TYPES",
    "TOOL_DECISION_TYPES",
    "decision_event_key",
    "insert_waiting_in_session",
    "open_approval_exists",
    "projection_args",
    "record_decision_in_session",
    "tool_approval_topic",
    "waiting_event_key",
    "waiting_event_type",
]
