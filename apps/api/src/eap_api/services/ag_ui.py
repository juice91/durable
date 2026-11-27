"""AG-UI: the data and business rules behind the AG-UI routes (agent threads, and what workflow threads share).

The routes keep the protocol: input mapping, the D7 stream plan, framing, headers, the stream limiter. This
service keeps what they read and write: runs, events, the AG-UI run rows, thread history as AG-UI
messages (D13), attachments of a turn, interrupts and their decisions, the viewer's feedback and the
conversation of a thread. A method takes plain values (the tenant, the agent, ids), never a request or a
principal; a refusal is a `ServiceError` whose body is what the API has always answered (`plain_refusals`:
`{"detail": <message>}`, or a list-valued `detail_object`).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from eap_api import model_discovery
from eap_api.approvals import DecisionResult, decide
from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.access import Access
from eap_api.services.converse import require_own_conversation
from eap_api.services.conversations import ConversationService
from eap_api.services.errors import Conflict, InvalidInput, NotFound, ServiceError
from eap_api.services.run_filters import plain_refusals
from eap_api.services.run_options import RunOptions
from eap_api.services.runs import launch_recorded_run, lock_agent
from eap_run_store import (
    AgUiRunModel,
    AgUiRunRecord,
    AgUiRunWrite,
    AgUiScopeMismatch,
    AgUiStore,
    ApprovalStore,
    ConversationIdConflict,
    ConversationResourceConflict,
    EventLog,
    RunNotFound,
    RunStore,
    TurnInProgress,
)
from eap_run_store.chat_attachments import (
    ATTACHMENT_CAPABILITIES_KEY,
    ChatAttachmentStore,
    MessageFeedbackStore,
    accepts_by_capabilities,
)
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

AGENT_NOT_FOUND = "Agent not found"
UNKNOWN_RUN_DETAIL = "Unknown runId"
FINGERPRINT_CONFLICT_DETAIL = "runId was already used with a different input"
MAX_MSG_CHARS = 200  # the cap of the compact detail the AG-UI input errors use (eap_api.ag_ui.input)

__all__ = [
    "AGENT_NOT_FOUND",
    "ATTACHMENT_CAPABILITIES_KEY",
    "AgUiRunRecord",
    "AgUiService",
    "LockedAgent",
    "TurnBusy",
    "FINGERPRINT_CONFLICT_DETAIL",
    "ThreadData",
    "TurnRun",
    "UNKNOWN_RUN_DETAIL",
    "compact_error",
]


def compact_error(code: str, loc: Sequence[str | int], msg: str, **extra: Any) -> list[dict[str, Any]]:
    """The one-item `detail` list of an AG-UI input error: `{loc: ["body", ...], msg, type}`."""
    return [{"loc": ["body", *loc], "msg": msg[:MAX_MSG_CHARS], "type": code, **extra}]


class TurnBusy(Exception):
    """The thread's previous turn is still running (`run_id`): the route waits for it (D7)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


class LockedAgent:
    """The tenant's agent, locked FOR SHARE until the block of `AgUiService.locked_agent` ends, with the
    validation of a new turn's options under that lock."""

    def __init__(self, conversations: ConversationService, tenant_id: str, agent: AgentModel) -> None:
        self.conversations = conversations
        self._tenant_id = tenant_id
        self.agent = agent

    def new_turn_options(
        self, *, version: Any, request_context: dict[str, Any] | None, model: Any, model_settings: Any,
        input_text: str,
    ) -> RunOptions:
        """409 AGENT_ARCHIVED for an archived agent, then 404/409/422/413 (ConversationService.new_turn_options)."""
        return self.conversations.new_turn_options(
            self._tenant_id, self.agent, version=version, request_context=request_context, model=model,
            model_settings=model_settings, input_text=input_text,
        )


@dataclass(frozen=True)
class TurnRun:
    """The AG-UI run that started a turn: its external id and the user-message ids it recorded."""

    external_run_id: str
    input_message_ids: tuple[str, ...]


