"""AG-UI workflow endpoints (AGUI.8): an AG-UI client runs a published workflow over SSE.

Plan: docs/plans/2026-11-13-milestone-ag-ui.md (D2, D4, D7, D9, D11, D12, D15 and "### AGUI.8").

- `POST /api/v1/ag-ui/workflows/{workflow_id}`: a `RunAgentInput` body, an AG-UI SSE answer.
- `GET  /api/v1/ag-ui/workflows/{workflow_id}/threads/{threadId}`: the thread's run, its AG-UI
  runs, its workflow state (D11) and the messages of its agent steps.
- `GET  /api/v1/ag-ui/workflows/{workflow_id}/threads/{threadId}/runs/{runId}/stream`: attach,
  from the start or after `Last-Event-ID: {runId}:{seq}` (or `?after=<seq>`).

A thread is **one workflow run** plus its resume runs (D3): the run id is derived from (tenant,
workflow, threadId). The first AG-UI run of a thread starts it; every later AG-UI run of the
thread answers its interrupts. Frame ids are `{runId}:{seq}` with the workflow run's seq; child
run (agent step) frames carry none (D7).

The POST, in order, everything before the stream opens:
1. 404 `Workflow not found` (unknown, another tenant's, outside the key's allowlist); the body
   guard and `parse_run_agent_input_async` as for agents; a key's draft or model override is 403;
   then the workflow rules (422 `run_option_unsupported`): no `model`, `requestContext`,
   `resourceId` or `"draft"` version, no client `tools` and no tool results; the `resume[]`
   payload rules (422, `resume_entries`); a stream slot (429);
2. a known runId: the D7 table (409 another fingerprint, 400 a malformed `Last-Event-ID`); the
   thread's first AG-UI run re-sends the hand-off (a crash between insert and enqueue heals);
3. an unknown runId with `Last-Event-ID`: 404, nothing recorded;
4. a new thread: the input is `forwardedProps.pydanticos.input`, else the last user message as
   JSON (422 `workflow_input_invalid`), an object valid for the version's input schema (422, the
   compact errors of POST /workflows/{id}/runs), at most 32 KiB; `version` (int) pins a published
   version, else the latest (404/409); archived agent steps 409, unrunnable references 422; an
   archived workflow 404. Then the run, its AG-UI row (from_seq -1) and the hand-off (503 on a
   failed enqueue; the same request heals);
5. an existing thread with `resume[]`: every open interrupt must be answered (an approval raised
   after the stream's close point is named by Q5, below), and every entry must name an interrupt of this run or of its agent
   steps' child runs (422); a new workflow input beside it is 422 `resume_with_input` (user
   messages are ignored). The decisions go through `eap_api.approvals.decide` (an earlier
   decision wins and is shown), then the AG-UI run is recorded on the workflow run with its
   answered interrupt ids as `input_message_ids` and `from_seq` = the last workflow event at or
   before the earliest answered interrupt's raise point, and streamed from there.

6. an existing thread without `resume[]`: new `forwardedProps.pydanticos.input` is 409
   (`workflow_thread_running` | `workflow_thread_finished`, loc `forwardedProps.pydanticos.input`:
   one run per thread, D3; the detail says to use a new threadId or attach). Without new input:
   with open interrupts, ALL of them again in the order requested (Q5), nothing recorded; else
   the same 409 codes at loc `threadId`, the detail saying what to send instead.

The user messages of the thread's first AG-UI run (the tail of its `messages`, whatever carried
the workflow input) are stored on its starter row (`ag_ui_runs.input_messages`) and prefix every
MESSAGES_SNAPSHOT of the thread and its thread GET (`history_of`), once each: a client that merges
snapshots by id (`@ag-ui/client` drops held messages a snapshot lacks) keeps them.

Streams are exact slices of the root's commit order (`root_order`, 0016_ag_ui: every append takes
the root run's row lock, so commit order is root_order and readers see a gapless prefix) over the
workflow run and its agent steps' child runs (`streams.workflow`). A run's start and end are
positions recorded with its AG-UI row (`from_order`, `end_order`; `run_bounds`), so live, attach
and continuation send the same frames under the same ids (D7). That holds for every event written
since 0016; older events (test data only) are ordered by (ts, run_id, seq) without it.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.deps import get_ag_ui_store, get_approval_store, get_database_engine, get_delta_source, get_stream_limiter, require_run_storage
from eap_api.ag_ui.input import (
    AGUI_NS,
    WORKFLOW_INPUT_INVALID,
    AgUiInput,
    AgUiInputError,
    check_principal_options,
    check_resume_covers,
    fingerprint,
    parse_run_agent_input_async,
    thread_key,
    workflow_input,
)
from eap_api.ag_ui.resume_entries import (
    RESUME_WITH_INPUT,
    ResumeDecision,
    ThreadInterrupts,
    apply_decisions,
    open_approvals,
    resume_decisions,
    thread_interrupts,
)
from eap_api.ag_ui.translate import AgUiFormatter, AgUiStreamContext, ChildPrior, OpenApproval, step_interrupt_id, tool_interrupt_id
from eap_api.agent_archive import archived_step_problems
from eap_api.auth import AgUiPrincipal, AgUiPrincipalDep, require_agui_workflow
from eap_api.deps import get_run_launcher
from eap_api.launcher import RunLauncher
from eap_api.routes.ag_ui import (
    FINGERPRINT_CONFLICT_DETAIL,
    UNKNOWN_RUN_DETAIL,
    AgUiRoute,
    AgUiRouteSettings,
    _acquire,
    _check_params,
    _checked_json_body,
    _Ctx,
    _path_id,
    _plan_or_raise,
    _response,
    _StreamDeps,
    get_agui_settings,
    static_stream,
)
from eap_api.routes.workflow_runs import MAX_WORKFLOW_INPUT_BYTES, input_schema_errors, non_finite_errors
from eap_api.routes.workflows import reference_problems
from eap_api.schemas import ErrorDetail
from eap_api.sse import (
    DeltaSource,
    StreamLimiter,
    StreamSettings,
    TERMINAL_STATUSES,
    get_stream_settings,
)
from eap_api.streams.formatter import format_stream
from eap_api.streams.native import ChildScope
from eap_api.streams.resume import LastEventIdError, StreamPlan, parse_last_event_id, plan_stream
from eap_api.streams.native import TERMINAL_EVENT_TYPES
from eap_api.streams.workflow import Merged, WorkflowItemStream, agent_step_child, merged_events
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability import current_trace_context, log_failure
from eap_run_store import (
    AgUiRunModel,
    AgUiRunRecord,
    AgUiScopeMismatch,
    RunIdConflict,
)
from eap_api.auth import get_api_key_store
from eap_schemas import RunEvent
from eap_schemas.workflows import WorkflowSpec

log = logging.getLogger("eap_api")

WORKFLOW_NOT_FOUND = "Workflow not found"
RUN_OPTION_UNSUPPORTED = "run_option_unsupported"
WORKFLOW_THREAD_RUNNING = "workflow_thread_running"
WORKFLOW_THREAD_FINISHED = "workflow_thread_finished"
RUNNING_DETAIL = (
    "This thread's workflow run is still running and waits for no approval: attach to it with GET "
    ".../threads/{threadId}/runs/{activeRunId}/stream (activeRunId from GET .../threads/{threadId}); "
    "if that AG-UI run ended at an interrupt decided elsewhere (inbox, REST), follow the run with a new "
    "runId whose resume[] names that interrupt (the recorded decision stands); or send "
    "forwardedProps.pydanticos.input under a new threadId to start another run"
)
NEW_INPUT_DETAIL = (
    "This thread already has its workflow run (a thread is one run): send the input under a new threadId to "
    "start another run, or attach to this thread's active run with GET .../threads/{threadId}/runs/{activeRunId}/stream"
)
FINISHED_DETAIL = (
    "This thread's workflow run has ended: send forwardedProps.pydanticos.input under a new threadId to "
    "start another run, or GET .../threads/{threadId} to read its outcome"
)

router = APIRouter(
    prefix="/ag-ui",
    tags=["ag-ui"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
    route_class=AgUiRoute,
)


# --------------------------------------------------------------------------- context


@dataclass
class _WCtx(_Ctx):
    """`_Ctx` of a workflow: `agent_id` holds the workflow id; the AG-UI rows have scope workflow."""

    @property
    def workflow_id(self) -> str:
        return self.agent_id

    def key(self, thread_id: str, run_id: str) -> dict[str, str]:
        return {
            "scope_kind": "workflow",
            "scope_id": self.agent_id,
            "external_thread_id": thread_id,
            "external_run_id": run_id,
        }


def _wctx(request: Request, principal: AgUiPrincipal, workflow_id: str, launcher: RunLauncher | None = None) -> _WCtx:
    run_store, event_log = require_run_storage(request)
    return _WCtx(
        engine=get_database_engine(request),
        run_store=run_store,
        event_log=event_log,
        ag_ui=get_ag_ui_store(request),
        approvals=get_approval_store(request),
        api_keys=get_api_key_store(request),
        principal=principal,
        agent_id=workflow_id,
        launcher=launcher,
    )


def _workflow_id_or_404(principal: AgUiPrincipal, value: str) -> str:
    try:
        workflow_id = str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKFLOW_NOT_FOUND)
    require_agui_workflow(principal, workflow_id)
    return workflow_id


def derive_workflow_run_id(tenant_id: str, workflow_id: str, thread_id: str) -> str:
    """The workflow run of an AG-UI thread (D3: one run per thread), tenant- and workflow-scoped."""
    name = json.dumps(["workflow_run", tenant_id, str(workflow_id), thread_id], separators=(",", ":"), ensure_ascii=False)
    return str(uuid.uuid5(AGUI_NS, name))


def _server_conflict(code: str, loc: list[str | int], msg: str) -> HTTPException:
    """A 409 whose guidance the server wrote: the compact detail of AgUiInputError, but never cut
    at its MAX_MSG_CHARS (that cap guards against echoing client input; this text is ours)."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=[{"loc": ["body", *loc], "msg": msg, "type": code}]
    )


