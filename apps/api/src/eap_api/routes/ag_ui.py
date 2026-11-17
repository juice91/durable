"""AG-UI agent endpoints (AGUI.6): an AG-UI client runs a published agent over SSE.

Plan: docs/plans/2026-11-13-milestone-ag-ui.md (D2-D5, D7, D9, D13, D16, D18 and "### AGUI.6").

- `POST /api/v1/ag-ui/agents/{agent_id}`: a `RunAgentInput` body, an AG-UI SSE answer. The API
  records the turn and hands it to the worker, then streams the translation of the run's durable
  events (it never runs the agent itself, D1).
- `GET  /api/v1/ag-ui/agents/{agent_id}/threads/{threadId}`: the thread's messages as AG-UI
  messages (history loading) and its turns, with the AG-UI runId of an unfinished turn.
- `GET  /api/v1/ag-ui/agents/{agent_id}/threads/{threadId}/runs/{runId}/stream`: attach to a
  recorded run, from the start or after `Last-Event-ID: {runId}:{seq}` (or `?after=<seq>`).

Auth (D16, the AGUI.3 hand-off): `agui_principal` (an API key or the dev bearer), never
`current_principal`; `require_agui_agent` and a tenant-scoped agent lookup give one 404 `Agent not
found` for an unknown agent, another tenant's and one outside the key's allowlist. Every request
re-authenticates; an API key's open stream re-checks its key at most every `key_recheck_s` (60 s)
and ends with `RUN_ERROR unauthorized` once it is revoked or expired (the run continues).

The POST, in order, everything before the stream opens:
1. 404 for the agent; the body guard (D18): 413 past EAP_AGUI_MAX_BODY_BYTES, 415, 422 deeper than
   64, then the unstorable-string check (422) after authentication;
2. `parse_run_agent_input_async` (AGUI.5: 422/413 for the input) and `check_principal_options`
   (403: a key may not run the draft or override the model);
3. a stream slot (429 when the limiter is full);
4. a known runId (`ag_ui_runs`, keyed on the canonical `AgUiInput.thread_id`): the D7 table via
   `plan_stream` (409 for another fingerprint, 400 for a malformed `Last-Event-ID`, the run's
   `end_seq` passed), then the recorded run is launched again (a crash between insert and enqueue
   heals) and streamed. A known runId on an archived agent still attaches;
5. an unknown runId: 404 with a `Last-Event-ID` other than `{runId}:-1` (nothing is recorded;
   `:-1` is a client that lost the stream during the wait below: the new turn, opened as a
   continuation without RUN_STARTED); else a new turn: the D5
   tail is deduped against the thread's recorded `input_message_ids` (AGUI.5 review M1), the
   options are validated (`new_turn_options`: 409 AGENT_ARCHIVED, 404/409/422/413), client tools
   checked against the spec's server tools (422), the thread resolved (D3: a conversation of this
   tenant and agent, else the derived one, created with its `resource_id`; another `resourceId`
   later is 409 resource_mismatch), tool results checked against the pending client calls (422),
   `resume[]` checked against the thread's open and decided interrupts (422), then
   `record_and_launch_turn` with `create_turn(ag_ui_run=AgUiRunWrite(...))`;
5a. (AGUI.8, D15) a `resume[]` (its payloads checked first: 422 `edited_args_unsupported`,
   `resume_payload_invalid`; new user text or tool results beside it are 422
   `resume_with_input`) decides the latest turn's interrupts through `eap_api.approvals.decide`
   (an earlier decision wins and is shown), then records the AG-UI run on that same turn with
   `from_seq` = the latest answered `tool_approval_waiting` seq, and streams the turn from there
   (no new turn, no hand-off);
5b. (AGUI.8, Q5) a new runId without `resume[]` while the latest turn has an open interrupt
   records nothing: the stream sends `RUN_STARTED`, `CUSTOM pydanticos.run`, `MESSAGES_SNAPSHOT`
   (with the gated calls) and the open interrupts again as `RUN_FINISHED{outcome: interrupt}`;
6. the previous turn still running (TurnInProgress): the stream opens with `RUN_STARTED` and
   `CUSTOM pydanticos.waiting`, waits up to EAP_AGUI_PREVIOUS_TURN_WAIT_S (120 s) for it, then
   records the turn and sends `CUSTOM pydanticos.run` and `MESSAGES_SNAPSHOT` (with the answer the
   client missed) before its own events; a timeout ends with `RUN_ERROR previous_turn_running`.

A client disconnect closes the stream only: the run is the worker's and keeps going.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_observability import log_failure

from eap_api.ag_ui.frames import (
    KEEP_ALIVE,
    custom_frame,
    previous_turn_timeout_frame,
    run_error_frame,
    run_started_frame,
    stream_interrupted_frame,
    unauthorized_frame,
)
from eap_api.ag_ui.input import (
    AGUI_DEPTH_DETAIL,
    AGUI_MAX_BODY_DEPTH,
    AgentThread,
    AgUiInput,
    AgUiInputError,
    NO_NEW_INPUT,
    RESOURCE_MISMATCH,
    agui_max_body_bytes,
    check_agent_input,
    check_client_tool_clashes,
    check_principal_options,
    check_resume_covers,
    check_thread_resource,
    check_tool_results,
    derive_agent_run_id,
    fingerprint,
    parse_run_agent_input,
    parse_run_agent_input_async,
    path_safe_id,
    resolve_agent_thread,
    thread_key,
    thread_resource_id,
)
from eap_api.ag_ui.resume_entries import (
    RESUME_WITH_INPUT,
    ResumeDecision,
    ThreadInterrupts,
    apply_decisions,
    open_approvals,
    resume_decisions,
    thread_interrupts,
    waiting_seq,
)
from eap_api.ag_ui.snapshot import assistant_message_id, turn_messages, user_message
from eap_run_store.chat_attachments import max_attachment_bytes
from eap_run_store.chat_attachments import ATTACHMENT_CAPABILITIES_KEY, ChatAttachmentStore, MessageFeedbackStore, accepts_by_capabilities
from eap_api.ag_ui.translate import AgUiFormatter, AgUiStreamContext
from eap_api import model_discovery
from eap_api.agent_archive import AGENT_ARCHIVED
from eap_api.attachments_shared import (
    attachment_response,
    attachment_store,
    created_by_for,
    delete_unsent_attachment,
    read_upload,
    store_upload,
)
from eap_api.auth import AgUiPrincipal, AgUiPrincipalDep, feedback_viewer, get_api_key_store, require_agui_agent
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import guarded_json_route, unstorable_params, unstorable_strings
from eap_api.routes.conversations import new_turn_options, record_and_launch_turn
from eap_api.run_options import RunOptions
from eap_api.routes.runs import launch_recorded_run, lock_agent
from eap_api.schemas import ErrorDetail
from eap_api.sse import (
    DeltaSource,
    StreamLease,
    StreamLimiter,
    StreamSettings,
    TERMINAL_STATUSES,
    get_delta_source,
    get_stream_limiter,
    get_stream_settings,
    release_stream_slot,
)
from eap_api.streams.formatter import format_stream
from eap_api.streams.native import run_items
from eap_api.streams.resume import LastEventIdError, StreamPlan, parse_last_event_id, plan_stream
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
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

PREVIOUS_TURN_WAIT_ENV = "EAP_AGUI_PREVIOUS_TURN_WAIT_S"
DEFAULT_PREVIOUS_TURN_WAIT_S = 120.0
DEFAULT_KEY_RECHECK_S = 60.0
UNKNOWN_RUN_DETAIL = "Unknown runId"
FINGERPRINT_CONFLICT_DETAIL = "runId was already used with a different input"
STREAM_LIMIT_DETAIL = "Concurrent SSE stream limit reached; retry after another stream closes"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_PATH_ID_CHARS = 256

# D18: 2 MiB (EAP_AGUI_MAX_BODY_BYTES), depth 64. No storable check in the route class: that check
# depends on `current_principal`, which refuses API keys; `_checked_json_body` runs it after
# `agui_principal` instead (the AGUI.3 hand-off).
AgUiRoute = guarded_json_route(
    max_body_bytes=agui_max_body_bytes,
    max_depth=AGUI_MAX_BODY_DEPTH,
    depth_detail=AGUI_DEPTH_DETAIL,
    check_storable=False,
)

router = APIRouter(
    prefix="/ag-ui",
    tags=["ag-ui"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
    route_class=AgUiRoute,
)


# --------------------------------------------------------------------------- settings and stores


@dataclass(frozen=True)
class AgUiRouteSettings:
    """`previous_turn_wait_s`: how long a new run waits for the thread's previous turn (D7);
    `key_recheck_s`: how often an API key's open stream re-checks its key (AGUI.3 review)."""

    previous_turn_wait_s: float = DEFAULT_PREVIOUS_TURN_WAIT_S
    key_recheck_s: float = DEFAULT_KEY_RECHECK_S