@dataclass
class ThreadData:
    """What a thread's history is built from: its turns, the completed ones' stored messages, and the AG-UI
    run that started each turn."""

    turns: list[dict[str, Any]]
    completed: dict[str, dict[str, Any]]
    records: dict[str, TurnRun]


def _unsupported_message(model_id: str, media_type: str, source: str) -> str:
    if source == "assumed":
        return (
            f"model {model_id} is assumed not to accept {media_type} (its provider did not report file "
            "support, so this is a guess from the provider); text files are always accepted"
        )
    return f"model {model_id} does not accept {media_type} ({source} capabilities); text files are always accepted"


@plain_refusals
class AgUiService:
    """Reads and writes of the AG-UI routes. Every lookup is tenant-scoped."""

    def __init__(
        self,
        engine: Any,
        run_store: RunStore,
        event_log: EventLog,
        ag_ui: AgUiStore,
        approvals: ApprovalStore,
        api_keys: Any,
        attachments: ChatAttachmentStore,
        feedback: MessageFeedbackStore,
        launcher: RunLauncher,
    ) -> None:
        self.engine = engine
        self.run_store = run_store
        self.event_log = event_log
        self.ag_ui = ag_ui
        self.approvals = approvals
        self.api_keys = api_keys
        self.attachments = attachments
        self.feedback = feedback
        self.launcher = launcher

    # -- plain reads ---------------------------------------------------------------------------

    def get_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any] | None:
        return self.run_store.get_run(tenant_id, run_id)

    def list_events(self, tenant_id: str, run_id: uuid.UUID, after_seq: int | None = None) -> list[RunEvent]:
        if after_seq is None:
            return self.event_log.list_events(tenant_id, run_id)
        return self.event_log.list_events(tenant_id, run_id, after_seq=after_seq)

    def list_root_events(self, tenant_id: str, run_id: str, after: int | None = None) -> Any:
        if after is None:
            return self.event_log.list_root_events(tenant_id, run_id)
        return self.event_log.list_root_events(tenant_id, run_id, after)

    def get_event(self, tenant_id: str, run_id: uuid.UUID, key: str) -> Any:
        return self.event_log.get_event(tenant_id, run_id, key)

    def get_conversation(self, tenant_id: str, conversation_id: str) -> dict[str, Any] | None:
        return self.run_store.get_conversation(tenant_id, conversation_id)

    def get_ag_ui_run(self, tenant_id: str, **key: str) -> AgUiRunRecord | None:
        return self.ag_ui.get_ag_ui_run(tenant_id, **key)

    def pending_client_tool_calls(self, tenant_id: str, conversation_id: str, *, before_run_id: str) -> list[Any]:
        return self.ag_ui.pending_client_tool_calls(tenant_id, conversation_id, before_run_id=before_run_id)

    def approval_rows(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        return self.approvals.list_for_run(tenant_id, run_id)

    def require_own_thread(self, access: Access, tenant_id: str, conversation_id: str) -> None:
        require_own_conversation(access, self.run_store, tenant_id, conversation_id)

    def require_agent(self, tenant_id: str, agent_id: str) -> None:
        """404 `Agent not found` for an agent that is not the tenant's."""
        with Session(self.engine) as db:
            found = db.execute(
                select(AgentModel.id).where(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id)
            ).first()
        if found is None:
            raise NotFound(AGENT_NOT_FOUND)

    def key_is_active(self, tenant_id: str, key_id: str) -> bool:
        """The API key is still active: a plain read of its row (revoked_at, expires_at), not
        `authenticate`, so the re-check neither needs the secret nor touches last_used_at."""
        record = self.api_keys.get(tenant_id, key_id)
        return record is not None and record.status() == "active"

    def model_requests(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        """`RunSummary` data for `RUN_FINISHED.usage`: the run's `run_model_requests` rows."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT model_id, request_key, operation, input_tokens, output_tokens,"
                    " cache_read_tokens, cache_write_tokens FROM run_model_requests"
                    " WHERE tenant_id = :t AND run_id = :r ORDER BY created_at, request_key"
                ),
                {"t": tenant_id, "r": run_id},
            ).mappings().all()
        return {"model_requests": [dict(r) for r in rows]}

    def end_seq(self, tenant_id: str, record: AgUiRunRecord) -> int | None:
        """Where this AG-UI run ended when a later AG-UI run continues the same durable run (a resume
        run after an interrupt, AGUI.8): the first later row's from_seq. None otherwise."""
        with Session(self.engine) as session:
            return session.execute(
                select(func.min(AgUiRunModel.from_seq)).where(
                    AgUiRunModel.tenant_id == tenant_id,
                    AgUiRunModel.run_id == record.run_id,
                    AgUiRunModel.from_seq > record.from_seq,
                )
            ).scalar_one_or_none()

    # -- agent threads: what is recorded ---------------------------------------------------------

    def thread_message_ids(self, tenant_id: str, agent_id: str, thread_id: str) -> list[tuple[str, ...]]:
        """The client user-message ids recorded by each AG-UI run of this thread, oldest first."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(AgUiRunModel.input_message_ids)
                .where(
                    AgUiRunModel.tenant_id == tenant_id,
                    AgUiRunModel.scope_kind == "agent",
                    AgUiRunModel.scope_id == agent_id,
                    AgUiRunModel.external_thread_id == thread_id,
                )
                .order_by(AgUiRunModel.created_at, AgUiRunModel.external_run_id)
            ).scalars()
            return [tuple(str(i) for i in (ids or []) if isinstance(i, str)) for ids in rows]

    def turn_runs(self, tenant_id: str, agent_id: str, run_ids: list[str]) -> dict[str, TurnRun]:
        """The AG-UI run that started each of these turns (the row with the lowest from_seq)."""
        if not run_ids:
            return {}
        with Session(self.engine) as session:
            rows = session.execute(
                select(AgUiRunModel)
                .where(
                    AgUiRunModel.tenant_id == tenant_id,
                    AgUiRunModel.scope_kind == "agent",
                    AgUiRunModel.scope_id == agent_id,
                    AgUiRunModel.run_id.in_(run_ids),
                )
                .order_by(AgUiRunModel.from_seq.desc(), AgUiRunModel.created_at.desc())
            ).scalars().all()
            # The last assignment wins: the lowest from_seq.
            return {
                row.run_id: TurnRun(row.external_run_id, tuple(str(i) for i in (row.input_message_ids or ())))
                for row in rows
            }

    def starter(self, tenant_id: str, agent_id: str, record: AgUiRunRecord) -> TurnRun:
        """The AG-UI run that started the record's turn: the record itself, or for a resume run
        (AGUI.8, from_seq > -1) the turn's first AG-UI run, whose ids the turn's messages keep."""
        if record.from_seq < 0:
            return TurnRun(record.external_run_id, record.input_message_ids)
        return self.turn_runs(tenant_id, agent_id, [record.run_id]).get(record.run_id) or TurnRun(
            record.external_run_id, ()
        )

    def latest_turn_id(self, tenant_id: str, conversation_id: str) -> str | None:
        turns = self.run_store.list_turns(tenant_id, conversation_id)
        return str(turns[-1]["id"]) if turns else None

    def open_and_decided_rows(self, tenant_id: str, latest: str | None) -> list[dict[str, Any]]:
        """The approval rows of the thread's latest turn itself (not of child runs)."""
        if latest is None:
            return []
        return [r for r in self.approvals.list_for_run(tenant_id, latest) if r["run_id"] == latest]

    # -- agent threads: what the history of a thread is built from (eap_api.ag_ui.thread) ----------

    def attachment_files(self, tenant_id: str, message_ids: list[str]) -> dict[str, Any]:
        """The attachments sent with these user messages, by message id."""
        return self.attachments.for_messages(tenant_id, message_ids)

    def thread_data(
        self, tenant_id: str, agent_id: str, conversation_id: str, before_turn: int | None = None
    ) -> ThreadData:
        """The thread's turns below `before_turn` (all when None), oldest first, with the stored messages of
        the completed ones and the AG-UI run that started each."""
        turns = self.run_store.list_turns(tenant_id, conversation_id)
        if before_turn is not None:
            turns = [t for t in turns if t["turn"] < before_turn]
        if not turns:
            return ThreadData([], {}, {})
        last = turns[-1]["turn"] + 1
        completed = {
            str(t["run_id"]): t for t in self.run_store.completed_turns_before(tenant_id, conversation_id, last)
        }
        records = self.turn_runs(tenant_id, agent_id, [str(t["id"]) for t in turns])
        return ThreadData(turns, completed, records)

    # -- feedback --------------------------------------------------------------------------------

    def viewer_feedback(self, tenant_id: str, run_id: str, viewer: str) -> list[dict[str, Any]]:
        """The viewing user's own feedback on one turn, for the replay."""
        rows = self.feedback.for_runs(tenant_id, [run_id], created_by=viewer)
        return [{"message_id": r.message_id, "rating": r.rating, "comment": r.comment} for r in rows]

    def thread_feedback(self, tenant_id: str, conversation_id: str, viewer: str) -> dict[str, Any]:
        ids = [str(t["id"]) for t in self.run_store.list_turns(tenant_id, conversation_id)]
        rows = self.feedback.for_runs(tenant_id, ids, created_by=viewer)
        return {
            r.message_id: {"rating": r.rating, "comment": r.comment, "updated_at": r.updated_at.isoformat()}
            for r in rows
        }

    # -- recording -------------------------------------------------------------------------------

    def record_resume_run(
        self, tenant_id: str, key: Mapping[str, str], *, turn_id: str, fingerprint: str, from_seq: int
    ) -> tuple[AgUiRunRecord, bool]:
        """Record an AG-UI run that resumes the thread's latest turn (D15). 409 for a threadId of
        another agent, or a runId already used with a different input."""
        try:
            record, created = self.ag_ui.record_ag_ui_run(
                tenant_id, **key, run_id=turn_id, fingerprint=fingerprint, from_seq=from_seq
            )
        except AgUiScopeMismatch:
            raise Conflict("threadId belongs to another agent") from None
        if not created and record.fingerprint != fingerprint:
            raise Conflict(FINGERPRINT_CONFLICT_DETAIL)
        return record, created

    def adopt_turn(
        self, tenant_id: str, key: Mapping[str, str], *, run_id: str, fingerprint: str,
        input_message_ids: Sequence[str],
    ) -> AgUiRunRecord | None:
        """The turn exists without its AG-UI row (recorded by a path that did not write it): record it
        now, first writer wins. None when the row cannot be written (another agent's thread, a run
        that is gone)."""
        try:
            record, _ = self.ag_ui.record_ag_ui_run(
                tenant_id, **key, run_id=run_id, fingerprint=fingerprint, input_message_ids=input_message_ids,
            )
        except (AgUiScopeMismatch, RunNotFound):
            return None
        return record

    def record_concurrent(
        self, tenant_id: str, key: Mapping[str, str], *, run_id: str, fingerprint: str,
        input_message_ids: Sequence[str],
    ) -> AgUiRunRecord:
        """The AG-UI row of a turn a concurrent POST of the same runId created: its row, or (first writer
        wins) one recorded now with this request's fingerprint."""
        record = self.ag_ui.get_ag_ui_run(tenant_id, **key)
        if record is None:
            record, _ = self.ag_ui.record_ag_ui_run(
                tenant_id, **key, run_id=run_id, fingerprint=fingerprint, input_message_ids=input_message_ids
            )
        return record

    def ensure_conversation(
        self, tenant_id: str, agent_id: str, conversation_id: str, external_thread_id: str | None,
        resource_id: str | None,
    ) -> None:
        """Create the thread's conversation (the first run of a thread)."""
        try:
            self.run_store.create_conversation(
                tenant_id, agent_id, None, conversation_id,
                resource_id=resource_id, external_thread_id=external_thread_id,
            )
        except ConversationResourceConflict:
            # A concurrent first run of the thread created it with another resource.
            raise Conflict(
                "this thread already has another resource; resourceId cannot change",
                detail_object=compact_error(
                    "resource_mismatch", ["forwardedProps", "pydanticos", "resourceId"],
                    "this thread already has another resource; resourceId cannot change",
                ),
            ) from None
        except ConversationIdConflict:
            raise Conflict("threadId is already in use") from None

    # -- interrupts --------------------------------------------------------------------------------

    def decide_approval(
        self, tenant_id: str, *, run_id: str, kind: Any, key: str, decision: Any, decided_by: str, comment: str | None
    ) -> DecisionResult:
        """Record one approval decision (idempotent) and wake the waiting run (eap_api.approvals)."""
        return decide(
            self.event_log, self.launcher, tenant_id=tenant_id, run_id=run_id, kind=kind, key=key,
            decision=decision, decided_by=decided_by, comment=comment,
        )

    # -- attachments of a turn ---------------------------------------------------------------------

    def check_attachments(
        self, tenant_id: str, created_by: str, attachments: Sequence[tuple[str, Sequence[str]]], model_id: str
    ) -> dict[str, Any] | None:
        """422 before anything is written: every referenced attachment exists in this tenant, is not
        already sent in another message, and the run's model takes its media type (W2.4).

        Returns the run's attachment capabilities `{image, pdf, source}`, decided here once from
        discovery (eap_api.model_discovery.capabilities_for); the run records them on
        `runs.model_settings.attachment_capabilities` and the worker reads them back. None when the
        request sends no file. A model whose capabilities are only `assumed` is refused with words that
        say so, not as a fact about the model."""
        if not attachments:
            return None
        provider, _, name = model_id.partition(":")
        capabilities = model_discovery.capabilities_for(provider, name).public()
        metas = self.attachments.metas(tenant_id, [i for _, ids in attachments for i in ids])
        # created_by: another principal's unsent upload is "not found"
        for message_id, ids in attachments:
            for attachment_id in ids:
                meta = metas.get(attachment_id)
                if meta is None or (meta.message_id is None and meta.created_by != created_by):
                    raise self._not_found(attachment_id)
                if meta.message_id is not None and meta.message_id != message_id:
                    msg = f"attachment {attachment_id} was already sent in another message"
                    raise InvalidInput(msg, detail_object=compact_error("attachment_in_use", ["messages"], msg))
                if not accepts_by_capabilities(capabilities, meta.media_type):
                    msg = _unsupported_message(model_id, meta.media_type, capabilities["source"])
                    raise InvalidInput(
                        msg,
                        detail_object=compact_error(
                            "attachment_unsupported_by_model", ["messages"], msg,
                            media_type=meta.media_type, source=capabilities["source"],
                        ),
                    )
        return capabilities

    @staticmethod
    def _not_found(attachment_id: str) -> InvalidInput:
        msg = f"attachment {attachment_id} was not found; upload it with POST /attachments"
        return InvalidInput(msg, detail_object=compact_error("attachment_not_found", ["messages"], msg))

    def link_attachments(
        self, tenant_id: str, created_by: str, attachments: Iterable[tuple[str, Sequence[str]]], run_id: str
    ) -> None:
        """Link each message's uploads to run `run_id`. Only the caller's own uploads link (the same check
        as `check_attachments`, repeated here under the update so a race cannot slip a foreign one in):
        another principal's upload is reported as not found, like an unknown id."""
        for message_id, ids in attachments:
            linked = self.attachments.link(tenant_id, ids, message_id=message_id, run_id=run_id, created_by=created_by)
            for attachment_id in ids:
                if attachment_id not in linked:
                    raise self._not_found(attachment_id)

    def attachment_for_download(
        self, tenant_id: str, attachment_id: str, *, created_by: str, via_api_key: bool,
        agent_ids: Sequence[str] | None,
    ) -> Any:
        """The attachment's metadata, or 404. A key sees what it uploaded and what is linked to a message
        of a conversation of an agent it may use (`agent_ids`; None: any); the dev bearer sees the tenant's."""
        if via_api_key:
            meta = self.attachments.get_for_key(
                tenant_id, attachment_id, created_by, None if agent_ids is None else tuple(agent_ids)
            )
        else:
            meta = self.attachments.get(tenant_id, attachment_id)
        if meta is None:
            raise NotFound("Attachment not found")
        return meta

    # -- hand-off ----------------------------------------------------------------------------------

    def relaunch_turn(self, tenant_id: str, run: dict[str, Any]) -> dict[str, Any]:
        """Hand a recorded turn to the worker again (a re-POST of a known runId: a crash between insert and
        enqueue heals). Returns the run as it stands afterwards; `Unavailable` when the enqueue fails,
        `Conflict` once a later turn has started."""
        with Session(self.engine) as db:
            return launch_recorded_run(db, self.launcher, self.run_store, tenant_id, run, False)

    # -- recording a new turn ------------------------------------------------------------------------

    @contextmanager
    def locked_agent(self, tenant_id: str, agent_id: str) -> Iterator[LockedAgent]:
        """The agent locked FOR SHARE (an archive waits for the block, or the block sees `archived`); the
        transaction commits when the block ends normally, which releases the lock."""
        with Session(self.engine) as db:
            try:
                agent = lock_agent(db, tenant_id, agent_id)
            except ServiceError as exc:
                exc.detail_object = exc.message  # the body this route always answered: no `code`
                raise
            yield LockedAgent(ConversationService(db, self.run_store, self.launcher), tenant_id, agent)
            db.commit()

    def record_turn(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        conversation_id: str,
        run_id: str,
        key: Mapping[str, str],
        fingerprint: str,
        input_message_ids: Sequence[str],
        input_text: str,
        version: Any,
        request_context: dict[str, Any] | None,
        model: Any,
        model_settings: Any,
        attachment_capabilities: dict[str, Any] | None,
        client_tools: list[dict[str, Any]] | None,
        client_tool_results: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], bool]:
        """Record the turn idempotently on `run_id` (with its AG-UI row) and hand it to the worker; validated
        again under the agent lock, so an archive or a version change during a wait is seen.
        Returns (the run after the hand-off, created). `TurnBusy` while the previous turn runs."""

        def same_fingerprint(run: dict[str, Any]) -> bool:
            # A concurrent POST of this runId created the turn (created False): compare its AG-UI row.
            record = self.record_concurrent(
                tenant_id, key, run_id=str(run["id"]), fingerprint=fingerprint, input_message_ids=input_message_ids
            )
            return record.fingerprint == fingerprint

        with self.locked_agent(tenant_id, agent_id) as locked:
            conversations = locked.conversations

            def options() -> RunOptions:
                built = locked.new_turn_options(
                    version=version, request_context=request_context, model=model,
                    model_settings=model_settings, input_text=input_text,
                )
                if attachment_capabilities is None:
                    return built
                return dataclasses.replace(
                    built,
                    model_settings={**(built.model_settings or {}), ATTACHMENT_CAPABILITIES_KEY: attachment_capabilities},
                )

            try:
                return conversations.record_and_launch_turn(
                    tenant_id, conversation_id, run_id=run_id, new_options=options, input_text=input_text,
                    same_request=same_fingerprint, conflict_detail=FINGERPRINT_CONFLICT_DETAIL,
                    client_tools=client_tools, client_tool_results=client_tool_results,
                    ag_ui_run=AgUiRunWrite(**key, fingerprint=fingerprint, input_message_ids=tuple(input_message_ids)),
                )
            except AgUiScopeMismatch:
                raise Conflict("threadId belongs to another agent", detail_object="threadId belongs to another agent") from None
            except TurnInProgress as busy:
                raise TurnBusy(busy.run_id) from None
