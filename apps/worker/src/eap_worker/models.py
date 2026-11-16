"""Per-run model resolution: a model-name string from the spec -> a Model, built inside the model step.

Model ids are `"{spec.model.provider}:{spec.model.name}"`:

- `test:ticket`  scripted model: calls `tickets__create` once (if the tool is offered), then answers
                 with streamed text naming the ticket. Deterministic; for tests and local demos.
- `test:first-tool` scripted model: calls the first offered tool once with `{}`, then answers.
- `test:mcp`   scripted model for MCP tools: the user prompt may be JSON `{"tool": <name>, "args": {...}}`
                 (both optional; default: the first offered `mcp__*` tool, `{}`). `<name>` is the offered
                 name (`mcp__orders__lookup_order`) or the server's tool name if exactly one matches.
                 With `"bad_args": {...}` the first call uses those and, after one retry prompt, `args`. Calls that tool once,
                 then answers `MCP result: <result JSON>. Tools offered: <sorted names, comma-separated>.`
                 (or `MCP error: ...` after a tool error), so a test can see what was offered.
- `test:history` scripted model that answers with what it was given, as one streamed chunk:
                 `Turn {k}. You said: p1 | ... | pk. I answered {m} times before.` (every user
                 prompt in the messages, then the number of model responses among them). Used to
                 prove a conversation turn receives the earlier turns as message history.
- `test:context` scripted model that answers with the instructions it was given (the spec's
                 instructions after `{{context.<name>}}` rendering), as one streamed chunk:
                 `Instructions: <rendered instructions>`. Proves run options reach the model.
- `test:moderation` scripted moderation model (P0.17): answers the moderation verdict JSON for the
                 text it was given: `{"flagged": true, "categories": ["illicit"]}` when the text
                 contains `FLAG_ME`, else `{"flagged": false, "categories": []}`.
- `test:moderation-invalid` scripted moderation model whose answer is not a verdict (`I cannot
                 classify this.`), to prove an unparsable verdict fails closed.
- `test:moderation-empty` scripted moderation model with an empty answer: fails closed on the first
                 (priced) request, never retried.
- `test:think-first` (P0.17) answers first with thinking only (no text, no tool call), which the
                 agent re-prompts; then `Echo: <first prompt>`. Models a thinking model's empty answer.
- `test:think-tool` (P0.17) first thinks `SECRET plan` AND calls the first offered tool with `{}`;
                 after the tool returned, answers `Done.`. Proves unchecked thinking is never stored.
- `test:retrieve` (RM.11) calls the platform tool `retrieve` once (if offered) with
                 `{"search_query": <user input>}`, or with the input itself when it is a JSON object
                 holding `search_query` (so a test can pass `knowledge_base` too). Then answers
                 `According to [1] <title>: <url>` from the first numbered source of the result,
                 `No sources found.` for an empty result, and `Retrieve error: <message>` after a tool
                 error or a retry prompt (it never calls again). Without the tool: `Echo: <input>`.
                 When an output tool is offered (structured output, AC.4), that final answer instead
                 calls the output tool with `schema_instance(schema, text=<answer>)`: every free
                 string field holds the answer, `[n]` markers included. So does `test:retrieve-multi`.
- `test:retrieve-multi` takes `{"rounds": [[q, ...], ...]}`: one response per round with one
                 `retrieve` call per query (several calls in one response, like a model's parallel
                 calls), then answers `Sources: [n] <title>: <url>; ...` with every numbered source
                 of every retrieve result of the turn, in order. Proves run-wide source numbers.
- `test:memory` (RM.12) parses `remember <key>=<value>` (any number, values without spaces; a value
                 that parses as JSON is used as such, e.g. `age=42`) and `forget <key>` from the input
                 and, when `update_working_memory` is offered and there is something to change,
                 calls it once: schema mode `{"patch": {key: value, forgotten key: null}}`; template
                 mode `{"content": <the note from the instructions' working memory block with a
                 `- <key>: <value>` line replaced (matched case-insensitively) or appended, and
                 forgotten keys' lines removed>}`. An input that is a JSON object holding `patch` or
                 `content` is passed as the arguments as-is. Then answers
                 `<tool result>\nWorking memory:\n<the block's note>` (`Memory error: <message>` after a
                 tool error or retry prompt; no tool call: just `Working memory:\n<note>`, `(none)`
                 without a block). The note echoed is the one rendered into the instructions, i.e.
                 as of the turn's start.
- `test:observer` (RM.13) scripted observer/reflector for `eap-observer`: for a `TASK: observe`
                 message, one note per `<turn number="N" date="D">` element, `turn N: <first 40 chars
                 of the turn's first User: line>` (priority medium, date D); for `TASK: reflect`, one
                 high-priority note `reflection of K notes over turns A-B` (K = the `<notes>` elements).
                 Answers the `{"observations": [...]}` JSON of eap_worker.observational.
- `test:observer-invalid` (RM.13) answers `I cannot summarise this.` (not the notes JSON), to prove
                 an unparsable observer answer is a `memory_observe_failed` event, not a failed turn.
- `test:structured` (AC.2) structured output in every mode. Tool mode (an output tool is offered,
                 e.g. `final_result`): calls it once with an instance of the tool's schema
                 (`schema_instance`: required properties only; `const`, else the first `enum` value;
                 string `"x"`, number and integer 1, boolean true, arrays with one item, nested
                 objects recursively; `minLength`/`minimum`/`maximum`/`minItems`/`maxItems`
                 respected). Native or prompted mode: answers that instance as JSON text. The prompt
                 may be JSON: `{"output": <value>}` is sent instead of the instance (as given,
                 valid or not), and `{"bad_first": true}` makes the turn's first candidate violate
                 the schema by type (`schema_violation`; `[]` when no property can be violated), so
                 one retry follows. No structured output asked for: `Echo: <prompt>`.
- `test:structured-never` (AC.2) like `test:structured`, but every candidate is `schema_violation`,
                 so retries are exhausted (`output_invalid`).
- `test:structured-no-native` (AC.2) `test:structured` with a profile without JSON-schema output
                 (`supports_json_schema_output=False`), so native mode fails at run start.
- `test:approval` (AC.2) calls the tool named in prompt JSON `{"tool": <name>, "args": {...}}`
                 (`<name>`: the offered name, a built-in id such as `tickets.create`, an MCP ref
                 `<server>/<tool>`, or a unique `__<name>` suffix), or the first offered tool with
                 `{}`. `{"calls": [{"tool", "args"}, ...]}` makes every call in one response (one
                 DeltaToolCall index each when streamed). After this turn's returns it answers one
                 line per return, in call order: `Tool <name> outcome=<outcome> content=<content>`
                 (outcome `success`, `denied`, `failed`, ...; content as text, or sorted JSON);
                 `... outcome=retry content=<retry prompt>` after a retry prompt (never calls
                 again); `No tool offered.` without tools.
- `test:fail-503`, `test:fail-400` (AC.2) every request raises `ModelHTTPError(503 | 400)` (a
                 `ModelAPIError`, which FallbackModel falls back on) before any output.
- `test:loop` (AC.2) calls the first offered tool with `{}` on every request and never answers (for
                 request and tool-call limits); give it a tool that accepts `{}`, such as
                 `clock.now`. `Loop: no tool offered.` without tools.
- `test:priced` (AC.2) `Echo: <prompt>` with a fixed usage of 1000 input and 500 output tokens per
                 request, streamed or not; priced in `config/model-prices.test.json` (USD 0.02 a
                 request). Prompt JSON `{"rounds": k}` first calls the first offered tool with `{}`
                 (e.g. `clock.now`), one call per request, until this turn holds k returns, then
                 echoes: k + 1 requests, for the cost cap.
- `test:judge` (AC.2) scripted judge: `scripted_judge_verdict(<this turn's prompts>, <instructions>)`.
                 The output is the first `<output>…</output>` block of the user prompt (its content
                 HTML-escaped by the judge prompt, unescaped after extraction); no block is a fail
                 (`The judge input has no <output> block.`). The marker `MUST_CONTAIN:<text>` (or
                 `MUST_CONTAIN:"<text with spaces>"`) is read only outside the `<input>`, `<output>`
                 and `<expected>` blocks. Pass (score 1.0) when the output contains the text, else
                 fail (0.0); also a fail without a marker. `reason` names the marker. Calls the output tool with
                 `{"reason", "pass", "score"}` when offered, else answers that JSON as text. Fixed
                 usage of 200 input and 50 output tokens; priced in the test price file.
- `test:refine` (LP.14) a structured generator for refine loops. Its prompt JSON is
                 `{"attempt": <int>, "feedback": <string>}` (missing: 0 and ""); it answers
                 `{"draft": "attempt <attempt + 1>", "after": <feedback>}`, through the output tool
                 when one is offered, else as JSON text (`after` is left out when the output schema
                 forbids extra properties and does not declare it). The feedback is echoed in its own
                 field, never inside `draft`: the scripted judge's failing reason names its marker
                 (`... MUST_CONTAIN marker "attempt 3".`), so a draft quoting the feedback would
                 contain the marker one attempt early. Fixed usage REFINE_USAGE (100 input, 20 output
                 tokens per request), priced by the test price file's `test:*` row.
- `test:text-tool` (AC.5) answers request 0 with text `Let me check.` AND a call of the first
                 offered tool with `{}` (one response); after the return it answers `Done.`. Without
                 tools: `Done.`. Proves a response with text and a tool call (output processors run
                 on its text) is handled like any other.
- `test:frontend_tool` (AGUI.7) calls the first client (frontend, `external`) tool offered with
                 `schema_instance(<its parameters>)`; after the client's result (the last request
                 holds the call's return) it answers `tool said: <content>`, or `tool said: error:
                 <message>` for an error the client reported (or the "not executed by the client"
                 result); a result counts as the client's when it names no server tool of the run. An argument retry (eap_worker.client_tools.ARGS_RETRY_HEADING) makes it
                 call again. Prompt JSON: `{"tool": <name>}` picks the client tool; `{"args": {...}}`
                 sends these arguments on every attempt (valid or not); `{"bad_first": true}` makes the
                 first attempt violate the schema (`schema_violation`); `{"with": <server tool>,
                 "with_args": {...}}` also calls that server tool, first, in the same response (as
                 `test:approval` names it). With an output tool or native/prompted output, the
                 answer is `schema_instance(<output schema>, text=<answer>)`. `No client tool
                 offered.` without client tools.
- `test:<other>` scripted model that streams a short text answer echoing the prompt, no tools.
- `litellm:<name>` OpenAI-compatible chat model against EAP_LITELLM_BASE_URL (fallback OPENAI_BASE_URL)
                 and EAP_LITELLM_API_KEY (fallback LITELLM_API_KEY), e.g. `litellm:qwen3.8-thinking`,
                 with SDK (transport) retries off; DBOS's model step retry policy owns retrying.
- anything else is left to Pydantic AI's `infer_model`.

Scripted embedding models (RM.6), resolved by `eap_worker.embeddings`, not by the chat resolver:

- `test:hash`    hashed bag of words into 1536 dimensions, L2-normalised and deterministic, so texts
                 that share words have a higher cosine similarity. Reports `ceil(len / 4)` input
                 tokens per text. Ignores a `dimensions` setting (always its native size), so a
                 policy declaring other dimensions gives an EmbeddingDimensionMismatch.
- `test:hash768` the same at 768 dimensions.
- `test:hash-nousage` the same at 1536 dimensions, but reports no usage (an unmeasured call).

Every id must also pass the platform model allowlist (EAP_ALLOWED_MODELS, default `test:*`). The
workflow checks it before the agent starts; the resolver checks it again so no code path can build
a model that the policy forbids.

Fallback models (agent capabilities milestone, AC.5), for `eap-agent` only (`make_resolver(...,
fallback=True)`): when the requested id is the run spec's primary model (`ctx.deps.spec`) and the
spec has `fallback_models`, the resolver returns `FallbackModel(primary, *fallbacks)`, every member
built by this same per-id resolver, so each is checked against the allowlist. It runs inside the
model step (and workflow-side, where the run picks its model), so every model request starts at the
primary and a DBOS retry of the step re-runs the whole chain. A run whose model was overridden
(P0.19) requests another id, so it gets the bare model: the override disables fallback. The
observer, the moderator and the judge build their resolvers without `fallback`: one model each.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.embeddings import EmbeddingModel as PaiEmbeddingModel, EmbeddingResult, EmbeddingSettings
from pydantic_ai.embeddings.result import EmbedInputType
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.usage import RequestUsage

from eap_schemas.knowledge import approx_tokens
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
    FunctionStreamedResponse,
)

from eap_worker.config import WorkerSettings

PLACEHOLDER_MODEL = "eap:unset"
TICKET_TOOL = "tickets__create"


def model_id_for(provider: str, name: str) -> str:
    return f"{provider}:{name}"


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


def _last_prompt(messages: list[ModelMessage]) -> str:
    for m in reversed(messages):
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart) and isinstance(p.content, str):
                    return p.content
    return ""


def _chunks(text: str, size: int = 8) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _ticket_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    """('tool', args) for the first turn when the tool is offered, else ('text', answer)."""
    offered = {t.name for t in info.function_tools}
    returns = [r for r in _tool_returns(messages) if r.tool_name == TICKET_TOOL]
    if TICKET_TOOL in offered and not returns:
        prompt = _last_prompt(messages)
        return "tool", {"title": (prompt[:60] or "Support request"), "body": prompt or "(no input)"}
    if returns:
        content = returns[-1].content
        ticket_id = content.get("ticket_id") if isinstance(content, dict) else str(content)
        return "text", f"Created ticket {ticket_id}. Our team will follow up shortly."
    return "text", f"Echo: {_last_prompt(messages)}"


def scripted_ticket_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, value = _ticket_plan(messages, info)
        if kind == "tool":
            return ModelResponse(parts=[ToolCallPart(TICKET_TOOL, value)])
        return ModelResponse(parts=[TextPart(value)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        kind, value = _ticket_plan(messages, info)
        if kind == "tool":
            yield {0: DeltaToolCall(name=TICKET_TOOL, json_args=json.dumps(value))}
            return
        for chunk in _chunks(value):
            yield chunk

    return FunctionModel(respond, stream_function=stream, model_name="scripted-ticket")


def scripted_first_tool_model() -> FunctionModel:
    def plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, str]:
        if info.function_tools and not _tool_returns(messages):
            return "tool", info.function_tools[0].name
        return "text", "Done."

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, value = plan(messages, info)
        return ModelResponse(parts=[ToolCallPart(value, {}) if kind == "tool" else TextPart(value)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        kind, value = plan(messages, info)
        if kind == "tool":
            yield {0: DeltaToolCall(name=value, json_args="{}")}
        else:
            yield value

    return FunctionModel(respond, stream_function=stream, model_name="scripted-first-tool")


def _mcp_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    offered = sorted(t.name for t in info.function_tools)
    parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    # Retry prompts: RetryPromptParts, or (pydantic-ai 2.x) returns with outcome 'retried'.
    errors = [p for p in parts if isinstance(p, RetryPromptPart)]
    all_returns = _tool_returns(messages)
    retried = [r for r in all_returns if r.outcome == "retried"]
    returns = [r for r in all_returns if r.outcome != "retried"]
    request = _mcp_request(messages)
    if request.get("bad_args") is not None and len(errors) + len(retried) == 1 and not returns:
        return "tool", (_mcp_tool(request, offered), request.get("args") or {})  # corrected retry
    if errors:
        return "text", f"MCP error: {errors[-1].model_response()}"
    if retried and not returns:
        return "text", f"MCP error: {retried[-1].content}"
    if returns and returns[-1].outcome != "success":  # a final tool failure (ToolFailed)
        return "text", f"MCP error: {returns[-1].content}"
    if returns:
        result = json.dumps(returns[-1].content, sort_keys=True, default=str)
        return "text", f"MCP result: {result}. Tools offered: {', '.join(offered)}."
    tool = _mcp_tool(request, offered)
    if tool is None:
        return "text", f"No MCP tool offered. Tools offered: {', '.join(offered)}."
    first = request.get("bad_args") if request.get("bad_args") is not None else request.get("args")
    return "tool", (tool, first or {})


def _mcp_request(messages: list[ModelMessage]) -> dict[str, Any]:
    try:
        request = json.loads(_last_prompt(messages))
    except ValueError:
        return {}
    return request if isinstance(request, dict) else {}


def _mcp_tool(request: dict[str, Any], offered: list[str]) -> str | None:
    tool = request.get("tool") or next((n for n in offered if n.startswith("mcp__")), None)
    if tool is not None and tool not in offered:  # a server-side tool name: its mcp__<server>__<tool>
        matches = [n for n in offered if n.startswith("mcp__") and n.endswith(f"__{tool}")]
        tool = matches[0] if len(matches) == 1 else tool
    return tool


def scripted_mcp_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, value = _mcp_plan(messages, info)
        return ModelResponse(parts=[ToolCallPart(value[0], value[1]) if kind == "tool" else TextPart(value)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        kind, value = _mcp_plan(messages, info)
        if kind == "tool":
            yield {0: DeltaToolCall(name=value[0], json_args=json.dumps(value[1]))}
        else:
            yield value

    return FunctionModel(respond, stream_function=stream, model_name="scripted-mcp")


def scripted_echo_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"Echo: {_last_prompt(messages)}")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        for chunk in _chunks(f"Echo: {_last_prompt(messages)}"):
            yield chunk

    return FunctionModel(respond, stream_function=stream, model_name="scripted-echo")


def _history_answer(messages: list[ModelMessage]) -> str:
    prompts = [
        p.content
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]
    answers = sum(1 for m in messages if isinstance(m, ModelResponse))
    return f"Turn {len(prompts)}. You said: {' | '.join(prompts)}. I answered {answers} times before."


def scripted_history_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(_history_answer(messages))])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        # One chunk: the first (and only) delta is the whole answer, so a crash right after it
        # still shows what history the crashed attempt was given.
        yield _history_answer(messages)

    return FunctionModel(respond, stream_function=stream, model_name="scripted-history")


def _context_answer(info: AgentInfo) -> str:
    return f"Instructions: {info.instructions or ''}"


def scripted_context_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(_context_answer(info))])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        yield _context_answer(info)

    return FunctionModel(respond, stream_function=stream, model_name="scripted-context")


MODERATION_FLAG = "FLAG_ME"


def _moderation_answer(messages: list[ModelMessage]) -> str:
    if MODERATION_FLAG in _last_prompt(messages):
        return json.dumps({"flagged": True, "categories": ["illicit"]})
    return json.dumps({"flagged": False, "categories": []})


def scripted_moderation_model(mode: str = "valid") -> FunctionModel:
    """`valid`: the verdict; `invalid`: a sentence that is not one; `empty`: an empty answer."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answer = {"valid": _moderation_answer(messages), "invalid": "I cannot classify this.", "empty": ""}[mode]
        return ModelResponse(parts=[TextPart(answer)])

    return FunctionModel(respond, model_name="scripted-moderation" if mode == "valid" else f"scripted-moderation-{mode}")