def _unsupported(loc: list[str | int], msg: str) -> AgUiInputError:
    return AgUiInputError(status.HTTP_422_UNPROCESSABLE_CONTENT, RUN_OPTION_UNSUPPORTED, loc, msg)


def check_workflow_options(mapped: AgUiInput) -> None:
    """What a workflow run cannot take (422 run_option_unsupported)."""
    options = mapped.options
    pydanticos = ["forwardedProps", "pydanticos"]
    if options.version == "draft":
        raise _unsupported([*pydanticos, "version"], "a workflow runs a published version; there is no draft")
    if options.model is not None:
        raise _unsupported([*pydanticos, "model"], "a workflow run cannot override the model")
    if options.request_context:
        raise _unsupported([*pydanticos, "requestContext"], "a workflow run takes no request context")
    if options.resource_id is not None:
        raise _unsupported([*pydanticos, "resourceId"], "a workflow run takes no resource id")
    if mapped.client_tools:
        raise _unsupported(["tools"], "a workflow run takes no client tools")
    if mapped.tool_results:
        raise _unsupported(["messages"], "a workflow run takes no tool results")


# --------------------------------------------------------------------------- reads


def _load_workflow(c: _WCtx) -> WorkflowModel:
    with Session(c.engine) as db:
        workflow = db.execute(
            select(WorkflowModel).where(WorkflowModel.id == c.workflow_id, WorkflowModel.tenant_id == c.tenant_id)
        ).scalar_one_or_none()
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKFLOW_NOT_FOUND)
        db.expunge(workflow)
        return workflow