def get_agui_settings() -> AgUiRouteSettings:
    try:
        wait = float(os.environ.get(PREVIOUS_TURN_WAIT_ENV, DEFAULT_PREVIOUS_TURN_WAIT_S))
    except ValueError:
        wait = DEFAULT_PREVIOUS_TURN_WAIT_S
    return AgUiRouteSettings(previous_turn_wait_s=wait if wait >= 0 else DEFAULT_PREVIOUS_TURN_WAIT_S)


def _app_store(request: Request, name: str, factory: Callable[[Any], Any]) -> Any:
    store = getattr(request.app.state, name, None)
    if store is None:
        store = factory(request.app.state.database_engine)
        setattr(request.app.state, name, store)
    return store


@dataclass
class _Ctx:
    """What one request needs, off the event loop."""

    engine: Any
    run_store: RunStore
    event_log: EventLog
    ag_ui: AgUiStore
    approvals: ApprovalStore
    api_keys: Any
    principal: AgUiPrincipal
    agent_id: str
    launcher: RunLauncher | None = None

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    def key(self, thread_id: str, run_id: str) -> dict[str, str]:
        return {
            "scope_kind": "agent",
            "scope_id": self.agent_id,
            "external_thread_id": thread_id,
            "external_run_id": run_id,
        }


def _ctx(request: Request, principal: AgUiPrincipal, agent_id: str, launcher: RunLauncher | None = None) -> _Ctx:
    run_store, event_log = request.app.state.run_stores
    return _Ctx(
        engine=request.app.state.database_engine,
        run_store=run_store,
        event_log=event_log,
        ag_ui=_app_store(request, "ag_ui_store", AgUiStore),
        approvals=_app_store(request, "approval_store", ApprovalStore),
        api_keys=get_api_key_store(request),
        principal=principal,
        agent_id=agent_id,
        launcher=launcher,
    )


# --------------------------------------------------------------------------- request checks


def _agent_id_or_404(principal: AgUiPrincipal, value: str) -> str:
    """The canonical agent UUID of the path; malformed and not allowed are 404 like unknown."""
    try:
        agent_id = str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    require_agui_agent(principal, agent_id)
    return agent_id


def _path_id(value: str, detail: str) -> str:
    """A threadId or runId path segment: the same rule as the POST's input (path_safe_id); anything
    else can never have been recorded, so it is 404 like an unknown id."""
    if not value or len(value) > MAX_PATH_ID_CHARS or _CONTROL.search(value) is not None or not path_safe_id(value):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    return value


def _check_params(request: Request) -> None:
    errors = unstorable_params(request)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=[{"loc": ["body"], "msg": "the body is not valid JSON", "type": "json_invalid"}],
        )


async def _checked_json_body(request: Request) -> Any:
    """The body as JSON, after the storable-string check of the path, query and body (422 with the
    guard's compact list). The route class already enforced size, UTF-8 and depth."""
    _check_params(request)
    raw = await request.body()
    body = await run_in_threadpool(_parse_json, raw) if len(raw) > 256 * 1024 else _parse_json(raw)
    errors = await run_in_threadpool(unstorable_strings, body) if len(raw) > 256 * 1024 else unstorable_strings(body)
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
    return body


def _limiter_key(principal: AgUiPrincipal) -> str:
    if principal.is_api_key:
        return f"{principal.tenant_id}:apikey:{principal.key_id}"
    return f"{principal.tenant_id}:{principal.user_id}"


def _acquire(limiter: StreamLimiter, principal: AgUiPrincipal) -> StreamLease:
    lease = limiter.acquire(_limiter_key(principal))
    if lease is None:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=STREAM_LIMIT_DETAIL)
    return lease


def _plan_or_raise(plan: StreamPlan) -> StreamPlan:
    if plan.status != 200:
        raise HTTPException(status_code=plan.status, detail=plan.detail)
    return plan


# --------------------------------------------------------------------------- reads


def _thread_message_ids(c: _Ctx, thread_id: str) -> list[tuple[str, ...]]:
    """The client user-message ids recorded by each AG-UI run of this thread, oldest first."""
    with Session(c.engine) as session:
        rows = session.execute(
            select(AgUiRunModel.input_message_ids)
            .where(
                AgUiRunModel.tenant_id == c.tenant_id,
                AgUiRunModel.scope_kind == "agent",
                AgUiRunModel.scope_id == c.agent_id,
                AgUiRunModel.external_thread_id == thread_id,
            )
            .order_by(AgUiRunModel.created_at, AgUiRunModel.external_run_id)
        ).scalars()
        return [tuple(str(i) for i in (ids or []) if isinstance(i, str)) for ids in rows]


