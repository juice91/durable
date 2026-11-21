"""AG-UI: the data and business rules behind the AG-UI routes (agent threads, and what workflow threads share).

The routes keep the protocol: input mapping, the D7 stream plan, framing, headers, the stream limiter. This
service keeps what they read and write: runs, events, the AG-UI run rows, thread history as AG-UI
messages (D13), attachments of a turn, interrupts and their decisions, the viewer's feedback and the
conversation of a thread. A method takes plain values (the tenant, the agent, ids), never a request or a
principal; a refusal is a `ServiceError` whose body is what the API has always answered (`plain_refusals`:
`{"detail": <message>}`, or a list-valued `detail_object`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from eap_api import model_discovery
from eap_api.ag_ui.snapshot import assistant_message_id, turn_messages, user_message
from eap_api.approvals import DecisionResult, decide
from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_api.services.run_filters import plain_refusals
from eap_api.services.runs import launch_recorded_run
from eap_run_store import (
    AgUiRunModel,
    AgUiRunRecord,
    AgUiScopeMismatch,
    AgUiStore,
    ApprovalStore,
    ConversationIdConflict,
    ConversationResourceConflict,
    EventLog,
    RunNotFound,
    RunStore,
)
from eap_run_store.chat_attachments import (
    ATTACHMENT_CAPABILITIES_KEY,
    ChatAttachmentStore,
    MessageFeedbackStore,
    accepts_by_capabilities,
)
from eap_run_store.store import TERMINAL_RUN_STATUSES
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
    "FINGERPRINT_CONFLICT_DETAIL",
    "ThreadView",
    "TurnRun",
    "UNKNOWN_RUN_DETAIL",
    "compact_error",
]


def compact_error(code: str, loc: Sequence[str | int], msg: str, **extra: Any) -> list[dict[str, Any]]:
    """The one-item `detail` list of an AG-UI input error: `{loc: ["body", ...], msg, type}`."""
    return [{"loc": ["body", *loc], "msg": msg[:MAX_MSG_CHARS], "type": code, **extra}]


@dataclass(frozen=True)
class TurnRun:
    """The AG-UI run that started a turn: its external id and the user-message ids it recorded."""

    external_run_id: str
    input_message_ids: tuple[str, ...]


@dataclass
class ThreadView:
    messages: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    active_run_id: str | None = None


def _turn_entry(prefix: str, client_id: str | None, run: dict[str, Any], done: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One earlier turn's AG-UI messages: from its stored messages when it completed with them,
    else its user input (and a completed run's output as the assistant answer)."""
    if done is not None and done.get("messages"):
        structured = done.get("output") if run.get("output_format") == "json" else None
        return turn_messages(prefix, done["messages"], user_message_client_id=client_id, structured_output=structured)
    out: list[dict[str, Any]] = []
    if run.get("input"):
        out.append(user_message(prefix, run["input"], client_id))
    if done is not None and done.get("output"):
        out.append({"id": assistant_message_id(prefix, 0), "role": "assistant", "content": done["output"]})
    return out


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

    # -- agent threads: history as AG-UI messages (D13) ------------------------------------------

    def attachment_parts(
        self, tenant_id: str, messages: list[dict[str, Any]], *, via_api_key: bool
    ) -> list[dict[str, Any]]:
        """User messages that sent attachments carry AG-UI 1.0 media parts (text first)."""
        ids = [m["id"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
        by_message = self.attachments.for_messages(tenant_id, ids)
        if not by_message:
            return messages
        # An API key fetches through the AG-UI twin; the dev bearer through the dev route.
        base = "/api/v1/ag-ui/attachments" if via_api_key else "/api/v1/attachments"
        out: list[dict[str, Any]] = []
        for m in messages:
            files = by_message.get(m.get("id")) if m.get("role") == "user" else None
            if not files:
                out.append(m)
                continue
            parts: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m["content"] else []
            for f in files:
                kind = "image" if f.media_type.startswith("image/") else "document"
                parts.append({
                    "type": kind,
                    "source": {"type": "url", "value": f"{base}/{f.id}", "mimeType": f.media_type},
                    "metadata": {"attachmentId": f.id, "filename": f.name, "size": f.size},
                })
            out.append({**m, "content": parts})
        return out

    def thread_view(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        *,
        via_api_key: bool,
        before_turn: int | None = None,
        later_ids: tuple[str, ...] = (),
    ) -> ThreadView:
        """The thread's turns below `before_turn` (all when None) as AG-UI messages, oldest first.
        Ids follow D13 with the AG-UI runId that started each turn as the prefix (the native run id
        for a turn started elsewhere, e.g. Studio's REST chat).

        Regenerates (D5, D7): a turn whose user message a later turn sends again (a regenerate
        re-POSTs the same user message under a new runId) is superseded: the later turn shows the
        same first client id, or re-sends all of the earlier turn's ids. Its messages are
        left out, so the thread shows the user message once, followed by the latest branch's answer,
        and AG-UI message ids stay unique in the thread. The superseded turn stays in `runs` with
        `superseded: true` (it is still a run, with its own usage and trace). `later_ids` are the
        user-message ids of a turn after `before_turn` (the run being streamed), which supersede too."""
        turns = self.run_store.list_turns(tenant_id, conversation_id)
        if before_turn is not None:
            turns = [t for t in turns if t["turn"] < before_turn]
        if not turns:
            return ThreadView()
        last = turns[-1]["turn"] + 1
        completed = {
            str(t["run_id"]): t for t in self.run_store.completed_turns_before(tenant_id, conversation_id, last)
        }
        records = self.turn_runs(tenant_id, agent_id, [str(t["id"]) for t in turns])
        # Walk newest first. A turn shows its user message under its first client id, so an earlier
        # turn is superseded when a later turn (or `later_ids`) shows the same first id, or re-sends
        # all of its ids: ids stay unique, and a turn re-running only part of an earlier turn's
        # messages (a regenerate of u1 after [u0, u1]) leaves that earlier turn's text in place.
        later: list[tuple[str, ...]] = [later_ids] if later_ids else []
        superseded: set[str] = set()
        for t in reversed(turns):
            rec = records.get(str(t["id"]))
            ids = rec.input_message_ids if rec is not None else ()
            if ids and any(l[0] == ids[0] or set(ids) <= set(l) for l in later):
                superseded.add(str(t["id"]))
            if ids:
                later.append(ids)
        view = ThreadView()
        for t in turns:
            run_id = str(t["id"])
            rec = records.get(run_id)
            prefix = rec.external_run_id if rec is not None else run_id
            client_id = rec.input_message_ids[0] if rec is not None and rec.input_message_ids else None
            if run_id not in superseded:
                view.messages += _turn_entry(prefix, client_id, t, completed.get(run_id))
            view.runs.append(
                {
                    "runId": rec.external_run_id if rec is not None else None,
                    "run_id": run_id,
                    "turn": t["turn"],
                    "status": t["status"],
                    "display_status": t["display_status"],
                    "superseded": run_id in superseded,
                }
            )
            if t["status"] not in TERMINAL_RUN_STATUSES and rec is not None:
                view.active_run_id = rec.external_run_id
        view.messages = self.attachment_parts(tenant_id, view.messages, via_api_key=via_api_key)
        return view

    def history(
        self, tenant_id: str, agent_id: str, run: dict[str, Any], record: AgUiRunRecord, *, via_api_key: bool
    ) -> list[dict[str, Any]]:
        """`AgUiStreamContext.history`: the thread's earlier turns (those this run regenerates left
        out, see thread_view), then this run's user message."""
        starter = self.starter(tenant_id, agent_id, record)
        out = self.thread_view(
            tenant_id, agent_id, str(run["conversation_id"]), via_api_key=via_api_key,
            before_turn=run["turn"], later_ids=starter.input_message_ids,
        ).messages
        if run.get("input"):
            client_id = starter.input_message_ids[0] if starter.input_message_ids else None
            out.append(user_message(starter.external_run_id, run["input"], client_id))
        return self.attachment_parts(tenant_id, out, via_api_key=via_api_key)

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
