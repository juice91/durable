"""Per-run model resolution: a model-name string from the spec -> a Model, built inside the model step.

Model ids are `"{spec.model.provider}:{spec.model.name}"`:

- `test:ticket`  scripted model: calls `tickets__create` once (if the tool is offered), then answers
                 with streamed text naming the ticket. Deterministic; for tests and local demos.
- `test:first-tool` scripted model: calls the first offered tool once with `{}`, then answers.
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

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
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
            return scripted_echo_model()
        if provider == "litellm":
            return litellm_model(name, settings_getter())
        return None  # defer to infer_model

    return resolve_model
