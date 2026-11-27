"""AG-UI run mapping (0016_ag_ui): which of our runs an AG-UI `(threadId, runId)` names, and the
client tool calls a conversation's latest turn left pending.

`ag_ui_runs` holds one row per AG-UI run (D4 of an internal design note), keyed by
tenant, scope (`agent` | `workflow` and its id), the client's threadId and runId. A POST with a known
runId never starts anything new: the API reads the row (`get_ag_ui_run`) and compares fingerprints.
`record_ag_ui_run` is idempotent: the first writer wins and every later call gets that row back.
A new agent turn records its row in the turn's own transaction instead
(`RunStore.create_turn(ag_ui_run=AgUiRunWrite(...))`), so no crash can leave the turn without it
and a same-runId race is always won by the request that created the turn. Every row names a run
of its scope (an agent run of that agent, a workflow run of that workflow).

`pending_client_tool_calls` reads the latest turn of a conversation only: its `run_completed` event
names the client calls the turn ended with (`pending_tool_call_ids`, AGUI.7), and their
`tool:{id}:call` events give the tool names and arguments.

Every query is tenant-scoped. This module is protocol-agnostic: it never imports `ag_ui`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.store import Base, ExecutionEventModel, RunModel, RunNotFound

AgUiScopeKind = Literal["agent", "workflow"]
AG_UI_SCOPE_KINDS = ("agent", "workflow")

# Same texts as migration 0016_ag_ui.
AG_UI_SCOPE_KIND_CHECK = "scope_kind IN ('agent', 'workflow')"
AG_UI_FROM_SEQ_CHECK = "from_seq >= -1"
AG_UI_FINGERPRINT_CHECK = "fingerprint ~ '^[0-9a-f]{64}$'"
AG_UI_INPUT_MESSAGE_IDS_CHECK = "jsonb_typeof(input_message_ids) = 'array'"
AG_UI_INPUT_MESSAGES_CHECK = "input_messages IS NULL OR jsonb_typeof(input_messages) = 'array'"

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


class AgUiRunModel(Base):
    """One AG-UI run. See the module docstring and 0016_ag_ui."""

    __tablename__ = "ag_ui_runs"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    scope_kind: Mapped[str] = mapped_column("scope_kind", String, nullable=False)
    # The agent id (scope `agent`) or the workflow id (scope `workflow`).
    scope_id: Mapped[str] = mapped_column("scope_id", String, nullable=False)
    # The client's AG-UI threadId and runId, as sent.
    external_thread_id: Mapped[str] = mapped_column("external_thread_id", String, nullable=False)
    external_run_id: Mapped[str] = mapped_column("external_run_id", String, nullable=False)
    # Our run: the agent turn, or the workflow run a resume run continues.
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    # The AG-UI run streams the run's events with seq > from_seq (-1 for an agent turn).
    from_seq: Mapped[int] = mapped_column("from_seq", BigInteger, nullable=False)
    # sha256 hex of the request's canonical JSON (D5): a known runId with another one is a 409.
    fingerprint: Mapped[str] = mapped_column("fingerprint", String, nullable=False)
    # The client's ids of the input (user) messages, in order (D13).
    input_message_ids: Mapped[list] = mapped_column(
        "input_message_ids", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Workflow AG-UI runs: where the run starts and ends in its root's commit order (`root_order`;
    # NULL: the beginning / not ended yet). Recorded once, never re-derived (AGUI.8, D7).
    from_order: Mapped[int | None] = mapped_column("from_order", BigInteger, nullable=True)
    end_order: Mapped[int | None] = mapped_column("end_order", BigInteger, nullable=True)
    # A workflow thread's first AG-UI run only: the user messages it carried, [{id, content}], so
    # the thread's snapshots and its thread GET show them. NULL on every other row.
    input_messages: Mapped[list | None] = mapped_column("input_messages", JSONB, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "scope_kind", "scope_id", "external_thread_id", "external_run_id", name="ag_ui_runs_pkey"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_ag_ui_runs_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(AG_UI_SCOPE_KIND_CHECK, name="ck_ag_ui_runs_scope_kind"),
        CheckConstraint(AG_UI_FROM_SEQ_CHECK, name="ck_ag_ui_runs_from_seq"),
        CheckConstraint(AG_UI_FINGERPRINT_CHECK, name="ck_ag_ui_runs_fingerprint"),
        CheckConstraint(AG_UI_INPUT_MESSAGE_IDS_CHECK, name="ck_ag_ui_runs_input_message_ids"),
        CheckConstraint(AG_UI_INPUT_MESSAGES_CHECK, name="ck_ag_ui_runs_input_messages"),
        Index("ix_ag_ui_runs_tenant_run", "tenant_id", "run_id"),
    )


@dataclass(frozen=True)
class AgUiRunRecord:
    tenant_id: str
    scope_kind: str
    scope_id: str
    external_thread_id: str
    external_run_id: str
    run_id: str
    from_seq: int
    fingerprint: str
    input_message_ids: tuple[str, ...]
    created_at: datetime
    from_order: int | None = None
    end_order: int | None = None
    # [{id, content}] of a workflow thread's first AG-UI run; () on every other row.
    input_messages: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class PendingClientToolCall:
    """A client tool call the latest turn ended with, still unanswered.

    `tool_name` and `args` come from the call's `tool:{id}:call` event; both are None when that
    event is missing (never expected: the worker writes it before `run_completed`)."""

    run_id: str
    turn: int
    tool_call_id: str
    tool_name: str | None
    args: Any


def _record(row: AgUiRunModel) -> AgUiRunRecord:
    return AgUiRunRecord(
        tenant_id=row.tenant_id,
        scope_kind=row.scope_kind,
        scope_id=row.scope_id,
        external_thread_id=row.external_thread_id,
        external_run_id=row.external_run_id,
        run_id=row.run_id,
        from_seq=int(row.from_seq),
        fingerprint=row.fingerprint,
        input_message_ids=tuple(row.input_message_ids or ()),
        created_at=row.created_at,
        from_order=None if row.from_order is None else int(row.from_order),
        end_order=None if row.end_order is None else int(row.end_order),
        input_messages=tuple(
            {"id": str(m["id"]), "content": str(m["content"])} for m in (row.input_messages or ()) if isinstance(m, dict)
        ),
    )


def _key_filter(
    tenant_id: str, scope_kind: str, scope_id: str, external_thread_id: str, external_run_id: str
) -> tuple[Any, ...]:
    return (
        AgUiRunModel.tenant_id == tenant_id,
        AgUiRunModel.scope_kind == scope_kind,
        AgUiRunModel.scope_id == str(scope_id),
        AgUiRunModel.external_thread_id == external_thread_id,
        AgUiRunModel.external_run_id == external_run_id,
    )


@dataclass(frozen=True)
class AgUiRunWrite:
    """An AG-UI run to record with the turn that runs it: RunStore.create_turn(ag_ui_run=...)
    writes it in the turn's own transaction, so the turn never exists without its AG-UI row
    (D4) and a concurrent POST of the same runId with another body always loses to the creator."""

    scope_kind: AgUiScopeKind
    scope_id: str
    external_thread_id: str
    external_run_id: str
    fingerprint: str
    from_seq: int = -1
    input_message_ids: tuple[str, ...] = ()
    # [{id, content}]: a workflow thread's first AG-UI run only (None: not stored).
    input_messages: tuple[dict[str, str], ...] | None = None


class AgUiScopeMismatch(ValueError):
    """The run is not a run of the AG-UI scope: an `agent` scope needs an agent run of that agent,
    a `workflow` scope a workflow run of that workflow. Pickle-safe."""

    def __init__(self, tenant_id: str, run_id: str, scope_kind: str, scope_id: str):
        super().__init__(tenant_id, run_id, scope_kind, scope_id)
        self.tenant_id = tenant_id
        self.run_id = run_id
        self.scope_kind = scope_kind
        self.scope_id = scope_id

    def __str__(self) -> str:
        return f"run {self.run_id} is not a run of {self.scope_kind} {self.scope_id}"


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def validated_ag_ui_values(tenant_id: str, run_id: str, write: AgUiRunWrite) -> dict[str, Any]:
    """The insert values of an AG-UI run row, after the same checks as the DB constraints (else
    ValueError): known scope_kind; non-empty tenant, scope, thread, run and run_id; from_seq an
    integer >= -1; fingerprint exactly 64 lowercase hex digits; message ids strings."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    if write.scope_kind not in AG_UI_SCOPE_KINDS:
        raise ValueError(f"scope_kind must be one of {AG_UI_SCOPE_KINDS}")
    if not write.scope_id or not write.external_thread_id or not write.external_run_id or not run_id:
        raise ValueError("scope_id, external_thread_id, external_run_id and run_id are required")
    from_seq = write.from_seq
    if isinstance(from_seq, bool) or not isinstance(from_seq, int) or from_seq < -1:
        raise ValueError("from_seq must be an integer >= -1")
    # fullmatch: `$` alone would accept a trailing newline, which the DB check refuses.
    if not isinstance(write.fingerprint, str) or not _FINGERPRINT_RE.fullmatch(write.fingerprint):
        raise ValueError("fingerprint must be a lowercase sha256 hex digest")
    message_ids = list(write.input_message_ids)
    if not all(isinstance(m, str) for m in message_ids):
        raise ValueError("input_message_ids must be strings")
    messages = None
    if write.input_messages is not None:
        messages = [dict(m) for m in write.input_messages]
        if not all(
            set(m) == {"id", "content"} and isinstance(m["id"], str) and m["id"] and isinstance(m["content"], str)
            for m in messages
        ):
            raise ValueError("input_messages must be {id, content} objects of strings with a non-empty id")
    values = dict(
        tenant_id=tenant_id,
        scope_kind=write.scope_kind,
        scope_id=str(write.scope_id),
        external_thread_id=write.external_thread_id,
        external_run_id=write.external_run_id,
        run_id=str(run_id),
        from_seq=from_seq,
        fingerprint=write.fingerprint,
        input_message_ids=message_ids,
    )
    if messages is not None:  # absent stays SQL NULL (a Python None would be JSON null)
        values["input_messages"] = messages
    return values