@dataclass(frozen=True)
class _TurnRun:
    external_run_id: str
    input_message_ids: tuple[str, ...]


def _records_by_run(c: _Ctx, run_ids: list[str]) -> dict[str, _TurnRun]:
    """The AG-UI run that started each of these turns (the row with the lowest from_seq)."""
    if not run_ids:
        return {}
    with Session(c.engine) as session:
        rows = session.execute(
            select(AgUiRunModel)
            .where(
                AgUiRunModel.tenant_id == c.tenant_id,
                AgUiRunModel.scope_kind == "agent",
                AgUiRunModel.scope_id == c.agent_id,
                AgUiRunModel.run_id.in_(run_ids),
            )
            .order_by(AgUiRunModel.from_seq.desc(), AgUiRunModel.created_at.desc())
        ).scalars().all()
        # The last assignment wins: the lowest from_seq.
        return {
            row.run_id: _TurnRun(row.external_run_id, tuple(str(i) for i in (row.input_message_ids or ())))
            for row in rows
        }


def _end_seq(c: _Ctx, record: AgUiRunRecord) -> int | None:
    """Where this AG-UI run ended when a later AG-UI run continues the same durable run (a resume
    run after an interrupt, AGUI.8): the first later row's from_seq. None otherwise."""
    with Session(c.engine) as session:
        return session.execute(
            select(func.min(AgUiRunModel.from_seq)).where(
                AgUiRunModel.tenant_id == c.tenant_id,
                AgUiRunModel.run_id == record.run_id,
                AgUiRunModel.from_seq > record.from_seq,
            )
        ).scalar_one_or_none()


def _model_requests(c: _Ctx, run_id: str) -> dict[str, Any]:
    """`RunSummary` data for `RUN_FINISHED.usage`: the run's `run_model_requests` rows."""
    with c.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT model_id, request_key, operation, input_tokens, output_tokens,"
                " cache_read_tokens, cache_write_tokens FROM run_model_requests"
                " WHERE tenant_id = :t AND run_id = :r ORDER BY created_at, request_key"
            ),
            {"t": c.tenant_id, "r": run_id},
        ).mappings().all()
    return {"model_requests": [dict(r) for r in rows]}


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


@dataclass
class _ThreadView:
    messages: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    active_run_id: str | None = None


def _thread_view(
    c: _Ctx, conversation_id: str, before_turn: int | None = None, later_ids: tuple[str, ...] = ()
) -> _ThreadView:
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
    turns = c.run_store.list_turns(c.tenant_id, conversation_id)
    if before_turn is not None:
        turns = [t for t in turns if t["turn"] < before_turn]
    if not turns:
        return _ThreadView()
    last = turns[-1]["turn"] + 1
    completed = {str(t["run_id"]): t for t in c.run_store.completed_turns_before(c.tenant_id, conversation_id, last)}
    records = _records_by_run(c, [str(t["id"]) for t in turns])
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
    view = _ThreadView()
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
        if t["status"] not in TERMINAL_STATUSES and rec is not None:
            view.active_run_id = rec.external_run_id
    view.messages = _with_attachment_parts(c, view.messages)
    return view


def _starter(c: _Ctx, record: AgUiRunRecord) -> _TurnRun:
    """The AG-UI run that started the record's turn: the record itself, or for a resume run
    (AGUI.8, from_seq > -1) the turn's first AG-UI run, whose ids the turn's messages keep."""
    if record.from_seq < 0:
        return _TurnRun(record.external_run_id, record.input_message_ids)
    return _records_by_run(c, [record.run_id]).get(record.run_id) or _TurnRun(record.external_run_id, ())


def _history(c: _Ctx, run: dict[str, Any], record: AgUiRunRecord) -> list[dict[str, Any]]:
    """`AgUiStreamContext.history`: the thread's earlier turns (those this run regenerates left
    out, see _thread_view), then this run's user message."""
    starter = _starter(c, record)
    out = _thread_view(
        c, str(run["conversation_id"]), before_turn=run["turn"], later_ids=starter.input_message_ids
    ).messages
    if run.get("input"):
        client_id = starter.input_message_ids[0] if starter.input_message_ids else None
        out.append(user_message(starter.external_run_id, run["input"], client_id))
    return _with_attachment_parts(c, out)


def _run_info(run: dict[str, Any]) -> dict[str, Any]:
    """`CUSTOM pydanticos.run`: the native ids Studio reads usage, cost and traces by."""
    return {
        "run_id": str(run["id"]),
        "conversation_id": run.get("conversation_id"),
        "turn": run.get("turn"),
        "trace_id": run.get("trace_id"),
        "model_id": run.get("model_id"),
        "version": run.get("agent_version"),
        "draft": run.get("draft_spec") is not None,
    }


# --------------------------------------------------------------------------- a stream to serve


@dataclass
class _Prepared:
    """A run to stream: the D7 plan, the run row, its AG-UI row, the events up to the plan's
    resume point (prior events) and the history."""

    plan: StreamPlan
    run: dict[str, Any]
    record: AgUiRunRecord
    prior: list[RunEvent]
    history: list[dict[str, Any]]
    # A new run recorded for a re-POST with `{runId}:-1`: open as a continuation (no RUN_STARTED).
    opens_as_continuation: bool = False
    # The D13 message-id prefix: the AG-UI runId that started the turn (None: the record's own).
    prefix: str | None = None
    # A whole stream computed up front (the Q5 re-send of the open interrupts): sent as is.
    static_frames: list[str] | None = None


@dataclass
class _Wait:
    """A new turn that must wait for the thread's previous turn (`busy_run_id`)."""

    busy_run_id: str
    new_turn: "_NewTurn"
    # The client already holds RUN_STARTED (a re-POST with `{runId}:-1` after losing the stream
    # during an earlier wait): the stream starts with CUSTOM pydanticos.waiting.
    continuation: bool = False


def _prepare_known(c: _Ctx, record: AgUiRunRecord, *, same_fingerprint: bool, last_event_id: str | None,
                   launch: Session | None) -> _Prepared:
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(record.run_id))
    if run is None:  # deleted with its conversation's runs: the row cascades, so never expected
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    events = c.event_log.list_events(c.tenant_id, uuid.UUID(record.run_id))
    plan = _plan_or_raise(
        plan_stream(
            run_id=record.external_run_id,
            recorded=True,
            same_fingerprint=same_fingerprint,
            created_now=False,
            from_seq=record.from_seq,
            last_event_id=last_event_id,
            max_seq=events[-1].seq if events else None,
            end_seq=_end_seq(c, record),
        )
    )
    if launch is not None:
        # The POST of a known runId re-sends the hand-off: a crash between insert and enqueue heals.
        assert c.launcher is not None
        run = launch_recorded_run(launch, c.launcher, c.run_store, c.tenant_id, run, False)
    upto = plan.after_seq if plan.mode == "continue" else record.from_seq
    return _Prepared(
        plan=plan, run=run, record=record, prior=[e for e in events if e.seq <= upto], history=_history(c, run, record),
        prefix=_starter(c, record).external_run_id,
    )


