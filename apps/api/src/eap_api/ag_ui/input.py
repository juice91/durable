"""AG-UI input mapping (AGUI.5): a `RunAgentInput` body onto the shared run-start path.

Plan: docs/plans/2026-11-13-milestone-ag-ui.md, D3 (threadId), D4 (runId, fingerprint), D5 (the
tail), D10 (client tools), D11 (ignored state and context), D15 (resume entries), D16 (what keys
may not do), D18 (limits). Protocol-agnostic on purpose: the body is read as plain JSON with our
own lenient parsing (the AG-UI 1.0 `RunAgentInput` shape), so this module needs no `ag_ui` import.

What it does, in order (`parse_run_agent_input`):

- `threadId`, `runId`: strings of 1-256 printable ASCII characters (space through `~`) without
  `/`, `\\` or `..`, and not `.` (both are URL path segments of the attach routes); the threadId is
  keyed with `thread_key` (a hyphenated UUID is lowercased, anything else is opaque).
- `messages`: only the **tail after the last assistant message** is read (D5). In it, `user`
  messages give the new input (text only: a string, or a list of `{"type": "text"}` parts; any
  other part is 422 `unsupported_content`), joined with a blank line; `tool` messages give the
  client tool results (`ClientToolResult`). `system`, `developer`, `reasoning` and `activity`
  messages in the tail, and every message before it, are ignored and counted (logged by count
  only). Client-sent assistant turns, system prompts and earlier history never reach the run.
- `tools`: at most 32, with all `parameters` at most 64 KiB and 256 JSON objects together,
  checked **before** any
  JSON Schema work; then each `ClientToolDecl` (an empty or null `parameters` is the zero-argument
  schema) and the list rules (`check_client_tools`).
- `forwardedProps.pydanticos`: the run options (`AgUiRunOptions`, strict: our namespace). Every
  other `forwardedProps` key is ignored.
- `resume`: entries `{interruptId, status: resolved|cancelled, payload?}`, unique ids. Their
  meaning (decide, payload schema) is AGUI.8's (`resume_entries.py`).
- `state`, `context`, `parentRunId`, `protocolVersion` and unknown fields: accepted and ignored.
- No user text, no tool result and no resume entry: 422 `no_new_input`.

Routes call `parse_run_agent_input_async` (the threadpool), never the sync parse on the event
loop. Then the route (AGUI.6, AGUI.8) applies the checks that need the spec, the principal or the DB:
`check_principal_options` (403), `check_client_tool_clashes` (422), `resolve_agent_thread` (D3),
`check_thread_resource` (409) / `thread_resource_id`, `check_tool_results` against the pending
calls (422), `check_resume_covers` against the open and decided interrupts (422), and stores
`fingerprint(...)` (D4).

Every error is an `AgUiInputError` (an HTTPException) whose detail is the compact list the guarded
routes use: `[{"loc": [...], "msg": ..., "type": <code>}]`. A message names a location and a
rule, never a value from the request, nor a key name the client chose (Pydantic locs are cut at
the first such key; JSON Schema paths name keywords only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import HTTPException, status
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from eap_api.request_guard import guarded_json_route
from eap_schemas import AgentSpec, ModelRef
from eap_schemas.client_tools import (
    MAX_AGUI_INPUT_BYTES,
    MAX_CLIENT_TOOL_SCHEMA_NODES,
    MAX_CLIENT_TOOL_SCHEMAS_BYTES,
    MAX_CLIENT_TOOLS,
    TOOL_CALL_ID_PATTERN,
    AgUiRunOptions,
    ClientToolDecl,
    ClientToolResult,
    ClientToolsInvalid,
    check_client_tools,
    schema_node_count,
)
from eap_schemas.json_schema_subset import json_size
from eap_schemas.mcp import MCP_TOOL_PREFIX, mcp_tool_name
from eap_schemas.tools import RESERVED_TOOL_NAMES, builtin_tool_name

logger = logging.getLogger(__name__)

# uuid5 namespace of every id derived from AG-UI ids (D3, D4). Never change it: derived
# conversation and run ids of existing threads depend on it.
AGUI_NS = uuid.UUID("6f0c2b1e-4a7d-5c3e-9b8a-a6e1d2c4f501")

# D18. The route body cap is read from the environment on every request (guarded_json_route).
AGUI_MAX_BODY_BYTES_ENV = "EAP_AGUI_MAX_BODY_BYTES"
DEFAULT_AGUI_MAX_BODY_BYTES = 2 * 1024 * 1024
AGUI_MAX_BODY_DEPTH = 64
AGUI_DEPTH_DETAIL = f"request body is nested more than {AGUI_MAX_BODY_DEPTH} levels deep"

MAX_EXTERNAL_ID_CHARS = 256  # threadId, runId, message ids
MAX_INTERRUPT_ID_CHARS = 512  # approval:{run_id}:tool:{tool_call_id} with a 256-character call id
MAX_RESUME_ENTRIES = 64
MAX_MSG_CHARS = 200

# AG-UI 1.0 message roles.
MESSAGE_ROLES = frozenset({"developer", "system", "assistant", "user", "tool", "activity", "reasoning"})
RESUME_STATUSES = ("resolved", "cancelled")
USER_INPUT_SEPARATOR = "\n\n"

# The zero-argument tool schema an empty or null AG-UI `parameters` stands for.
EMPTY_PARAMETERS: dict[str, Any] = {"type": "object", "properties": {}}

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TOOL_CALL_ID = re.compile(TOOL_CALL_ID_PATTERN)
_INTERRUPT_ID = re.compile(r"^[\x21-\x7e]{1,%d}$" % MAX_INTERRUPT_ID_CHARS)

# Error codes (the `type` of the single detail item).
NO_NEW_INPUT = "no_new_input"
UNSUPPORTED_CONTENT = "unsupported_content"
INPUT_TOO_LARGE = "input_too_large"
BODY_INVALID = "body_invalid"
THREAD_ID_INVALID = "thread_id_invalid"
RUN_ID_INVALID = "run_id_invalid"
MESSAGE_INVALID = "message_invalid"
TOOL_RESULT_INVALID = "client_tool_result_invalid"
TOOL_RESULT_DUPLICATE = "client_tool_result_duplicate"
TOOL_RESULT_NOT_PENDING = "client_tool_result_not_pending"
TOOL_INVALID = "client_tool_invalid"
TOOL_SHADOWS_SERVER_TOOL = "client_tool_shadows_server_tool"
OPTIONS_INVALID = "run_options_invalid"
RESUME_INVALID = "resume_invalid"
RESUME_DUPLICATE = "resume_duplicate"
RESUME_MISSING_INTERRUPT = "resume_missing_interrupt"
RESUME_UNKNOWN_INTERRUPT = "resume_unknown_interrupt"
WORKFLOW_INPUT_INVALID = "workflow_input_invalid"
DRAFT_NOT_ALLOWED = "draft_not_allowed"
MODEL_OVERRIDE_NOT_ALLOWED = "model_override_not_allowed"
RESOURCE_MISMATCH = "resource_mismatch"
RESOURCE_ID_INVALID = "resource_id_invalid"


class AgUiInputError(HTTPException):
    """A request the input mapping refuses, before the stream opens. `code` is the detail item's
    `type`. Never carries a value from the request."""

    def __init__(self, status_code: int, code: str, loc: Sequence[str | int], msg: str) -> None:
        self.code = code
        super().__init__(
            status_code=status_code,
            detail=[{"loc": ["body", *loc], "msg": msg[:MAX_MSG_CHARS], "type": code}],
        )


def _unprocessable(code: str, loc: Sequence[str | int], msg: str) -> AgUiInputError:
    return AgUiInputError(status.HTTP_422_UNPROCESSABLE_CONTENT, code, loc, msg)


def _from_validation(exc: ValidationError, loc: Sequence[str | int], default_code: str) -> AgUiInputError:
    """The first error of a model's ValidationError, located under `loc`. Our models use
    PydanticCustomError codes (`client_tool_schema_*`, `client_tool_shadows_server_tool`...), kept as
    the type; Pydantic's own errors (missing, string_type...) become `default_code`. Our messages
    and Pydantic's never contain the input (the models set hide_input_in_errors)."""
    err = exc.errors(include_url=False, include_input=False, include_context=False)[0]
    code = err["type"]
    if not (code.startswith("client_tool") or code.startswith("run_options")):
        code = default_code
    return _unprocessable(code, [*loc, *_safe_loc(err.get("loc", ()))], str(err.get("msg", "invalid value")))


