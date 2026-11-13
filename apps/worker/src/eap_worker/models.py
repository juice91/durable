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
- `test:<other>` scripted model that streams a short text answer echoing the prompt, no tools.
- `litellm:<name>` OpenAI-compatible chat model against EAP_LITELLM_BASE_URL (fallback OPENAI_BASE_URL)
                 and EAP_LITELLM_API_KEY (fallback LITELLM_API_KEY), e.g. `litellm:qwen3.8-thinking`,
                 with SDK (transport) retries off; DBOS's model step retry policy owns retrying.
- anything else is left to Pydantic AI's `infer_model`.

Every id must also pass the platform model allowlist (EAP_ALLOWED_MODELS, default `test:*`). The
workflow checks it before the agent starts; the resolver checks it again so no code path can build
a model that the policy forbids.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
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
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

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


def make_resolver(settings_getter: Any):
    """Build the ResolveModelId callback. `settings_getter()` returns the live WorkerSettings."""

    def resolve_model(ctx: ModelResolutionContext[Any], model_id: str) -> Model | None:
        if model_id == PLACEHOLDER_MODEL:
            raise RuntimeError("agent run started without a per-run model from the spec")
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
            return scripted_echo_model()
        if provider == "litellm":
            return litellm_model(name, settings_getter())
        return None  # defer to infer_model

    return resolve_model
