"""The AG-UI formatter: native items in, AG-UI 1.0 SSE frames out (D0, D7, D9, D13, D15).

`AgUiFormatter` is a pure `StreamFormatter`: it never reads the database or the clock. Everything
beyond the items comes in `AgUiStreamContext`:
- `thread_id`, `run_id`: the AG-UI ids (`run_id` is the frame-id prefix, `{runId}:{seq}`);
- `native_run_id`: the durable run (interrupt ids, `CUSTOM` payloads);
- `kind`: `agent` (a chat turn) or `workflow`;
- `from_seq`: this AG-UI run's own events start after it (-1 for an agent turn; the seq after
  `step_waiting` / the gated batch for a resumed run);
- `run_info`: the `CUSTOM pydanticos.run` value (`run_id`, `conversation_id`, `turn`, ...);
- `history`: messages before this run's events (the thread's earlier turns plus this run's user
  message), for `MESSAGES_SNAPSHOT`;
- `prior_events`: the durable events up to the resume point. Those with seq <= `from_seq` only
  seed state (a resumed workflow's steps); those after it are this run's events the client
  already holds (a continuation): they update state as if sent, so nothing is sent twice;
- `snapshot_on_start`: attach from start (`MESSAGES_SNAPSHOT` after `RUN_STARTED`; a workflow
  adds `STATE_SNAPSHOT`, and `MESSAGES_SNAPSHOT` when child runs were seeded or the thread has
  input user messages in `history`);
- `message_prefix` (AGUI.8): the prefix of D13 message ids when it is not `run_id`: a resumed
  AG-UI run continues the turn the first AG-UI run started, so its messages keep that run's ids;
- `answered_interrupts` (AGUI.8): the interrupts a workflow resume run answered; reaching their
  waiting events again does not end it;
- `prior_child_events` (AGUI.8): for a workflow, the events of agent steps' child runs started
  before the resume point, per child (`ChildPrior`), seeded silently; the opening then carries
  `MESSAGES_SNAPSHOT` with their messages. Events with seq <= `own_after` only seed state (an
  earlier AG-UI run sent them, or they are shown by the snapshot); later ones are this AG-UI
  run's own, already held by the client: they update state as if sent, and an interrupt among
  them ends the run (its `RUN_FINISHED` is re-sent, without a frame id: child events carry none).

Frames: every frame is one `data:` line with a standard AG-UI event. Of the frames one durable
event yields, only the last carries `id: {runId}:{seq}`; frames from deltas, status, heartbeats
and child-run events (a workflow agent step) carry none. The keep-alive is `: keep-alive`.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ag_ui.core import (
    PROTOCOL_VERSION,
    BaseEvent,
    CustomEvent,
    Interrupt,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedCancelledOutcome,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateDeltaEvent,
    StateSnapshotEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    TokenUsage,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

from eap_api.ag_ui.snapshot import (
    OUTPUT_TOOL_NAME,
    announces_calls,
    merge_transcripts,
    response_call_ids,
    Transcript,
    args_json,
    assistant_message_id,
    tool_content,
    tool_message_id,
)
from eap_api.streams.native import (
    ChildScope,
    Delta,
    DurableEvent,
    EndReason,
    Heartbeat,
    FeedbackItems,
    RunSummary,
    Status,
    StreamItem,
)
from eap_api.streams.resume import frame_id, with_frame_id
from eap_schemas import RunEvent
from eap_schemas.workflow_scope import instance_token, scope_from_data

KEEP_ALIVE = ": keep-alive\n\n"
CUSTOM_PREFIX = "pydanticos."
NEEDS_ATTENTION_MESSAGE = (
    "The run stopped with an outcome that needs an operator to check it; it was not completed."
)
RUN_FAILED_MESSAGE = "The run failed."
STREAM_INTERRUPTED_MESSAGE = "The stream was interrupted; the run continues. Reconnect to resume it."
MAX_STATE_OUTPUT_BYTES = 4096
MAX_BUFFERED_DELTAS = 1000

# Pydantic AI's interrupt payload schema (`_ResumePayload`), without `editedArgs` (D15).
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["approved"],
}

# Durable events with no AG-UI rendering (cost stays native; memory work follows the turn; evals
# and knowledge ingestion are not served over AG-UI). `memory_observe_trimmed` is post-turn memory
# work (RAG P1.C6); `extracted` belongs to kb_ingest runs (RAG P1.A3). `cancel_requested` is an
# API-authored request (D12); the terminal `run_failed` with `run_cancelled` is what reaches the
# client as `RUN_FINISHED{outcome: cancelled}` (D17).
SILENT_EVENT_TYPES = frozenset(
    {"embedding", "memory_updated", "memory_observe_failed", "memory_observe_trimmed", "post_turn_done",
     "eval_case_result", "ingest_progress", "extracted", "text_delta", "cancel_requested",
     # `score` is read by Studio's Scores tab from /runs/{id}/events, never from the AG-UI stream.
     "score",
     # Agents as tools: the child run's link (Studio reads it from /runs/{id}/events); the AG-UI client
     # sees the call and its result as an ordinary tool call.
     "agent_tool_started", "agent_tool_completed",
     # The Architect's context provenance (hashes only); Studio reads it from /runs/{id}/events.
     "system_context_loaded",
     # `message_feedback` run events are legacy and no longer written: feedback is per user, so the
     # replay sends the viewer's own rows (FeedbackItems), never the shared run event.
     "message_feedback"}
)
STEP_DECISIONS = {"step_approved": "approved", "step_rejected": "rejected", "step_timed_out": "timed_out"}
TOOL_DECISIONS = {"tool_approved": "approved", "tool_denied": "denied", "tool_approval_timed_out": "timed_out"}

_ENCODER = EventEncoder()


def encode(event: BaseEvent) -> str:
    return _ENCODER.encode(event)


def custom(name: str, value: Any) -> CustomEvent:
    return CustomEvent(name=f"{CUSTOM_PREFIX}{name}", value=value)


def _pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def step_interrupt_id(run_id: str, step_id: str) -> str:
    return f"approval:{run_id}:{step_id}"


def tool_interrupt_id(run_id: str, tool_call_id: str) -> str:
    return f"approval:{run_id}:tool:{tool_call_id}"


def tool_interrupt(native_run_id: str, call_id: str, data: Mapping[str, Any]) -> Interrupt:
    """The interrupt of a gated tool call (D15) from its `tool_approval_waiting` data."""
    name = str(data.get("tool_name") or "")
    fields: dict[str, Any] = {
        "id": tool_interrupt_id(native_run_id, call_id),
        "reason": "tool_call",
        "tool_call_id": call_id,
        "message": f"Approve {name}({args_json(data.get('args'))})?",
        "response_schema": json.loads(json.dumps(RESPONSE_SCHEMA)),
        "metadata": {"pydanticos": {"run_id": native_run_id, "tool_name": name}},
    }
    if isinstance(data.get("deadline"), str):
        fields["expires_at"] = data["deadline"]
    return Interrupt(**fields)


def step_interrupt(native_run_id: str, step_id: str, data: Mapping[str, Any]) -> Interrupt:
    """The interrupt of a workflow approval step (D15) from its `step_waiting` data."""
    prompt = data.get("prompt")
    fields: dict[str, Any] = {
        "id": step_interrupt_id(native_run_id, step_id),
        "reason": "confirmation",
        "message": prompt if isinstance(prompt, str) and prompt else f"Approve step {step_id}?",
        "response_schema": json.loads(json.dumps(RESPONSE_SCHEMA)),
    }
    if isinstance(data.get("deadline"), str):
        fields["expires_at"] = data["deadline"]
    return Interrupt(**fields)


@dataclass(frozen=True)
class OpenApproval:
    """An approval a run waits for: its run (a workflow run, an agent turn, or a workflow agent
    step's child run), kind, key and its waiting event's data."""

    run_id: str
    kind: Literal["tool", "step"]
    key: str
    data: Mapping[str, Any]

    def interrupt(self) -> Interrupt:
        if self.kind == "tool":
            return tool_interrupt(self.run_id, self.key, self.data)
        return step_interrupt(self.run_id, self.key, self.data)

    def required_value(self) -> dict[str, Any]:
        """`CUSTOM pydanticos.approval_required`."""
        value: dict[str, Any] = {"run_id": self.run_id, "kind": self.kind, "key": self.key}
        if self.kind == "tool":
            value["tool_name"] = str(self.data.get("tool_name") or "")
        if self.data.get("deadline") is not None:
            value["deadline"] = self.data.get("deadline")
        return value


def token_usage(rows: Sequence[Mapping[str, Any]]) -> list[TokenUsage]:
    """`RUN_FINISHED.usage`: one entry per answering model (first appearance order) from the run's
    `run_model_requests` rows. Only chat model requests (`request_key` `model:...`): embedding,
    moderation and judge rows are left out. A billed failed fallback attempt counts under its own
    model, because it has its own row."""
    grouped: dict[str, dict[str, int]] = {}
    for row in rows:
        if (row.get("operation") or "chat") != "chat":
            continue
        if not str(row.get("request_key") or "").startswith("model:"):
            continue
        model_id = str(row.get("model_id") or "")
        if not model_id:
            continue
        counts = grouped.setdefault(model_id, {})
        for src, dst in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cached_input_tokens"),
            ("cache_write_tokens", "cache_write_input_tokens"),
        ):
            value = row.get(src)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                counts[dst] = counts.get(dst, 0) + value
    out = []
    for model_id, counts in grouped.items():
        provider, sep, model = model_id.partition(":")
        if "input_tokens" in counts or "output_tokens" in counts:
            counts["total_tokens"] = counts.get("input_tokens", 0) + counts.get("output_tokens", 0)
        out.append(TokenUsage(provider=provider if sep else None, model=model if sep else model_id, **counts))
    return out


@dataclass(frozen=True)
class ChildPrior:
    """A workflow agent step's child run seeded into a stream (see AgUiStreamContext)."""

    scope: ChildScope
    events: Sequence[RunEvent] = ()
    own_after: int = -1


@dataclass(frozen=True)
class AgUiStreamContext:
    thread_id: str
    run_id: str
    native_run_id: str
    kind: Literal["agent", "workflow"] = "agent"
    from_seq: int = -1
    run_info: Mapping[str, Any] | None = None
    history: Sequence[Mapping[str, Any]] = ()
    prior_events: Sequence[RunEvent] = ()
    snapshot_on_start: bool = False
    message_prefix: str | None = None
    prior_child_events: Sequence[ChildPrior] = ()
    # Interrupt ids this AG-UI run's own `resume[]` answered (a workflow resume run): reaching one
    # of them again does not end the run (all of a batch's, for a gated batch).
    answered_interrupts: frozenset[str] = frozenset()

    def closes_on(self, interrupt_ids: Sequence[str]) -> bool:
        return not interrupt_ids or not all(i in self.answered_interrupts for i in interrupt_ids)

    @property
    def prefix(self) -> str:
        return self.message_prefix or self.run_id


@dataclass
class _Interrupt:
    interrupts: list[Interrupt]


@dataclass
class _Turn:
    """One agent turn's stream state (the run itself, or a workflow agent step's child run)."""

    prefix: str
    native_run_id: str
    # The whole stream's snapshot (history plus every turn's messages, in announced order) and the
    # shared message-position counter; a lone turn defaults to its own.
    snapshot: Callable[[], list[Mapping[str, Any]]] | None = None
    order: Callable[[], int] | None = None
    transcript: Transcript = field(init=False)
    open_n: int | None = None
    open_attempt: str | None = None
    streamed_parts: list[str] = field(default_factory=list)
    diverged: bool = False
    # Tool calls of the last finalized response that have no result yet: the next request's
    # deltas wait until they have one, so messages and calls are announced in native order.
    awaiting_results: set[str] = field(default_factory=set)
    finalized: set[int] = field(default_factory=set)
    buffered: list[Delta] = field(default_factory=list)
    sent_calls: set[str] = field(default_factory=set)
    emitted_messages: set[str] = field(default_factory=set)
    waiting: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    waiting_order: list[str] = field(default_factory=list)
    batch: list[str] | None = None

    def __post_init__(self) -> None:
        self.transcript = Transcript(self.prefix, self.order)

    @property
    def streamed(self) -> str:
        return "".join(self.streamed_parts)

    def _snapshot_messages(self) -> list[Mapping[str, Any]]:
        return self.snapshot() if self.snapshot is not None else self.transcript.messages()

    def _may_stream(self, n: int) -> bool:
        """Deltas of request n go out only once request n-1 is final and its tool calls have
        results (request 0 of a turn always may)."""
        if n == 0:
            return True
        return (n - 1) in self.finalized and not self.awaiting_results

    # ------------------------------------------------------------------ text

    def on_delta(self, delta: Delta) -> list[BaseEvent]:
        n = delta.model_request
        if n is None or n in self.finalized:
            return []
        if (self.open_n is not None and n != self.open_n) or (self.open_n is None and not self._may_stream(n)):
            if (self.open_n is None or n > self.open_n) and len(self.buffered) < MAX_BUFFERED_DELTAS:
                self.buffered.append(delta)
            return []
        text = delta.text
        message_id = assistant_message_id(self.prefix, n)
        if self.open_n is None:
            self.open_n, self.open_attempt, self.streamed_parts, self.diverged = n, delta.attempt, [], False
            self.emitted_messages.add(message_id)
            events: list[BaseEvent] = [TextMessageStartEvent(message_id=message_id, role="assistant")]
        elif delta.attempt != self.open_attempt or self.diverged:
            # A new attempt of request n: text cannot be retracted; stop forwarding, correct at the end.
            self.diverged = True
            return []
        else:
            events = []
        if text:
            self.streamed_parts.append(text)
            events.append(TextMessageContentEvent(message_id=message_id, delta=text))
        return events

    def close(self) -> list[BaseEvent]:
        if self.open_n is None:
            return []
        message_id = assistant_message_id(self.prefix, self.open_n)
        self.open_n, self.open_attempt, self.streamed_parts, self.diverged = None, None, [], False
        return [TextMessageEndEvent(message_id=message_id)]

    def _flush(self) -> list[BaseEvent]:
        """Deltas that waited for an earlier request (in arrival order)."""
        buffered, self.buffered = self.buffered, []
        events: list[BaseEvent] = []
        for delta in buffered:
            events += self.on_delta(delta)
        return events

    def _model_custom(self, data: Mapping[str, Any], n: int) -> list[BaseEvent]:
        fallback_index = data.get("fallback_index")
        failed = data.get("failed_attempts")
        if not (isinstance(fallback_index, int) and fallback_index > 0) and not failed:
            return []
        value: dict[str, Any] = {
            "model_request": n,
            "model_id": data.get("model_id"),
            "requested_model_id": data.get("requested_model_id"),
            "fallback_index": fallback_index if isinstance(fallback_index, int) else 0,
            "failed_attempts": [
                {k: a.get(k) for k in ("model_id", "outcome", "error_type")}
                for a in (failed or [])
                if isinstance(a, Mapping)
            ],
        }
        return [custom("model", value)]

    def on_model_message(self, data: Mapping[str, Any]) -> list[BaseEvent]:
        n = data.get("model_request")
        if not isinstance(n, int):
            return []
        self.finalized.add(n)
        self.awaiting_results = set(response_call_ids(data.get("message")))
        events: list[BaseEvent] = []
        message_id = assistant_message_id(self.prefix, n)
        correction = False
        if data.get("output_candidate") or data.get("blocked"):
            if self.open_n == n:
                correction = bool(self.streamed)
                events += self.close()
        else:
            text = data.get("text") if isinstance(data.get("text"), str) else ""
            has_calls = announces_calls(data.get("message"))
            model_custom = self._model_custom(data, n)
            if self.open_n == n:
                correction = self.diverged or self.streamed != text
                events += model_custom
                events += self.close()
            elif text:
                self.emitted_messages.add(message_id)
                events += model_custom
                events += [
                    TextMessageStartEvent(message_id=message_id, role="assistant"),
                    TextMessageContentEvent(message_id=message_id, delta=text),
                    TextMessageEndEvent(message_id=message_id),
                ]
            elif has_calls:
                # Like Pydantic AI's stream: a response with only tool calls still opens (and
                # closes) its assistant message, so the calls' parentMessageId names a message.
                self.emitted_messages.add(message_id)
                events += model_custom
                events += [
                    TextMessageStartEvent(message_id=message_id, role="assistant"),
                    TextMessageEndEvent(message_id=message_id),
                ]
            else:
                events += model_custom
        if correction:
            events.append(MessagesSnapshotEvent(messages=self._snapshot_messages()))
        return events + self._flush()

    # ------------------------------------------------------------------ tools

    def _call_events(self, tool_call_id: str, name: str, args: Any) -> list[BaseEvent]:
        if not tool_call_id or name == OUTPUT_TOOL_NAME or tool_call_id in self.sent_calls:
            return []
        self.sent_calls.add(tool_call_id)
        n = self.transcript.last_request
        parent = assistant_message_id(self.prefix, n) if n is not None else None
        if parent not in self.emitted_messages:
            parent = None
        return [
            ToolCallStartEvent(tool_call_id=tool_call_id, tool_call_name=name, parent_message_id=parent),
            ToolCallArgsEvent(tool_call_id=tool_call_id, delta=args_json(args)),
            ToolCallEndEvent(tool_call_id=tool_call_id),
        ]

    def on_tool_call(self, data: Mapping[str, Any]) -> list[BaseEvent]:
        return self._call_events(str(data.get("tool_call_id") or ""), str(data.get("tool_name") or ""), data.get("args"))

    def on_tool_result(self, data: Mapping[str, Any]) -> list[BaseEvent]:
        call_id = str(data.get("tool_call_id") or "")
        events: list[BaseEvent] = []
        if call_id and data.get("tool_name") != OUTPUT_TOOL_NAME and data.get("source") != "client":
            events.append(
                ToolCallResultEvent(
                    message_id=tool_message_id(self.prefix, call_id),
                    tool_call_id=call_id,
                    role="tool",
                    content=tool_content(data.get("content")),
                )
            )
        if call_id in self.awaiting_results:
            self.awaiting_results.discard(call_id)
            if not self.awaiting_results:
                events += self._flush()
        return events

    def on_approval_waiting(self, data: Mapping[str, Any]) -> tuple[list[BaseEvent], _Interrupt | None]:
        call_id = str(data.get("tool_call_id") or "")
        name = str(data.get("tool_name") or "")
        events = self._call_events(call_id, name, data.get("args"))
        value: dict[str, Any] = {"run_id": self.native_run_id, "kind": "tool", "key": call_id, "tool_name": name}
        if data.get("deadline") is not None:
            value["deadline"] = data.get("deadline")
        events.append(custom("approval_required", value))
        if call_id not in self.waiting:
            self.waiting[call_id] = data
            self.waiting_order.append(call_id)
        batch = data.get("batch")
        if self.batch is None:
            self.batch = [str(b) for b in batch] if isinstance(batch, list) and batch else [call_id]
        if not all(b in self.waiting for b in self.batch):
            return events, None
        interrupts = [self._tool_interrupt(b, self.waiting[b]) for b in self.batch]
        self.waiting, self.waiting_order, self.batch = {}, [], None
        return events, _Interrupt(interrupts)

    def _tool_interrupt(self, call_id: str, data: Mapping[str, Any]) -> Interrupt:
        return tool_interrupt(self.native_run_id, call_id, data)

    # ------------------------------------------------------------------ dispatch

    def on_event(self, event: RunEvent) -> tuple[list[BaseEvent], _Interrupt | None]:
        """Events for one durable event of this turn (lifecycle events are the caller's)."""
        self.transcript.add(event)
        data = event.data or {}
        kind = event.type
        if kind == "model_message":
            return self.on_model_message(data), None
        if kind == "tool_call":
            return self.on_tool_call(data), None
        if kind == "tool_result":
            return self.on_tool_result(data), None
        if kind == "processor_result":
            value = {
                "phase": data.get("phase"),
                "type": data.get("processor_type"),
                "action": data.get("action"),
                "matches": data.get("match_counts") or {},
            }
            return [custom("processor_result", value)], None
        if kind == "output_retry":
            errors = [e for e in (data.get("errors") or []) if isinstance(e, str)]
            return [custom("output_retry", {"model_request": data.get("model_request"), "errors": errors})], None
        if kind == "tool_approval_waiting":
            return self.on_approval_waiting(data)
        if kind in TOOL_DECISIONS:
            key = str(data.get("tool_call_id") or "")
            return [custom("approval_decided", {"kind": "tool", "key": key, "decision": TOOL_DECISIONS[kind]})], None
        return [], None

    def structured_text(self, data: Mapping[str, Any]) -> list[BaseEvent]:
        """The one assistant text message of a structured run (canonical JSON), at run_completed."""
        output = data.get("output")
        if "output_json" not in data or not isinstance(output, str) or not output:
            return []
        message_id = assistant_message_id(self.prefix, self.transcript.structured_request())
        if message_id in self.emitted_messages:
            return []
        self.emitted_messages.add(message_id)
        return [
            TextMessageStartEvent(message_id=message_id, role="assistant"),
            TextMessageContentEvent(message_id=message_id, delta=output),
            TextMessageEndEvent(message_id=message_id),
        ]


def _run_error(data: Mapping[str, Any]) -> RunErrorEvent:
    if data.get("status") == "needs_attention":
        return RunErrorEvent(message=NEEDS_ATTENTION_MESSAGE, code="needs_attention")
    message = data.get("error") if isinstance(data.get("error"), str) and data.get("error") else RUN_FAILED_MESSAGE
    code = data.get("error_code") if isinstance(data.get("error_code"), str) and data.get("error_code") else None
    return RunErrorEvent(message=message, code=code)


class AgUiFormatter:
    """See the module docstring. One instance per stream."""

    def __init__(self, ctx: AgUiStreamContext) -> None:
        self.ctx = ctx
        self.closed = False
        self._usage: list[TokenUsage] | None = None
        self._shown_status: str | None = None
        self._order = itertools.count().__next__
        self._turn: _Turn | None = (
            _Turn(ctx.prefix, ctx.native_run_id, self._all_messages, self._order) if ctx.kind == "agent" else None
        )
        self._child_turns: dict[str, _Turn] = {}
        # A body agent step's child run (D17): child_run_id -> instance token the AG-UI side sees
        # it under (`{S}~{step}`, e.g. `each.0~triage`). Top-level steps are not tracked — their
        # instance token equals the bare step id and `_child` falls back to it.
        self._child_instance: dict[str, str] = {}
        # The lifecycle event that already ended this AG-UI run inside `prior_events` (a
        # continuation at or after its interrupt or end event), and its seq: re-sent, then close.
        self._ended: tuple[BaseEvent, int | None] | None = None
        # Workflow state (D11) and the steps this AG-UI run has announced with STEP_STARTED. Step
        # names in `_open_steps`/`_announced` are **instance tokens**: bare step ids at the top
        # level, scoped (`L.i~step`) inside a loop body (D17). State paths (`/steps/{...}`) use
        # the top-level step id only — body instances emit no STATE_DELTA.
        self._state: dict[str, Any] = {"status": "running", "steps": {}}
        self._open_steps: list[str] = []
        self._announced: list[str] = []
        self._seeded = False
        self._seed()

    # ------------------------------------------------------------------ seeding

    def _seed(self) -> None:
        """Feed `prior_events` silently: state and sent-flags only, no frames. Steps open after
        them are the ones this AG-UI run has announced: start() announces them (a fresh run or
        an attach); a continuation's client already holds those announcements."""
        for event in self.ctx.prior_events:
            own = event.seq > self.ctx.from_seq
            if not own:
                self._seeded = True
            events = self._on_durable(DurableEvent(event))
            if self.closed and own:
                # This AG-UI run ended here (interrupt or end event); later events belong to
                # the next AG-UI run (D7: a finished run's stream re-sends its end and closes).
                self._ended = (events[-1], event.seq)
                break
            self.closed = False
        self.closed = False
        if self._ended is None:
            self._seed_children()
        self._announced = list(self._open_steps)

    def _seed_children(self) -> None:
        """Feed `prior_child_events` silently (a workflow's open agent steps, AGUI.8)."""
        for prior in self.ctx.prior_child_events:
            self._child(prior.scope)
            for event in prior.events:
                events = self._on_durable(DurableEvent(event, prior.scope))
                if self.closed and event.seq > prior.own_after:
                    self._ended = (events[-1], None)
                    self.closed = False
                    return
                self.closed = False

    # ------------------------------------------------------------------ helpers

    def _frames(self, events: list[BaseEvent], seq: int | None = None) -> list[str]:
        frames = [encode(e) for e in events]
        return with_frame_id(frames, frame_id(self.ctx.run_id, seq) if seq is not None else None)

    def _turns(self) -> list[_Turn]:
        return ([self._turn] if self._turn is not None else []) + list(self._child_turns.values())

    def _child(self, scope: ChildScope) -> _Turn:
        # D17: a body agent's child run keys on its distinct run id (iterations may overlap under
        # foreach concurrency), and its message/call ids carry the step **instance** token
        # (`each.0~triage`). Top-level steps keep `scope.step_id` as both key and token, so their
        # ids are unchanged (AG-UI D13: `{runId}-{stepId}-m{n}`).
        token = self._child_instance.get(scope.run_id, scope.step_id)
        turn = self._child_turns.get(scope.run_id)
        if turn is None or turn.native_run_id != scope.run_id:
            turn = self._child_turns[scope.run_id] = _Turn(
                f"{self.ctx.prefix}-{token}", scope.run_id, self._all_messages, self._order
            )
        return turn

    def _all_messages(self) -> list[Mapping[str, Any]]:
        """The client's whole message list: history, then every turn's messages in the order the
        stream announced them (a snapshot replaces the client's list, so nothing may be left out)."""
        return [*self.ctx.history, *merge_transcripts(t.transcript for t in self._turns())]

    def _close_open(self) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        for turn in self._turns():
            events += turn.close()
        for step_id in self._announced:
            events.append(StepFinishedEvent(step_name=step_id))
        self._announced = []
        return events

    def _run_started(self) -> RunStartedEvent:
        return RunStartedEvent(thread_id=self.ctx.thread_id, run_id=self.ctx.run_id, protocol_version=PROTOCOL_VERSION)

    def _run_custom(self) -> list[BaseEvent]:
        return [custom("run", dict(self.ctx.run_info))] if self.ctx.run_info is not None else []

    def _messages_snapshot(self) -> MessagesSnapshotEvent:
        return MessagesSnapshotEvent(messages=self._all_messages())

    def state(self) -> dict[str, Any]:
        """A copy of the workflow state (D11) as the events seen so far make it."""
        return json.loads(json.dumps(self._state))

    def messages(self) -> list[Mapping[str, Any]]:
        """The stream's message list as the events seen so far make it (MESSAGES_SNAPSHOT)."""
        return self._all_messages()

    def reinterrupt_frames(self, approvals: Sequence[OpenApproval], *, run_started: bool = True) -> list[str]:
        """Q5 (D15): a whole stream that shows the run as it stands (seeded with every event) and
        ends with ALL its open approvals as one interrupt outcome, in the given order: RUN_STARTED
        (unless the client holds it), CUSTOM run, STATE_SNAPSHOT (a workflow), MESSAGES_SNAPSHOT
        (which names every gated call), STEP_STARTED of the open steps, one CUSTOM
        approval_required each, then the open steps' STEP_FINISHED and RUN_FINISHED. No frame ids:
        nothing of it is recorded."""
        events: list[BaseEvent] = ([self._run_started()] if run_started else []) + self._run_custom()
        if self.ctx.kind == "workflow":
            events.append(StateSnapshotEvent(snapshot=self.state()))
        events.append(self._messages_snapshot())
        events += [StepStartedEvent(step_name=step_id) for step_id in self._announced]
        events += [custom("approval_required", a.required_value()) for a in approvals]
        events += self._interrupt(_Interrupt([a.interrupt() for a in approvals]))
        return [encode(e) for e in events]

    def interrupt_tail(self, approvals: Sequence[OpenApproval], shown: set[str], id_: str | None) -> list[str]:
        """Frames that replace a stream's final interrupt `RUN_FINISHED` when more approvals are
        open than it names (parallel branches): CUSTOM approval_required for the others,
        MESSAGES_SNAPSHOT (naming every gated call), then RUN_FINISHED with all of them, in the
        given order, carrying the replaced frame's id. This formatter must be seeded with every
        event of the run."""
        events: list[BaseEvent] = [
            custom("approval_required", a.required_value()) for a in approvals if a.interrupt().id not in shown
        ]
        events.append(self._messages_snapshot())
        events.append(self._finished(RunFinishedInterruptOutcome(interrupts=[a.interrupt() for a in approvals])))
        self.closed = True
        return with_frame_id([encode(e) for e in events], id_)

    def _resend_end(self) -> list[str]:
        """A stream of an AG-UI run that already ended: its end event again, then close."""
        if self._ended is None:
            return []
        event, seq = self._ended
        self.closed = True
        return self._frames([event], seq)

    def _finished(self, outcome: Any, result: Any = None) -> RunFinishedEvent:
        fields: dict[str, Any] = {"thread_id": self.ctx.thread_id, "run_id": self.ctx.run_id, "outcome": outcome}
        if result is not None:
            fields["result"] = result
        if self._usage:
            fields["usage"] = self._usage
        return RunFinishedEvent(**fields)

    def _interrupt(self, interrupt: _Interrupt) -> list[BaseEvent]:
        events = self._close_open()
        events.append(self._finished(RunFinishedInterruptOutcome(interrupts=interrupt.interrupts)))
        self.closed = True
        return events

    def _state_ops(self, ops: list[dict[str, Any]]) -> list[BaseEvent]:
        return [StateDeltaEvent(delta=ops)] if ops else []

    def _step_status(self, step_id: str, status: str, extra: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        steps = self._state["steps"]
        path = f"/steps/{_pointer(step_id)}"
        if step_id not in steps:
            steps[step_id] = {"status": status, **(extra or {})}
            return [{"op": "add", "path": path, "value": json.loads(json.dumps(steps[step_id]))}]
        steps[step_id]["status"] = status
        ops = [{"op": "replace", "path": f"{path}/status", "value": status}]
        for key, value in (extra or {}).items():
            op = "replace" if key in steps[step_id] else "add"
            steps[step_id][key] = value
            ops.append({"op": op, "path": f"{path}/{_pointer(key)}", "value": value})
        return ops

    def _bump(self, step_id: str, key: str) -> list[dict[str, Any]]:
        """Increment a loop step's iteration counter (D17). `add` if the step has never seen one,
        else `replace`; the loop's own `step_started` seeds `iterations_started|done|failed = 0`
        so `replace` is the common case."""
        steps = self._state["steps"]
        entry = steps.setdefault(step_id, {})
        exists = key in entry
        entry[key] = entry.get(key, 0) + 1
        return [{"op": "replace" if exists else "add", "path": f"/steps/{_pointer(step_id)}/{_pointer(key)}",
                 "value": entry[key]}]

    # ------------------------------------------------------------------ StreamFormatter

    def start(self) -> list[str]:
        events: list[BaseEvent] = [self._run_started(), *self._run_custom()]
        if self.ctx.kind == "agent" and self.ctx.snapshot_on_start:
            events.append(self._messages_snapshot())
        if self.ctx.kind == "workflow":
            if self._seeded or self.ctx.snapshot_on_start:
                events.append(StateSnapshotEvent(snapshot=json.loads(json.dumps(self._state))))
            if (self.ctx.snapshot_on_start and self.ctx.history) or (
                (self.ctx.snapshot_on_start or self.ctx.prior_child_events) and self._child_turns
            ):
                # Seeded child runs: the client's list is replaced by one with all their messages.
                # An attach from the start also sends the thread's input user messages (history).
                events.append(self._messages_snapshot())
            for step_id in self._announced:
                events.append(StepStartedEvent(step_name=step_id))
        return self._frames(events) + self._resend_end()

    def resume_frames(self, after_seq: int) -> list[str]:
        events: list[BaseEvent] = [*self._run_custom()]
        if self.ctx.kind == "agent":
            events.append(self._messages_snapshot())
        else:
            events.append(StateSnapshotEvent(snapshot=json.loads(json.dumps(self._state))))
            if self._child_turns:
                # Child-run frames carry no id: the client may hold part of an open step's text.
                events.append(self._messages_snapshot())
        return self._frames(events) + self._resend_end()

    def on_item(self, item: StreamItem) -> list[str]:
        if self.closed:
            return []
        if isinstance(item, Heartbeat):
            return [KEEP_ALIVE]
        if isinstance(item, RunSummary):
            rows = item.data.get("model_requests")
            if isinstance(rows, list):
                self._usage = token_usage([r for r in rows if isinstance(r, Mapping)])
            return []
        if isinstance(item, Status):
            if item.display_status == self._shown_status:
                return []
            self._shown_status = item.display_status
            return self._frames([custom("status", {"display_status": item.display_status})])
        if isinstance(item, Delta):
            turn = self._child(item.scope) if item.scope is not None else self._turn
            return self._frames(turn.on_delta(item)) if turn is not None else []
        if isinstance(item, FeedbackItems):
            # The viewer's own feedback precedes the terminal frame: no frame id.
            return self._frames([custom("message_feedback", dict(row)) for row in item.rows])
        if isinstance(item, DurableEvent):
            events = self._on_durable(item)
            return self._frames(events, item.seq if item.scope is None else None)
        return []

    def finish(self, reason: EndReason) -> list[str]:
        """The items ended without this run's end event: the lifetime expired, the run vanished,
        or the run is terminal without an end event. End the stream with `RUN_ERROR
        stream_interrupted` (resumable for our client, D7) so a plain client sees a closed run."""
        if self.closed:
            return []
        return self.fail() or []

    def fail(self) -> list[str] | None:
        if self.closed:
            return []
        events: list[BaseEvent] = []
        for turn in self._turns():
            events += turn.close()
        events.append(RunErrorEvent(message=STREAM_INTERRUPTED_MESSAGE, code="stream_interrupted"))
        self.closed = True
        return self._frames(events)

    # ------------------------------------------------------------------ durable events

    def _on_durable(self, item: DurableEvent) -> list[BaseEvent]:
        event = item.event
        if event.type in SILENT_EVENT_TYPES:
            return []
        if item.scope is not None:
            return self._on_child(item.scope, event)
        if self.ctx.kind == "agent":
            return self._on_agent(event)
        return self._on_workflow(event)

    def _on_agent(self, event: RunEvent) -> list[BaseEvent]:
        assert self._turn is not None
        data = event.data or {}
        if event.type == "run_completed":
            self._turn.transcript.add(event)
            events = self._turn.close() + self._turn.structured_text(data)
            pending = [str(i) for i in (data.get("pending_tool_call_ids") or [])]
            outcome = RunFinishedSuccessOutcome(pending_tool_call_ids=pending or None)
            result = data.get("output_json") if "output_json" in data else None
            events.append(self._finished(outcome, result))
            self.closed = True
            return events
        if event.type == "run_failed":
            # D17: a cancelled agent turn ends `RUN_FINISHED{outcome: cancelled}`, like a
            # workflow run. Top-level agent runs can't be cancelled directly today (P1), but a
            # cancelled workflow's child agent run that leaked back here would still render right.
            if data.get("error_code") == "run_cancelled":
                events = self._turn.close() + [self._finished(RunFinishedCancelledOutcome())]
            else:
                events = self._turn.close() + [_run_error(data)]
            self.closed = True
            return events
        events, interrupt = self._turn.on_event(event)
        if interrupt is not None:
            events += self._interrupt(interrupt)
        return events

    def _on_child(self, scope: ChildScope, event: RunEvent) -> list[BaseEvent]:
        turn = self._child(scope)
        if event.type == "run_completed":
            turn.transcript.add(event)
            return turn.close() + turn.structured_text(event.data or {})
        if event.type == "run_failed":
            return turn.close()
        events, interrupt = turn.on_event(event)
        if interrupt is not None and not self.ctx.closes_on([i.id for i in interrupt.interrupts]):
            return events
        if interrupt is not None:
            events += self._interrupt(interrupt)
        return events

    def _on_workflow(self, event: RunEvent) -> list[BaseEvent]:
        data = event.data or {}
        kind = event.type
        step_id = str(data.get("step_id") or "")
        # D17: a body step carries `scope` (the chain of enclosing iterations). Top-level steps
        # have no `scope` key, so the scope is empty and nothing below changes for them.
        scope = scope_from_data(data.get("scope"))
        in_body = bool(scope)
        if kind == "run_started":
            self._state = {"status": "running", "steps": {}}
            return [StateSnapshotEvent(snapshot={"status": "running", "steps": {}})]
        if kind == "step_started" and step_id:
            # Top-level steps keep bare ids byte-identical (pre-loops fixtures also use ids with
            # `/`, which `instance_token` rejects because loop ids are stricter; D4 says the empty
            # scope keeps every identifier exactly as today).
            token = instance_token(scope, step_id) if in_body else step_id
            step_type = data.get("step_type")
            child_run_id = data.get("child_run_id")
            # Remember the child's instance token so its downstream events (tool calls, model
            # messages) get `{runId}-{instance_token}-m{n}` ids. Top-level agent steps don't need
            # it — _child falls back to the bare step id.
            if in_body and step_type == "agent" and isinstance(child_run_id, str):
                self._child_instance[child_run_id] = token
            if token not in self._open_steps:
                self._open_steps.append(token)
            events: list[BaseEvent] = []
            if token not in self._announced:
                self._announced.append(token)
                events.append(StepStartedEvent(step_name=token))
            if in_body:
                # Body instances emit no STATE_DELTA (D17): with hundreds of iterations they would
                # overwrite one key or bloat the state. The loop step's own `/steps/{L}` is enough.
                return events
            extra: dict[str, Any] = {}
            if step_type:
                extra["type"] = step_type
            if step_type in ("foreach", "loop"):
                # Seed the loop step's iteration counters (D17) so iteration events can `replace`.
                extra.update(iterations_started=0, iterations_done=0, iterations_failed=0)
            ops = self._step_status(step_id, "running", extra or None)
            return events + self._state_ops(ops)
        if kind == "step_waiting" and step_id:
            events = self._state_ops(self._step_status(step_id, "waiting"))
            if not self.ctx.closes_on([step_interrupt_id(self.ctx.native_run_id, step_id)]):
                return events  # this run's resume answered it already
            value: dict[str, Any] = {"run_id": self.ctx.native_run_id, "kind": "step", "key": step_id}
            if data.get("deadline") is not None:
                value["deadline"] = data.get("deadline")
            events.append(custom("approval_required", value))
            return events + self._interrupt(_Interrupt([step_interrupt(self.ctx.native_run_id, step_id, data)]))
        if kind in STEP_DECISIONS and step_id:
            decision = STEP_DECISIONS[kind]
            events = self._state_ops(self._step_status(step_id, decision))
            events.append(custom("approval_decided", {"kind": "step", "key": step_id, "decision": decision}))
            return events
        if kind in ("step_completed", "step_failed") and step_id:
            token = instance_token(scope, step_id) if in_body else step_id
            events = []
            if token in self._open_steps:
                self._open_steps.remove(token)
            if token in self._announced:
                self._announced.remove(token)
                events.append(StepFinishedEvent(step_name=token))
            if in_body:
                return events  # body instances emit no STATE_DELTA (D17)
            if kind == "step_completed":
                output = data.get("output")
                encoded = json.dumps(output, separators=(",", ":"), ensure_ascii=False)
                extra = {"output": output} if len(encoded.encode()) <= MAX_STATE_OUTPUT_BYTES else {"output_truncated": True}
                return events + self._state_ops(self._step_status(step_id, "completed", extra))
            status = data.get("status") if isinstance(data.get("status"), str) else "failed"
            return events + self._state_ops(self._step_status(step_id, status))
        if kind in ("loop_iteration_started", "loop_iteration_completed", "loop_iteration_failed") and step_id:
            # D17: iteration frames carry the loop's bare step_id, the outer scope (enclosing
            # iterations, outermost first) and the index. The counter at the loop step's path is
            # updated only when the loop is at the top level.
            status_label = {
                "loop_iteration_started": "started",
                "loop_iteration_completed": "completed",
                "loop_iteration_failed": "failed",
            }[kind]
            index = data.get("index")
            value: dict[str, Any] = {"step_id": step_id, "scope": data.get("scope") or [], "index": index,
                                     "status": status_label}
            events: list[BaseEvent] = [custom("loop_iteration", value)]
            if in_body:
                # A nested loop's iterations are body instances of the outer loop: no state update.
                return events
            counter = {
                "started": "iterations_started",
                "completed": "iterations_done",
                "failed": "iterations_failed",
            }[status_label]
            return events + self._state_ops(self._bump(step_id, counter))
        if kind == "run_completed":
            ended = "skipped" if data.get("outcome") == "skipped" else "completed"  # a skip is a success finish
            self._state["status"] = ended
            events = self._state_ops([{"op": "replace", "path": "/status", "value": ended}])
            events += self._close_open()
            events.append(self._finished(RunFinishedSuccessOutcome(), data.get("result")))
            self.closed = True
            return events
        if kind == "run_failed":
            # D17: a cancelled run ends `RUN_FINISHED{outcome: cancelled}`, not RUN_ERROR; its
            # state status is still `failed` (the terminal row has `status: failed`, D12).
            cancelled = data.get("error_code") == "run_cancelled"
            status = "needs_attention" if data.get("status") == "needs_attention" else "failed"
            self._state["status"] = status
            events = self._state_ops([{"op": "replace", "path": "/status", "value": status}])
            events += self._close_open()
            if cancelled:
                events.append(self._finished(RunFinishedCancelledOutcome()))
            else:
                events.append(_run_error(data))
            self.closed = True
            return events
        if kind in ("tool_call", "tool_result", "tool_approval_waiting", *TOOL_DECISIONS):
            return []
        return []