def _model_keys(*models: type[BaseModel]) -> frozenset[str]:
    """Field names and aliases of our models: the only key names a loc may repeat."""
    keys: set[str] = set()
    for model in models:
        for name, info in model.model_fields.items():
            keys.add(name)
            for alias in (info.alias, info.validation_alias, info.serialization_alias):
                if isinstance(alias, str):
                    keys.add(alias)
    return frozenset(keys)


_KNOWN_LOC_KEYS = _model_keys(AgUiRunOptions, ModelRef, ClientToolDecl, ClientToolResult)


def _safe_loc(loc: Iterable[Any]) -> list[str | int]:
    """A Pydantic loc cut at its first part that is neither a list index nor a field name (or
    alias) of our models: a client-supplied key name (an `extra_forbidden` key, a dict key) never
    comes back in an error, whatever its size, and Pydantic's union-branch tags (`int`,
    `literal['draft']`) do not read as nested keys."""
    out: list[str | int] = []
    for part in loc:
        if not (isinstance(part, int) or part in _KNOWN_LOC_KEYS):
            break
        out.append(part)
    return out


# --------------------------------------------------------------------------- the route class


def agui_max_body_bytes() -> int:
    """EAP_AGUI_MAX_BODY_BYTES (2 MiB by default; a non-integer or non-positive value is the default)."""
    try:
        value = int(os.environ.get(AGUI_MAX_BODY_BYTES_ENV, DEFAULT_AGUI_MAX_BODY_BYTES))
    except ValueError:
        return DEFAULT_AGUI_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_AGUI_MAX_BODY_BYTES