def _fresh(c: _Ctx, run: dict[str, Any], record: AgUiRunRecord, created: bool) -> _Prepared:
    plan = _plan_or_raise(
        plan_stream(
            run_id=record.external_run_id,
            recorded=True,
            same_fingerprint=True,
            created_now=created,
            from_seq=record.from_seq,
            last_event_id=None,
            max_seq=None,
        )
    )
    prior = c.event_log.list_events(c.tenant_id, uuid.UUID(record.run_id)) if record.from_seq >= 0 else []
    return _Prepared(
        plan=plan, run=run, record=record, prior=[e for e in prior if e.seq <= record.from_seq],
        history=_history(c, run, record), prefix=_starter(c, record).external_run_id,
    )


@dataclass
class _NewTurn:
    """A validated new turn, recorded by `_record` (now, or after the previous-turn wait)."""

    mapped: AgUiInput  # as sent: its fingerprint keys the runId
    effective: AgUiInput  # after the tail dedupe: what the turn runs
    conversation_id: str
    native_run_id: str
    fingerprint: str
    # Decided once when the request was validated (before any previous-turn wait); recorded on the run.
    attachment_capabilities: dict[str, Any] | None = None


def _without_users(body: dict[str, Any], messages: list[Any], drop: set[str]) -> AgUiInput:
    kept = [
        m
        for m in messages
        if not (isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("id"), str) and m["id"] in drop)
    ]
    return parse_run_agent_input({**body, "messages": kept})


def _deduped(body: Any, mapped: AgUiInput, recorded_runs: list[tuple[str, ...]]) -> AgUiInput:
    """The input without the tail's user messages already recorded for this thread (AGUI.5
    review M1): after a lost stream, a plain client's next message re-sends the previous user
    message too, because it never got the answer.

    When nothing new would remain, the request is a regenerate: it re-runs the user messages of
    the **latest** recorded run that holds any of the tail's ids, and only those (an earlier
    turn's message the tail still carries, e.g. [u1, u2] after a lost stream, stays deduped).
    The new turn then has exactly that run's ids, so it supersedes that turn alone in snapshots
    (`_thread_view`)."""
    recorded = {i for ids in recorded_runs for i in ids}
    if not recorded or not any(i in recorded for i in mapped.input_message_ids):
        return mapped
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list):
        return mapped
    try:
        return _without_users(body, messages, recorded)
    except AgUiInputError as exc:
        if exc.code != NO_NEW_INPUT:
            raise
    sent = set(mapped.input_message_ids)
    latest = next((set(ids) for ids in reversed(recorded_runs) if sent & set(ids)), set())
    try:
        return _without_users(body, messages, recorded - latest)
    except AgUiInputError as exc:
        if exc.code == NO_NEW_INPUT:
            return mapped
        raise


def _latest_turn_id(c: _Ctx, conversation_id: str) -> str | None:
    turns = c.run_store.list_turns(c.tenant_id, conversation_id)
    return str(turns[-1]["id"]) if turns else None


def _open_and_decided_interrupts(c: _Ctx, latest: str | None) -> ThreadInterrupts:
    """The interrupts of the thread's latest turn: open tool approvals (of a run that is not
    terminal), and decided or otherwise known ones."""
    if latest is None:
        return ThreadInterrupts([], [], [])
    rows = [r for r in c.approvals.list_for_run(c.tenant_id, latest) if r["run_id"] == latest]
    return thread_interrupts(rows, kinds=("tool",))