def _version_row(db: Session, workflow: WorkflowModel, version: int | None) -> WorkflowVersionModel:
    query = select(WorkflowVersionModel).where(
        WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == workflow.tenant_id
    )
    if version is None:
        latest = db.execute(
            select(func.max(WorkflowVersionModel.version)).where(
                WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == workflow.tenant_id
            )
        ).scalar_one_or_none()
        if latest is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow has no published version")
        version = latest
    row = db.execute(query.where(WorkflowVersionModel.version == version)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return row


def _thread_rows(c: _WCtx, thread_id: str) -> list[AgUiRunModel]:
    """The thread's AG-UI runs, in order (the first one started the run)."""
    with Session(c.engine) as session:
        rows = session.execute(
            select(AgUiRunModel)
            .where(
                AgUiRunModel.tenant_id == c.tenant_id,
                AgUiRunModel.scope_kind == "workflow",
                AgUiRunModel.scope_id == c.workflow_id,
                AgUiRunModel.external_thread_id == thread_id,
            )
            .order_by(AgUiRunModel.from_seq, AgUiRunModel.created_at, AgUiRunModel.external_run_id)
        ).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _as_user_messages(stored: Any) -> list[dict[str, Any]]:
    return [
        {"id": str(m["id"]), "role": "user", "content": str(m["content"])}
        for m in (stored or ())
        if isinstance(m, dict) and m.get("id")
    ]


def history_of(rows: list[AgUiRunModel]) -> list[dict[str, Any]]:
    """The user messages the thread's first AG-UI run carried (`ag_ui_runs.input_messages` of its
    starter row, from_seq -1), as AG-UI user messages: the prefix of every snapshot of the thread
    and of its thread GET, so a client's merge keeps them (AGUI.16 follow-up)."""
    starter = next((r for r in rows if r.from_seq < 0), None)
    return [] if starter is None else _as_user_messages(starter.input_messages)


def _thread_start(c: _WCtx, record: AgUiRunRecord) -> tuple[str, list[dict[str, Any]]]:
    """The AG-UI runId that started the thread's run (the D13 prefix of child-run messages) and
    the thread's input user messages (`history_of`). The starter record carries both, so a
    stream of the thread's first run reads no rows; a later run reads the thread's rows once."""
    if record.from_seq < 0:
        return record.external_run_id, _as_user_messages(record.input_messages)
    rows = _thread_rows(c, record.external_thread_id)
    return (rows[0].external_run_id if rows else record.external_run_id), history_of(rows)


def _run_info(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(run["id"]),
        "workflow_id": run.get("workflow_id"),
        "version": run.get("workflow_version"),
        "trace_id": run.get("trace_id"),
    }


def _events(c: _WCtx, run_id: str) -> list[RunEvent]:
    return c.event_log.list_events(c.tenant_id, uuid.UUID(run_id))


def _thread_interrupts(c: _WCtx, run_id: str) -> ThreadInterrupts:
    return thread_interrupts(c.approvals.list_for_run(c.tenant_id, run_id))


# --------------------------------------------------------------------------- a stream to serve


@dataclass
class _WPrepared:
    plan: StreamPlan
    run: dict[str, Any]
    record: AgUiRunRecord
    prior: list[RunEvent]
    prefix: str
    child_priors: list[ChildPrior] = field(default_factory=list)
    static_frames: list[str] | None = None
    # A new runId re-POSTed with `{runId}:-1` (D7): the client holds only RUN_STARTED, so the
    # stream opens as a continuation (no RUN_STARTED).
    opens_as_continuation: bool = False
    after: int | None = None  # the position (root_order) the stream's durable items start after
    scopes: dict[str, ChildScope] = field(default_factory=dict)  # agent steps started by then
    stop_after: int | None = None  # this AG-UI run's end, once it has ended
    answered: frozenset[str] = frozenset()
    # The thread's input user messages (`history_of`), the prefix of every snapshot.
    history: list[dict[str, Any]] = field(default_factory=list)


SEED_ALL = 2**62  # own_after for a child run seeded only for snapshots (nothing in it ends the run)


def _agent_steps(events: list[RunEvent], upto: int) -> list[tuple[ChildScope, int, bool]]:
    """Agent steps started at or before `upto`: (child scope, started seq, ended by `upto`)."""
    started: dict[str, tuple[ChildScope, int, bool]] = {}
    for e in events:
        if e.seq > upto:
            break
        child = agent_step_child(e)
        if child is not None:
            started[child.step_id] = (child, e.seq, False)
        elif e.type in ("step_completed", "step_failed"):
            step_id = str((e.data or {}).get("step_id") or "")
            if step_id in started:
                scope, seq, _ = started[step_id]
                started[step_id] = (scope, seq, True)
    return list(started.values())


# --------------------------------------------------------------------------- the commit order (D7)


@dataclass
class _Timeline:
    """The workflow run and its agent steps' child runs in the root's commit order (`root_order`,
    streams.workflow). Positions are stable: a reader only ever sees a gapless prefix."""

    run_id: str
    merged: list[Merged]

    @property
    def events(self) -> list[RunEvent]:
        return [m.event for m in self.merged if m.scope is None]

    def parent_keyed(self) -> list[tuple[int, RunEvent]]:
        return [(m.key, m.event) for m in self.merged if m.scope is None]

    def child_keyed(self, child_run_id: str) -> list[tuple[int, RunEvent]]:
        return [(m.key, m.event) for m in self.merged if m.scope is not None and m.scope.run_id == child_run_id]

    @property
    def last(self) -> int | None:
        return self.merged[-1].key if self.merged else None


def _timeline(c: _WCtx, run_id: str) -> _Timeline:
    return _Timeline(run_id, merged_events(run_id, c.event_log.list_root_events(c.tenant_id, run_id)))


def segment_end(tl: _Timeline, start: int | None, answered: frozenset[str] | set[str]) -> Merged | None:
    """Where an AG-UI run that starts after `start` ends, by the formatter's own rule: the first
    approval step wait, or completed gated batch of an agent step's child run, that its resume did
    not answer, or the workflow's end event. None: it has not ended yet."""
    batches: dict[str, tuple[set[str], list[str] | None]] = {}
    for m in tl.merged:
        e, after = m.event, start is None or m.key > start
        data = e.data or {}
        if m.scope is None:
            if e.type in TERMINAL_EVENT_TYPES and after:
                return m
            if e.type == "step_waiting" and after and step_interrupt_id(tl.run_id, str(data.get("step_id"))) not in answered:
                return m
            continue
        if e.type != "tool_approval_waiting":
            continue
        call = str(data.get("tool_call_id") or "")
        if not call:
            continue
        waiting, batch = batches.get(m.scope.run_id, (set(), None))
        waiting.add(call)
        if batch is None:
            raw = data.get("batch")
            batch = [str(b) for b in raw] if isinstance(raw, list) and raw else [call]
        if all(b in waiting for b in batch):
            batches[m.scope.run_id] = (set(), None)
            ids = [tool_interrupt_id(m.scope.run_id, b) for b in batch]
            if after and not all(i in answered for i in ids):
                return m
        else:
            batches[m.scope.run_id] = (waiting, batch)
    return None


def _answered(row: Any) -> frozenset[str]:
    return frozenset(str(i) for i in (row.input_message_ids or ())) if row.from_seq >= 0 else frozenset()


def raise_keys(tl: _Timeline) -> dict[str, int]:
    """Where each interrupt of the thread was raised in the commit order (root_order): an approval step's
    `step_waiting`; a gated call's batch-completing `tool_approval_waiting` (a whole batch is one
    interrupt outcome). The latest raise wins (never expected twice)."""
    out: dict[str, int] = {}
    batches: dict[str, tuple[set[str], list[str] | None]] = {}
    for m in tl.merged:
        data = m.event.data or {}
        if m.scope is None:
            if m.event.type == "step_waiting":
                out[step_interrupt_id(tl.run_id, str(data.get("step_id")))] = m.key
            continue
        if m.event.type != "tool_approval_waiting" or not data.get("tool_call_id"):
            continue
        call = str(data["tool_call_id"])
        waiting, batch = batches.get(m.scope.run_id, (set(), None))
        waiting.add(call)
        if batch is None:
            raw = data.get("batch")
            batch = [str(b) for b in raw] if isinstance(raw, list) and raw else [call]
        if all(b in waiting for b in batch):
            batches[m.scope.run_id] = (set(), None)
            for b in batch:
                out[tool_interrupt_id(m.scope.run_id, b)] = m.key
        else:
            batches[m.scope.run_id] = (waiting, batch)
    return out


def run_start(tl: _Timeline, answered: frozenset[str]) -> int | None:
    """Where a resume run starts in the commit order: where the earliest interrupt it answers was
    raised, which is where the AG-UI run that raised it closed (that run closes at the first
    interrupt it meets, even when Q5 or its final frame named later ones too). An answered
    interrupt raised later does not close it (`answered_interrupts`), so its frames are this run's.
    Computed once, when the resume run is recorded (`from_order`)."""
    keys = raise_keys(tl)
    found = [keys[i] for i in answered if i in keys]
    return min(found) if found else tl.last


def run_bounds(c: _WCtx, tl: _Timeline, record: AgUiRunRecord) -> tuple[int | None, int | None]:
    """(start, end) of an AG-UI run in the commit order, from what is recorded with it: the start
    (`from_order`; None for the thread's first run: the beginning) and, once known, the end
    (`end_order`). An end not recorded yet is the formatter's own close (`segment_end`) over the
    committed prefix; once it exists it can never move (no event is ever added before a seen
    one), and it is recorded the first time it is found. None while the run has not ended."""
    start = record.from_order if record.from_seq >= 0 else None
    if record.from_seq >= 0 and start is None:  # a row recorded before from_order (never expected)
        start = run_start(tl, _answered(record))
    if record.end_order is not None:
        return start, record.end_order
    end = segment_end(tl, start, _answered(record))
    if end is None:
        return start, None
    try:
        recorded = c.ag_ui.set_end_order(
            c.tenant_id, scope_kind="workflow", scope_id=record.scope_id,
            external_thread_id=record.external_thread_id, external_run_id=record.external_run_id,
            end_order=end.key,
        )
    except NoResultFound:  # an unrecorded run (the Q5 re-send) has no row
        recorded = end.key
    return start, recorded


def _parent_seq_at(tl: _Timeline, key: int | None) -> int:
    """The last workflow event at or before position `key` (-1: none)."""
    if key is None:
        return -1
    seqs = [e.seq for k, e in tl.parent_keyed() if k <= key]
    return seqs[-1] if seqs else -1


def _prepared(c: _WCtx, run: dict[str, Any], record: AgUiRunRecord, plan: StreamPlan,
              tl: _Timeline | None = None) -> _WPrepared:
    """The stream of one AG-UI run as the exact slice of the commit order between its start and
    its end (D7, `run_bounds`), from where the client stands:
    - a fresh run or an attach seeds everything up to its start (STATE_SNAPSHOT,
      MESSAGES_SNAPSHOT: the earlier AG-UI runs sent it) and replays the rest;
    - a continuation after frame N seeds up to the later of the start and N (the frames up to N,
      child frames between them included, are the client's) and replays the rest;
    - nothing past its end is ever sent (`stop_after`); a live run ends where the formatter closes
      it, by the same rule. Interrupts its resume answered do not end it (`answered`)."""
    tl = tl if tl is not None else _timeline(c, record.run_id)
    start, stop = run_bounds(c, tl, record)
    cutoff = start
    if plan.mode == "continue" and plan.after_seq >= 0:
        at_n = dict((e.seq, k) for k, e in tl.parent_keyed()).get(plan.after_seq)
        if at_n is not None and (cutoff is None or at_n > cutoff):
            cutoff = at_n
    parent_after = _parent_seq_at(tl, cutoff)
    priors: list[ChildPrior] = []
    scopes: dict[str, ChildScope] = {}
    for scope, _, _ in _agent_steps(tl.events, parent_after):
        keyed_child = tl.child_keyed(scope.run_id)
        seeded = [e for k, e in keyed_child if cutoff is not None and k <= cutoff]
        earlier = [e.seq for k, e in keyed_child if start is not None and k <= start]
        priors.append(ChildPrior(scope, seeded, earlier[-1] if earlier else -1))
        scopes[scope.run_id] = scope
    prefix, history = _thread_start(c, record)
    return _WPrepared(
        plan=plan, run=run, record=record, prior=[e for e in tl.events if e.seq <= parent_after],
        prefix=prefix, child_priors=priors, after=cutoff, scopes=scopes,
        stop_after=stop, answered=_answered(record), history=history,
    )


def _prepare_known(c: _WCtx, record: AgUiRunRecord, *, same_fingerprint: bool, last_event_id: str | None,
                   relaunch: bool) -> _WPrepared:
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(record.run_id))
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    tl = _timeline(c, record.run_id)
    _, stop = run_bounds(c, tl, record)
    plan = _plan_or_raise(
        plan_stream(
            run_id=record.external_run_id, recorded=True, same_fingerprint=same_fingerprint, created_now=False,
            from_seq=record.from_seq, last_event_id=last_event_id,
            max_seq=tl.events[-1].seq if tl.events else None,
            # A cursor past the run's end names frames it never sent (D7): 400.
            end_seq=_parent_seq_at(tl, stop) if stop is not None else None,
        )
    )
    if relaunch and record.from_seq < 0:
        _launch(c, run, created=False)
        run = c.run_store.get_run(c.tenant_id, uuid.UUID(record.run_id)) or run
    return _prepared(c, run, record, plan, tl)