def agui_route_class() -> type[APIRoute]:
    """The guarded route class of the AG-UI routes (D18): over EAP_AGUI_MAX_BODY_BYTES is 413,
    nesting deeper than 64 is 422 (AGUI_DEPTH_DETAIL), plus the storable-string checks."""
    return guarded_json_route(
        max_body_bytes=agui_max_body_bytes, max_depth=AGUI_MAX_BODY_DEPTH, depth_detail=AGUI_DEPTH_DETAIL
    )


# --------------------------------------------------------------------------- the mapped input


@dataclass(frozen=True)
class ResumeEntry:
    """One `resume[]` entry of RunAgentInput. `payload` is None when absent (`has_payload` False)."""

    interrupt_id: str
    status: Literal["resolved", "cancelled"]
    payload: Any = None
    has_payload: bool = False

    def canonical(self) -> dict[str, Any]:
        out: dict[str, Any] = {"interruptId": self.interrupt_id, "status": self.status}
        if self.has_payload:
            out["payload"] = self.payload
        return out


@dataclass(frozen=True)
class IgnoredInput:
    """What the mapping read past (D5, D11), as counts only, for the log."""

    messages_before_tail: int = 0
    tail_by_role: dict[str, int] = field(default_factory=dict)
    context_entries: int = 0
    state_present: bool = False

    def log_fields(self) -> dict[str, Any]:
        return {
            "agui_ignored_messages": self.messages_before_tail + sum(self.tail_by_role.values()),
            "agui_ignored_tail_roles": dict(sorted(self.tail_by_role.items())),
            "agui_ignored_context": self.context_entries,
            "agui_state_present": self.state_present,
        }


@dataclass(frozen=True)
class AgUiInput:
    """A RunAgentInput as the run-start path needs it.

    - `thread_id`: the thread key (`thread_key`): the client's threadId, or its lowercase form when
      it is a hyphenated UUID. Key `ag_ui_runs`, the derived ids and the conversation lookup on
      it; `client_thread_id` is the threadId as sent, for the frames (RUN_STARTED).
    - `input_text`: the tail's non-blank user messages, joined with a blank line ("" when none);
      `last_user_text` is the last of them alone (a workflow's JSON input, D12).
    - `input_message_ids`: the client's ids of exactly those (non-blank) user messages, in order
      (D13); blank user messages contribute neither text nor id.
    - `tool_results`: the tail's client tool results, in message order;
      `tool_result_message_indexes[k]` is the index in `messages` of `tool_results[k]`.
    - `client_tools`: the declared frontend tools, in declaration order.
    - `options`: `forwardedProps.pydanticos`.
    - `resume`: the resume entries, in request order.
    """

    thread_id: str
    run_id: str
    input_text: str
    last_user_text: str | None
    input_message_ids: tuple[str, ...]
    tool_results: tuple[ClientToolResult, ...]
    client_tools: tuple[ClientToolDecl, ...]
    options: AgUiRunOptions
    resume: tuple[ResumeEntry, ...]
    ignored: IgnoredInput
    client_thread_id: str = ""
    tool_result_message_indexes: tuple[int, ...] = ()

    @property
    def has_user_input(self) -> bool:
        return bool(self.input_message_ids) and bool(self.input_text.strip())

    def fingerprint(self) -> str:
        return fingerprint(self)


# --------------------------------------------------------------------------- parsing


# threadId and runId are also path segments of the GET routes (.../threads/{threadId}/runs/{runId}/
# stream): printable ASCII (space through ~), no '/' (decoded before routing, so the segment
# would split), no '\\' (some proxies read it as '/'), and no '..' or bare '.' (clients and proxies
# collapse dot segments, so the request would reach another path).
_PATH_SAFE_ID = re.compile(r"^[\x20-\x7e]{1,%d}$" % MAX_EXTERNAL_ID_CHARS)


def path_safe_id(value: str) -> bool:
    """`value` can be addressed as one URL path segment by every AG-UI route (see _PATH_SAFE_ID)."""
    return (
        _PATH_SAFE_ID.match(value) is not None
        and "/" not in value
        and "\\" not in value
        and ".." not in value
        and value != "."
    )


def _path_id(value: Any, loc: Sequence[str | int], code: str) -> str:
    """A threadId or runId (D7): _external_id, and addressable as a URL path segment (422)."""
    value = _external_id(value, loc, code)
    if not path_safe_id(value):
        name = str(loc[-1])
        raise _unprocessable(
            code, loc,
            f"{name} must be printable ASCII without '/', '\\' or '..' (it is a URL path segment of the attach routes)",
        )
    return value


def _external_id(value: Any, loc: Sequence[str | int], code: str) -> str:
    name = str(loc[-1])
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EXTERNAL_ID_CHARS
        or _CONTROL.search(value) is not None
    ):
        raise _unprocessable(
            code, loc, f"{name} must be a string of 1 to {MAX_EXTERNAL_ID_CHARS} characters without control characters"
        )
    return value


