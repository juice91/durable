"""The durable agent run: one generic DBOSDurability agent driven by one DBOS workflow per run.

Hand-off contract (docs/plans/2026-11-11-milestone-1-vertical-slice.md): the API enqueues
`eap.agent_run` on queue `eap-agent-runs` with workflow_id = run_id and positional args
`(run_id, tenant_id, agent_id, version, spec_json, input_text)`.

Step layout of a run (each line is a DBOS step, so recovery replays recorded results):

    eap.start_run                       ensure the runs row, status -> running
    eap.append_event   start            run_started
    eap-agent__dynamic_toolset__eap-spec-tools.get_tools
    eap-agent__model.request_stream     model request n (deltas NOTIFY'd live inside)
    eap.append_event   model:{n}:final  model_message (from the checkpointed response)
    eap.append_event   tool:{id}:call   tool_call (before the tool runs)
    eap-agent__dynamic_toolset__eap-spec-tools.call_tool   tickets.create etc.
    eap.append_event   tool:{id}:result tool_result
    ... (next model request) ...
    eap.append_event   end              run_completed | run_failed
    eap.finish_run                      status -> completed | failed | needs_attention

Durable events are written by our own steps from replayed data (never from inside the model
step), so a re-executed model request can never leave a stored message that differs from the
checkpointed one. Event keys are deterministic, and EventLog.append is idempotent per key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID, uuid4

from dbos import DBOS
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ResolveModelId
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import DynamicToolset, FunctionToolset
from pydantic_core import to_jsonable_python

from eap_runtime import RunDeps, SideEffectOutcomeUnknown, check_spec_tools, toolset_for_spec
from eap_schemas import AgentSpec
from eap_worker.config import WORKFLOW_NAME
from eap_worker.crash import crash_point
from eap_worker.models import PLACEHOLDER_MODEL, make_resolver, model_id_for
from eap_worker.runtime import WorkerDeps, get_runtime

log = logging.getLogger("eap_worker")

AGENT_NAME = "eap-agent"
TOOLSET_ID = "eap-spec-tools"
HEARTBEAT_SECONDS = 5.0


# --------------------------------------------------------------------------- agent (one, generic)


def _instructions(ctx: RunContext[WorkerDeps]) -> str:
    return ctx.deps.spec.instructions


def _toolset_for_run(ctx: RunContext[WorkerDeps]) -> FunctionToolset[RunDeps]:
    """Deterministic: only the published spec's tools, each checked against registry + allowlist."""
    return toolset_for_spec(ctx.deps.spec, get_runtime().registry, ctx.deps.allowed_tools)


def _text_of(event: Any) -> str | None:
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content or None
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta or None
    return None


async def _publish_deltas(ctx: RunContext[WorkerDeps], events: Any) -> None:
    """DBOSDurability event_stream_handler.

    Model events arrive here live, inside the model-request step; each execution of that step
    (first try, DBOS retry, or re-execution after a crash) gets a fresh `attempt` uuid so the UI can
    drop partial text from an abandoned attempt. Deltas go out over NOTIFY and are never stored.
    Workflow-side events (tool calls/results) also reach this handler, in their own steps; they
    carry no text and are ignored here, because durable events are written by `eap.append_event`.
    """
    attempt = str(uuid4())
    model_request = ctx.run_step - 1  # run_step is 1 for the first model request of a run
    run_id = str(ctx.deps.run_id)
    rt = get_runtime()
    async for event in events:
        text = _text_of(event)
        if text:
            await asyncio.to_thread(rt.publish_delta, run_id, model_request, attempt, text)
            crash_point("model_request")


agent: Agent[WorkerDeps, str] = Agent(
    PLACEHOLDER_MODEL,  # placeholder; each run passes model=<spec model id>, resolved inside the model step
    name=AGENT_NAME,
    deps_type=WorkerDeps,
    instructions=_instructions,
    toolsets=[DynamicToolset(_toolset_for_run, id=TOOLSET_ID)],
    model_settings=ModelSettings(max_retries=0),
    capabilities=[
        ResolveModelId(make_resolver(lambda: get_runtime().settings)),
        DBOSDurability(
            parallel_execution_mode="sequential",
            event_stream_handler=_publish_deltas,
            model_step_config={"retries_allowed": True, "max_attempts": 2, "interval_seconds": 1.0, "backoff_rate": 2.0},
            # Tool calls (dynamic toolset steps): no automatic retry; idempotency comes from side_effects.
            mcp_step_config={"retries_allowed": False},
            event_stream_handler_step_config={"retries_allowed": False},
        ),
    ],
)


# --------------------------------------------------------------------------- steps (business writes)


@DBOS.step(name="eap.start_run")
async def start_run_step(run_id: str, tenant_id: str, agent_id: str, version: int, input_text: str) -> None:
    rt = get_runtime()

    def work() -> None:
        # The API inserts the row first (client-supplied run_id); this is an idempotent safety net.
        rt.run_store.create_run(tenant_id, UUID(agent_id), version, input_text, UUID(run_id))
        rt.run_store.set_status(UUID(run_id), "running")
        rt.run_store.heartbeat(UUID(run_id))

    await asyncio.to_thread(work)


@DBOS.step(name="eap.append_event")
async def append_event_step(run_id: str, event_key: str, type: str, data: dict[str, Any]) -> int:
    rt = get_runtime()
    event, _created = await asyncio.to_thread(rt.event_log.append, UUID(run_id), event_key, type, data)
    crash_point("after_event_write", event_key=event_key)
    return event.seq


