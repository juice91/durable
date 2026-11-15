"""Frontend (client) tools in the worker (AG-UI milestone, AGUI.7; plan D10, "Frontend tools").

A client tool is executed by the AG-UI client (the browser), never here. The turn that calls one
ENDS with the call pending, and the next turn of the conversation continues it with the client's
result. This is not a DBOS wait: no worker slot is held between the two AG-UI runs.

Turn N (it declares client tools, `runs.client_tools`):

- `eap.start_run` records the declarations (`client_tools`), so a replay uses the same ones.
- `_toolset_for_run` adds a `ClientToolset` (kind `external`) as one more member of `RunToolset`,
  and the run's `output_type` becomes `[<the spec's output type>, DeferredToolRequests]` (`str`, or
  `capabilities.output_type_for(spec)` for a structured spec). A run without client tools keeps
  its exact toolset, output type and step layout.
- The model's arguments are checked against the declared JSON Schema by `ClientToolsCapability`
  (`after_tool_validate`, workflow-side, pure: no step). Invalid arguments go back to the model
  as a retry (value-free errors, at most CLIENT_TOOL_MAX_RETRIES), then the run fails
  `client_tool_args_invalid` (ClientToolArgsInvalid).
- Valid calls are deferred: AC.6's SpecCapability resolves only `requests.approvals` (the durable
  approval wait) and leaves external calls alone, so a response with a gated server call and a
  client call waits for the approval first and then ends the turn with the client call pending.
- `tool:{id}:call` events are written as for any tool; no `tool:{id}:result` is written for a
  pending client call. `run_completed` is `{output: null, pending_tool_call_ids: [...]}`, and the
  turn is `completed` with no output. Its post-turn memory steps are skipped (it has no answer yet).

Turn N+1 (any caller: AG-UI, or a Studio REST turn):

- `eap.start_run` also records the calls turn N left pending (`client_tool_pending`, from
  `AgUiStore.pending_client_tool_calls(before_run_id=<this run>)`) and this turn's results
  (`client_tool_results`, stored by the API).
- Before the first model request, one `tool:{id}:result` event per pending call with
  `source: "client"`: the client's content, its error, or NOT_EXECUTED_MESSAGE for a call the
  client did not answer (a REST turn answers none). The agent run gets
  `DeferredToolResults(calls={id: content | ModelRetry(error)})` and resumes the pending response;
  the new user message (if any) follows the results in the same request.

Client results are untrusted tool output, size-capped by the API; schemas and results never reach
logs or spans (only names and counts do).
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import DeferredToolResults, ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_ai.toolsets.external import TOOL_SCHEMA_VALIDATOR

from eap_schemas.output import MAX_OUTPUT_ERROR_BYTES
from eap_worker.capabilities import schema_errors

# How often the model may retry a client tool call whose arguments break the declared schema.
CLIENT_TOOL_MAX_RETRIES = 2
# The result of a pending client call the next turn does not answer (D10).
NOT_EXECUTED_MESSAGE = "This tool call was not executed by the client."
# The first line of the retry message for arguments that break the declared schema.
ARGS_RETRY_HEADING = "The tool arguments do not match the tool's JSON schema. Fix these errors and call the tool again:"
CLIENT_SOURCE = "client"
# The time budget of one argument check (off the event loop). AGUI.1 bounds the schema (ref-expanded
# node cap, no $ref cycles), so this is defence in depth: a check past it fails the run with
# client_tool_args_invalid instead of stalling the worker.
CLIENT_ARGS_CHECK_SECONDS = 2.0
# The result recorded for a pending call whose response is no longer the history's tail.
NOT_CONTINUED_MESSAGE = (
    "This tool call could not be continued: its response is no longer in the conversation history."
)


class ClientToolArgsInvalid(Exception):
    """The model kept calling a client tool with arguments that break its declared schema, past
    CLIENT_TOOL_MAX_RETRIES retries. Carries no name, argument or schema (all client-chosen).
    Pickle-safe."""

    def __init__(self, max_retries: int = CLIENT_TOOL_MAX_RETRIES, timed_out: bool = False) -> None:
        self.max_retries = max_retries
        self.timed_out = timed_out
        super().__init__(max_retries, timed_out)

    def __str__(self) -> str:
        if self.timed_out:
            return f"client tool argument check exceeded {CLIENT_ARGS_CHECK_SECONDS} s"
        return f"client tool arguments invalid after {self.max_retries} retries"


class ClientArgsRetry(ModelRetry):
    """The ModelRetry for schema-invalid client tool arguments (so a retry limit is recognised)."""


@dataclass(frozen=True)
class ClientTool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ClientToolsRun:
    """A run's client tools (from eap.start_run's recorded `client_tools`)."""

    tools: tuple[ClientTool, ...]
    by_name: Mapping[str, ClientTool] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def of(cls, tools: Iterable[ClientTool]) -> ClientToolsRun:
        listed = tuple(tools)
        return cls(listed, {t.name: t for t in listed})

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.by_name)


def client_tools_from_link(link: Mapping[str, Any]) -> ClientToolsRun | None:
    """The run's client tools from eap.start_run's recorded output, or None (none declared, or a
    checkpoint recorded before AGUI.7). Re-validated with the AGUI.1 contract (defence in depth: the
    API validated them; a tampered row fails the run instead of reaching the model)."""
    raw = link.get("client_tools")
    if not raw:
        return None
    from eap_schemas.client_tools import ClientToolDecl, check_client_tools

    decls = [ClientToolDecl.model_validate(item) for item in raw]
    check_client_tools(decls)
    return ClientToolsRun.of(ClientTool(d.name, d.description, dict(d.parameters)) for d in decls)


_CLIENT_TOOLS: ContextVar[ClientToolsRun | None] = ContextVar("eap_client_tools", default=None)


def set_client_tools(tools: ClientToolsRun | None) -> Token[ClientToolsRun | None]:
    """Make `tools` the current run's client tools for `_toolset_for_run` and ClientToolsCapability
    (a context variable, like capabilities.set_approval_settings: each workflow runs in its own task)."""
    return _CLIENT_TOOLS.set(tools)


def reset_client_tools(token: Token[ClientToolsRun | None]) -> None:
    _CLIENT_TOOLS.reset(token)


def current_client_tools() -> ClientToolsRun | None:
    return _CLIENT_TOOLS.get()


# --------------------------------------------------------------------------- the toolset


class ClientToolset(AbstractToolset[Any]):
    """The run's client tools as `external` tools: Pydantic AI never calls them; a valid call ends
    the run with DeferredToolRequests. The arguments are only parsed here (the schema check is
    ClientToolsCapability's, workflow-side)."""

    def __init__(self, tools: ClientToolsRun) -> None:
        self.tools = tools

    @property
    def id(self) -> str | None:
        return None

    @property
    def names(self) -> frozenset[str]:
        return self.tools.names

    def _tool(self, tool: ClientTool) -> ToolsetTool[Any]:
        return ToolsetTool(
            toolset=self,
            tool_def=ToolDefinition(
                name=tool.name,
                description=tool.description or None,
                parameters_json_schema=tool.parameters,
                kind="external",
            ),
            max_retries=CLIENT_TOOL_MAX_RETRIES,
            args_validator=TOOL_SCHEMA_VALIDATOR,
        )

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        return {t.name: self._tool(t) for t in self.tools.tools}

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        return self._tool(self.tools.by_name[tool_def.name])

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]) -> Any:
        raise RuntimeError("a client tool is never executed by the worker")