def _answered_before(messages: list[ModelMessage]) -> bool:
    # The agent re-prompts an empty answer with a user prompt ("Please return text."), so "retried"
    # means: a response already exists. Standalone runs only (history would count too).
    return any(isinstance(m, ModelResponse) for m in messages)


def _first_prompt(messages: list[ModelMessage]) -> str:
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart) and isinstance(p.content, str):
                    return p.content
    return ""


def scripted_think_first_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _answered_before(messages):
            return ModelResponse(parts=[ThinkingPart("pondering")])
        return ModelResponse(parts=[TextPart(f"Echo: {_first_prompt(messages)}")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        if not _answered_before(messages):
            yield {0: DeltaThinkingPart(content="pondering")}
            return
        yield f"Echo: {_first_prompt(messages)}"

    return FunctionModel(respond, stream_function=stream, model_name="scripted-think-first")


THINK_TOOL_THOUGHT = "SECRET plan"


def scripted_think_tool_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.function_tools and not _tool_returns(messages):
            return ModelResponse(parts=[ThinkingPart(THINK_TOOL_THOUGHT), ToolCallPart(info.function_tools[0].name, {})])
        return ModelResponse(parts=[TextPart("Done.")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        if info.function_tools and not _tool_returns(messages):
            yield {0: DeltaThinkingPart(content=THINK_TOOL_THOUGHT)}
            yield {1: DeltaToolCall(name=info.function_tools[0].name, json_args="{}")}
            return
        yield "Done."

    return FunctionModel(respond, stream_function=stream, model_name="scripted-think-tool")


RETRIEVE_TOOL = "retrieve"
_FIRST_SOURCE = re.compile(r"^\[1\] (.*) — (\S+)$", re.MULTILINE)


def _retrieve_args(prompt: str) -> dict[str, Any]:
    try:
        request = json.loads(prompt)
    except ValueError:
        request = None
    if isinstance(request, dict) and "search_query" in request:
        return request
    return {"search_query": prompt}


def _retrieve_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    offered = {t.name for t in info.function_tools}
    parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    retries = [p for p in parts if isinstance(p, RetryPromptPart) and p.tool_name == RETRIEVE_TOOL]
    returns = [r for r in _tool_returns(messages) if r.tool_name == RETRIEVE_TOOL]
    if retries:
        return "text", f"Retrieve error: {retries[-1].model_response()}"
    if returns:
        last = returns[-1]
        if last.outcome != "success":
            return "text", f"Retrieve error: {last.content}"
        found = _FIRST_SOURCE.search(str(last.content))
        if found is None:
            return "text", "No sources found."
        return "text", f"According to [1] {found.group(1)}: {found.group(2)}"
    if RETRIEVE_TOOL in offered:
        return "tool", _retrieve_args(_last_prompt(messages))
    return "text", f"Echo: {_last_prompt(messages)}"


def _retrieve_model_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    kind, value = _retrieve_plan(messages, info)
    if kind == "tool":
        return "tool", (RETRIEVE_TOOL, value)
    return _as_output(kind, value, info)


def scripted_retrieve_model() -> FunctionModel:
    return _planned_model(_retrieve_model_plan, "scripted-retrieve")


_SOURCE_LINE = re.compile(r"^\[(\d+)\] (.*) — (\S+)$", re.MULTILINE)


def _retrieve_multi_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    """`test:retrieve-multi`: the input is JSON `{"rounds": [[q, ...], ...]}` (each q a query string
    or retrieve arguments). Round i is one model response with one retrieve call per query (the
    agent runs them one after another, in order). After the last round it answers `Sources: ` and
    every numbered source line of every retrieve result of the turn, in result order, as
    `[n] <title>: <url>` joined by `; ` (`Sources: none` without any)."""
    offered = {t.name for t in info.function_tools}
    prompt = _last_prompt(messages)
    try:
        request = json.loads(prompt)
    except ValueError:
        request = None
    rounds = request.get("rounds") if isinstance(request, dict) else None
    if RETRIEVE_TOOL not in offered or not isinstance(rounds, list):
        return "text", f"Echo: {prompt}"
    # This turn's messages only: after the last user prompt.
    start = 0
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            start = i
    turn = messages[start:]
    done = sum(
        1 for m in turn if isinstance(m, ModelResponse) and any(isinstance(p, ToolCallPart) for p in m.parts)
    )
    if done < len(rounds):
        queries = rounds[done] if isinstance(rounds[done], list) else [rounds[done]]
        return "tools", [q if isinstance(q, dict) else {"search_query": str(q)} for q in queries]
    lines = [
        f"[{n}] {title}: {url}"
        for r in _tool_returns(turn) if r.tool_name == RETRIEVE_TOOL
        for n, title, url in _SOURCE_LINE.findall(str(r.content))
    ]
    return "text", "Sources: " + ("; ".join(lines) if lines else "none")


def _retrieve_multi_model_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    kind, value = _retrieve_multi_plan(messages, info)
    if kind == "tools":
        return "tools", [(RETRIEVE_TOOL, args) for args in value]
    return _as_output(kind, value, info)


def scripted_retrieve_multi_model() -> FunctionModel:
    return _planned_model(_retrieve_multi_model_plan, "scripted-retrieve-multi")


WORKING_MEMORY_TOOL = "update_working_memory"
_REMEMBER = re.compile(r"\bremember\s+([A-Za-z_][\w.-]*)=(\S+)", re.IGNORECASE)
_FORGET = re.compile(r"\bforget\s+([A-Za-z_][\w.-]*)", re.IGNORECASE)
_WM_BLOCK = re.compile(r"<working_memory[^>]*>\n(.*?)\n</working_memory>", re.DOTALL)


def _wm_note(info: AgentInfo) -> str | None:
    """The note in the instructions' working memory block, unescaped (eap_worker.memory.escape_note)."""
    found = _WM_BLOCK.search(info.instructions or "")
    return None if found is None else html.unescape(found.group(1))


def _wm_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _wm_template_content(note: str, pairs: list[tuple[str, str]], forget: list[str]) -> str:
    lines = note.split("\n") if note else []
    for key, value in pairs:
        pattern = re.compile(rf"^(\s*[-*]\s*){re.escape(key)}\s*:.*$", re.IGNORECASE)
        for i, line in enumerate(lines):
            found = pattern.match(line)
            if found:
                label = line[found.end(1):].split(":", 1)[0]
                lines[i] = f"{found.group(1)}{label}: {value}"
                break
        else:
            lines.append(f"- {key}: {value}")
    for key in forget:
        pattern = re.compile(rf"^\s*[-*]\s*{re.escape(key)}\s*:", re.IGNORECASE)
        lines = [line for line in lines if not pattern.match(line)]
    return "\n".join(lines)


def _memory_args(prompt: str, info: AgentInfo, tool_def: Any) -> dict[str, Any] | None:
    try:
        request = json.loads(prompt)
    except ValueError:
        request = None
    if isinstance(request, dict) and ("patch" in request or "content" in request):
        return request
    pairs = _REMEMBER.findall(prompt)
    forget = _FORGET.findall(prompt)
    if not pairs and not forget:
        return None
    properties = (tool_def.parameters_json_schema or {}).get("properties", {})
    if "patch" in properties:
        patch: dict[str, Any] = {key: _wm_value(value) for key, value in pairs}
        patch.update({key: None for key in forget})
        return {"patch": patch}
    return {"content": _wm_template_content(_wm_note(info) or "", pairs, forget)}


def _this_turn(messages: list[ModelMessage]) -> list[Any]:
    """The request parts from the current turn's user prompt on (a conversation's history holds
    earlier turns' tool returns, which must not count as this turn's)."""
    parts = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]
    starts = [i for i, p in enumerate(parts) if isinstance(p, UserPromptPart)]
    return parts[starts[-1]:] if starts else parts


def _memory_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    offered = {t.name: t for t in info.function_tools}
    parts = _this_turn(messages)
    retries = [p for p in parts if isinstance(p, RetryPromptPart) and p.tool_name == WORKING_MEMORY_TOOL]
    returns = [p for p in parts if isinstance(p, ToolReturnPart) and p.tool_name == WORKING_MEMORY_TOOL]
    note = _wm_note(info)
    shown = f"Working memory:\n{note if note is not None else '(none)'}"
    if retries:
        return "text", f"Memory error: {retries[-1].model_response()}\n{shown}"
    if returns:
        last = returns[-1]
        if last.outcome != "success":
            return "text", f"Memory error: {last.content}\n{shown}"
        return "text", f"{last.content}\n{shown}"
    if WORKING_MEMORY_TOOL in offered:
        args = _memory_args(_last_prompt(messages), info, offered[WORKING_MEMORY_TOOL])
        if args is not None:
            return "tool", args
    return "text", shown


def scripted_memory_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, value = _memory_plan(messages, info)
        return ModelResponse(parts=[ToolCallPart(WORKING_MEMORY_TOOL, value) if kind == "tool" else TextPart(value)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        kind, value = _memory_plan(messages, info)
        if kind == "tool":
            yield {0: DeltaToolCall(name=WORKING_MEMORY_TOOL, json_args=json.dumps(value))}
        else:
            yield value

    return FunctionModel(respond, stream_function=stream, model_name="scripted-memory")


# ----------------------------------------------------------------------------- scripted observer (RM.13)

_OBS_TURN_RE = re.compile(r'<turn number="(\d+)" date="([^"]*)">\n(.*?)\n</turn>', re.DOTALL)
_OBS_NOTES_RE = re.compile(r'<notes turns="(\d+)-(\d+)"[^>]*>')
_OBS_DATED_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) ", re.MULTILINE)


def scripted_observer_answer(prompt: str) -> str:
    """`test:observer`'s answer to an eap-observer message (see the module docstring)."""
    if prompt.startswith("TASK: reflect"):
        spans = [(int(a), int(b)) for a, b in _OBS_NOTES_RE.findall(prompt)]
        dates = _OBS_DATED_RE.findall(html.unescape(prompt))
        lo = min((a for a, _ in spans), default=0)
        hi = max((b for _, b in spans), default=0)
        return json.dumps({"observations": [{
            "date": max(dates, default=""), "priority": "high",
            "text": f"reflection of {len(spans)} notes over turns {lo}-{hi}",
        }]})
    notes = []
    for number, date, body in _OBS_TURN_RE.findall(prompt):
        user = ""
        for line in html.unescape(body).splitlines():
            if line.startswith("User: "):
                user = line[len("User: "):]
                break
        notes.append({"date": date, "priority": "medium", "text": f"turn {number}: {user[:40]}"})
    return json.dumps({"observations": notes})


def scripted_observer_model(mode: str = "valid") -> FunctionModel:
    """`valid`: scripted_observer_answer; `invalid`: a sentence that is not the notes JSON."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answer = scripted_observer_answer(_last_prompt(messages)) if mode == "valid" else "I cannot summarise this."
        return ModelResponse(parts=[TextPart(answer)])

    return FunctionModel(respond, model_name="scripted-observer" if mode == "valid" else f"scripted-observer-{mode}")


# ----------------------------------------------------------------------------- agent capabilities (AC.2)


def _turn_messages(messages: list[ModelMessage]) -> list[ModelMessage]:
    """The messages from the current turn's user prompt on (earlier turns are history)."""
    start = 0
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            start = i
    return messages[start:]


def _prompt_json(messages: list[ModelMessage]) -> dict[str, Any]:
    """The last user prompt parsed as a JSON object, else {}."""
    try:
        request = json.loads(_last_prompt(messages))
    except ValueError:
        return {}
    return request if isinstance(request, dict) else {}


def _content_text(content: Any) -> str:
    return content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)


def _response(kind: str, value: Any) -> ModelResponse:
    """A plan as a ModelResponse: 'tools' -> [(name, args), ...] (one response, several calls);
    'tool' -> (name, args); anything else -> text."""
    if kind == "tools":
        return ModelResponse(parts=[ToolCallPart(name, args) for name, args in value])
    if kind == "tool":
        return ModelResponse(parts=[ToolCallPart(value[0], value[1])])
    return ModelResponse(parts=[TextPart(value)])


def _stream_items(kind: str, value: Any) -> list[str | DeltaToolCalls]:
    """The same plan as stream items (one DeltaToolCall index per call)."""
    if kind == "tools":
        return [{i: DeltaToolCall(name=name, json_args=json.dumps(args)) for i, (name, args) in enumerate(value)}]
    if kind == "tool":
        return [{0: DeltaToolCall(name=value[0], json_args=json.dumps(value[1]))}]
    return [value]


def _planned_model(plan: Any, model_name: str, **kwargs: Any) -> FunctionModel:
    """A FunctionModel answering `plan(messages, info) -> (kind, value)`, streamed or not."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return _response(*plan(messages, info))

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        for item in _stream_items(*plan(messages, info)):
            yield item

    cls = kwargs.pop("cls", FunctionModel)
    return cls(respond, stream_function=stream, model_name=model_name, **kwargs)


def _schema_types(schema: dict[str, Any]) -> list[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return [declared]
    if isinstance(declared, list):
        return [t for t in declared if isinstance(t, str)]
    return []


def schema_instance(schema: Any, text: str | None = None) -> Any:
    """A deterministic instance of a JSON Schema in the output subset (eap_schemas' subset): `const`,
    else the first `enum` value, else by type (the first non-null type of a two-type list): string
    `"x"` (or `text` when given; padded with `x` to `minLength`, cut at `maxLength`), number and
    integer 1 (moved into `minimum`..`maximum`), boolean true, null, arrays with one item (at least
    `minItems`, at most `maxItems`), and objects with their required properties only, built
    recursively. Untyped: object if it has `properties`, array if it has `items`, else a string."""
    if not isinstance(schema, dict):
        return "x" if text is None else text
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    types = _schema_types(schema)
    kind = next((t for t in types if t != "null"), "null" if types else None)
    if kind is None:
        kind = "object" if "properties" in schema else "array" if "items" in schema else "string"
    if kind == "null":
        return None
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        low, high = schema.get("minimum"), schema.get("maximum")
        value: float = 1
        if isinstance(low, (int, float)) and not isinstance(low, bool):
            value = max(value, math.ceil(low) if kind == "integer" else low)
        if isinstance(high, (int, float)) and not isinstance(high, bool):
            value = min(value, math.floor(high) if kind == "integer" else high)
        return int(value) if kind == "integer" else value
    if kind == "string":
        result = "x" if text is None else text
        min_length = int(schema.get("minLength") or 0)
        if len(result) < min_length:
            result += "x" * (min_length - len(result))
        if isinstance(schema.get("maxLength"), int):
            result = result[: schema["maxLength"]]
        return result
    if kind == "array":
        count = max(1, int(schema.get("minItems") or 0))
        if isinstance(schema.get("maxItems"), int):
            count = min(count, schema["maxItems"])
        return [schema_instance(schema.get("items", {}), text) for _ in range(count)]
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {name: schema_instance(properties.get(name, {}), text) for name in required}


def _as_output(kind: str, value: Any, info: AgentInfo) -> tuple[str, Any]:
    """A final text answer through the output tool when one is offered: the tool is called with
    `schema_instance(schema, text=answer)`, so every free string field holds the answer (and its
    `[n]` source markers). Tool calls, and text when no output tool is offered, pass unchanged."""
    if kind != "text" or not info.output_tools:
        return kind, value
    tool = info.output_tools[0]
    return "tool", (tool.name, schema_instance(tool.parameters_json_schema, text=value))


def _wrong_value(schema: Any) -> tuple[bool, Any]:
    """(True, a value of the wrong type for `schema`), or (False, None) when anything is valid."""
    if not isinstance(schema, dict):
        return False, None
    types = set(_schema_types(schema))
    constrained = bool(types) or "enum" in schema or "const" in schema
    if not constrained:
        return False, None
    if "string" in types:
        return True, 12345
    return True, "wrong type"


def schema_violation(schema: Any) -> Any:
    """A candidate that violates `schema` by type: every constrained property holds a value of the
    wrong type (a string schema gets 12345, anything else `"wrong type"`). When no property can be
    violated by type, the candidate is a JSON array (`[]`), which is never a valid object."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    bad: dict[str, Any] = {}
    for name, sub in (properties or {}).items():
        violated, value = _wrong_value(sub)
        if violated:
            bad[name] = value
    return bad if bad else []


def _structured_plan(messages: list[ModelMessage], info: AgentInfo, never_valid: bool) -> tuple[str, Any]:
    """('tool', (output tool name, args)) in tool mode, ('json', text) in native or prompted mode,
    ('text', echo) when the run asks for no structured output."""
    params = info.model_request_parameters
    turn = _turn_messages(messages)
    request = _prompt_json(messages)
    if info.output_tools:
        tool = info.output_tools[0]
        schema: Any = tool.parameters_json_schema
        candidates = sum(
            1
            for m in turn
            if isinstance(m, ModelResponse) and any(isinstance(p, ToolCallPart) and p.tool_name == tool.name for p in m.parts)
        )
    elif params.output_mode in ("native", "prompted") and params.output_object is not None:
        tool = None
        schema = params.output_object.json_schema
        candidates = sum(
            1 for m in turn if isinstance(m, ModelResponse) and any(isinstance(p, TextPart) and p.content for p in m.parts)
        )
    else:
        return "text", f"Echo: {_last_prompt(messages)}"
    if never_valid or (request.get("bad_first") and candidates == 0):
        value = schema_violation(schema)
    elif "output" in request:
        value = request["output"]
    else:
        value = schema_instance(schema)
    if tool is not None:
        return "tool", (tool.name, value)
    return "json", json.dumps(value, sort_keys=True)


def scripted_structured_model(never_valid: bool = False, native: bool = True) -> FunctionModel:
    """`test:structured`, `test:structured-never` (`never_valid`) and `test:structured-no-native`
    (`native=False`: a profile without JSON-schema output, so native mode is refused at run start)."""
    name = "scripted-structured-never" if never_valid else "scripted-structured" if native else "scripted-structured-no-native"
    extra: dict[str, Any] = {} if native else {"profile": ModelProfile(supports_json_schema_output=False)}
    return _planned_model(lambda messages, info: _structured_plan(messages, info, never_valid), name, **extra)


def _offered_tool(requested: str, offered: list[str]) -> str:
    """The offered name for `requested`: itself, a built-in id (`tickets.create`), an MCP ref
    (`<server>/<tool>`), or a unique offered name ending in `__<requested>`; else `requested`."""
    if requested in offered:
        return requested
    from eap_schemas.mcp import mcp_tool_name
    from eap_schemas.tools import builtin_tool_name

    server, slash, tool = requested.partition("/")
    candidate = mcp_tool_name(server, tool) if slash else builtin_tool_name(requested)
    if candidate in offered:
        return candidate
    matches = [n for n in offered if n.endswith(f"__{requested}")]
    return matches[0] if len(matches) == 1 else requested


def _approval_calls(request: dict[str, Any], offered: list[str]) -> list[tuple[str, dict[str, Any]]]:
    """The calls asked for: `{"calls": [{"tool", "args"}, ...]}`, `{"tool", "args"}`, or the first
    offered tool with `{}`."""
    entries = request.get("calls") if isinstance(request.get("calls"), list) else [request]
    calls: list[tuple[str, dict[str, Any]]] = []
    for entry in entries:
        entry = entry if isinstance(entry, dict) else {}
        requested = entry.get("tool")
        if isinstance(requested, str) and requested:
            tool = _offered_tool(requested, offered)
        elif offered:
            tool = offered[0]
        else:
            continue
        calls.append((tool, entry.get("args") if isinstance(entry.get("args"), dict) else {}))
    return calls


def _approval_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    offered = [t.name for t in info.function_tools]
    turn = _turn_messages(messages)
    parts = _this_turn(messages)
    returns = [p for p in parts if isinstance(p, ToolReturnPart)]
    retries = [p for p in parts if isinstance(p, RetryPromptPart) and p.tool_name]
    if returns:
        order = [
            p.tool_call_id for m in turn if isinstance(m, ModelResponse) for p in m.parts if isinstance(p, ToolCallPart)
        ]
        rank = {call_id: i for i, call_id in enumerate(order)}
        returns.sort(key=lambda r: rank.get(r.tool_call_id, len(rank)))
        return "text", "\n".join(
            f"Tool {r.tool_name} outcome={r.outcome} content={_content_text(r.content)}" for r in returns
        )
    if retries:
        last_retry = retries[-1]
        return "text", f"Tool {last_retry.tool_name} outcome=retry content={last_retry.model_response()}"
    calls = _approval_calls(_prompt_json(messages), offered)
    if not calls:
        return "text", "No tool offered."
    return "tools", calls


def scripted_approval_model() -> FunctionModel:
    return _planned_model(_approval_plan, "scripted-approval")


def _frontend_answer(answer: str, info: AgentInfo) -> tuple[str, Any]:
    """A final answer in the run's output form: the output tool, native/prompted JSON, or text."""
    if info.output_tools:
        return _as_output("text", answer, info)
    params = info.model_request_parameters
    if params.output_mode in ("native", "prompted") and params.output_object is not None:
        return "text", json.dumps(schema_instance(params.output_object.json_schema, text=answer), sort_keys=True)
    return "text", answer


def _frontend_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    """`test:frontend_tool`: see the module docstring."""
    from eap_worker.client_tools import ARGS_RETRY_HEADING

    client = [t for t in info.function_tools if t.kind == "external"]
    # A client tool's result: any return for a tool that is not a server tool of this run (a REST
    # turn declares no client tools, yet answers the previous turn's pending calls).
    server = {t.name for t in info.function_tools if t.kind != "external"} | {t.name for t in info.output_tools}
    last = messages[-1] if messages else None
    parts = list(last.parts) if isinstance(last, ModelRequest) else []
    for part in parts:
        if not isinstance(part, (ToolReturnPart, RetryPromptPart)) or not part.tool_name or part.tool_name in server:
            continue
        content = part.content if isinstance(part.content, str) else _content_text(part.content)
        retried = isinstance(part, RetryPromptPart) or part.outcome in ("retried", "failed")
        if not retried:
            return _frontend_answer(f"tool said: {content}", info)
        if not content.startswith(ARGS_RETRY_HEADING):
            return _frontend_answer(f"tool said: error: {content}", info)
    if not client:
        return _frontend_answer("No client tool offered.", info)
    request = _prompt_json(messages)
    tool = next((t for t in client if t.name == request.get("tool")), client[0])
    attempts = sum(
        1
        for m in _turn_messages(messages)
        if isinstance(m, ModelRequest)
        for p in m.parts
        if (isinstance(p, RetryPromptPart) or (isinstance(p, ToolReturnPart) and p.outcome == "retried"))
        and p.tool_name == tool.name
    )
    if "args" in request:
        args: Any = request["args"]
    elif request.get("bad_first") and attempts == 0:
        args = schema_violation(tool.parameters_json_schema)
    else:
        args = schema_instance(tool.parameters_json_schema)
    calls: list[tuple[str, Any]] = []
    server = request.get("with")
    if isinstance(server, str):
        offered = [t.name for t in info.function_tools if t.kind != "external"]
        calls.append((_offered_tool(server, offered), request.get("with_args") or {}))
    calls.append((tool.name, args))
    return "tools", calls


def scripted_frontend_tool_model() -> FunctionModel:
    """`test:frontend_tool`: see the module docstring."""
    return _planned_model(_frontend_plan, "scripted-frontend-tool")


TEXT_TOOL_PREFIX = "Let me check."


def scripted_text_tool_model() -> FunctionModel:
    """`test:text-tool`: see the module docstring."""

    def first_turn(messages: list[ModelMessage]) -> bool:
        return not any(isinstance(p, ToolReturnPart) for p in _this_turn(messages))

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if info.function_tools and first_turn(messages):
            return ModelResponse(parts=[
                TextPart(TEXT_TOOL_PREFIX), ToolCallPart(info.function_tools[0].name, {}, tool_call_id="text-tool-0")
            ])
        return ModelResponse(parts=[TextPart("Done.")])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if info.function_tools and first_turn(messages):
            yield TEXT_TOOL_PREFIX
            yield {0: DeltaToolCall(name=info.function_tools[0].name, json_args="{}", tool_call_id="text-tool-0")}
            return
        yield "Done."

    return FunctionModel(respond, stream_function=stream, model_name="scripted-text-tool")


def scripted_failing_model(status_code: int) -> FunctionModel:
    """`test:fail-<status>`: every request raises `ModelHTTPError(status_code)` before any output
    (503 is a `ModelAPIError` that FallbackModel falls back on; so is 400)."""
    model_name = f"scripted-fail-{status_code}"

    def fail() -> ModelHTTPError:
        return ModelHTTPError(status_code, model_name, {"error": f"scripted failure {status_code}"})

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise fail()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        raise fail()
        yield ""  # pragma: no cover - makes this an async generator

    return FunctionModel(respond, stream_function=stream, model_name=model_name)


LOOP_NO_TOOL_ANSWER = "Loop: no tool offered."


def _loop_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    if info.function_tools:
        return "tool", (info.function_tools[0].name, {})
    return "text", LOOP_NO_TOOL_ANSWER


def scripted_loop_model() -> FunctionModel:
    """`test:loop`: calls the first offered tool with `{}` on every request and never answers while a
    tool is offered (for request and tool-call limits); give it a tool that accepts `{}`, such as
    `clock.now`. Without tools it answers LOOP_NO_TOOL_ANSWER."""
    return _planned_model(_loop_plan, "scripted-loop")


class _FixedUsageStreamedResponse(FunctionStreamedResponse):
    """Base of the per-model streamed response classes that report the class's `_fixed_usage`
    instead of FunctionModel's estimate (FixedUsageFunctionModel builds one subclass per model)."""

    __slots__ = ()
    _fixed_usage: RequestUsage

    @property
    def usage(self) -> RequestUsage:  # type: ignore[override]
        fixed = type(self)._fixed_usage
        return RequestUsage(input_tokens=fixed.input_tokens, output_tokens=fixed.output_tokens)


class FixedUsageFunctionModel(FunctionModel):
    """A FunctionModel whose every response, streamed or not, reports exactly `usage`. Non-streamed:
    the response carries the usage (FunctionModel keeps a non-empty usage). Streamed: the response
    becomes this model's `_FixedUsageStreamedResponse` subclass (no instance state is added)."""

    def __init__(self, function: Any, *args: Any, usage: RequestUsage, **kwargs: Any) -> None:
        def with_usage(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            response = function(messages, info)
            response.usage = RequestUsage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
            return response

        super().__init__(with_usage, *args, **kwargs)
        self._stream_class = type(
            "FixedUsageStreamedResponse", (_FixedUsageStreamedResponse,), {"__slots__": (), "_fixed_usage": usage}
        )

    @asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async with super().request_stream(*args, **kwargs) as response:
            response.__class__ = self._stream_class
            yield response


PRICED_USAGE = (1000, 500)  # (input_tokens, output_tokens) of every `test:priced` request


def _priced_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    """`Echo: <prompt>`; with prompt JSON `{"rounds": k}`, first the first offered tool with `{}`
    until this turn holds k tool returns (one call per request)."""
    rounds = _prompt_json(messages).get("rounds")
    if isinstance(rounds, int) and not isinstance(rounds, bool) and info.function_tools:
        done = sum(1 for p in _this_turn(messages) if isinstance(p, ToolReturnPart))
        if done < rounds:
            return "tool", (info.function_tools[0].name, {})
    return "text", f"Echo: {_last_prompt(messages)}"


def scripted_priced_model() -> FunctionModel:
    """`test:priced`: see the module docstring. Fixed usage PRICED_USAGE on every request."""
    return _planned_model(
        _priced_plan,
        "scripted-priced",
        cls=FixedUsageFunctionModel,
        usage=RequestUsage(input_tokens=PRICED_USAGE[0], output_tokens=PRICED_USAGE[1]),
    )


REFINE_USAGE = (100, 20)  # (input_tokens, output_tokens) of every `test:refine` request


def scripted_refine_answer(prompt: str, schema: Any = None) -> dict[str, Any]:
    """`test:refine`'s answer to `prompt` (see the module docstring); `schema` is the output schema,
    whose `additionalProperties: false` without an `after` property drops `after`."""
    try:
        request = json.loads(prompt)
    except ValueError:
        request = {}
    request = request if isinstance(request, dict) else {}
    attempt = request.get("attempt")
    attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
    feedback = request.get("feedback")
    feedback = feedback if isinstance(feedback, str) else ""
    answer: dict[str, Any] = {"draft": f"attempt {attempt + 1}", "after": feedback}
    if isinstance(schema, dict) and schema.get("additionalProperties") is False:
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        answer = {k: v for k, v in answer.items() if k in properties or k == "draft"}
    return answer


def _refine_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    params = info.model_request_parameters
    if info.output_tools:
        tool = info.output_tools[0]
        return "tool", (tool.name, scripted_refine_answer(_last_prompt(messages), tool.parameters_json_schema))
    schema = params.output_object.json_schema if params.output_object is not None else None
    return "json", json.dumps(scripted_refine_answer(_last_prompt(messages), schema), sort_keys=True)


def scripted_refine_model() -> FunctionModel:
    """`test:refine`: see the module docstring. Fixed usage REFINE_USAGE on every request."""
    return _planned_model(
        _refine_plan,
        "scripted-refine",
        cls=FixedUsageFunctionModel,
        usage=RequestUsage(input_tokens=REFINE_USAGE[0], output_tokens=REFINE_USAGE[1]),
    )


JUDGE_MARKER = "MUST_CONTAIN:"
JUDGE_USAGE = (200, 50)  # (input_tokens, output_tokens) of every `test:judge` request
JUDGE_NO_OUTPUT_REASON = "The judge input has no <output> block."
_JUDGE_MARKER_RE = re.compile(r'MUST_CONTAIN:(?:"([^"]+)"|([^\s<]+))')
_JUDGE_OUTPUT_RE = re.compile(r"<output(?:\s[^>]*)?>\n?(.*?)\n?</output>", re.DOTALL)
_JUDGE_DATA_BLOCK_RE = re.compile(r"<(input|output|expected(?:_output)?)(?:\s[^>]*)?>.*?</\1>", re.DOTALL)


def scripted_judge_verdict(prompt: str, instructions: str = "") -> dict[str, Any]:
    """`test:judge`'s verdict `{"reason", "pass", "score"}`.

    - The output is the first `<output ...>…</output>` block of the user `prompt` (never the
      instructions), HTML-unescaped after it is extracted. No block: a fail with
      JUDGE_NO_OUTPUT_REASON.
    - The marker `MUST_CONTAIN:<text>` (`<text>` runs to whitespace or `<`; `MUST_CONTAIN:"two
      words"` for spaces) is taken only from text outside the `<input>`, `<output>` and `<expected>`
      (or `<expected_output>`) blocks, in the instructions and then the prompt, unescaped. None: fail.
    - Pass and score 1.0 when the output contains the marker's text, else fail and 0.0; `reason`
      names the marker."""
    block = _JUDGE_OUTPUT_RE.search(prompt)
    if block is None:
        return {"reason": JUDGE_NO_OUTPUT_REASON, "pass": False, "score": 0.0}
    output = html.unescape(block.group(1))
    outside = html.unescape(_JUDGE_DATA_BLOCK_RE.sub("", f"{instructions}\n{prompt}"))
    found = _JUDGE_MARKER_RE.search(outside)
    if found is None:
        return {"reason": "The rubric has no MUST_CONTAIN marker.", "pass": False, "score": 0.0}
    needle = found.group(1) or found.group(2)
    if needle in output:
        return {"reason": f'The output contains the MUST_CONTAIN marker "{needle}".', "pass": True, "score": 1.0}
    return {"reason": f'The output does not contain the MUST_CONTAIN marker "{needle}".', "pass": False, "score": 0.0}


def _judge_plan(messages: list[ModelMessage], info: AgentInfo) -> tuple[str, Any]:
    prompts = [
        p.content
        for m in _turn_messages(messages)
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart) and isinstance(p.content, str)
    ]
    verdict = scripted_judge_verdict("\n".join(prompts), info.instructions or "")
    if info.output_tools:
        return "tool", (info.output_tools[0].name, verdict)
    return "json", json.dumps(verdict, sort_keys=True)


def scripted_judge_model() -> FunctionModel:
    """`test:judge`: the scripted_judge_verdict, through the output tool when one is offered (the
    judge's `ToolOutput(JudgeVerdict)`), else as JSON text. Fixed usage JUDGE_USAGE."""
    return _planned_model(
        _judge_plan,
        "scripted-judge",
        cls=FixedUsageFunctionModel,
        usage=RequestUsage(input_tokens=JUDGE_USAGE[0], output_tokens=JUDGE_USAGE[1]),
    )


def litellm_model(name: str, settings: WorkerSettings) -> Model:
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if not settings.litellm_base_url:
        raise RuntimeError("EAP_LITELLM_BASE_URL (or OPENAI_BASE_URL) is not set; cannot build a litellm model")
    if not settings.litellm_api_key:
        raise RuntimeError("EAP_LITELLM_API_KEY (or LITELLM_API_KEY) is not set; cannot build a litellm model")
    client = AsyncOpenAI(
        base_url=settings.litellm_base_url,
        api_key=settings.litellm_api_key,
        max_retries=0,  # transport retries off: the DBOS model step owns retrying
    )
    return OpenAIChatModel(name, provider=OpenAIProvider(openai_client=client))


# ----------------------------------------------------------------------------- scripted embedders

# name -> (native dimensions, reports usage)
HASH_EMBEDDING_MODELS: dict[str, tuple[int, bool]] = {
    "hash": (1536, True),
    "hash768": (768, True),
    "hash-nousage": (1536, False),
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def hash_embedding(text: str, dimensions: int) -> list[float]:
    """Deterministic hashed bag of words, L2-normalised. Each lower-cased word adds its count at a
    blake2b-chosen index with a blake2b-chosen sign, so shared words always raise the dot product.
    Text without words gives the unit vector e0 (never a zero vector, which has no cosine)."""
    vec = [0.0] * dimensions
    for word in _WORD_RE.findall(text.lower()):
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vec[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


class HashEmbeddingModel(PaiEmbeddingModel):
    """The `test:hash*` scripted embedders as a Pydantic AI embedding model (so they go through
    `Embedder`, its settings and its instrumentation exactly like a provider model)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self._dimensions, self._reports_usage = HASH_EMBEDDING_MODELS[name]

    @property
    def model_name(self) -> str:
        return self._name

    @property
    def system(self) -> str:
        return "test"

    async def embed(
        self, inputs: str | Sequence[str], *, input_type: EmbedInputType, settings: EmbeddingSettings | None = None
    ) -> EmbeddingResult:
        texts, _settings = self.prepare_embed(inputs, settings)
        usage = (
            RequestUsage(input_tokens=sum(approx_tokens(t) for t in texts)) if self._reports_usage else RequestUsage()
        )
        return EmbeddingResult(
            embeddings=[hash_embedding(t, self._dimensions) for t in texts],
            inputs=texts,
            input_type=input_type,
            usage=usage,
            model_name=self._name,
            provider_name="test",
        )


def scripted_embedding_model(name: str) -> HashEmbeddingModel | None:
    """The scripted embedder for `test:<name>`, or None if there is none of that name."""
    return HashEmbeddingModel(name) if name in HASH_EMBEDDING_MODELS else None


def fallback_chain(spec: Any, model_id: str) -> tuple[str, ...]:
    """The model ids a request for `model_id` tries, in order (AC.5): `(primary, *fallbacks)` when
    `model_id` is the spec's primary model and the spec has `fallback_models`, else `(model_id,)`
    (no fallback, or a per-run override: a different id). Pure."""
    fallbacks = tuple(getattr(spec, "fallback_models", ()) or ())
    primary = model_id_for(spec.model.provider, spec.model.name) if spec is not None else None
    if not fallbacks or model_id != primary:
        return (model_id,)
    return (model_id, *(model_id_for(ref.provider, ref.name) for ref in fallbacks))


def make_resolver(settings_getter: Any, *, fallback: bool = False):
    """Build the ResolveModelId callback. `settings_getter()` returns the live WorkerSettings.

    `fallback=True` (eap-agent only, AC.5): a request for the run spec's primary model resolves to a
    FallbackModel over `fallback_chain(ctx.deps.spec, model_id)`, each member resolved (and
    allowlist-checked) on its own."""

    def resolve_model(ctx: ModelResolutionContext[Any], model_id: str) -> Model | None:
        if fallback:
            chain = fallback_chain(getattr(ctx.deps, "spec", None), model_id)
            if len(chain) > 1:
                members = [_member(ctx, member_id) for member_id in chain]
                return FallbackModel(members[0], *members[1:])
        return resolve_one(ctx, model_id)

    def _member(ctx: ModelResolutionContext[Any], model_id: str) -> Model | str:
        # A provider this resolver leaves to infer_model is passed as its string (FallbackModel
        # infers it), exactly as the bare resolver would.
        model = resolve_one(ctx, model_id)
        return model if model is not None else model_id

    def resolve_one(ctx: ModelResolutionContext[Any], model_id: str) -> Model | None:
        if model_id == PLACEHOLDER_MODEL:
            raise RuntimeError("agent run started without a per-run model")
        provider, _, name = model_id.partition(":")
        settings_getter().allowed_models.check(provider, name)  # raises ModelNotAllowed
        if provider == "test":
            if name == "ticket":
                return scripted_ticket_model()
            if name == "first-tool":
                return scripted_first_tool_model()
            if name == "history":
                return scripted_history_model()
            if name == "mcp":
                return scripted_mcp_model()
            if name == "context":
                return scripted_context_model()
            if name == "moderation":
                return scripted_moderation_model()
            if name == "moderation-invalid":
                return scripted_moderation_model("invalid")
            if name == "moderation-empty":
                return scripted_moderation_model("empty")
            if name == "think-first":
                return scripted_think_first_model()
            if name == "think-tool":
                return scripted_think_tool_model()
            if name == "retrieve":
                return scripted_retrieve_model()
            if name == "retrieve-multi":
                return scripted_retrieve_multi_model()
            if name == "memory":
                return scripted_memory_model()
            if name == "observer":
                return scripted_observer_model()
            if name == "observer-invalid":
                return scripted_observer_model("invalid")
            if name == "structured":
                return scripted_structured_model()
            if name == "structured-never":
                return scripted_structured_model(never_valid=True)
            if name == "structured-no-native":
                return scripted_structured_model(native=False)
            if name == "approval":
                return scripted_approval_model()
            if name == "fail-503":
                return scripted_failing_model(503)
            if name == "fail-400":
                return scripted_failing_model(400)
            if name == "loop":
                return scripted_loop_model()
            if name == "priced":
                return scripted_priced_model()
            if name == "judge":
                return scripted_judge_model()
            if name == "refine":
                return scripted_refine_model()
            if name == "text-tool":
                return scripted_text_tool_model()
            if name == "frontend_tool":
                return scripted_frontend_tool_model()
            return scripted_echo_model()
        if provider == "litellm":
            return litellm_model(name, settings_getter())
        return None  # defer to infer_model

    return resolve_model