def _fresh(c: _WCtx, run: dict[str, Any], record: AgUiRunRecord, created: bool) -> _WPrepared:
    plan = _plan_or_raise(
        plan_stream(
            run_id=record.external_run_id, recorded=True, same_fingerprint=True, created_now=created,
            from_seq=record.from_seq, last_event_id=None, max_seq=None,
        )
    )
    return _prepared(c, run, record, plan)


def _launch(c: _WCtx, run: dict[str, Any], *, created: bool) -> None:
    """Hand the run to the worker (again: DBOS attaches a re-enqueue to the existing workflow).
    A failed enqueue marks a run this request owns failed and answers 503; the same POST heals."""
    assert c.launcher is not None
    run_id = uuid.UUID(str(run["id"]))
    with Session(c.engine) as db:
        version_id = db.execute(
            select(WorkflowVersionModel.id).where(
                WorkflowVersionModel.tenant_id == c.tenant_id,
                WorkflowVersionModel.workflow_id == str(run["workflow_id"]),
                WorkflowVersionModel.version == run["workflow_version"],
            )
        ).scalar_one_or_none()
    if version_id is None:  # the version is immutable; never expected
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    owns_handoff = created or c.run_store.requeue_after_enqueue_failure(c.tenant_id, run_id)
    try:
        c.launcher.launch_workflow(
            run_id=run_id, tenant_id=c.tenant_id, workflow_version_id=str(version_id),
            input_json=run["input"], traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        log_failure(log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                    run_id=str(run_id), tenant_id=c.tenant_id)
        if owns_handoff:
            try:
                c.run_store.mark_enqueue_failed(c.tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                log_failure(log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                            level=logging.ERROR, run_id=str(run_id), tenant_id=c.tenant_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Run recorded but could not be started; retry the same request (same runId)",
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _input_errors(value: Any, spec: WorkflowSpec) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return [{"loc": ["body", "forwardedProps", "pydanticos", "input"], "msg": "the workflow input must be an object",
                 "type": WORKFLOW_INPUT_INVALID}]
    return non_finite_errors(value) or input_schema_errors(spec.input_schema, value)


def _start(c: _WCtx, workflow: WorkflowModel, mapped: AgUiInput, fp: str, run_id: str) -> _WPrepared:
    """A new thread: validate the input and the version, record the run and its AG-UI row, hand off."""
    if workflow.archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKFLOW_NOT_FOUND)
    value = workflow_input(mapped)
    with Session(c.engine) as db:
        version_row = _version_row(db, workflow, mapped.options.version)  # type: ignore[arg-type]
        spec = WorkflowSpec.model_validate(version_row.spec)
        errors = _input_errors(value, spec)
        if errors:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
        input_json = _canonical(value)
        if len(input_json.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
            raise AgUiInputError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, WORKFLOW_INPUT_INVALID, ["forwardedProps", "pydanticos", "input"],
                f"the workflow input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON",
            )
        archived = archived_step_problems(db, c.tenant_id, spec.steps)
        if archived:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="; ".join(archived))
        problems = reference_problems(db, spec, c.tenant_id)
        if problems:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(problems))
        version = version_row.version
    trace_id, traceparent = current_trace_context()
    try:
        _, created = c.run_store.create_workflow_run(
            c.tenant_id, workflow.id, version, input_json, uuid.UUID(run_id), trace_id=trace_id, traceparent=traceparent
        )
    except RunIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId is already in use")
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(run_id))
    if run is None:  # pragma: no cover - deleted between insert and read
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    if not created and (run["kind"] != "workflow" or run["workflow_id"] != str(workflow.id) or run["input"] != input_json):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId is already in use")
    record, rec_created = _record(c, mapped, fp, run_id, -1, input_messages=_input_messages(mapped))
    _launch(c, run, created=created)
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(run_id)) or run
    if created:
        log.info("ag-ui workflow run %s recorded: tenant=%s workflow=%s", run_id, c.tenant_id, workflow.id)
    return _fresh(c, run, record, rec_created)


