"""The durable agent run: one generic DBOSDurability agent driven by one DBOS workflow per run.

Hand-off contract (docs/plans/2026-11-11-milestone-1-vertical-slice.md): the API enqueues
`eap.agent_run` on queue `eap-agent-runs` with workflow_id = run_id and positional args
`(run_id, tenant_id, agent_id, version, spec_json, input_text)`.

Conversation turns (milestone 2) use exactly the same six arguments. Whether a run is a turn, and
which one, is carried in the run row (runs.conversation_id, runs.turn), which the API writes under
the conversation's lock before it enqueues. `eap.start_run` returns that link, so it is checkpointed
with the step; a workflow enqueued before conversations existed recorded `None` there and replays as
a standalone run. Keeping the arguments unchanged means an API and a worker of different releases
never disagree on the call shape, and workflows already queued or in flight recover unchanged.

Step layout of a run (each line is a DBOS step, so recovery replays recorded results):

    eap.start_run                       ensure the runs row, status -> running; returns the turn link
                                        and the stored request traceparent
    --- from here on, inside the `eap.run` span (child of the API request span) ---
    eap.append_event   start            run_started
    eap.load_history                    only for a conversation turn: the earlier COMPLETED turns'
                                        stored messages, passed to the agent as message_history
    eap-agent__dynamic_toolset__eap-spec-tools.get_tools
    eap-agent__model.request_stream     model request n (deltas NOTIFY'd live inside)
    eap.append_event   model:{n}:final  model_message (from the checkpointed response)
    eap.append_event   tool:{id}:call   tool_call (before the tool runs)
    eap-agent__dynamic_toolset__eap-spec-tools.call_tool   tickets.create, MCP tools (mcp__<server>__<tool>)
    eap.append_event   tool:{id}:result tool_result
    ... (next model request) ...
    eap.append_event   end              run_completed | run_failed
    eap.finish_run                      status -> completed | failed | needs_attention

Durable events are written by our own steps from replayed data (never from inside the model
step), so a re-executed model request can never leave a stored message that differs from the
checkpointed one. Event keys are deterministic, and EventLog.append is idempotent per key.

Tracing: the API stores its request span's W3C traceparent on the run row. `eap.start_run` reads it
(so a replay uses the checkpointed value) and the rest of the run executes inside
`run_span(..., traceparent=...)`, so every later step, model request and tool call is in the API
request's trace. Spans are not durable state: a recovered run simply opens another `eap.run` span.
A checkpoint written before tracing existed (None, or no "traceparent" key) runs untraced.

Failures: exception text never leaves the worker. `eap_worker.errors.classify` turns the exception
into a stable code and a fixed user-safe message (stored as runs.error and in run_failed); logs and
the run span get only the code, the exception type and the run/tenant ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset, DynamicToolset, FunctionToolset
from pydantic_core import to_jsonable_python

from eap_observability import log_failure, run_span, set_run_outcome
from eap_runtime import RunDeps, SideEffectOutcomeUnknown, check_spec_tools, toolset_for_spec
from eap_run_store import ENQUEUE_FAILED_ERROR
from eap_schemas import AgentSpec
from eap_worker.config import WORKFLOW_NAME
from eap_worker.crash import crash_point
from eap_worker.errors import classify
from eap_worker.mcp import RunToolset, mcp_toolset_for_run
from eap_worker.models import PLACEHOLDER_MODEL, make_resolver, model_id_for
from eap_worker.runtime import WorkerDeps, get_runtime

log = logging.getLogger("eap_worker")

AGENT_NAME = "eap-agent"
TOOLSET_ID = "eap-spec-tools"
HEARTBEAT_SECONDS = 5.0


# --------------------------------------------------------------------------- agent (one, generic)


def _instructions(ctx: RunContext[WorkerDeps]) -> str:
    return ctx.deps.spec.instructions


async def _toolset_for_run(ctx: RunContext[WorkerDeps]) -> AbstractToolset[Any]:
    """Only the published spec's tools: built-ins checked against registry + allowlist, plus its MCP
    tools (eap_worker.mcp). No I/O here: MCP server rows and secrets are read later, inside the
    discovery step and inside a newly reserved call, so a replayed step needs neither the database
    nor the platform key. A spec without MCP tools gets exactly the FunctionToolset it always did."""
    rt = get_runtime()
    builtin: FunctionToolset[RunDeps] = toolset_for_spec(ctx.deps.spec, rt.registry, ctx.deps.allowed_tools)
    if not ctx.deps.spec.mcp_tools:
        return builtin
    return RunToolset(builtin, mcp_toolset_for_run(rt.engine, ctx.deps.tenant_id, ctx.deps.spec.mcp_tools))


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
async def start_run_step(
    run_id: str, tenant_id: str, agent_id: str, version: int, input_text: str
) -> dict[str, Any]:
    """Returns {"conversation_id", "turn", "traceparent", "superseded"}; conversation_id/turn are
    None for a standalone run, traceparent is None when the API had telemetry off. `superseded` is
    True when this turn must not run (see mark_running); the decision is part of this step's
    recorded output, so a replayed workflow takes the same branch."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        # The API inserts the row first (client-supplied run_id); this is an idempotent safety net.
        # Raises RunIdConflict if the run_id belongs to another tenant: nothing of theirs is touched.
        rt.run_store.create_run(tenant_id, UUID(agent_id), version, input_text, UUID(run_id))
        # running, clearing an ENQUEUE_FAILED_ERROR in the same statement: the API marks a run failed
        # when its enqueue raised, yet DBOS may have accepted it, and then this workflow runs it.
        # A turn that a later turn has overtaken meanwhile stays failed and is not run (False).
        started = rt.run_store.mark_running(tenant_id, UUID(run_id))
        if started:
            rt.run_store.heartbeat(tenant_id, UUID(run_id))
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        return {
            "conversation_id": run.get("conversation_id"),
            "turn": run.get("turn"),
            "traceparent": run.get("traceparent"),
            "superseded": not started,
        }

    return await asyncio.to_thread(work)