def client_toolset_for_run() -> ClientToolset | None:
    tools = current_client_tools()
    return ClientToolset(tools) if tools is not None and tools.tools else None


# --------------------------------------------------------------------------- argument check


def args_retry_message(errors: Sequence[tuple[str, str]]) -> str:
    lines = [ARGS_RETRY_HEADING]
    size = len(ARGS_RETRY_HEADING.encode("utf-8"))
    for path, message in errors:
        line = f"- {path}: {message}"
        size += 1 + len(line.encode("utf-8"))
        if size > MAX_OUTPUT_ERROR_BYTES:
            break
        lines.append(line)
    return "\n".join(lines)


def check_client_args(tool: ClientTool, args: Any) -> None:
    """Raise ClientArgsRetry when `args` break the tool's declared schema (messages and paths from
    the schema only, never a value; eap_worker.capabilities.schema_errors). Synchronous: the
    workflow uses client_arg_errors (off the event loop, time-bounded)."""
    errors = schema_errors(tool.parameters, args)
    if errors:
        raise ClientArgsRetry(args_retry_message(errors))


async def client_arg_errors(tool: ClientTool, args: Any, budget: float | None = None) -> list[tuple[str, str]]:
    """schema_errors for a client call, in a worker thread under CLIENT_ARGS_CHECK_SECONDS. Past the
    budget it raises ClientToolArgsInvalid(timed_out=True): the run fails, the event loop never
    stalls (the thread finishes on its own; AGUI.1's schema bounds make that unreachable)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(schema_errors, tool.parameters, args),
            timeout=CLIENT_ARGS_CHECK_SECONDS if budget is None else budget,
        )
    except TimeoutError:
        raise ClientToolArgsInvalid(timed_out=True) from None


class ClientToolsCapability(AbstractCapability[Any]):
    """Workflow-side schema check of client tool arguments (no step). Untouched for any other tool
    and for runs without client tools."""

    async def after_tool_validate(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any
    ) -> Any:
        tools = current_client_tools()
        if tools is None or tool_def.kind != "external":
            return args
        tool = tools.by_name.get(tool_def.name)
        if tool is not None:
            errors = await client_arg_errors(tool, args)
            if errors:
                raise ClientArgsRetry(args_retry_message(errors))
        return args

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None


def retries_exhausted(exc: BaseException) -> bool:
    """Whether `exc` is Pydantic AI's retry limit for a client tool's invalid arguments."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ClientArgsRetry):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# --------------------------------------------------------------------------- the next turn