def _resume_turn(c: _Ctx, mapped: AgUiInput, fp: str, turn_id: str, decisions: list[ResumeDecision]) -> _Prepared:
    """D15 for an agent thread: decide the entries (the decision event, then the wake-up; an
    earlier decision wins), then record this AG-UI run on the same turn from the latest answered
    waiting seq, and stream the turn from there. A crash after deciding: the identical re-POST
    heals while the turn still waits on that batch (the ids are decided, check_resume_covers
    accepts them, `decide` answers `exists`); once the woken turn has opened a new batch, it is 422
    resume_missing_interrupt, and the client recovers through Q5 (a new runId without resume)."""
    apply_decisions(c.event_log, c.launcher, c.tenant_id, decisions, c.principal.default_resource_id)
    events = c.event_log.list_events(c.tenant_id, uuid.UUID(turn_id))
    seqs = [s for d in decisions if (s := waiting_seq(events, "tool", d.ref.key)) is not None]
    from_seq = max(seqs) if seqs else (events[-1].seq if events else -1)
    try:
        record, created = c.ag_ui.record_ag_ui_run(
            c.tenant_id, **c.key(mapped.thread_id, mapped.run_id), run_id=turn_id, fingerprint=fp, from_seq=from_seq
        )
    except AgUiScopeMismatch:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId belongs to another agent")
    if not created and record.fingerprint != fp:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=FINGERPRINT_CONFLICT_DETAIL)
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(turn_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    log.info("ag-ui resume %s recorded on run %s: %d decision(s)", mapped.run_id, turn_id, len(decisions))
    return _fresh(c, run, record, created)


def _reinterrupt(
    c: _Ctx, mapped: AgUiInput, fp: str, turn_id: str, interrupts: ThreadInterrupts, *, continuation: bool
) -> _Prepared:
    """Q5 (D15): a new runId without `resume[]` while the latest turn waits for approvals. Nothing
    is recorded; the stream is RUN_STARTED (unless the client holds it, `{runId}:-1`), CUSTOM run,
    MESSAGES_SNAPSHOT (the turn so far, naming the gated calls), one CUSTOM approval_required per
    open approval and RUN_FINISHED with all of them, in the order they were requested."""
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(turn_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    events = c.event_log.list_events(c.tenant_id, uuid.UUID(turn_id))
    max_seq = events[-1].seq if events else -1
    record = AgUiRunRecord(
        tenant_id=c.tenant_id, scope_kind="agent", scope_id=c.agent_id, external_thread_id=mapped.thread_id,
        external_run_id=mapped.run_id, run_id=turn_id, from_seq=max(max_seq, 0), fingerprint=fp,
        input_message_ids=(), created_at=datetime.now(timezone.utc),
    )
    formatter = AgUiFormatter(
        AgUiStreamContext(
            thread_id=mapped.client_thread_id or mapped.thread_id, run_id=mapped.run_id, native_run_id=turn_id,
            kind="agent", from_seq=max_seq, run_info=_run_info(run), history=_history(c, run, record),
            prior_events=events, message_prefix=_starter(c, record).external_run_id,
        )
    )
    approvals = open_approvals(c.event_log, c.tenant_id, interrupts.open_rows)
    log.info("ag-ui run %s re-sends %d open interrupt(s) of run %s; nothing recorded",
             mapped.run_id, len(approvals), turn_id)
    return _Prepared(
        plan=StreamPlan(status=200, mode="attach", after_seq=max_seq), run=run, record=record, prior=events,
        history=[], static_frames=formatter.reinterrupt_frames(approvals, run_started=not continuation),
    )


def _ensure_conversation(c: _Ctx, thread: AgentThread, mapped: AgUiInput) -> None:
    if thread.exists:
        return
    resource_id = thread_resource_id(
        mapped.options,
        user_id=c.principal.user_id if not c.principal.is_api_key else None,
        api_key_id=c.principal.key_id if c.principal.is_api_key else None,
    )
    try:
        c.run_store.create_conversation(
            c.tenant_id,
            c.agent_id,
            None,
            thread.conversation_id,
            resource_id=resource_id,
            external_thread_id=thread.external_thread_id,
        )
    except ConversationResourceConflict:
        # A concurrent first run of the thread created it with another resource.
        raise AgUiInputError(
            status.HTTP_409_CONFLICT,
            RESOURCE_MISMATCH,
            ["forwardedProps", "pydanticos", "resourceId"],
            "this thread already has another resource; resourceId cannot change",
        )
    except ConversationIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId is already in use")


def _check_attachments(c: _Ctx, mapped: AgUiInput, model_id: str) -> dict[str, Any] | None:
    """422 before anything is written: every referenced attachment exists in this tenant, is not
    already sent in another message, and the run's model takes its media type (W2.4).

    Returns the run's attachment capabilities `{image, pdf, source}`, decided here once from
    discovery (eap_api.model_discovery.capabilities_for); the run records them on
    `runs.model_settings.attachment_capabilities` and the worker reads them back. None when the
    request sends no file. A model whose capabilities are only `assumed` is refused with words that
    say so, not as a fact about the model."""
    if not mapped.attachments:
        return None
    provider, _, name = model_id.partition(":")
    capabilities = model_discovery.capabilities_for(provider, name).public()
    metas = ChatAttachmentStore(c.engine).metas(c.tenant_id, [i for _, ids in mapped.attachments for i in ids])
    for message_id, ids in mapped.attachments:
        for attachment_id in ids:
            meta = metas.get(attachment_id)
            if meta is None:
                raise AgUiInputError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "attachment_not_found", ["messages"],
                    f"attachment {attachment_id} was not found; upload it with POST /attachments",
                )
            if meta.message_id is not None and meta.message_id != message_id:
                raise AgUiInputError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "attachment_in_use", ["messages"],
                    f"attachment {attachment_id} was already sent in another message",
                )
            if not accepts_by_capabilities(capabilities, meta.media_type):
                error = AgUiInputError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT, "attachment_unsupported_by_model", ["messages"],
                    _unsupported_message(model_id, meta.media_type, capabilities["source"]),
                )
                error.detail[0].update({"media_type": meta.media_type, "source": capabilities["source"]})
                raise error
    return capabilities


def _with_attachment_capabilities(options: RunOptions, capabilities: dict[str, Any] | None) -> RunOptions:
    """The run's options with the attachment decision recorded among its model settings (the JSON
    the worker reads; absent for a turn without files)."""
    if capabilities is None:
        return options
    return dataclasses.replace(options, model_settings={**(options.model_settings or {}), ATTACHMENT_CAPABILITIES_KEY: capabilities})


def _unsupported_message(model_id: str, media_type: str, source: str) -> str:
    if source == "assumed":
        return (
            f"model {model_id} is assumed not to accept {media_type} (its provider did not report file "
            "support, so this is a guess from the provider); text files are always accepted"
        )
    return f"model {model_id} does not accept {media_type} ({source} capabilities); text files are always accepted"


def _link_attachments(c: _Ctx, mapped: AgUiInput) -> None:
    store = ChatAttachmentStore(c.engine)
    for message_id, ids in mapped.attachments:
        store.link(c.tenant_id, ids, message_id=message_id)