def _input_messages(mapped: AgUiInput) -> list[dict[str, str]] | None:
    """The tail's user messages as stored on a thread's starter row ([{id, content}]), or None.

    Recorded once, by the first writer (D4: a known runId never records again). Their text is in
    the fingerprint (`input`), their ids are not (the fingerprint never covers ids), so a re-POST
    of the same runId whose messages have the same joined text under other ids (or split across
    messages differently) is the same request: it attaches, and the snapshots keep the STORED ids
    and messages. The thread then shows exactly one copy of what the run received; the client's
    merge replaces its own copy with the server's, as for every other D13 id. Agent turns keep
    their first `input_message_ids` the same way."""
    return [{"id": i, "content": t} for i, t in mapped.input_messages] or None


def _record(
    c: _WCtx, mapped: AgUiInput, fp: str, run_id: str, from_seq: int, answered: tuple[str, ...] = (),
    from_order: int | None = None, input_messages: list[dict[str, str]] | None = None,
) -> tuple[AgUiRunRecord, bool]:
    try:
        record, created = c.ag_ui.record_ag_ui_run(
            c.tenant_id, **c.key(mapped.thread_id, mapped.run_id), run_id=run_id, fingerprint=fp, from_seq=from_seq,
            input_message_ids=answered, from_order=from_order, input_messages=input_messages,
        )
    except AgUiScopeMismatch:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId belongs to another workflow")
    if not created and record.fingerprint != fp:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=FINGERPRINT_CONFLICT_DETAIL)
    return record, created