def max_history_bytes() -> int:
    # DBOS checkpoints the step output; keep it well under the 2 MB guidance.
    return int(os.environ.get("EAP_MAX_HISTORY_BYTES", str(1_000_000)))


def _turn_messages(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """A completed turn's messages as JSON: its stored messages, else its input and output."""
    if turn["messages"]:
        return list(turn["messages"])
    ts = turn["created_at"]
    fallback: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(turn["input"] or "", timestamp=ts)]),
        ModelResponse(parts=[TextPart(turn["output"] or "")], timestamp=ts),
    ]
    return ModelMessagesTypeAdapter.dump_python(fallback, mode="json")


@DBOS.step(name="eap.load_history")
async def load_history_step(run_id: str, tenant_id: str, conversation_id: str, turn: int) -> list[dict[str, Any]]:
    """The message history for turn `turn`: the conversation's earlier COMPLETED turns, oldest first.

    Built only from durable data (runs.messages of those turns) and checkpointed as this step's
    output, so a recovered workflow gives the agent exactly the history the first attempt had. When
    the history exceeds EAP_MAX_HISTORY_BYTES, the oldest whole turns are left out.
    """
    rt = get_runtime()
    turns = await asyncio.to_thread(rt.run_store.completed_turns_before, tenant_id, conversation_id, turn)
    per_turn = [_turn_messages(t) for t in turns]
    budget = max_history_bytes()
    kept: list[list[dict[str, Any]]] = []
    for messages in reversed(per_turn):
        size = len(json.dumps(messages, default=str))
        if size > budget:
            break
        budget -= size
        kept.append(messages)
    if len(kept) < len(per_turn):
        log.warning("run %s: history truncated to the last %d of %d turns", run_id, len(kept), len(per_turn))
    return [m for messages in reversed(kept) for m in messages]


@DBOS.step(name="eap.append_event")
async def append_event_step(run_id: str, tenant_id: str, event_key: str, type: str, data: dict[str, Any]) -> int:
    rt = get_runtime()
    event, _created = await asyncio.to_thread(rt.event_log.append, tenant_id, UUID(run_id), event_key, type, data)
    crash_point("after_event_write", event_key=event_key)
    return event.seq


@DBOS.step(name="eap.finish_run")
async def finish_run_step(
    run_id: str,
    tenant_id: str,
    status: str,
    output: str | None,
    error: str | None,
    messages: list[dict[str, Any]] | None = None,
) -> None:
    rt = get_runtime()
    await asyncio.to_thread(rt.run_store.set_status, tenant_id, UUID(run_id), status, output, error, messages)


async def _heartbeat(run_id: str, tenant_id: str) -> None:
    """Liveness only (not durable): runs.heartbeat_at every 5 s while this process drives the run."""
    rt = get_runtime()
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        try:
            await asyncio.to_thread(rt.run_store.heartbeat, tenant_id, UUID(run_id))
        except Exception as exc:  # noqa: BLE001 - a missed heartbeat must never fail the run
            log_failure(log, "heartbeat failed", exc, code="heartbeat_failed", run_id=run_id, tenant_id=tenant_id)


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


def _is_side_effect_unknown(exc: BaseException, seen: set[int] | None = None) -> bool:
    # One `seen` for the whole walk: a group member's __context__ can point back at the group.
    seen = set() if seen is None else seen
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, SideEffectOutcomeUnknown) or type(cur).__name__ == "SideEffectOutcomeUnknown":
            return True
        if isinstance(cur, BaseExceptionGroup):
            if any(_is_side_effect_unknown(e, seen) for e in cur.exceptions):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