def _with_attachment_parts(c: _Ctx, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """User messages that sent attachments carry AG-UI 1.0 media parts (text first)."""
    ids = [m["id"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    by_message = ChatAttachmentStore(c.engine).for_messages(c.tenant_id, ids)
    if not by_message:
        return messages
    # An API key fetches through the AG-UI twin; the dev bearer through the dev route.
    base = "/api/v1/ag-ui/attachments" if c.principal.is_api_key else "/api/v1/attachments"
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


def _tool_dicts(mapped: AgUiInput) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    tools = [t.model_dump(mode="json") for t in mapped.client_tools] or None
    results = [r.model_dump(mode="json") for r in mapped.tool_results] or None
    return tools, results


def _record(c: _Ctx, new: _NewTurn) -> _Prepared:
    """Record and hand off the turn (raises TurnInProgress while the previous turn runs). Validated
    again under the agent lock, so an archive or a version change during a wait is seen."""
    assert c.launcher is not None
    mapped, effective = new.mapped, new.effective
    key = c.key(mapped.thread_id, mapped.run_id)

    def same_fingerprint(run: dict[str, Any]) -> bool:
        # A concurrent POST of this runId created the turn (created False): compare its AG-UI row.
        record = c.ag_ui.get_ag_ui_run(c.tenant_id, **key)
        if record is None:
            record, _ = c.ag_ui.record_ag_ui_run(
                c.tenant_id, **key, run_id=str(run["id"]), fingerprint=new.fingerprint,
                input_message_ids=effective.input_message_ids,
            )
        return record.fingerprint == new.fingerprint

    tools, results = _tool_dicts(effective)
    _link_attachments(c, effective)  # before the launch: the worker reads them through the turn's message ids
    with Session(c.engine) as db:
        agent = lock_agent(db, c.tenant_id, c.agent_id)
        try:
            run, created = record_and_launch_turn(
                db, c.launcher, c.run_store, c.tenant_id, new.conversation_id,
                run_id=new.native_run_id,
                new_options=lambda: _with_attachment_capabilities(
                    new_turn_options(
                        db, c.tenant_id, agent,
                        version=effective.options.version,
                        request_context=effective.options.request_context,
                        model=effective.options.model,
                        model_settings=effective.options.model_settings,
                        input_text=effective.input_text,
                    ),
                    new.attachment_capabilities,
                ),
                input_text=effective.input_text,
                same_request=same_fingerprint,
                conflict_detail=FINGERPRINT_CONFLICT_DETAIL,
                client_tools=tools,
                client_tool_results=results,
                ag_ui_run=AgUiRunWrite(
                    **key, fingerprint=new.fingerprint, input_message_ids=effective.input_message_ids
                ),
            )
        except AgUiScopeMismatch:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId belongs to another agent")
        db.commit()
    record = c.ag_ui.get_ag_ui_run(c.tenant_id, **key)
    assert record is not None  # written with the turn, or by same_fingerprint
    if created:
        log.info("ag-ui run %s recorded: tenant=%s agent=%s", run["id"], c.tenant_id, c.agent_id)
    return _fresh(c, run, record, created)


def _prepare_post(
    c: _Ctx, body: Any, mapped: AgUiInput, last_event_id: str | None, decisions: list[ResumeDecision] | None = None
) -> _Prepared | _Wait:
    """Everything of the POST before the stream opens (see the module docstring)."""
    header = last_event_id.strip() if last_event_id else None
    fp = fingerprint(mapped)
    key = c.key(mapped.thread_id, mapped.run_id)
    native_run_id = derive_agent_run_id(c.tenant_id, c.agent_id, mapped.thread_id, mapped.run_id)
    _load_agent_or_404(c)  # tenant-scoped: another tenant's agent is 404 before anything else
    record = c.ag_ui.get_ag_ui_run(c.tenant_id, **key)
    if record is None and c.run_store.get_run(c.tenant_id, uuid.UUID(native_run_id)) is not None:
        # The turn exists without its AG-UI row (recorded by a path that did not write it):
        # record it now, first writer wins, then treat the runId as known.
        try:
            record, _ = c.ag_ui.record_ag_ui_run(
                c.tenant_id, **key, run_id=native_run_id, fingerprint=fp,
                input_message_ids=mapped.input_message_ids,
            )
        except (AgUiScopeMismatch, RunNotFound):
            record = None
    if record is not None:
        # A replay starts nothing new, so it takes no agent lock (an archive or publish never
        # waits for its event and history loads); an archived agent's runs still attach.
        with Session(c.engine) as db:
            return _prepare_known(
                c, record, same_fingerprint=record.fingerprint == fp, last_event_id=header, launch=db
            )
    continuation = False
    if header:
        # Only `{runId}:-1` may name a runId not recorded yet: a client that lost the stream during
        # the previous-turn wait holds RUN_STARTED alone, and the wait ended with the connection,
        # so nothing was recorded. It is served as the new run, opened as a continuation.
        try:
            continuation = parse_last_event_id(header, mapped.run_id, None) == -1
        except LastEventIdError:
            continuation = False
        if not continuation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)

    with Session(c.engine) as db:
        agent = lock_agent(db, c.tenant_id, c.agent_id)
        # A new runId: validate everything before anything is written.
        thread = resolve_agent_thread(c.run_store, c.tenant_id, c.agent_id, mapped.thread_id)
        effective = _deduped(body, mapped, _thread_message_ids(c, mapped.thread_id))
        options = new_turn_options(
            db, c.tenant_id, agent,
            version=effective.options.version,
            request_context=effective.options.request_context,
            model=effective.options.model,
            model_settings=effective.options.model_settings,
            input_text=effective.input_text,
        )
        check_client_tool_clashes(effective.client_tools, options.target.spec)
        attachment_capabilities = _check_attachments(c, effective, options.model_id)
        if thread.exists:
            assert thread.conversation is not None
            check_thread_resource(thread.conversation, effective.options)
            pending = c.ag_ui.pending_client_tool_calls(c.tenant_id, thread.conversation_id, before_run_id=native_run_id)
            latest = _latest_turn_id(c, thread.conversation_id)
            interrupts = _open_and_decided_interrupts(c, latest)
        else:
            pending, latest, interrupts = [], None, ThreadInterrupts([], [], [])
        check_tool_results(effective, [p.tool_call_id for p in pending])
        check_resume_covers(effective, interrupts.open, interrupts.decided)
        if effective.resume and (effective.has_user_input or effective.tool_results):
            raise AgUiInputError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, RESUME_WITH_INPUT, ["resume"],
                "a run that answers interrupts may not carry a new user message or tool results",
            )
        if not effective.resume and not interrupts.open:
            # Under the agent lock, so an archive cannot slip in between the check and the thread.
            _ensure_conversation(c, thread, effective)
        db.commit()  # release the agent lock; _record takes it again

    if effective.resume:
        assert latest is not None  # check_resume_covers accepted ids of the latest turn
        return _resume_turn(c, mapped, fp, latest, decisions or resume_decisions(effective.resume))
    if interrupts.open:
        assert latest is not None
        return _reinterrupt(c, mapped, fp, latest, interrupts, continuation=continuation)

    new = _NewTurn(
        mapped=mapped, effective=effective, conversation_id=thread.conversation_id,
        native_run_id=native_run_id, fingerprint=fp, attachment_capabilities=attachment_capabilities,
    )
    try:
        prepared = _record(c, new)
    except TurnInProgress as busy:
        return _Wait(busy_run_id=busy.run_id, new_turn=new, continuation=continuation)
    prepared.opens_as_continuation = continuation
    return prepared


# --------------------------------------------------------------------------- streaming


def _http_error_frame(exc: HTTPException) -> str:
    """A refusal after the stream opened (the previous-turn wait), as `RUN_ERROR`."""
    detail = exc.detail
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        return run_error_frame(str(detail[0].get("msg") or "The run could not be started."), str(detail[0].get("type") or ""))
    message = detail if isinstance(detail, str) and detail else "The run could not be started."
    code = {
        status.HTTP_404_NOT_FOUND: "not_found",
        status.HTTP_409_CONFLICT: "agent_archived" if detail == AGENT_ARCHIVED else "conflict",
        status.HTTP_413_CONTENT_TOO_LARGE: "input_too_large",
        status.HTTP_422_UNPROCESSABLE_CONTENT: "invalid",
        status.HTTP_503_SERVICE_UNAVAILABLE: "unavailable",
    }.get(exc.status_code, "run_not_started")
    return run_error_frame(message, code)


@dataclass
class _StreamDeps:
    c: _Ctx
    thread_id: str  # the threadId as the client sent it (RUN_STARTED echoes it)
    settings: StreamSettings
    delta_source: DeltaSource
    agui: AgUiRouteSettings


async def static_stream(frames: list[str]) -> AsyncIterator[str]:
    for frame in frames:
        yield frame


def _frames(d: _StreamDeps, p: _Prepared, *, after_wait: bool = False) -> AsyncIterator[str]:
    c = d.c
    if p.static_frames is not None:
        return static_stream(p.static_frames)
    native = uuid.UUID(p.record.run_id)
    items = run_items(
        run_id=p.record.run_id,
        after_seq=p.plan.after_seq,
        load_events=lambda after: c.event_log.list_events(c.tenant_id, native, after_seq=after),
        load_run=lambda: c.run_store.get_run(c.tenant_id, native),
        delta_source=d.delta_source,
        settings=d.settings,
        load_summary=lambda: _model_requests(c, p.record.run_id),
        load_feedback=lambda: _viewer_feedback(c, p.record.run_id),
    )
    formatter = AgUiFormatter(
        AgUiStreamContext(
            thread_id=d.thread_id,
            run_id=p.record.external_run_id,
            native_run_id=p.record.run_id,
            kind="agent",
            from_seq=p.record.from_seq,
            run_info=_run_info(p.run),
            history=p.history,
            prior_events=p.prior,
            snapshot_on_start=p.plan.mode == "attach",
            message_prefix=p.prefix,
        )
    )
    # After the previous-turn wait the client already holds RUN_STARTED: the continuation opening
    # (CUSTOM pydanticos.run, MESSAGES_SNAPSHOT with the missed answer) follows.
    resume_after = p.plan.after_seq if (p.plan.mode == "continue" or after_wait or p.opens_as_continuation) else None
    return format_stream(items, formatter, resume_after=resume_after)


async def _wait_then_stream(d: _StreamDeps, wait: _Wait) -> AsyncIterator[str]:
    c = d.c
    new = wait.new_turn
    if not wait.continuation:
        yield run_started_frame(d.thread_id, new.mapped.run_id)
    busy = wait.busy_run_id
    yield custom_frame("waiting", {"run_id": busy})
    deadline = time.monotonic() + d.agui.previous_turn_wait_s
    next_keep_alive = time.monotonic() + d.settings.heartbeat_interval
    while True:
        previous = await asyncio.to_thread(c.run_store.get_run, c.tenant_id, uuid.UUID(busy))
        if previous is None or previous["status"] in TERMINAL_STATUSES:
            try:
                prepared = await run_in_threadpool(_record, c, new)
            except TurnInProgress as again:  # yet another turn started meanwhile: wait for it too
                busy = again.run_id
                continue
            except HTTPException as exc:
                yield _http_error_frame(exc)
                return
            break
        now = time.monotonic()
        if now >= deadline:
            yield previous_turn_timeout_frame()
            return
        if now >= next_keep_alive:
            yield KEEP_ALIVE
            next_keep_alive = now + d.settings.heartbeat_interval
        await asyncio.sleep(max(0.0, min(d.settings.poll_interval, deadline - now)))
    async with aclosing(_frames(d, prepared, after_wait=True)) as frames:
        async for frame in frames:
            yield frame


def _key_active(c: _Ctx) -> bool:
    """The stream's key is still active: a plain read of its row (revoked_at, expires_at), not
    `authenticate`, so the re-check neither needs the secret nor touches last_used_at."""
    record = c.api_keys.get(c.tenant_id, c.principal.key_id or "")
    return record is not None and record.status() == "active"


async def _rechecking_key(stream: AsyncIterator[str], c: _Ctx, interval: float) -> AsyncIterator[str]:
    """An API key's stream: before sending a frame, at most every `interval` seconds, check that
    the key is still active; once it is revoked or expired, `RUN_ERROR unauthorized` replaces the
    frame and the stream ends (never after the run's own terminal frame, which is the last one).
    A check that fails (a database error) ends the stream with `RUN_ERROR stream_interrupted`."""
    next_check = time.monotonic() + interval
    async with aclosing(stream) as frames:
        async for frame in frames:
            if time.monotonic() >= next_check:
                try:
                    active = await run_in_threadpool(_key_active, c)
                except Exception as exc:  # noqa: BLE001 - a failed check must still end the stream cleanly
                    # The run continues; the client resumes (D7) and is re-authenticated then.
                    log_failure(log, "ag-ui key re-check failed", exc, code="agui_key_recheck_failed")
                    yield stream_interrupted_frame()
                    return
                if not active:
                    yield unauthorized_frame()
                    return
                next_check = time.monotonic() + interval
            yield frame


def _response(d: _StreamDeps, stream: AsyncIterator[str], lease: StreamLease) -> StreamingResponse:
    if d.c.principal.is_api_key:
        stream = _rechecking_key(stream, d.c, d.agui.key_recheck_s)
    return StreamingResponse(release_stream_slot(stream, lease), media_type="text/event-stream", headers=SSE_HEADERS)


# --------------------------------------------------------------------------- routes


@router.post(
    "/agents/{agent_id}",
    response_class=StreamingResponse,
    responses={
        400: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 409: {"model": ErrorDetail},
        413: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 429: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
)
async def run_agent(
    agent_id: str,
    request: Request,
    principal: AgUiPrincipalDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
    settings: Annotated[StreamSettings, Depends(get_stream_settings)],
    limiter: Annotated[StreamLimiter, Depends(get_stream_limiter)],
    delta_source: Annotated[DeltaSource, Depends(get_delta_source)],
    agui: Annotated[AgUiRouteSettings, Depends(get_agui_settings)],
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Run the agent for one AG-UI run (`RunAgentInput`), answering with AG-UI SSE (D2, D7)."""
    agent = _agent_id_or_404(principal, agent_id)
    body = await _checked_json_body(request)
    mapped = await parse_run_agent_input_async(body)
    check_agent_input(mapped)
    check_principal_options(mapped.options, is_api_key=principal.is_api_key)
    decisions = resume_decisions(mapped.resume)  # the D15 payload rules: 422 before anything else
    c = _ctx(request, principal, agent, launcher)
    lease = _acquire(limiter, principal)
    try:
        prepared = await run_in_threadpool(_prepare_post, c, body, mapped, last_event_id, decisions)
    except BaseException:
        lease.release()
        raise
    d = _StreamDeps(c=c, thread_id=mapped.client_thread_id or mapped.thread_id, settings=settings,
                    delta_source=delta_source, agui=agui)
    if isinstance(prepared, _Wait):
        return _response(d, _wait_then_stream(d, prepared), lease)
    return _response(d, _frames(d, prepared), lease)


def _load_agent_or_404(c: _Ctx) -> None:
    with Session(c.engine) as db:
        found = db.execute(
            select(AgentModel.id).where(AgentModel.id == c.agent_id, AgentModel.tenant_id == c.tenant_id)
        ).first()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")


def _viewer_feedback(c: _Ctx, run_id: str) -> list[dict[str, Any]]:
    """The viewing user's own feedback on one turn, for the replay (a tenant-mate's rating or comment
    is never sent). API keys have no user, so they see none."""
    viewer = feedback_viewer(c.principal)
    if viewer is None:
        return []
    rows = MessageFeedbackStore(c.engine).for_runs(c.tenant_id, [run_id], created_by=viewer)
    return [{"message_id": r.message_id, "rating": r.rating, "comment": r.comment} for r in rows]


def _thread_feedback(c: _Ctx, conversation_id: str) -> dict[str, Any]:
    viewer = feedback_viewer(c.principal)
    if viewer is None:
        return {}
    ids = [str(t["id"]) for t in c.run_store.list_turns(c.tenant_id, conversation_id)]
    rows = MessageFeedbackStore(c.engine).for_runs(c.tenant_id, ids, created_by=viewer)
    return {
        r.message_id: {"rating": r.rating, "comment": r.comment, "updated_at": r.updated_at.isoformat()}
        for r in rows
    }


def _thread_json(c: _Ctx, thread_id: str) -> dict[str, Any]:
    _load_agent_or_404(c)
    thread = resolve_agent_thread(c.run_store, c.tenant_id, c.agent_id, thread_key(thread_id))
    view = _thread_view(c, thread.conversation_id) if thread.exists else _ThreadView()
    return {
        "threadId": thread_id,
        "conversationId": thread.conversation_id if thread.exists else None,
        "messages": view.messages,
        "runs": view.runs,
        "activeRunId": view.active_run_id,
        # The caller's thumbs on this thread's assistant messages: {messageId: {rating, comment, updated_at}}.
        "feedback": _thread_feedback(c, thread.conversation_id) if thread.exists else {},
    }


@router.get("/agents/{agent_id}/threads/{thread_id}")
async def get_thread(agent_id: str, thread_id: str, request: Request, principal: AgUiPrincipalDep) -> JSONResponse:
    """The thread's messages (AG-UI shape, D13 ids) for history loading, its turns, and
    `activeRunId`: the AG-UI runId of an unfinished turn to attach to (else null). An unknown
    thread is empty (200), like a thread before its first run."""
    agent = _agent_id_or_404(principal, agent_id)
    _check_params(request)
    thread_id = _path_id(thread_id, "Thread not found")
    c = _ctx(request, principal, agent)
    return JSONResponse(content=await run_in_threadpool(_thread_json, c, thread_id))


def _prepare_attach(c: _Ctx, thread_id: str, run_id: str, last_event_id: str | None) -> _Prepared:
    _load_agent_or_404(c)
    record = c.ag_ui.get_ag_ui_run(c.tenant_id, **c.key(thread_key(thread_id), run_id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    return _prepare_known(c, record, same_fingerprint=True, last_event_id=last_event_id, launch=None)


@router.get(
    "/agents/{agent_id}/threads/{thread_id}/runs/{run_id}/stream",
    response_class=StreamingResponse,
    responses={400: {"model": ErrorDetail}, 429: {"model": ErrorDetail}},
)
async def attach_run(
    agent_id: str,
    thread_id: str,
    run_id: str,
    request: Request,
    principal: AgUiPrincipalDep,
    settings: Annotated[StreamSettings, Depends(get_stream_settings)],
    limiter: Annotated[StreamLimiter, Depends(get_stream_limiter)],
    delta_source: Annotated[DeltaSource, Depends(get_delta_source)],
    agui: Annotated[AgUiRouteSettings, Depends(get_agui_settings)],
    after: Annotated[int | None, Query()] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Attach to a recorded AG-UI run (after a reload, D7): from the start, or a continuation after
    `Last-Event-ID: {runId}:{seq}` (wins) or `?after=<seq>`. Nothing is started."""
    agent = _agent_id_or_404(principal, agent_id)
    _check_params(request)
    thread_id = _path_id(thread_id, UNKNOWN_RUN_DETAIL)
    run_id = _path_id(run_id, UNKNOWN_RUN_DETAIL)
    header = last_event_id if last_event_id and last_event_id.strip() else (
        f"{run_id}:{after}" if after is not None else None
    )
    c = _ctx(request, principal, agent)
    lease = _acquire(limiter, principal)
    try:
        prepared = await run_in_threadpool(_prepare_attach, c, thread_id, run_id, header)
    except BaseException:
        lease.release()
        raise
    d = _StreamDeps(c=c, thread_id=thread_id, settings=settings, delta_source=delta_source, agui=agui)
    return _response(d, _frames(d, prepared), lease)


# --------------------------------------------------------------------------- attachments (API keys)

# Their own router: the AG-UI route class above reads every write body as capped UTF-8 JSON, which a
# multipart upload (up to EAP_CHAT_ATTACHMENT_MAX_BYTES) is not. include_router keeps each route's class.
_attachment_routes = APIRouter(
    tags=["ag-ui"], responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}}
)