def _resume(c: _WCtx, run: dict[str, Any], mapped: AgUiInput, fp: str, decisions: list[ResumeDecision]) -> _WPrepared:
    """D15 for a workflow thread: decide (an earlier decision wins), record the AG-UI run, stream.

    The new AG-UI run starts where the earliest interrupt it answers was raised (`run_start`): that
    is where the AG-UI run that raised it closed, even when the client answers several at once.
    Its `from_seq` is the last workflow event at or before that point (the earlier run's last frame
    id); the answered interrupt ids are stored as its `input_message_ids`, so an attach finds the
    same start, and they do not end it. A child-run interrupt is decided against the child
    (check_resume_covers limited the ids to this run's and its children's)."""
    run_id = str(run["id"])
    answered = frozenset(d.ref.interrupt_id for d in decisions)
    tl = _timeline(c, run_id)
    start = run_start(tl, answered)
    from_seq = _parent_seq_at(tl, start)
    apply_decisions(c.event_log, c.launcher, c.tenant_id, decisions, c.principal.default_resource_id)
    record, created = _record(
        c, mapped, fp, run_id, max(from_seq, 0), tuple(d.ref.interrupt_id for d in decisions), from_order=start
    )
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(run_id)) or run
    log.info("ag-ui workflow resume %s recorded on run %s: %d decision(s)", mapped.run_id, run_id, len(decisions))
    return _fresh(c, run, record, created)


def _seeded_formatter(c: _WCtx, run: dict[str, Any], thread_id: str, agui_run_id: str, prefix: str,
                      events: list[RunEvent], history: list[dict[str, Any]]) -> AgUiFormatter:
    """A formatter seeded with every event of the run and of all its agent steps' child runs."""
    children = [
        ChildPrior(scope, _events(c, scope.run_id), SEED_ALL)
        for scope, _, _ in _agent_steps(events, events[-1].seq if events else -1)
        if c.run_store.get_run(c.tenant_id, uuid.UUID(scope.run_id)) is not None
    ]
    return AgUiFormatter(
        AgUiStreamContext(
            thread_id=thread_id, run_id=agui_run_id, native_run_id=str(run["id"]), kind="workflow",
            from_seq=events[-1].seq if events else -1, run_info=_run_info(run), prior_events=events,
            message_prefix=prefix, prior_child_events=children, history=history,
        )
    )


def _reinterrupt(
    c: _WCtx, run: dict[str, Any], mapped: AgUiInput, fp: str, interrupts: ThreadInterrupts, *, continuation: bool = False
) -> _WPrepared:
    """Q5 (D15): ALL open interrupts (approval steps and agent steps' gated tool calls) again under
    this runId, in the order they were requested; nothing recorded."""
    run_id = str(run["id"])
    events = _events(c, run_id)
    rows = _thread_rows(c, mapped.thread_id)
    prefix = rows[0].external_run_id if rows else mapped.run_id
    formatter = _seeded_formatter(
        c, run, mapped.client_thread_id or mapped.thread_id, mapped.run_id, prefix, events, history_of(rows)
    )
    approvals = open_approvals(c.event_log, c.tenant_id, interrupts.open_rows)
    record = AgUiRunRecord(
        tenant_id=c.tenant_id, scope_kind="workflow", scope_id=c.workflow_id, external_thread_id=mapped.thread_id,
        external_run_id=mapped.run_id, run_id=run_id, from_seq=max(events[-1].seq if events else 0, 0),
        fingerprint=fp, input_message_ids=(), created_at=datetime.now(timezone.utc),
    )
    log.info("ag-ui workflow run %s re-sends %d open interrupt(s) of run %s; nothing recorded",
             mapped.run_id, len(approvals), run_id)
    return _WPrepared(
        plan=StreamPlan(status=200, mode="attach", after_seq=record.from_seq), run=run, record=record, prior=[],
        prefix=prefix, static_frames=formatter.reinterrupt_frames(approvals, run_started=not continuation),
    )


def _prepare_post(
    c: _WCtx, mapped: AgUiInput, last_event_id: str | None, decisions: list[ResumeDecision]
) -> _WPrepared:
    header = last_event_id.strip() if last_event_id else None
    fp = fingerprint(mapped)
    key = c.key(mapped.thread_id, mapped.run_id)
    workflow = _load_workflow(c)
    run_id = derive_workflow_run_id(c.tenant_id, c.workflow_id, mapped.thread_id)
    record = c.ag_ui.get_ag_ui_run(c.tenant_id, **key)
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(run_id))
    if record is None and run is not None and not header and not _thread_rows(c, mapped.thread_id):
        # The run exists without any AG-UI row (a crash between the two writes): this is the
        # thread's first runId again when its input is the run's.
        try:
            same = _canonical(workflow_input(mapped)) == run["input"]
        except (AgUiInputError, ValueError):
            same = False
        if not same:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="threadId is already in use")
        record, _ = _record(c, mapped, fp, run_id, -1, input_messages=_input_messages(mapped))
    if record is not None:
        return _prepare_known(c, record, same_fingerprint=record.fingerprint == fp, last_event_id=header, relaunch=True)
    continuation = False
    if header:
        # As on the agent path (D7): only `{runId}:-1` may name a runId not recorded yet, a client
        # that holds RUN_STARTED alone (a Q5 re-send, which records nothing, cut after its first
        # frame). It is served as the new request, opened as a continuation.
        try:
            continuation = parse_last_event_id(header, mapped.run_id, None) == -1
        except LastEventIdError:
            continuation = False
        if not continuation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    prepared = _prepare_new(c, workflow, run, run_id, mapped, fp, decisions, continuation=continuation)
    prepared.opens_as_continuation = continuation
    return prepared


def _prepare_new(
    c: _WCtx, workflow: Any, run: dict[str, Any] | None, run_id: str, mapped: AgUiInput, fp: str,
    decisions: list[ResumeDecision], *, continuation: bool,
) -> _WPrepared:
    """A runId not recorded yet: a new thread, a resume, or the Q5 re-send (module docstring 4-6)."""
    if run is None:
        check_resume_covers(mapped, [], [])  # a resume on a thread without a run names nothing
        return _start(c, workflow, mapped, fp, run_id)
    interrupts = _thread_interrupts(c, run_id)
    check_resume_covers(mapped, interrupts.open, interrupts.decided)
    if mapped.resume:
        if mapped.options.has_input:
            raise AgUiInputError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, RESUME_WITH_INPUT, ["forwardedProps", "pydanticos", "input"],
                "a run that answers interrupts takes no new workflow input",
            )
        return _resume(c, run, mapped, fp, decisions)
    finished = run["status"] in TERMINAL_STATUSES
    if mapped.options.has_input:
        # D3: a thread is one workflow run. New input never starts a second run here, and is never
        # dropped silently (not even while the run waits: Q5 is for clients sending no input).
        raise _server_conflict(
            WORKFLOW_THREAD_FINISHED if finished else WORKFLOW_THREAD_RUNNING,
            ["forwardedProps", "pydanticos", "input"],
            NEW_INPUT_DETAIL,
        )
    if interrupts.open:
        return _reinterrupt(c, run, mapped, fp, interrupts, continuation=continuation)
    raise _server_conflict(
        WORKFLOW_THREAD_FINISHED if finished else WORKFLOW_THREAD_RUNNING,
        ["threadId"],
        FINISHED_DETAIL if finished else RUNNING_DETAIL,
    )