@dataclass(frozen=True)
class ClientResume:
    """What turn N+1 hands the agent run: the deferred results and their `tool:{id}:result` events."""

    results: DeferredToolResults
    events: tuple[dict[str, Any], ...]


def client_resume(link: Mapping[str, Any]) -> ClientResume | None:
    """The results for the calls the previous turn left pending (eap.start_run's recorded
    `client_tool_pending`, in its order), from this turn's `client_tool_results`; an unanswered call
    gets NOT_EXECUTED_MESSAGE as an error. None when nothing is pending (or an older checkpoint)."""
    pending = link.get("client_tool_pending") or []
    if not pending:
        return None
    answers: dict[str, Mapping[str, Any]] = {}
    for item in link.get("client_tool_results") or []:
        if isinstance(item, Mapping) and isinstance(item.get("tool_call_id"), str):
            answers.setdefault(item["tool_call_id"], item)
    calls: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    for call in pending:
        call_id = str(call["tool_call_id"])
        answer = answers.get(call_id)
        error = answer.get("error") if answer is not None else NOT_EXECUTED_MESSAGE
        if isinstance(error, str) and error:
            calls[call_id] = ModelRetry(error)
            status, content = "error", error
        else:
            content = str((answer or {}).get("content") or "")
            calls[call_id] = content
            status = "ok"
        events.append({
            "tool_call_id": call_id,
            "tool_name": call.get("tool_name"),
            "status": status,
            "content": content,
            "source": CLIENT_SOURCE,
        })
    return ClientResume(DeferredToolResults(calls=calls), tuple(events))


def _answered_ids(messages: Sequence[ModelMessage], start: int) -> set[str]:
    return {
        p.tool_call_id
        for m in messages[start:]
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, (ToolReturnPart, RetryPromptPart))
    }


def _last_response_index(messages: Sequence[ModelMessage]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], ModelResponse):
            return index
    return None


def resumable(history: Sequence[ModelMessage] | None, call_ids: Iterable[str]) -> bool:
    """Whether the history ends in the response holding every one of `call_ids`, still unanswered
    (only then can Pydantic AI continue it with deferred results)."""
    if not history:
        return False
    index = _last_response_index(history)
    if index is None:
        return False
    response = history[index]
    assert isinstance(response, ModelResponse)
    open_ids = {p.tool_call_id for p in response.parts if isinstance(p, ToolCallPart)} - _answered_ids(history, index + 1)
    wanted = set(call_ids)
    return bool(wanted) and wanted <= open_ids


def dangling_tail(history: Sequence[ModelMessage] | None) -> bool:
    """Whether the history's last response has a tool call no later request answers: a client call
    that was never continued (the turn after a pending one failed, so its results are not history).
    Pydantic AI refuses a new prompt on such a history, so the caller repairs it."""
    if not history:
        return False
    index = _last_response_index(history)
    if index is None:
        return False
    response = history[index]
    assert isinstance(response, ModelResponse)
    call_ids = {p.tool_call_id for p in response.parts if isinstance(p, ToolCallPart)}
    return bool(call_ids - _answered_ids(history, index + 1))


# --------------------------------------------------------------------------- limits and memory


async def client_args_valid(tool: ClientTool | None, args: Any) -> bool:
    """Whether a client call's arguments pass its declared schema (ClientToolsCapability rejects the
    others as a retry, so they never become pending calls). Off the event loop, time-bounded."""
    if tool is None:
        return False
    return not await client_arg_errors(tool, args)


def check_client_tool_calls(limits: Any, usage: Any, count: int) -> None:
    """Client calls count against `max_tool_calls` like any call (Pydantic AI counts only the calls
    it runs, and a deferred client call never runs here). The same check as AC.6's
    check_approved_tool_calls: the run's own UsageLimits on its live usage plus `count` calls, so the
    breach is the same UsageLimitExceeded (`tool_calls_limit`; errors.describe_usage_limit gives it
    `limit: max_tool_calls` and `value: <the cap>`). Pure: replays raise identically."""
    if limits is None or getattr(limits, "tool_calls_limit", None) is None or count <= 0:
        return
    projected = copy.deepcopy(usage)
    projected.tool_calls += count
    limits.check_before_tool_call(projected)