async def _record_tool_results(run_id: str, tenant_id: str, calls: list[ToolCallPart], *sources: list[Any]) -> None:
    results = _find_tool_results({c.tool_call_id for c in calls}, *sources)
    for call in calls:
        part = results.get(call.tool_call_id)
        if isinstance(part, ToolReturnPart):
            data = {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                # pydantic-ai 2.x returns a tool's ModelRetry / ToolFailed as a return with outcome
                # 'retried' / 'failed'; other outcomes (denied, interrupted) stay "ok" as before.
                "status": "error" if part.outcome in ("failed", "retried") else "ok",
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
        await append_event_step(run_id, tenant_id, f"tool:{call.tool_call_id}:result", "tool_result", data)


async def _drive_agent(
    run_id: str, deps: WorkerDeps, input_text: str, history: list[ModelMessage] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Run the agent; returns (output, this run's new messages as JSON)."""
    spec = deps.spec
    tenant_id = deps.tenant_id
    settings = ModelSettings(max_retries=0)
    if spec.model_settings.temperature is not None:
        settings["temperature"] = spec.model_settings.temperature
    if spec.model_settings.max_tokens is not None:
        settings["max_tokens"] = spec.model_settings.max_tokens

    n = 0
    pending: list[ToolCallPart] = []
    async with agent.iter(
        input_text,
        message_history=history or None,
        deps=deps,
        model=model_id_for(spec.model.provider, spec.model.name),
        model_settings=settings,
    ) as run:
        async for node in run:
            if pending:
                request_parts = list(node.request.parts) if Agent.is_model_request_node(node) else []
                await _record_tool_results(run_id, tenant_id, pending, request_parts, _all_parts(run.all_messages()))
                pending = []
            if Agent.is_call_tools_node(node):
                # The model request that produced this response is already checkpointed.
                response: ModelResponse = node.model_response
                calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
                text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
                await append_event_step(
                    run_id,
                    tenant_id,
                    f"model:{n}:final",
                    "model_message",
                    {"model_request": n, "text": text, "message": _message_json(response)},
                )
                for call in calls:
                    await append_event_step(
                        run_id,
                        tenant_id,
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
            await _record_tool_results(run_id, tenant_id, pending, _all_parts(run.all_messages()))
        assert run.result is not None
        return run.result.output, ModelMessagesTypeAdapter.dump_python(run.result.new_messages(), mode="json")


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
    link = await start_run_step(run_id, tenant_id, agent_id, version, input_text) or {}
    if link.get("superseded"):
        # Its hand-off failed (the run is failed with ENQUEUE_FAILED_ERROR) and a later turn has
        # started since: running it now would run two turns at once. No event, no model call, no tool.
        log.warning("run %s not started: a later turn of its conversation has started", run_id)
        return {"status": "failed", "error": ENQUEUE_FAILED_ERROR, "error_code": "superseded"}
    with run_span(run_id, tenant_id, agent_id, version, traceparent=link.get("traceparent")) as span:
        log.info("run %s started: tenant=%s agent=%s version=%s", run_id, tenant_id, agent_id, version)
        result = await _run_body(run_id, tenant_id, agent_id, version, spec_json, input_text, link)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("run %s %s", run_id, result["status"])
        return result


async def _run_body(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: int,
    spec_json: str | dict[str, Any],
    input_text: str,
    link: dict[str, Any],
) -> dict[str, Any]:
    await append_event_step(
        run_id, tenant_id, "start", "run_started", {"agent_id": agent_id, "version": version, "input": input_text}
    )
    history: list[ModelMessage] | None = None
    if link.get("conversation_id") is not None:
        history_json = await load_history_step(run_id, tenant_id, link["conversation_id"], link["turn"])
        history = ModelMessagesTypeAdapter.validate_python(history_json)
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        rt = get_runtime()
        spec = _parse_spec(spec_json)
        # Model governance: re-checked here (not only at publish) because the policy may have
        # changed since the version was published. Fails before any model request is made.
        rt.settings.allowed_models.check(spec.model.provider, spec.model.name)
        check_spec_tools(spec, rt.registry, rt.allowed_tools)
        output, new_messages = await _drive_agent(run_id, rt.deps_for(run_id, tenant_id, spec), input_text, history)
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        failure = classify(exc)
        status = "needs_attention" if _is_side_effect_unknown(exc) else failure.status
        log_failure(
            log, f"run {status}", exc, code=failure.code, error_type=failure.error_type,
            run_id=run_id, tenant_id=tenant_id,
        )
        heartbeat.cancel()
        await append_event_step(
            run_id, tenant_id, "end", "run_failed",
            {"status": status, "error": failure.error, "error_code": failure.code},
        )
        await finish_run_step(run_id, tenant_id, status, None, failure.error)
        return {"status": status, "error": failure.error, "error_code": failure.code}
    finally:
        heartbeat.cancel()
    # End event before the terminal status: a reader that sees a terminal status always finds the
    # complete event log.
    await append_event_step(run_id, tenant_id, "end", "run_completed", {"output": output})
    await finish_run_step(run_id, tenant_id, "completed", output, None, new_messages)
    return {"status": "completed", "output": output}


__all__ = ["agent", "agent_run", "load_history_step", "AGENT_NAME", "TOOLSET_ID"]
