"""AG-UI messages built from durable data (D13), for `MESSAGES_SNAPSHOT`.

Two sources, one id scheme, so a snapshot, a replay and a live stream always agree:
- `Transcript`: one turn's messages from its durable events, as they are read (the stream's own
  view; used for correction snapshots and for continuations);
- `turn_messages`: a finished turn's messages from its stored `runs.messages` JSON (Pydantic AI's
  ModelMessage JSON, parsed by `kind` / `part_kind`; `eap_api` never imports `pydantic_ai`).

Ids (D13), where `prefix` is the AG-UI runId (a workflow child turn adds the step id,
`{runId}-{stepId}`):
- the assistant message of model request n: `{prefix}-m{n}`;
- a tool result message: `{prefix}-t-{toolCallId}`;
- the user message: the client's id when stored, else `{prefix}-u`;
- tool call ids: the model's.

Structured output (AC): the output tool (`final_result`) is never shown, neither as a call nor as a
result; rejected candidates are left out; the accepted response `{prefix}-m{n}` carries the
canonical JSON (`runs.output` / `run_completed.output`) as its text.

Messages are plain dicts in AG-UI wire shape (camelCase); the caller validates them through
`ag_ui.core` when it builds the event.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from eap_schemas import RunEvent

OUTPUT_TOOL_NAME = "final_result"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_json(args: Any) -> str:
    """Tool call arguments as canonical JSON (a JSON string argument is parsed first)."""
    if isinstance(args, str):
        try:
            return canonical_json(json.loads(args)) if args.strip() else "{}"
        except ValueError:
            return args
    return canonical_json({} if args is None else args)


def tool_content(content: Any) -> str:
    """A tool result's content as text: a string verbatim, anything else as compact JSON (like
    Pydantic AI's `dump_tool_return_content`), None as ""."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"), ensure_ascii=False)


def assistant_message_id(prefix: str, n: int) -> str:
    return f"{prefix}-m{n}"


def tool_message_id(prefix: str, tool_call_id: str) -> str:
    return f"{prefix}-t-{tool_call_id}"


def user_message_id(prefix: str, client_id: str | None = None) -> str:
    return client_id if client_id else f"{prefix}-u"


def user_message(prefix: str, text: str, client_id: str | None = None) -> dict[str, Any]:
    return {"id": user_message_id(prefix, client_id), "role": "user", "content": text}


def _tool_call(tool_call_id: str, name: str, args: Any) -> dict[str, Any]:
    return {"id": tool_call_id, "type": "function", "function": {"name": name, "arguments": args_json(args)}}


def _finish(messages: Iterable[dict[str, Any]], keep: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Drop assistant messages with neither text nor tool calls, except those in `keep` (ids the
    stream has already announced, e.g. a response whose tool calls come in later events)."""
    out = []
    for message in messages:
        if (
            message["role"] == "assistant"
            and not message.get("content")
            and not message.get("toolCalls")
            and message["id"] not in keep
        ):
            continue
        out.append(message)
    return out


def announces_calls(message: Any) -> bool:
    """Whether a stored ModelResponse JSON has a tool call other than the output tool."""
    parts = message.get("parts") if isinstance(message, Mapping) else None
    return any(
        isinstance(p, Mapping) and p.get("part_kind") == "tool-call" and p.get("tool_name") != OUTPUT_TOOL_NAME
        for p in parts or []
    )


def response_call_ids(message: Any) -> list[str]:
    """The ids of a stored ModelResponse JSON's tool calls, the output tool's excepted."""
    parts = message.get("parts") if isinstance(message, Mapping) else None
    return [
        str(p.get("tool_call_id"))
        for p in parts or []
        if isinstance(p, Mapping) and p.get("part_kind") == "tool-call" and p.get("tool_name") != OUTPUT_TOOL_NAME
        and p.get("tool_call_id")
    ]


class Transcript:
    """One turn's messages from its durable events. `add` every event in seq order."""

    def __init__(self, prefix: str, order: Callable[[], int] | None = None) -> None:
        self.prefix = prefix
        # `order` hands out a position shared by several transcripts (a workflow's child turns),
        # so their messages can be merged in the order the stream announced them.
        self._order = order if order is not None else itertools.count().__next__
        self._positions: dict[str, int] = {}
        self._messages: list[dict[str, Any]] = []
        self._assistants: dict[int, dict[str, Any]] = {}
        self._announced: set[str] = set()
        self._calls: set[str] = set()
        self._results: set[str] = set()
        self.last_request: int | None = None
        self.last_candidate: int | None = None

    def _assistant(self, n: int) -> dict[str, Any]:
        message = self._assistants.get(n)
        if message is None:
            message = {"id": assistant_message_id(self.prefix, n), "role": "assistant"}
            self._assistants[n] = message
            self._append(message)
        return message

    def _append(self, message: dict[str, Any]) -> None:
        self._positions[message["id"]] = self._order()
        self._messages.append(message)

    def add_call(self, tool_call_id: str, name: str, args: Any) -> None:
        if not tool_call_id or name == OUTPUT_TOOL_NAME or tool_call_id in self._calls:
            return
        self._calls.add(tool_call_id)
        n = self.last_request if self.last_request is not None else 0
        self._assistant(n).setdefault("toolCalls", []).append(_tool_call(tool_call_id, name, args))

    def add(self, event: RunEvent) -> None:
        data = event.data or {}
        kind = event.type
        if kind == "model_message":
            n = data.get("model_request")
            if not isinstance(n, int):
                return
            if data.get("output_candidate") or data.get("blocked"):
                if data.get("output_candidate"):
                    self.last_candidate = n
                return
            self.last_request = n
            message = self._assistant(n)
            text = data.get("text")
            if isinstance(text, str) and text:
                message["content"] = text
            if announces_calls(data.get("message")):
                # The stream opened (and closed) this message for its tool calls already: a
                # snapshot taken before the tool_call events must still name it (D7).
                self._announced.add(message["id"])
        elif kind == "tool_call":
            self.add_call(str(data.get("tool_call_id") or ""), str(data.get("tool_name") or ""), data.get("args"))
        elif kind == "tool_approval_waiting":
            self.add_call(str(data.get("tool_call_id") or ""), str(data.get("tool_name") or ""), data.get("args"))
        elif kind == "tool_result":
            call_id = str(data.get("tool_call_id") or "")
            if not call_id or data.get("tool_name") == OUTPUT_TOOL_NAME or call_id in self._results:
                return
            self._results.add(call_id)
            self._append(
                {
                    "id": tool_message_id(self.prefix, call_id),
                    "role": "tool",
                    "toolCallId": call_id,
                    "content": tool_content(data.get("content")),
                }
            )
        elif kind == "run_completed" and "output_json" in data:
            output = data.get("output")
            if isinstance(output, str) and output:
                n = self.structured_request()
                self._assistant(n)["content"] = output

    def structured_request(self) -> int:
        """The model request whose candidate was accepted (the last candidate seen)."""
        if self.last_candidate is not None:
            return self.last_candidate
        return self.last_request if self.last_request is not None else 0

    def messages(self) -> list[dict[str, Any]]:
        return _finish(json.loads(json.dumps(self._messages)), frozenset(self._announced))

    def positioned(self) -> list[tuple[int, dict[str, Any]]]:
        """`messages()` with each message's shared position (for merging transcripts)."""
        return [(self._positions[m["id"]], m) for m in self.messages()]


def merge_transcripts(transcripts: Iterable[Transcript]) -> list[dict[str, Any]]:
    """Several transcripts' messages in the order they were first announced."""
    rows = [row for t in transcripts for row in t.positioned()]
    return [m for _, m in sorted(rows, key=lambda row: row[0])]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item for item in content if isinstance(item, str))
    return ""