# --------------------------------------------------------------------------- streaming


def _interrupt_ids(frame: str) -> list[str] | None:
    """The interrupt ids of a `RUN_FINISHED{outcome: interrupt}` frame, else None."""
    for line in frame.splitlines():
        if line.startswith("data: "):
            try:
                event = json.loads(line[6:])
            except ValueError:
                return None
            outcome = event.get("outcome") if isinstance(event, dict) and event.get("type") == "RUN_FINISHED" else None
            if isinstance(outcome, dict) and outcome.get("type") == "interrupt":
                return [str(i.get("id")) for i in outcome.get("interrupts") or [] if isinstance(i, dict)]
    return None


def _frame_id(frame: str) -> str | None:
    first = frame.split("\n", 1)[0]
    return first[4:] if first.startswith("id: ") else None


STEP_DECISION_TYPES = frozenset({"step_approved", "step_rejected", "step_timed_out"})
TOOL_DECISION_TYPES = frozenset({"tool_approved", "tool_denied", "tool_approval_timed_out"})


def open_at(tl: _Timeline, key: int) -> list[OpenApproval]:
    """The approvals open at position `key` in the commit order, from durable data only (so a live stream
    and any later replay agree): raised at or before it (`raise_keys`) and not decided by then.
    In the order raised (a batch's calls in batch order)."""
    raised = raise_keys(tl)
    decided: set[str] = set()
    waiting: dict[str, OpenApproval] = {}
    for m in tl.merged:
        if m.key > key:
            break
        data = m.event.data or {}
        if m.scope is None:
            step = str(data.get("step_id") or "")
            if m.event.type == "step_waiting":
                waiting[step_interrupt_id(tl.run_id, step)] = OpenApproval(tl.run_id, "step", step, dict(data))
            elif m.event.type in STEP_DECISION_TYPES:
                decided.add(step_interrupt_id(tl.run_id, step))
        else:
            call = str(data.get("tool_call_id") or "")
            if m.event.type == "tool_approval_waiting":
                waiting[tool_interrupt_id(m.scope.run_id, call)] = OpenApproval(m.scope.run_id, "tool", call, dict(data))
            elif m.event.type in TOOL_DECISION_TYPES:
                decided.add(tool_interrupt_id(m.scope.run_id, call))
    open_ids = [i for i in waiting if i in raised and raised[i] <= key and i not in decided]
    return [waiting[i] for i in sorted(open_ids, key=lambda i: raised[i])]


def _seeded_upto(c: _WCtx, tl: _Timeline, run: dict[str, Any], thread_id: str, agui_run_id: str, prefix: str,
                 key: int, history: list[dict[str, Any]]) -> AgUiFormatter:
    """A formatter seeded with everything at or before position `key`."""
    events = [e for k, e in tl.parent_keyed() if k <= key]
    children = [
        ChildPrior(scope, [e for k, e in tl.child_keyed(scope.run_id) if k <= key], SEED_ALL)
        for scope, _, _ in _agent_steps(events, events[-1].seq if events else -1)
    ]
    return AgUiFormatter(
        AgUiStreamContext(
            thread_id=thread_id, run_id=agui_run_id, native_run_id=str(run["id"]), kind="workflow",
            from_seq=events[-1].seq if events else -1, run_info=_run_info(run), prior_events=events,
            message_prefix=prefix, prior_child_events=children, history=history,
        )
    )


def _all_open_tail(d: _StreamDeps, p: _WPrepared, frame: str, shown: list[str]) -> list[str] | None:
    """The stream ends at an interrupt (the formatter closes at the first waiting it meets). When
    more approvals were open at that point (parallel branches), that RUN_FINISHED is replaced by one
    naming all of them (check_resume_covers asks for all), with a MESSAGES_SNAPSHOT naming every
    gated call. Both are computed from durable data up to the close point, so a replay sends the
    same frames as the live stream. None: keep it."""
    c = d.c
    tl = _timeline(c, p.record.run_id)
    _, end = run_bounds(c, tl, p.record)
    if end is None:
        return None
    approvals = open_at(tl, end)
    if {a.interrupt().id for a in approvals} <= set(shown):
        return None
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(p.record.run_id)) or p.run
    formatter = _seeded_upto(c, tl, run, d.thread_id, p.record.external_run_id, p.prefix, end, p.history)
    return formatter.interrupt_tail(approvals, set(shown), _frame_id(frame))


async def _with_all_open(d: _StreamDeps, p: _WPrepared, frames: AsyncIterator[str]) -> AsyncIterator[str]:
    async with aclosing(frames) as stream:
        async for frame in stream:
            shown = _interrupt_ids(frame)
            if shown is not None:
                tail = await run_in_threadpool(_all_open_tail, d, p, frame, shown)
                if tail is not None:
                    for extra in tail:
                        yield extra
                    return
            yield frame


