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
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

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
from pydantic_ai.usage import RequestUsage

from eap_schemas.knowledge import approx_tokens
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.models.function import AgentInfo, DeltaThinkingPart, DeltaToolCall, DeltaToolCalls, FunctionModel

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


def scripted_retrieve_model() -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, value = _retrieve_plan(messages, info)
        return ModelResponse(parts=[ToolCallPart(RETRIEVE_TOOL, value) if kind == "tool" else TextPart(value)])

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        kind, value = _retrieve_plan(messages, info)
        if kind == "tool":
            yield {0: DeltaToolCall(name=RETRIEVE_TOOL, json_args=json.dumps(value))}
        else:
            yield value

    return FunctionModel(respond, stream_function=stream, model_name="scripted-retrieve")


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


def make_resolver(settings_getter: Any):
    """Build the ResolveModelId callback. `settings_getter()` returns the live WorkerSettings."""

    def resolve_model(ctx: ModelResolutionContext[Any], model_id: str) -> Model | None:
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
            if name == "memory":
                return scripted_memory_model()
            return scripted_echo_model()
        if provider == "litellm":
            return litellm_model(name, settings_getter())
        return None  # defer to infer_model

    return resolve_model