def check_run_scope(tenant_id: str, run: RunModel, scope_kind: str, scope_id: str) -> None:
    """AgUiScopeMismatch unless `run` is an agent run of agent `scope_id` (scope `agent`) or a
    workflow run of workflow `scope_id` (scope `workflow`)."""
    if scope_kind == "agent":
        ok = run.kind == "agent" and run.agent_id is not None and str(run.agent_id) == str(scope_id)
    else:
        ok = run.kind == "workflow" and run.workflow_id is not None and str(run.workflow_id) == str(scope_id)
    if not ok:
        raise AgUiScopeMismatch(tenant_id, str(run.id), scope_kind, str(scope_id))


def insert_ag_ui_run_in_session(session, values: dict[str, Any]) -> bool:
    """INSERT ... ON CONFLICT (pkey) DO NOTHING in the caller's transaction; True if inserted."""
    return (
        session.execute(
            pg_insert(AgUiRunModel)
            .values(**values)
            .on_conflict_do_nothing(constraint="ag_ui_runs_pkey")
            .returning(AgUiRunModel.run_id)
        ).fetchone()
        is not None
    )


class AgUiStore:
    """The AG-UI run mapping and the pending client tool calls, tenant-scoped."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def record_ag_ui_run(
        self,
        tenant_id: str,
        *,
        scope_kind: AgUiScopeKind,
        scope_id: str,
        external_thread_id: str,
        external_run_id: str,
        run_id: str,
        fingerprint: str,
        from_seq: int = -1,
        input_message_ids: tuple[str, ...] | list[str] = (),
        from_order: int | None = None,
        input_messages: tuple[dict[str, str], ...] | list[dict[str, str]] | None = None,
    ) -> tuple[AgUiRunRecord, bool]:
        """Record the AG-UI run `(scope, threadId, runId)`; idempotent, the first writer wins.

        Returns (record, created). When the row already exists it is returned unchanged with
        created False, whatever this call's run_id, fingerprint or from_seq: the caller compares the
        fingerprint (a mismatch is the D4 409). Concurrent callers are serialised by the primary
        key: exactly one gets created True, and all see its row.

        For a new agent turn, prefer RunStore.create_turn(ag_ui_run=AgUiRunWrite(...)), which writes
        the row in the turn's transaction; this method serves workflow resume runs and turns
        recorded without it.

        Raises ValueError for an invalid argument (see validated_ag_ui_values), RunNotFound when
        `run_id` is not a run of this tenant, and AgUiScopeMismatch (a ValueError) when the run is
        not a run of the scope (another agent or workflow, or another kind).
        """
        write = AgUiRunWrite(
            scope_kind=scope_kind,
            scope_id=scope_id,
            external_thread_id=external_thread_id,
            external_run_id=external_run_id,
            fingerprint=fingerprint,
            from_seq=from_seq,
            input_message_ids=tuple(input_message_ids),
            input_messages=None if input_messages is None else tuple(input_messages),
        )
        values = validated_ag_ui_values(tenant_id, run_id, write)
        if from_order is not None:
            values["from_order"] = int(from_order)
        key = _key_filter(tenant_id, scope_kind, str(scope_id), external_thread_id, external_run_id)
        with self.Session() as session:
            run = session.execute(
                select(RunModel).where(RunModel.tenant_id == tenant_id, RunModel.id == str(run_id))
            ).scalar_one_or_none()
            if run is None:
                session.rollback()
                raise RunNotFound(tenant_id, str(run_id))
            check_run_scope(tenant_id, run, scope_kind, str(scope_id))
            try:
                inserted = insert_ag_ui_run_in_session(session, values)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if _constraint_name(exc) == "fk_ag_ui_runs_tenant_run":  # the run was deleted in between
                    raise RunNotFound(tenant_id, str(run_id)) from exc
                raise
            row = session.execute(select(AgUiRunModel).where(*key)).scalar_one()
            return _record(row), inserted

    def set_end_order(
        self, tenant_id: str, *, scope_kind: AgUiScopeKind, scope_id: str, external_thread_id: str,
        external_run_id: str, end_order: int,
    ) -> int:
        """Record where the AG-UI run ended in its root's commit order; the first value written
        stands (it is derived from a committed, gapless prefix, so every writer computes the same).
        Returns the recorded value."""
        key = _key_filter(tenant_id, scope_kind, str(scope_id), external_thread_id, external_run_id)
        with self.Session() as session:
            row = session.execute(select(AgUiRunModel).where(*key).with_for_update()).scalar_one()
            if row.end_order is None:
                row.end_order = int(end_order)
            value = int(row.end_order)
            session.commit()
            return value

    def get_ag_ui_run(
        self,
        tenant_id: str,
        *,
        scope_kind: AgUiScopeKind,
        scope_id: str,
        external_thread_id: str,
        external_run_id: str,
    ) -> AgUiRunRecord | None:
        """The tenant's AG-UI run `(scope, threadId, runId)`, or None."""
        key = _key_filter(tenant_id, scope_kind, str(scope_id), external_thread_id, external_run_id)
        with self.Session() as session:
            row = session.execute(select(AgUiRunModel).where(*key)).scalar_one_or_none()
            return None if row is None else _record(row)

    def pending_client_tool_calls(
        self, tenant_id: str, conversation_id: str, *, before_run_id: str | None = None
    ) -> list[PendingClientToolCall]:
        """The client tool calls a turn ended with, in the order its `run_completed` event lists
        them (`pending_tool_call_ids`): the calls the NEXT turn answers.

        The turn read is the conversation's latest turn, or, with `before_run_id` naming a turn of
        this conversation that already exists, the turn just before it. Pass the run id of the turn
        being started (AG-UI: the uuid5 run id): when a crash left that turn created but its AG-UI
        row unwritten, the re-POST still validates its tool results against the turn it answers,
        and the worker or a REST caller building the turn's results gets the same answer. An
        unknown `before_run_id` (the turn does not exist yet) reads the latest turn.

        Only one turn counts: an earlier turn's pending calls were answered (or given the synthetic
        "not executed by the client" error) by the turn after it. [] when the conversation has no
        such turn, is another tenant's, or the turn read is not completed or left nothing pending.
        """
        conv_id = str(conversation_id)
        with self.Session() as session:
            query = select(RunModel.id, RunModel.turn, RunModel.status).where(
                RunModel.tenant_id == tenant_id, RunModel.conversation_id == conv_id
            )
            if before_run_id is not None:
                before_turn = session.execute(
                    select(RunModel.turn).where(
                        RunModel.tenant_id == tenant_id,
                        RunModel.conversation_id == conv_id,
                        RunModel.id == str(before_run_id),
                    )
                ).scalar_one_or_none()
                if before_turn is not None:
                    query = query.where(RunModel.turn < before_turn)
            latest = session.execute(query.order_by(RunModel.turn.desc()).limit(1)).one_or_none()
            if latest is None or latest.status != "completed":
                return []
            completed = session.execute(
                select(ExecutionEventModel.data).where(
                    ExecutionEventModel.tenant_id == tenant_id,
                    ExecutionEventModel.run_id == latest.id,
                    ExecutionEventModel.type == "run_completed",
                )
            ).scalars().first()
            ids = (completed or {}).get("pending_tool_call_ids") if isinstance(completed, dict) else None
            if not isinstance(ids, list):
                return []
            ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]
            if not ids:
                return []
            calls = {
                (data or {}).get("tool_call_id"): data or {}
                for data in session.execute(
                    select(ExecutionEventModel.data).where(
                        ExecutionEventModel.tenant_id == tenant_id,
                        ExecutionEventModel.run_id == latest.id,
                        ExecutionEventModel.event_key.in_([f"tool:{i}:call" for i in ids]),
                    )
                ).scalars()
            }
            return [
                PendingClientToolCall(
                    run_id=latest.id,
                    turn=int(latest.turn),
                    tool_call_id=i,
                    tool_name=calls.get(i, {}).get("tool_name"),
                    args=calls.get(i, {}).get("args"),
                )
                for i in ids
            ]