def _frames(d: _StreamDeps, p: _WPrepared) -> AsyncIterator[str]:
    if p.static_frames is not None:
        return static_stream(p.static_frames)
    c = d.c
    tenant = c.tenant_id
    items = WorkflowItemStream(
        run_id=p.record.run_id,
        after=p.after,
        stop_after=p.stop_after,
        load_ordered=lambda after: c.event_log.list_root_events(tenant, p.record.run_id, after),
        load_run=lambda rid: c.run_store.get_run(tenant, uuid.UUID(rid)),
        delta_source=d.delta_source,
        settings=d.settings,
        scopes=p.scopes,
    )
    formatter = AgUiFormatter(
        AgUiStreamContext(
            thread_id=d.thread_id,
            run_id=p.record.external_run_id,
            native_run_id=p.record.run_id,
            kind="workflow",
            from_seq=p.record.from_seq,
            run_info=_run_info(p.run),
            prior_events=p.prior,
            snapshot_on_start=p.plan.mode == "attach",
            message_prefix=p.prefix,
            prior_child_events=p.child_priors,
            answered_interrupts=p.answered,
            history=p.history,
        )
    )
    resume_after = p.plan.after_seq if (p.plan.mode == "continue" or p.opens_as_continuation) else None
    return _with_all_open(d, p, format_stream(items, formatter, resume_after=resume_after))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- routes


@router.post(
    "/workflows/{workflow_id}",
    response_class=StreamingResponse,
    responses={
        400: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 409: {"model": ErrorDetail},
        413: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 429: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
)
async def run_workflow(
    workflow_id: str,
    request: Request,
    principal: AgUiPrincipalDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
    settings: Annotated[StreamSettings, Depends(get_stream_settings)],
    limiter: Annotated[StreamLimiter, Depends(get_stream_limiter)],
    delta_source: Annotated[DeltaSource, Depends(get_delta_source)],
    agui: Annotated[AgUiRouteSettings, Depends(get_agui_settings)],
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Run the workflow for one AG-UI run (`RunAgentInput`), answering with AG-UI SSE (D2, D12)."""
    wid = _workflow_id_or_404(principal, workflow_id)
    body = await _checked_json_body(request)
    mapped = await parse_run_agent_input_async(body)
    check_principal_options(mapped.options, is_api_key=principal.is_api_key)
    check_workflow_options(mapped)
    decisions = resume_decisions(mapped.resume)
    c = _wctx(request, principal, wid, launcher)
    lease = _acquire(limiter, principal)
    try:
        prepared = await run_in_threadpool(_prepare_post, c, mapped, last_event_id, decisions)
    except BaseException:
        lease.release()
        raise
    d = _StreamDeps(c=c, thread_id=mapped.client_thread_id or mapped.thread_id, settings=settings,
                    delta_source=delta_source, agui=agui)
    return _response(d, _frames(d, prepared), lease)


def _thread_json(c: _WCtx, thread_id: str) -> dict[str, Any]:
    _load_workflow(c)
    key = thread_key(thread_id)
    run_id = derive_workflow_run_id(c.tenant_id, c.workflow_id, key)
    run = c.run_store.get_run(c.tenant_id, uuid.UUID(run_id))
    rows = _thread_rows(c, key) if run is not None else []
    if run is None or not rows:
        return {"threadId": thread_id, "run_id": None, "status": None, "display_status": None, "runs": [],
                "activeRunId": None, "state": None, "messages": []}
    events = _events(c, run_id)
    max_seq = events[-1].seq if events else -1
    children = []
    for e in events:
        child = agent_step_child(e)
        if child is not None and c.run_store.get_run(c.tenant_id, uuid.UUID(child.run_id)) is not None:
            children.append(ChildPrior(child, _events(c, child.run_id), own_after=2**62))
    formatter = AgUiFormatter(
        AgUiStreamContext(
            thread_id=thread_id, run_id=rows[0].external_run_id, native_run_id=run_id, kind="workflow",
            from_seq=max_seq, prior_events=events, prior_child_events=children, history=history_of(rows),
        )
    )
    latest = max(rows, key=lambda r: (r.from_seq, r.created_at))
    return {
        "threadId": thread_id,
        "run_id": run_id,
        "status": run["status"],
        "display_status": run["display_status"],
        "runs": [{"runId": r.external_run_id, "from_seq": r.from_seq} for r in rows],
        "activeRunId": None if run["status"] in TERMINAL_STATUSES else latest.external_run_id,
        "state": formatter.state(),
        "messages": formatter.messages(),
    }


@router.get("/workflows/{workflow_id}/threads/{thread_id}")
async def get_workflow_thread(
    workflow_id: str, thread_id: str, request: Request, principal: AgUiPrincipalDep
) -> JSONResponse:
    """The thread's workflow run (its status, AG-UI runs, state and agent-step messages), and
    `activeRunId`: the AG-UI runId to attach to while the run is not finished (else null). An
    unknown thread is empty (200)."""
    wid = _workflow_id_or_404(principal, workflow_id)
    _check_params(request)
    thread_id = _path_id(thread_id, "Thread not found")
    c = _wctx(request, principal, wid)
    return JSONResponse(content=await run_in_threadpool(_thread_json, c, thread_id))


def _prepare_attach(c: _WCtx, thread_id: str, run_id: str, last_event_id: str | None) -> _WPrepared:
    _load_workflow(c)
    record = c.ag_ui.get_ag_ui_run(c.tenant_id, **c.key(thread_key(thread_id), run_id))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=UNKNOWN_RUN_DETAIL)
    return _prepare_known(c, record, same_fingerprint=True, last_event_id=last_event_id, relaunch=False)


@router.get(
    "/workflows/{workflow_id}/threads/{thread_id}/runs/{run_id}/stream",
    response_class=StreamingResponse,
    responses={400: {"model": ErrorDetail}, 429: {"model": ErrorDetail}},
)
async def attach_workflow_run(
    workflow_id: str,
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
    """Attach to a recorded AG-UI run of a workflow thread (D7); nothing is started or decided."""
    wid = _workflow_id_or_404(principal, workflow_id)
    _check_params(request)
    thread_id = _path_id(thread_id, UNKNOWN_RUN_DETAIL)
    run_id = _path_id(run_id, UNKNOWN_RUN_DETAIL)
    header = last_event_id if last_event_id and last_event_id.strip() else (
        f"{run_id}:{after}" if after is not None else None
    )
    c = _wctx(request, principal, wid)
    lease = _acquire(limiter, principal)
    try:
        prepared = await run_in_threadpool(_prepare_attach, c, thread_id, run_id, header)
    except BaseException:
        lease.release()
        raise
    d = _StreamDeps(c=c, thread_id=thread_id, settings=settings, delta_source=delta_source, agui=agui)
    return _response(d, _frames(d, prepared), lease)


__all__ = ["check_workflow_options", "derive_workflow_run_id", "router"]