@DBOS.step(name="eap.finish_run")
async def finish_run_step(run_id: str, status: str, output: str | None, error: str | None) -> None:
    rt = get_runtime()
    await asyncio.to_thread(rt.run_store.set_status, UUID(run_id), status, output, error)


async def _heartbeat(run_id: str) -> None:
    """Liveness only (not durable): runs.heartbeat_at every 5 s while this process drives the run."""
    rt = get_runtime()
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await asyncio.to_thread(rt.run_store.heartbeat, UUID(run_id))
        except Exception:  # noqa: BLE001 - a missed heartbeat must never fail the run
            log.warning("heartbeat failed for run %s", run_id, exc_info=True)


# --------------------------------------------------------------------------- helpers


def _parse_spec(spec_json: str | dict[str, Any]) -> AgentSpec:
    if isinstance(spec_json, str):
        return AgentSpec.model_validate_json(spec_json)
    return AgentSpec.model_validate(spec_json)


def _message_json(message: ModelMessage) -> dict[str, Any]:
    return ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]


def _find_tool_results(call_ids: set[str], *sources: list[ModelMessage] | list[Any]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for parts in sources:
        for part in parts:
            if isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id in call_ids:
                found.setdefault(part.tool_call_id, part)
    return found


def _all_parts(messages: list[ModelMessage]) -> list[Any]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts]


def _is_side_effect_unknown(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, SideEffectOutcomeUnknown) or type(cur).__name__ == "SideEffectOutcomeUnknown":
            return True
        if isinstance(cur, BaseExceptionGroup):
            if any(_is_side_effect_unknown(e) for e in cur.exceptions):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


async def _record_tool_results(run_id: str, calls: list[ToolCallPart], *sources: list[Any]) -> None:
    results = _find_tool_results({c.tool_call_id for c in calls}, *sources)
    for call in calls:
        part = results.get(call.tool_call_id)
        if isinstance(part, ToolReturnPart):
            data = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": "ok",
                "content": to_jsonable_python(part.content, fallback=str),
            }
        elif isinstance(part, RetryPromptPart):
            data = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": "error",
                "content": to_jsonable_python(part.content, fallback=str),
            }
        else:
            data = {"tool_call_id": call.tool_call_id, "tool_name": call.tool_name, "status": "no_result"}
        await append_event_step(run_id, f"tool:{call.tool_call_id}:result", "tool_result", data)


async def _drive_agent(run_id: str, deps: WorkerDeps, input_text: str) -> str:
    spec = deps.spec
    settings = ModelSettings(max_retries=0)
    if spec.model_settings.temperature is not None:
        settings["temperature"] = spec.model_settings.temperature
    if spec.model_settings.max_tokens is not None:
        settings["max_tokens"] = spec.model_settings.max_tokens

    n = 0
    pending: list[ToolCallPart] = []
    async with agent.iter(
        input_text,
        deps=deps,
        model=model_id_for(spec.model.provider, spec.model.name),
        model_settings=settings,
    ) as run:
        async for node in run:
            if pending:
                request_parts = list(node.request.parts) if Agent.is_model_request_node(node) else []
                await _record_tool_results(run_id, pending, request_parts, _all_parts(run.all_messages()))
                pending = []
            if Agent.is_call_tools_node(node):
                # The model request that produced this response is already checkpointed.
                response: ModelResponse = node.model_response
                calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
                text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
                await append_event_step(
                    run_id,
                    f"model:{n}:final",
                    "model_message",
                    {"model_request": n, "text": text, "message": _message_json(response)},
                )
                for call in calls:
                    await append_event_step(
                        run_id,
                        f"tool:{call.tool_call_id}:call",
                        "tool_call",
                        {
                            "tool_call_id": call.tool_call_id,
                            "tool_name": call.tool_name,
                            "args": to_jsonable_python(call.args_as_dict(), fallback=str),
                        },
                    )
                pending = calls
                n += 1
        if pending:
            await _record_tool_results(run_id, pending, _all_parts(run.all_messages()))
        assert run.result is not None
        return run.result.output


# --------------------------------------------------------------------------- the workflow


@DBOS.workflow(name=WORKFLOW_NAME)
async def agent_run(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: int,
    spec_json: str | dict[str, Any],
    input_text: str,
) -> dict[str, Any]:
    await start_run_step(run_id, tenant_id, agent_id, version, input_text)
    await append_event_step(
        run_id, "start", "run_started", {"agent_id": agent_id, "version": version, "input": input_text}
    )
    heartbeat = asyncio.create_task(_heartbeat(run_id))
    try:
        rt = get_runtime()
        spec = _parse_spec(spec_json)
        check_spec_tools(spec, rt.registry, rt.allowed_tools)
        output = await _drive_agent(run_id, rt.deps_for(run_id, tenant_id, spec), input_text)
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        status = "needs_attention" if _is_side_effect_unknown(exc) else "failed"
        error = f"{type(exc).__name__}: {exc}"
        log.warning("run %s %s: %s", run_id, status, error)
        heartbeat.cancel()
        await append_event_step(run_id, "end", "run_failed", {"status": status, "error": error})
        await finish_run_step(run_id, status, None, error)
        return {"status": status, "error": error}
    finally:
        heartbeat.cancel()
    # End event before the terminal status: a reader that sees a terminal status always finds the
    # complete event log.
    await append_event_step(run_id, "end", "run_completed", {"output": output})
    await finish_run_step(run_id, "completed", output, None)
    return {"status": "completed", "output": output}


__all__ = ["agent", "agent_run", "AGENT_NAME", "TOOLSET_ID"]