MAX_CHAIN_TURNS = 20  # how far eap.start_run walks back through turns that ended on client calls
# Bounds of the recorded chain (eap.start_run's checkpoint): each input, args text and result is
# clipped, and older links are dropped once the chain holds MAX_CHAIN_CHARS (the index text is
# clipped to recall.MAX_INDEX_CHARS = 8,000 anyway).
MAX_CHAIN_VALUE_CHARS = 2_000
MAX_CHAIN_CHARS = 16_000


def clip_chain_text(value: Any, limit: int = MAX_CHAIN_VALUE_CHARS) -> str | None:
    """A chain value as text, clipped to `limit` characters (None stays None)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chain_link_size(link: Mapping[str, Any]) -> int:
    size = len(link.get("input") or "")
    for call in link.get("calls") or []:
        size += len(call.get("args_text") or "") + len(str(call.get("tool_name") or ""))
    for value in (link.get("results") or {}).values():
        size += len(value or "")
    return size


def _result_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)


def logical_turn_texts(
    chain: Sequence[Mapping[str, Any]],
    resume: ClientResume | None,
    input_text: str | None,
    output: str | None,
) -> tuple[str | None, str | None, str | None]:
    """The (user, tool calls, answer) text a turn that continues client calls indexes for semantic recall:
    the whole logical turn, not just the continuation. `chain` (eap.start_run's recorded
    `client_turn_chain`, oldest first): each earlier turn of it with its processed input, its client
    calls and their results; `resume`: this turn's results for the last of them. User text: every
    non-empty input of the chain, then this turn's. Tool text: one `Tool <name>(<args>) ->
    <result>` line per client call (client-chosen, untrusted: the caller runs it through the spec's
    output processors before it joins the answer as the assistant text)."""
    last_results = {e["tool_call_id"]: e.get("content") for e in (resume.events if resume else ())}
    inputs: list[str] = []
    lines: list[str] = []
    for index, link in enumerate(chain):
        text = link.get("input")
        if isinstance(text, str) and text.strip():
            inputs.append(text)
        results = link.get("results") or {}
        if index == len(chain) - 1:
            results = {**results, **last_results}
        for call in link.get("calls") or []:
            call_id = call.get("tool_call_id")
            args = call.get("args_text")
            if not isinstance(args, str):
                args = json.dumps(call.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str)
            result = results.get(call_id, NOT_EXECUTED_MESSAGE)
            lines.append(f"Tool {call.get('tool_name')}({args}) -> {_result_text(result)}")
    if isinstance(input_text, str) and input_text.strip():
        inputs.append(input_text)
    answer = output if isinstance(output, str) and output.strip() else None
    return ("\n\n".join(inputs) or None), ("\n".join(lines) or None), answer


def pending_ids(output: Any) -> list[str]:
    """The client call ids a run ended with (its DeferredToolRequests output), in call order."""
    return [c.tool_call_id for c in getattr(output, "calls", ()) or ()]


def pending_link(calls: Iterable[Any]) -> list[dict[str, Any]]:
    """eap.start_run's `client_tool_pending`: [{tool_call_id, tool_name}] (never the arguments)."""
    return [{"tool_call_id": c.tool_call_id, "tool_name": c.tool_name} for c in calls]


__all__ = [
    "ARGS_RETRY_HEADING",
    "CLIENT_SOURCE",
    "CLIENT_TOOL_MAX_RETRIES",
    "NOT_EXECUTED_MESSAGE",
    "ClientArgsRetry",
    "ClientResume",
    "ClientTool",
    "ClientToolArgsInvalid",
    "ClientToolsCapability",
    "ClientToolsRun",
    "ClientToolset",
    "MAX_CHAIN_CHARS",
    "MAX_CHAIN_TURNS",
    "MAX_CHAIN_VALUE_CHARS",
    "chain_link_size",
    "clip_chain_text",
    "CLIENT_ARGS_CHECK_SECONDS",
    "NOT_CONTINUED_MESSAGE",
    "check_client_args",
    "client_arg_errors",
    "check_client_tool_calls",
    "client_args_valid",
    "logical_turn_texts",
    "client_resume",
    "client_tools_from_link",
    "client_toolset_for_run",
    "current_client_tools",
    "dangling_tail",
    "pending_ids",
    "pending_link",
    "reset_client_tools",
    "resumable",
    "retries_exhausted",
    "set_client_tools",
]