def _text_content(content: Any, loc: list[str | int], *, allow_none: bool) -> str:
    """A message's content as text: a string, or a list of text parts joined without a separator.
    Any other part type (image, binary, audio...) is 422 unsupported_content."""
    if content is None and allow_none:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for i, part in enumerate(content):
            if not isinstance(part, dict) or not isinstance(part.get("type"), str):
                raise _unprocessable(MESSAGE_INVALID, [*loc, i], "a content part must be an object with a type")
            if part["type"] != "text":
                raise _unprocessable(
                    UNSUPPORTED_CONTENT, [*loc, i, "type"], "only text content is supported; other parts are refused"
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise _unprocessable(MESSAGE_INVALID, [*loc, i, "text"], "a text part needs a string text")
            texts.append(text)
        return "".join(texts)
    raise _unprocessable(MESSAGE_INVALID, loc, "content must be a string or a list of text parts")


def _parse_messages(
    messages: Any,
) -> tuple[list[tuple[str, str]], list[ClientToolResult], list[int], int, dict[str, int]]:
    """(user messages [(id, text)], tool results, each result's index in `messages`, messages
    before the tail, ignored tail roles)."""
    if messages is None:
        messages = []
    if not isinstance(messages, list):
        raise _unprocessable(MESSAGE_INVALID, ["messages"], "messages must be a list")
    last_assistant = -1
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in MESSAGE_ROLES:
            raise _unprocessable(MESSAGE_INVALID, ["messages", i, "role"], "each message needs a known AG-UI role")
        if message["role"] == "assistant":
            last_assistant = i
    users: list[tuple[str, str]] = []
    results: list[ClientToolResult] = []
    result_indexes: list[int] = []
    seen_calls: set[str] = set()
    ignored_roles: dict[str, int] = {}
    for i in range(last_assistant + 1, len(messages)):
        message = messages[i]
        role = message["role"]
        loc: list[str | int] = ["messages", i]
        if role == "user":
            message_id = _external_id(message.get("id"), [*loc, "id"], MESSAGE_INVALID)
            users.append((message_id, _text_content(message.get("content"), [*loc, "content"], allow_none=False)))
        elif role == "tool":
            call_id = message.get("toolCallId")
            if not isinstance(call_id, str) or _TOOL_CALL_ID.match(call_id) is None:
                raise _unprocessable(
                    TOOL_RESULT_INVALID, [*loc, "toolCallId"], "toolCallId must be 1 to 256 printable ASCII characters"
                )
            if call_id in seen_calls:
                raise _unprocessable(
                    TOOL_RESULT_DUPLICATE, [*loc, "toolCallId"], "a tool call may be answered only once per run"
                )
            seen_calls.add(call_id)
            content = _text_content(message.get("content"), [*loc, "content"], allow_none=True)
            error = message.get("error")
            if error is not None and (not isinstance(error, str) or not error):
                # An empty error is refused, as ClientToolResult does: it must never pass as a success.
                raise _unprocessable(TOOL_RESULT_INVALID, [*loc, "error"], "error must be a non-empty string when set")
            try:
                results.append(ClientToolResult(tool_call_id=call_id, content=content, error=error))
                result_indexes.append(i)
            except ValidationError as exc:
                raise _from_validation(exc, loc, TOOL_RESULT_INVALID) from None
        else:
            ignored_roles[role] = ignored_roles.get(role, 0) + 1
    return users, results, result_indexes, last_assistant + 1, ignored_roles


def _parse_tools(tools: Any) -> tuple[ClientToolDecl, ...]:
    """The client tools. The cheap list rules run first (count, object shape, the total size and
    the total JSON-object count of all `parameters`), so the JSON Schema work of ClientToolDecl is
    bounded by MAX_CLIENT_TOOL_SCHEMA_NODES subschemas in total, never by the 2 MiB body
    (review B1)."""
    if tools is None:
        return ()
    if not isinstance(tools, list):
        raise _unprocessable(TOOL_INVALID, ["tools"], "tools must be a list")
    if len(tools) > MAX_CLIENT_TOOLS:
        raise _unprocessable("client_tools_too_many", ["tools"], f"at most {MAX_CLIENT_TOOLS} client tools are allowed")
    normalised: list[tuple[dict[str, Any], Any]] = []
    total = 0
    nodes = 0
    for i, tool in enumerate(tools):
        if not isinstance(tool, dict):
            raise _unprocessable(TOOL_INVALID, ["tools", i], "a tool must be an object")
        parameters = tool.get("parameters")
        if parameters is None or parameters == {}:
            parameters = dict(EMPTY_PARAMETERS)
        try:
            total += json_size(parameters)
        except (TypeError, ValueError):
            raise _unprocessable(
                "client_tool_schema_invalid", ["tools", i, "parameters"], "parameters must be JSON"
            ) from None
        if total > MAX_CLIENT_TOOL_SCHEMAS_BYTES:
            raise _unprocessable(
                "client_tools_schemas_too_large",
                ["tools"],
                f"client tool parameters must be at most {MAX_CLIENT_TOOL_SCHEMAS_BYTES} bytes in total",
            )
        nodes += schema_node_count(parameters, MAX_CLIENT_TOOL_SCHEMA_NODES)
        if nodes > MAX_CLIENT_TOOL_SCHEMA_NODES:
            raise _unprocessable(
                "client_tools_schemas_too_complex",
                ["tools"],
                f"client tool parameters may hold at most {MAX_CLIENT_TOOL_SCHEMA_NODES} JSON objects in total",
            )
        normalised.append((tool, parameters))
    decls: list[ClientToolDecl] = []
    for i, (tool, parameters) in enumerate(normalised):
        description = tool.get("description")
        try:
            decls.append(
                ClientToolDecl(
                    name=tool.get("name"),
                    description="" if description is None else description,
                    parameters=parameters,
                )
            )
        except ValidationError as exc:
            raise _from_validation(exc, ["tools", i], TOOL_INVALID) from None
    try:
        check_client_tools(decls)
    except ClientToolsInvalid as exc:
        raise _unprocessable(exc.code, ["tools"], exc.reason) from None
    return tuple(decls)


def _check_json(value: Any, loc: list[str | int], code: str) -> None:
    """Strict JSON (no NaN or Infinity, which json.loads accepts): the value is stored and hashed."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise _unprocessable(code, loc, "must be JSON without NaN or Infinity") from None


def _parse_options(forwarded: Any) -> AgUiRunOptions:
    if not isinstance(forwarded, dict) or forwarded.get("pydanticos") is None:
        return AgUiRunOptions()
    raw = forwarded["pydanticos"]
    loc: list[str | int] = ["forwardedProps", "pydanticos"]
    if not isinstance(raw, dict):
        raise _unprocessable(OPTIONS_INVALID, loc, "forwardedProps.pydanticos must be an object")
    _check_json(raw, loc, OPTIONS_INVALID)
    try:
        return AgUiRunOptions.model_validate(raw)
    except ValidationError as exc:
        raise _from_validation(exc, loc, OPTIONS_INVALID) from None


def _parse_resume(resume: Any) -> tuple[ResumeEntry, ...]:
    if resume is None:
        return ()
    if not isinstance(resume, list):
        raise _unprocessable(RESUME_INVALID, ["resume"], "resume must be a list")
    if len(resume) > MAX_RESUME_ENTRIES:
        raise _unprocessable(RESUME_INVALID, ["resume"], f"resume may hold at most {MAX_RESUME_ENTRIES} entries")
    entries: list[ResumeEntry] = []
    seen: set[str] = set()
    for i, entry in enumerate(resume):
        loc: list[str | int] = ["resume", i]
        if not isinstance(entry, dict):
            raise _unprocessable(RESUME_INVALID, loc, "a resume entry must be an object")
        interrupt_id = entry.get("interruptId")
        if not isinstance(interrupt_id, str) or _INTERRUPT_ID.match(interrupt_id) is None:
            raise _unprocessable(
                RESUME_INVALID, [*loc, "interruptId"], "interruptId must be a string of printable ASCII characters"
            )
        if interrupt_id in seen:
            raise _unprocessable(RESUME_DUPLICATE, [*loc, "interruptId"], "an interrupt may be answered only once")
        seen.add(interrupt_id)
        entry_status = entry.get("status")
        if entry_status not in RESUME_STATUSES:
            raise _unprocessable(RESUME_INVALID, [*loc, "status"], "status must be 'resolved' or 'cancelled'")
        has_payload = "payload" in entry and entry["payload"] is not None
        payload = entry.get("payload") if has_payload else None
        if has_payload:
            _check_json(payload, [*loc, "payload"], RESUME_INVALID)
        entries.append(
            ResumeEntry(interrupt_id=interrupt_id, status=entry_status, payload=payload, has_payload=has_payload)
        )
    return tuple(entries)


def parse_run_agent_input(body: Any) -> AgUiInput:
    """Map a RunAgentInput body (parsed JSON) onto AgUiInput. See the module docstring for the
    rules; any violation is an AgUiInputError (422) before the stream."""
    if not isinstance(body, dict):
        raise _unprocessable(BODY_INVALID, [], "the body must be a RunAgentInput object")
    thread_id = _path_id(body.get("threadId"), ["threadId"], THREAD_ID_INVALID)
    run_id = _path_id(body.get("runId"), ["runId"], RUN_ID_INVALID)
    users, results, result_indexes, before_tail, ignored_roles = _parse_messages(body.get("messages"))
    client_tools = _parse_tools(body.get("tools"))
    options = _parse_options(body.get("forwardedProps"))
    resume = _parse_resume(body.get("resume"))

    kept = [(message_id, text) for message_id, text in users if text.strip()]
    texts = [text for _, text in kept]
    input_text = USER_INPUT_SEPARATOR.join(texts)
    if len(input_text.encode("utf-8")) > MAX_AGUI_INPUT_BYTES:
        raise _unprocessable(
            INPUT_TOO_LARGE, ["messages"], f"the new user input must be at most {MAX_AGUI_INPUT_BYTES} bytes in UTF-8"
        )
    if not texts and not results and not resume and not options.has_input:
        # `forwardedProps.pydanticos.input` alone is a workflow run's input (D12, AGUI.8); the
        # agent route refuses it like no input (`check_agent_input`).
        raise _unprocessable(
            NO_NEW_INPUT,
            ["messages"],
            "no new input: send a user message, a tool result or a resume entry after the last assistant message",
        )
    context = body.get("context")
    ignored = IgnoredInput(
        messages_before_tail=before_tail,
        tail_by_role=ignored_roles,
        context_entries=len(context) if isinstance(context, list) else 0,
        state_present=body.get("state") is not None,
    )
    mapped = AgUiInput(
        thread_id=thread_key(thread_id),
        client_thread_id=thread_id,
        run_id=run_id,
        input_text=input_text,
        last_user_text=kept[-1][1] if kept else None,
        input_message_ids=tuple(message_id for message_id, _ in kept),
        tool_results=tuple(results),
        tool_result_message_indexes=tuple(result_indexes),
        client_tools=client_tools,
        options=options,
        resume=resume,
        ignored=ignored,
    )
    logger.info("ag-ui input mapped", extra=ignored.log_fields())
    return mapped


async def parse_run_agent_input_async(body: Any) -> AgUiInput:
    """parse_run_agent_input in the threadpool. **Routes must call this one** (AGUI.6, AGUI.8):
    the parse is bounded (client tool schemas at most 64 KiB in total, see `_parse_tools`; worst
    accepted body well under 100 ms) but still synchronous CPU work on up to a 2 MiB body, which
    must never run on the event loop that serves every open SSE stream."""
    return await run_in_threadpool(parse_run_agent_input, body)


# --------------------------------------------------------------------------- fingerprint (D4, D5)


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, UTF-8 kept, strict numbers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


FINGERPRINT_VERSION = 1


def fingerprint_payload(mapped: AgUiInput) -> dict[str, Any]:
    """What the fingerprint covers: the new input, the tool results, the resume entries, the client
    tools and every run option. Not ids (the row is keyed by them), not ignored history, state or
    context, so a forged history never changes it. Order-free collections are sorted: tool results
    by call id, resume entries by interrupt id, tools by name. An absent and an empty
    requestContext are the same.

    Versioned (`v`) and built from explicit fields only, never a model dump, so a field added to
    a model later cannot change the fingerprint of a stored run (an identical re-POST after a
    deploy must still match). A change to this payload bumps FINGERPRINT_VERSION; rows keep the
    old digest, so a route comparing must accept the version they were written with (today only
    v1 exists). Numbers are not canonicalised: `1` and `1.0` differ, which is harmless for a
    client re-sending the same body (D7 re-POSTs the identical RunAgentInput)."""
    options = mapped.options
    payload: dict[str, Any] = {
        "v": FINGERPRINT_VERSION,
        "input": mapped.input_text,
        "tool_results": sorted(
            (
                {"tool_call_id": r.tool_call_id, "content": r.content, "error": r.error}
                for r in mapped.tool_results
            ),
            key=lambda r: r["tool_call_id"],
        ),
        "resume": sorted((e.canonical() for e in mapped.resume), key=lambda e: e["interruptId"]),
        "tools": sorted(
            (
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                for tool in mapped.client_tools
            ),
            key=lambda t: t["name"],
        ),
        "version": options.version,
        "model": None if options.model is None else {"provider": options.model.provider, "name": options.model.name},
        "request_context": options.request_context or {},
        "resource_id": options.resource_id,
    }
    if options.has_input:
        payload["workflow_input"] = options.input
    return payload


def fingerprint(mapped: AgUiInput) -> str:
    """sha256 hex of the canonical JSON of fingerprint_payload (64 lowercase hex digits)."""
    return hashlib.sha256(canonical_json(fingerprint_payload(mapped)).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- ids (D3, D4)


_HYPHENATED_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def thread_key(thread_id: str) -> str:
    """The key of an AG-UI thread: a hyphenated 36-character UUID in any case becomes its canonical
    lowercase form; any other string (32-hex, `{...}`, `urn:uuid:...` included) is opaque and kept
    as sent. One key per thread, so the same runId under two spellings of one conversation id can
    never give two runs (review m1)."""
    return thread_id.lower() if _HYPHENATED_UUID.match(thread_id) else thread_id


def _derived(*parts: str) -> uuid.UUID:
    # A JSON array as the uuid5 name: injective even when the AG-UI ids contain ':'.
    return uuid.uuid5(AGUI_NS, json.dumps([str(p) for p in parts], separators=(",", ":"), ensure_ascii=False))


def derive_conversation_id(tenant_id: str, agent_id: str, thread_id: str) -> str:
    """The conversation id a non-conversation threadId maps to (D3), tenant- and agent-scoped."""
    return str(_derived("conversation", tenant_id, str(agent_id), thread_id))


def derive_agent_run_id(tenant_id: str, agent_id: str, thread_id: str, run_id: str) -> str:
    """The run id of the agent turn an AG-UI run starts (D4)."""
    return str(_derived("agent_run", tenant_id, str(agent_id), thread_id, run_id))


@dataclass(frozen=True)
class AgentThread:
    """Where an AG-UI thread of an agent lives.

    - `conversation_id`: the conversation to run the turn in;
    - `external_thread_id`: None when the threadId named an existing conversation of this tenant
      and agent (Studio), else the raw threadId (stored on a conversation created for it);
    - `conversation`: the conversation row when it exists already, else None (the route creates it
      with `RunStore.create_conversation(..., conversation_id=, external_thread_id=,
      resource_id=thread_resource_id(...))`).
    """

    conversation_id: str
    external_thread_id: str | None
    conversation: dict[str, Any] | None

    @property
    def derived(self) -> bool:
        return self.external_thread_id is not None

    @property
    def exists(self) -> bool:
        return self.conversation is not None


def resolve_agent_thread(run_store: Any, tenant_id: str, agent_id: str, thread_id: str) -> AgentThread:
    """D3: a threadId that is a UUID naming a conversation **of this tenant and agent** uses it (a
    conversation derived earlier included: Studio passes the ids its rail lists);
    anything else (another tenant's or agent's conversation, an unknown UUID, any other string)
    gets the derived conversation id. Only the hyphenated UUID form names a conversation; the
    threadId is keyed with `thread_key` first (AgUiInput.thread_id already is), and
    `external_thread_id` is that key. `run_store` needs only `get_conversation(tenant_id, id)`."""
    agent = str(agent_id)
    thread_id = thread_key(thread_id)
    as_uuid = thread_id if _HYPHENATED_UUID.match(thread_id) else None
    if as_uuid is not None:
        conv = run_store.get_conversation(tenant_id, as_uuid)
        if conv is not None and str(conv.get("agent_id")) == agent:
            return AgentThread(conversation_id=as_uuid, external_thread_id=None, conversation=conv)
    derived = derive_conversation_id(tenant_id, agent, thread_id)
    conv = run_store.get_conversation(tenant_id, derived)
    if conv is not None and str(conv.get("agent_id")) != agent:  # never expected: the id is agent-scoped
        conv = None
    return AgentThread(conversation_id=derived, external_thread_id=thread_id, conversation=conv)


# --------------------------------------------------------------------------- resource (D3)


def thread_resource_id(options: AgUiRunOptions, *, user_id: str | None, api_key_id: str | None) -> str | None:
    """The resource_id of a conversation created for a thread: `forwardedProps.pydanticos.resourceId`
    (the store's rule: 422 for a dot-only id), else the dev bearer's user id when it fits the
    resource pattern, else `apikey:<key_id>` for an API key, else None (resource memory off)."""
    from eap_run_store import validate_resource_id

    if options.resource_id is not None:
        try:
            return validate_resource_id(options.resource_id)
        except ValueError:
            raise _unprocessable(
                RESOURCE_ID_INVALID, ["forwardedProps", "pydanticos", "resourceId"], "resourceId is not a valid resource id"
            ) from None
    for candidate in (user_id, None if api_key_id is None else f"apikey:{api_key_id}"):
        if candidate is None:
            continue
        try:
            value = validate_resource_id(candidate)
        except ValueError:
            continue
        if value is not None:
            return value
    return None


def check_thread_resource(conversation: dict[str, Any], options: AgUiRunOptions) -> None:
    """409 resource_mismatch when a run names a resourceId other than the existing conversation's
    (the resource is never changed). A run that names none uses the conversation as it is."""
    if options.resource_id is not None and conversation.get("resource_id") != options.resource_id:
        raise AgUiInputError(
            status.HTTP_409_CONFLICT,
            RESOURCE_MISMATCH,
            ["forwardedProps", "pydanticos", "resourceId"],
            "this thread already has another resource; resourceId cannot change",
        )


# --------------------------------------------------------------------------- checks needing context


def check_principal_options(options: AgUiRunOptions, *, is_api_key: bool) -> None:
    """D16: an API key may not run the draft or override the model (403). The dev bearer may."""
    if not is_api_key:
        return
    if options.version == "draft":
        raise AgUiInputError(
            status.HTTP_403_FORBIDDEN,
            DRAFT_NOT_ALLOWED,
            ["forwardedProps", "pydanticos", "version"],
            "an API key may not run the draft",
        )
    if options.model is not None:
        raise AgUiInputError(
            status.HTTP_403_FORBIDDEN,
            MODEL_OVERRIDE_NOT_ALLOWED,
            ["forwardedProps", "pydanticos", "model"],
            "an API key may not override the model",
        )


def server_tool_names(spec: AgentSpec) -> frozenset[str]:
    """Model-facing names of the spec's server tools: built-in (dots as `__`) and MCP tools."""
    names = {builtin_tool_name(tool_id) for tool_id in spec.tools}
    names |= {mcp_tool_name(ref.server, ref.tool) for ref in spec.mcp_tools}
    return frozenset(names)


def check_client_tool_clashes(tools: Iterable[ClientToolDecl], spec: AgentSpec) -> None:
    """422 client_tool_shadows_server_tool for the first client tool named like a server tool of the
    spec, a reserved platform tool, or anything in the `mcp__` namespace."""
    server = server_tool_names(spec) | set(RESERVED_TOOL_NAMES)
    for i, tool in enumerate(tools):
        if tool.name in server or tool.name.startswith(MCP_TOOL_PREFIX):
            raise _unprocessable(
                TOOL_SHADOWS_SERVER_TOOL, ["tools", i, "name"], "a client tool may not use the name of a server tool"
            )


def check_tool_results(mapped: AgUiInput, pending_ids: Iterable[str]) -> None:
    """D10: every tool result answers a pending client call of the thread's latest turn (else 422
    client_tool_result_not_pending). Pending ids left unanswered are the worker's to fill with
    the synthetic "not executed by the client" error. The loc is the result's message path,
    `["body", "messages", <index in messages>, "toolCallId"]`. `pending_ids`: from
    AgUiStore.pending_client_tool_calls(..., before_run_id=<this turn's run id>)."""
    pending = set(pending_ids)
    indexes = mapped.tool_result_message_indexes
    for k, result in enumerate(mapped.tool_results):
        if result.tool_call_id not in pending:
            index = indexes[k] if k < len(indexes) else k
            raise _unprocessable(
                TOOL_RESULT_NOT_PENDING,
                ["messages", index, "toolCallId"],
                "this tool result answers no pending client tool call of the thread",
            )


def check_resume_covers(
    mapped: AgUiInput, open_interrupt_ids: Iterable[str], decided_interrupt_ids: Iterable[str] = ()
) -> None:
    """D15: a run carrying `resume[]` must answer every open interrupt of the thread (else 422
    resume_missing_interrupt), and every entry must name an interrupt of the thread, open or
    already decided (the inbox, REST, a timeout, an earlier re-POST); any other id is 422
    resume_unknown_interrupt. AGUI.8 passes both id sets for the thread; what a decided entry
    means (the recorded decision wins) is its own."""
    if not mapped.resume:
        return
    open_ids = list(dict.fromkeys(open_interrupt_ids))
    answered = {entry.interrupt_id for entry in mapped.resume}
    missing = [i for i in open_ids if i not in answered]
    if missing:
        raise _unprocessable(
            RESUME_MISSING_INTERRUPT,
            ["resume"],
            f"resume must answer every open interrupt of the thread ({len(missing)} missing)",
        )
    known = set(open_ids) | set(decided_interrupt_ids)
    for i, entry in enumerate(mapped.resume):
        if entry.interrupt_id not in known:
            raise _unprocessable(
                RESUME_UNKNOWN_INTERRUPT,
                ["resume", i, "interruptId"],
                "this resume entry names no interrupt of the thread",
            )


def check_agent_input(mapped: AgUiInput) -> None:
    """422 no_new_input for an agent run whose only input is `forwardedProps.pydanticos.input`."""
    if not mapped.input_text and not mapped.tool_results and not mapped.resume:
        raise _unprocessable(
            NO_NEW_INPUT,
            ["messages"],
            "no new input: send a user message, a tool result or a resume entry after the last assistant message",
        )


def workflow_input(mapped: AgUiInput) -> Any:
    """D12: a workflow run's input: `forwardedProps.pydanticos.input` when sent (even null), else the
    last user message parsed as JSON (422 workflow_input_invalid when there is none or it is not
    JSON). Validation against the workflow's input schema is the route's."""
    if mapped.options.has_input:
        return mapped.options.input
    if mapped.last_user_text is None or not mapped.last_user_text.strip():
        raise _unprocessable(
            WORKFLOW_INPUT_INVALID,
            ["forwardedProps", "pydanticos", "input"],
            "a workflow run needs forwardedProps.pydanticos.input or a user message holding JSON",
        )
    try:
        value = json.loads(mapped.last_user_text)
        json.dumps(value, allow_nan=False)
    except ValueError:
        raise _unprocessable(
            WORKFLOW_INPUT_INVALID, ["messages"], "the last user message is not JSON; send the input in forwardedProps"
        ) from None
    return value


__all__ = [
    "AGUI_MAX_BODY_BYTES_ENV",
    "AGUI_MAX_BODY_DEPTH",
    "AGUI_NS",
    "AgUiInput",
    "AgUiInputError",
    "AgentThread",
    "DEFAULT_AGUI_MAX_BODY_BYTES",
    "IgnoredInput",
    "ResumeEntry",
    "agui_max_body_bytes",
    "agui_route_class",
    "canonical_json",
    "check_agent_input",
    "check_client_tool_clashes",
    "check_principal_options",
    "check_resume_covers",
    "check_thread_resource",
    "check_tool_results",
    "derive_agent_run_id",
    "derive_conversation_id",
    "fingerprint",
    "fingerprint_payload",
    "FINGERPRINT_VERSION",
    "parse_run_agent_input",
    "parse_run_agent_input_async",
    "path_safe_id",
    "thread_key",
    "resolve_agent_thread",
    "server_tool_names",
    "thread_resource_id",
    "workflow_input",
]
