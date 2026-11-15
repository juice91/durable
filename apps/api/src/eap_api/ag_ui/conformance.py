"""AG-UI 1.0 stream conformance: SSE frame parsing, schema validation and a lifecycle checker.

Used by the AG-UI tests (AGUI.4 conformance, AGUI.6/8/14/15); pure, no I/O. The schema is the
vendored official 1.0 schema (`tests/fixtures/ag-ui/schema-1.0.json`), passed in by the caller.

Lifecycle rules checked (https://docs.ag-ui.com/spec/1.0/events/lifecycle):
- a stream begins with `RUN_STARTED` (with `protocolVersion`) or `RUN_ERROR`, except a
  continuation (our D7 resume), which begins inside an already started run;
- runs do not nest; after `RUN_FINISHED` / `RUN_ERROR` only a new `RUN_STARTED` may follow;
- text messages and tool calls are started once, receive content only while open, and are ended;
  a message id is never started twice nor after a snapshot holding it, and assistant messages of
  one turn (`{prefix}-m{n}`) start in request order;
  steps are started and finished by name;
- everything a run opened is closed before `RUN_FINISHED` (`RUN_ERROR` may cut a run short);
- an interrupt's `toolCallId` names a tool call the stream has shown;
- optional fields are omitted, never `null` (except `CUSTOM.value`, `RUN_FINISHED.result` is
  never null);
- SSE ids, where present, are `{runId}:{seq}` of the current run with non-decreasing seq.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Frame:
    event: dict[str, Any] | None  # None for a comment (keep-alive)
    id: str | None
    comment: str | None = None


def parse_frames(chunks: Iterable[str]) -> list[Frame]:
    """Parse SSE text (any chunking) into frames: one JSON event per `data:` frame."""
    text = "".join(chunks)
    frames: list[Frame] = []
    for block in text.split("\n\n"):
        if not block:
            continue
        data_lines: list[str] = []
        id_: str | None = None
        comment: str | None = None
        for line in block.split("\n"):
            if line.startswith(":"):
                comment = line[1:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].removeprefix(" "))
            elif line.startswith("id:"):
                id_ = line[3:].removeprefix(" ")
            else:
                raise AssertionError(f"unexpected SSE line {line!r}")
        if data_lines:
            frames.append(Frame(json.loads("\n".join(data_lines)), id_))
        else:
            if id_ is not None:
                raise AssertionError("an id without data")
            frames.append(Frame(None, None, comment))
    return frames


def events_of(frames: Iterable[Frame]) -> list[dict[str, Any]]:
    return [f.event for f in frames if f.event is not None]


def schema_errors(events: Iterable[Mapping[str, Any]], schema: Mapping[str, Any]) -> list[str]:
    """Each event against the vendored AG-UI 1.0 schema (root = any event)."""
    import jsonschema
    from referencing import Registry, Resource

    resource = Resource.from_contents(dict(schema))
    registry = Registry().with_resource(str(schema.get("$id", "urn:ag-ui")), resource)
    validator = jsonschema.Draft202012Validator(dict(schema), registry=registry)
    errors = []
    for index, event in enumerate(events):
        for error in validator.iter_errors(dict(event)):
            errors.append(f"#{index} {event.get('type')}: {error.message[:300]}")
    return errors


@dataclass
class _RunState:
    run_id: str | None
    texts: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    steps: set[str] = field(default_factory=set)
    seen_texts: set[str] = field(default_factory=set)
    seen_tools: set[str] = field(default_factory=set)
    last_seq: int | None = None
    last_request: dict[str, int] = field(default_factory=dict)


_REQUEST_ID = re.compile(r"^(?P<prefix>.+)-m(?P<n>\d+)$")


def _request_order(message_id: Any) -> tuple[str, int] | None:
    """`({prefix}, n)` of a D13 assistant message id `{prefix}-m{n}`; None for other ids."""
    match = _REQUEST_ID.match(message_id) if isinstance(message_id, str) else None
    return (match["prefix"], int(match["n"])) if match else None


_NULLABLE = {("CUSTOM", "value")}


def lifecycle_errors(
    frames: Iterable[Frame], *, continuation: bool = False, run_id: str | None = None,
    known_tool_calls: Iterable[str] = (),
    known_open_steps: Iterable[str] = (),
) -> list[str]:
    """Lifecycle problems of one stream (empty = conformant). `continuation`: the stream resumes
    a run already started (by `run_id`); `known_tool_calls`: calls shown earlier in that run;
    `known_open_steps`: steps the client saw start earlier in that run and not yet finish (a
    continuation may finish them). Message ids in a `MESSAGES_SNAPSHOT` count as started
    messages (a tool call may name one as its parent)."""
    errors: list[str] = []
    state: _RunState | None = _RunState(run_id) if continuation else None
    if state is not None:
        state.seen_tools.update(known_tool_calls)
        state.steps.update(known_open_steps)
    ended = False
    for index, frame in enumerate(frames):
        if frame.event is None:
            continue
        event = frame.event
        kind = event.get("type")
        where = f"#{index} {kind}"
        for key, value in event.items():
            if value is None and (kind, key) not in _NULLABLE:
                errors.append(f"{where}: {key} is null")
        if kind == "RUN_STARTED":
            if state is not None and not ended:
                errors.append(f"{where}: nested run")
            if event.get("protocolVersion") != "1.0":
                errors.append(f"{where}: protocolVersion must be 1.0")
            state, ended = _RunState(event.get("runId")), False
            continue
        if state is None or ended:
            if kind == "RUN_ERROR" and state is None:
                ended = True
                continue
            errors.append(f"{where}: outside a run")
            continue
        if frame.id is not None:
            head, _, tail = frame.id.rpartition(":")
            if head != state.run_id or not tail.lstrip("-").isdigit():
                errors.append(f"{where}: frame id {frame.id!r} is not {state.run_id}:<seq>")
            else:
                seq = int(tail)
                if state.last_seq is not None and seq < state.last_seq:
                    errors.append(f"{where}: frame id seq went back")
                state.last_seq = seq
        if kind == "MESSAGES_SNAPSHOT":
            state.seen_texts.update(m.get("id") for m in event.get("messages") or [] if isinstance(m, Mapping))
        elif kind == "TEXT_MESSAGE_START":
            mid = event.get("messageId")
            if mid in state.texts:
                errors.append(f"{where}: {mid} already open")
            elif mid in state.seen_texts:
                errors.append(f"{where}: {mid} was already started or is in a snapshot")
            order = _request_order(mid)
            if order is not None:
                prefix, n = order
                if n < state.last_request.get(prefix, -1):
                    errors.append(f"{where}: {mid} starts after a later request of {prefix}")
                state.last_request[prefix] = max(n, state.last_request.get(prefix, -1))
            state.texts.add(mid)
            state.seen_texts.add(mid)
        elif kind == "TEXT_MESSAGE_CONTENT":
            if event.get("messageId") not in state.texts:
                errors.append(f"{where}: content for a message that is not open")
        elif kind == "TEXT_MESSAGE_END":
            if event.get("messageId") not in state.texts:
                errors.append(f"{where}: end for a message that is not open")
            state.texts.discard(event.get("messageId"))
        elif kind == "TOOL_CALL_START":
            tid = event.get("toolCallId")
            if tid in state.tools or tid in state.seen_tools:
                errors.append(f"{where}: tool call {tid} started twice")
            parent = event.get("parentMessageId")
            if parent is not None and parent not in state.seen_texts:
                errors.append(f"{where}: parentMessageId {parent} was never started")
            state.tools.add(tid)
            state.seen_tools.add(tid)
        elif kind == "TOOL_CALL_ARGS":
            if event.get("toolCallId") not in state.tools:
                errors.append(f"{where}: args for a call that is not open")
        elif kind == "TOOL_CALL_END":
            if event.get("toolCallId") not in state.tools:
                errors.append(f"{where}: end for a call that is not open")
            state.tools.discard(event.get("toolCallId"))
        elif kind == "TOOL_CALL_RESULT":
            if event.get("toolCallId") in state.tools:
                errors.append(f"{where}: result before the call ended")
        elif kind == "STEP_STARTED":
            name = event.get("stepName")
            if name in state.steps:
                errors.append(f"{where}: step {name} already open")
            state.steps.add(name)
        elif kind == "STEP_FINISHED":
            if event.get("stepName") not in state.steps:
                errors.append(f"{where}: step {event.get('stepName')} not open")
            state.steps.discard(event.get("stepName"))
        elif kind == "RUN_FINISHED":
            if event.get("runId") != state.run_id:
                errors.append(f"{where}: runId differs from RUN_STARTED")
            if state.texts or state.tools or state.steps:
                errors.append(f"{where}: open items {sorted(state.texts | state.tools | state.steps)}")
            outcome = event.get("outcome") or {}
            for interrupt in outcome.get("interrupts") or []:
                tid = interrupt.get("toolCallId")
                if tid is not None and tid not in state.seen_tools:
                    errors.append(f"{where}: interrupt names unseen tool call {tid}")
            ended = True
        elif kind == "RUN_ERROR":
            ended = True
    return errors