def _is_rejected_output_retry(part: Mapping[str, Any]) -> bool:
    return part.get("part_kind") == "retry-prompt" and (
        not part.get("tool_call_id") or part.get("tool_name") == OUTPUT_TOOL_NAME
    )


def turn_messages(
    prefix: str,
    messages_json: Sequence[Mapping[str, Any]] | None,
    *,
    user_message_client_id: str | None = None,
    structured_output: str | None = None,
) -> list[dict[str, Any]]:
    """The AG-UI messages of one finished turn from its stored `runs.messages` (new messages of
    the turn, Pydantic AI JSON). `structured_output` is `runs.output` of a structured run (the
    canonical JSON text); None for a text run."""
    out: list[dict[str, Any]] = []
    last_response: dict[str, Any] | None = None
    accepted: dict[str, Any] | None = None
    seen_user = False
    n = 0
    items = list(messages_json or [])
    for index, message in enumerate(items):
        parts = [p for p in (message.get("parts") or []) if isinstance(p, Mapping)]
        if message.get("kind") == "request":
            for part in parts:
                kind = part.get("part_kind")
                if kind == "user-prompt" and not seen_user:
                    seen_user = True
                    out.append(user_message(prefix, _text_of(part.get("content")), user_message_client_id))
                elif kind in ("tool-return", "retry-prompt") and part.get("tool_call_id"):
                    if part.get("tool_name") == OUTPUT_TOOL_NAME:
                        continue
                    content = part.get("content")
                    out.append(
                        {
                            "id": tool_message_id(prefix, str(part["tool_call_id"])),
                            "role": "tool",
                            "toolCallId": str(part["tool_call_id"]),
                            "content": tool_content(content),
                        }
                    )
        elif message.get("kind") == "response":
            text = "".join(str(p.get("content") or "") for p in parts if p.get("part_kind") == "text")
            calls = [
                _tool_call(str(p.get("tool_call_id")), str(p.get("tool_name")), p.get("args"))
                for p in parts
                if p.get("part_kind") == "tool-call" and p.get("tool_name") != OUTPUT_TOOL_NAME
            ]
            has_output_call = any(
                p.get("part_kind") == "tool-call" and p.get("tool_name") == OUTPUT_TOOL_NAME for p in parts
            )
            following = items[index + 1] if index + 1 < len(items) else None
            rejected = following is not None and any(
                isinstance(p, Mapping) and _is_rejected_output_retry(p) for p in (following.get("parts") or [])
            )
            assistant: dict[str, Any] = {"id": assistant_message_id(prefix, n), "role": "assistant"}
            candidate = structured_output is not None and (has_output_call or (not calls and bool(text)))
            if not candidate and text:
                assistant["content"] = text
            if calls:
                assistant["toolCalls"] = calls
            if not (candidate and rejected and not calls):
                out.append(assistant)
            last_response = assistant
            if candidate and not rejected:
                accepted = assistant
            n += 1
    if structured_output:
        target = accepted if accepted is not None else last_response
        if target is None:
            target = {"id": assistant_message_id(prefix, 0), "role": "assistant"}
        if not any(m is target for m in out):
            out.append(target)
        target["content"] = structured_output
    return _finish(out)


def thread_messages(turns: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Concatenate turns' messages (oldest first)."""
    out: list[dict[str, Any]] = []
    for turn in turns:
        out.extend(turn)
    return out