@_attachment_routes.post(
    "/attachments",
    status_code=status.HTTP_201_CREATED,
    responses={413: {"model": ErrorDetail}, 415: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
async def upload_attachment(request: Request, principal: AgUiPrincipalDep, file: UploadFile) -> JSONResponse:
    """Upload a file (multipart field `file`) to reference from a run's message as an `image` or
    `document` part. Same limits and codes as `POST /attachments`; 201 {id, name, media_type, size}."""
    uploaded = await read_upload(request, file, max_attachment_bytes())
    return await store_upload(request, principal.tenant_id, file, uploaded, created_by_for(principal))


@_attachment_routes.get("/attachments/{attachment_id}", responses={410: {"description": "the blob is gone"}})
async def get_attachment(attachment_id: str, request: Request, principal: AgUiPrincipalDep) -> Response:
    """The bytes. A key sees what it uploaded and what is linked to a message of a conversation of an
    agent it may use; anything else (and another tenant's) is 404. The dev bearer sees the tenant's."""
    store = attachment_store(request)
    if principal.is_api_key:
        agents = None if principal.is_unrestricted else tuple(principal.agent_ids or ())
        meta = await run_in_threadpool(
            store.get_for_key, principal.tenant_id, attachment_id, created_by_for(principal), agents
        )
    else:
        meta = await run_in_threadpool(store.get, principal.tenant_id, attachment_id)
    if meta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    return await attachment_response(request, meta)


@_attachment_routes.delete(
    "/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail}},
)
async def delete_attachment(attachment_id: str, request: Request, principal: AgUiPrincipalDep) -> Response:
    """Discard an upload of the caller (the key that made it) before a message sends it: 204; 409
    `attachment_in_use` once a message references it; 404 when unknown, another tenant's or another
    caller's."""
    return await delete_unsent_attachment(request, principal.tenant_id, attachment_id, created_by_for(principal))


router.include_router(_attachment_routes)


__all__ = ["AgUiRouteSettings", "get_agui_settings", "router"]
