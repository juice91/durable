"""Tool test runs: one platform tool call in a short durable DBOS workflow (`eap.tool_run`).

Hand-off (apps/api/src/eap_api/launcher.py): the API inserts a `runs` row of kind 'tool' and
enqueues `eap.tool_run` on queue `eap-agent-runs` with workflow_id = run_id and positional args
`(run_id, tenant_id, tool_id, input_json)`, `input_json` being the run row's input (the tool's
validated arguments as canonical JSON text). A separate workflow name keeps `eap.agent_run`'s
arguments unchanged (queued and in-flight agent runs recover as before); the same queue means one
worker pool, one concurrency setting and one app_version for both.

Step layout (each line is a DBOS step, so recovery replays recorded results):

    eap.start_tool_run                  ensure the runs row, status -> running; returns traceparent
    eap.append_event   start            run_started   {kind, tool_id, input}
    eap.check_tool                      registry + EAP_ALLOWED_TOOLS + input model, re-checked here
    eap.append_event   tool:tool_run:call    tool_call    {tool_call_id, tool_name, tool_id, args}
    eap.tool_call                       the tool call (no automatic retry)
    eap.append_event   tool:tool_run:result  tool_result  {..., status ok|error, content | error, error_code}
    eap.append_event   end              run_completed {output, result} | run_failed {status, error, error_code}
    eap.finish_run                      status -> completed | failed | needs_attention

A tool the check rejects is never called: the run goes straight from run_started to run_failed.

Side effects (durability contract rule 5, stricter for the console): a side-effecting tool's call
is reserved under the key `{run_id}:tool` with `run_side_effect(..., external_idempotent=False)`.
If the worker dies after the call and before the reservation is completed (crash point
`after_tool_run_call`), recovery re-executes `eap.tool_call`, finds the reservation `reserved`,
raises SideEffectOutcomeUnknown and the run ends in `needs_attention`: the tool is never called
twice, even one that honours an idempotency key. A completed reservation returns the stored result
without calling again. A read-only tool is simply called again on recovery.

The tool sees `ctx.tool_call_id == "tool_run"`, so tools that derive their own idempotency key
from it (tickets.create: `{run_id}:{tool_call_id}`) use `{run_id}:tool_run`, distinct from the
run's guard key.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any
from uuid import UUID

from dbos import DBOS
from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RunUsage
from pydantic_core import to_jsonable_python

from eap_observability import log_context, log_failure, run_span, set_run_outcome
from eap_run_store import PostgresSideEffectStore
from eap_runtime import ToolNotAllowedError, UnknownToolError, run_side_effect
from eap_worker.config import TOOL_WORKFLOW_NAME
from eap_worker.crash import crash_point
from eap_worker.errors import ToolInputInvalid, classify
from eap_worker.platform_tools import HIDDEN_TOOL_IDS
from eap_worker.runtime import get_runtime
from eap_worker.workflow import _heartbeat, _is_side_effect_unknown, append_event_step, finish_run_step

log = logging.getLogger("eap_worker")

TOOL_CALL_ID = "tool_run"
CALL_EVENT_KEY = f"tool:{TOOL_CALL_ID}:call"
RESULT_EVENT_KEY = f"tool:{TOOL_CALL_ID}:result"


def side_effect_key(run_id: str) -> str:
    """The side_effects key that guards a tool run's single call."""
    return f"{run_id}:tool"


def tool_name_for(tool_id: str) -> str:
    """The tool's name as agent runs show it (same rule as eap_runtime: '.' -> '__')."""
    return tool_id.replace(".", "__")


_REJECTIONS: dict[str, type[Exception]] = {
    "unknown_tool": UnknownToolError,
    "tool_not_allowed": ToolNotAllowedError,
    "tool_input_invalid": ToolInputInvalid,
}


# --------------------------------------------------------------------------- steps


@DBOS.step(name="eap.start_tool_run")
async def start_tool_run_step(run_id: str, tenant_id: str, tool_id: str, input_json: str) -> dict[str, Any]:
    """Returns {"traceparent"} (None when the API had telemetry off)."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        # The API inserts the row first; this is an idempotent safety net (RunIdConflict for a
        # run_id of another tenant: nothing of theirs is touched).
        rt.run_store.create_tool_run(tenant_id, tool_id, input_json, UUID(run_id))
        # running, clearing an ENQUEUE_FAILED_ERROR in the same statement: the API marks a run failed
        # when its enqueue raised, yet DBOS may have accepted it, and then this workflow runs it.
        rt.run_store.mark_running(tenant_id, UUID(run_id))
        rt.run_store.heartbeat(tenant_id, UUID(run_id))
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        return {"traceparent": run.get("traceparent")}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.check_tool", retries_allowed=False)
async def check_tool_step(tool_id: str, args: dict[str, Any], hidden_ok: bool = False) -> dict[str, Any]:
    """The worker-side policy check, checkpointed so a replay takes the same path even if the
    allowlist changed meanwhile. Returns {"rejected": None | code, "side_effect", "args"} where
    `args` is the input as the tool's input model accepts it (JSON mode).

    Hidden platform tools (knowledge.search, RM.11) pass only with `hidden_ok`, which only the tool
    run path sets (the API starts such a run only from the knowledge base search endpoint); a
    workflow tool step naming one is `unknown_tool`. They are not governed by EAP_ALLOWED_TOOLS."""
    rt = get_runtime()
    if tool_id not in rt.registry.tool_ids():
        return {"rejected": "unknown_tool"}
    if tool_id in HIDDEN_TOOL_IDS:
        if not hidden_ok:
            return {"rejected": "unknown_tool"}
    elif tool_id not in rt.allowed_tools:
        return {"rejected": "tool_not_allowed"}
    try:
        declaration = rt.registry.get_declaration(tool_id)
    except ValueError:
        # Registered without a declaration (a test or extension tool): no input contract to check
        # the console's input against, so it is not offered there.
        return {"rejected": "unknown_tool"}
    try:
        validated = declaration.input_model.model_validate(args)
    except ValidationError:
        return {"rejected": "tool_input_invalid"}
    return {"rejected": None, "side_effect": declaration.side_effect, "args": validated.model_dump(mode="json")}


def call_tool_guarded(
    run_id: str,
    tenant_id: str,
    tool_id: str,
    args: dict[str, Any],
    side_effect: bool,
    *,
    guard_key: str,
    tool_call_id: str,
    crash_name: str,
    crash_key: str | None = None,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Call a platform tool once (blocking; run it in a thread) and return its JSON-able result.

    Shared by tool test runs and workflow tool steps. A side-effecting tool runs under
    run_side_effect(`guard_key`, external_idempotent=False); `crash_name` fires right after the
    call returned, before the reservation is completed. The tool sees `ctx.tool_call_id ==
    tool_call_id`, which must differ from the guard key's suffix (tickets.create reserves
    `{run_id}:{tool_call_id}` itself)."""
    rt = get_runtime()
    func = rt.registry.get(tool_id)
    declaration = rt.registry.get_declaration(tool_id)
    deps = rt.tool_deps_for(run_id, tenant_id)
    takes_ctx = Tool(func, name=tool_name_for(tool_id)).takes_ctx

    def invoke() -> Any:
        # Python values as the input model parsed them (the implementation's signature matches the
        # declaration, ToolRegistry.register checks that), not the JSON forms.
        kwargs = dict(declaration.input_model.model_validate(args))
        if takes_ctx:
            # No model request ever happens here, so there is no model to give the context.
            ctx = RunContext(
                deps=deps, model=None, usage=RunUsage(), tool_call_id=tool_call_id, tool_name=tool_name_for(tool_id)
            )  # type: ignore[arg-type]
            result = func(ctx, **kwargs)
        else:
            result = func(**kwargs)
        if inspect.isawaitable(result):
            result = asyncio.run_coroutine_threadsafe(result, loop).result()
        return to_jsonable_python(result, fallback=str)

    if not side_effect:
        value = invoke()
        crash_point(crash_name, event_key=crash_key)
        return value

    def call() -> dict[str, Any]:
        value = invoke()
        crash_point(crash_name, event_key=crash_key)
        return {"value": value}

    stored, _performed = run_side_effect(
        PostgresSideEffectStore(rt.engine, tenant_id), guard_key, call, external_idempotent=False
    )
    return stored.get("value")


@DBOS.step(name="eap.tool_call", retries_allowed=False)
async def tool_call_step(run_id: str, tenant_id: str, tool_id: str, args: dict[str, Any], side_effect: bool) -> Any:
    """Call the tool once and return its result as JSON-able data.

    A side-effecting tool runs under run_side_effect(`{run_id}:tool`, external_idempotent=False);
    see the module docstring for what recovery does with each reservation state."""
    loop = asyncio.get_running_loop()
    return await asyncio.to_thread(
        call_tool_guarded,
        run_id,
        tenant_id,
        tool_id,
        args,
        side_effect,
        guard_key=side_effect_key(run_id),
        tool_call_id=TOOL_CALL_ID,
        crash_name="after_tool_run_call",
        loop=loop,
    )


# --------------------------------------------------------------------------- the workflow


@DBOS.workflow(name=TOOL_WORKFLOW_NAME)
async def tool_run(run_id: str, tenant_id: str, tool_id: str, input_json: str) -> dict[str, Any]:
    with log_context(tenant_id=tenant_id, run_id=run_id):  # records before run_span carry the ids too
        link = await start_tool_run_step(run_id, tenant_id, tool_id, input_json) or {}
    # run_span's agent attributes are empty for a tool run; the tool is named in its own attributes.
    with run_span(run_id, tenant_id, "", "", traceparent=link.get("traceparent")) as span:
        span.set_attribute("eap.run_kind", "tool")
        span.set_attribute("eap.tool_id", tool_id)
        log.info("tool run %s started: tenant=%s tool=%s", run_id, tenant_id, tool_id)
        result = await _tool_run_body(run_id, tenant_id, tool_id, input_json)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("tool run %s %s", run_id, result["status"])
        return result


async def _tool_run_body(run_id: str, tenant_id: str, tool_id: str, input_json: str) -> dict[str, Any]:
    raw_args = json.loads(input_json)
    await append_event_step(
        run_id, tenant_id, "start", "run_started", {"kind": "tool", "tool_id": tool_id, "input": raw_args}
    )
    tool_name = tool_name_for(tool_id)
    call_data = {"tool_call_id": TOOL_CALL_ID, "tool_name": tool_name, "tool_id": tool_id}
    called = False
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        check = await check_tool_step(tool_id, raw_args, hidden_ok=True)
        if check["rejected"] is not None:
            raise _REJECTIONS[check["rejected"]](tool_id)
        args = check["args"]
        await append_event_step(run_id, tenant_id, CALL_EVENT_KEY, "tool_call", {**call_data, "args": args})
        called = True
        content = await tool_call_step(run_id, tenant_id, tool_id, args, bool(check["side_effect"]))
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        failure = classify(exc)
        status = "needs_attention" if _is_side_effect_unknown(exc) else failure.status
        log_failure(
            log, f"tool run {status}", exc, code=failure.code, error_type=failure.error_type,
            run_id=run_id, tenant_id=tenant_id,
        )
        heartbeat.cancel()
        if called:
            await append_event_step(
                run_id, tenant_id, RESULT_EVENT_KEY, "tool_result",
                {**call_data, "status": "error", "error": failure.error, "error_code": failure.code},
            )
        await append_event_step(
            run_id, tenant_id, "end", "run_failed",
            {"status": status, "error": failure.error, "error_code": failure.code},
        )
        await finish_run_step(run_id, tenant_id, status, None, failure.error)
        return {"status": status, "error": failure.error, "error_code": failure.code}
    finally:
        heartbeat.cancel()
    output = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    await append_event_step(
        run_id, tenant_id, RESULT_EVENT_KEY, "tool_result", {**call_data, "status": "ok", "content": content}
    )
    # End event before the terminal status, as in agent runs.
    await append_event_step(run_id, tenant_id, "end", "run_completed", {"output": output, "result": content})
    await finish_run_step(run_id, tenant_id, "completed", output, None)
    return {"status": "completed", "output": output}


__all__ = [
    "CALL_EVENT_KEY",
    "RESULT_EVENT_KEY",
    "TOOL_CALL_ID",
    "call_tool_guarded",
    "check_tool_step",
    "side_effect_key",
    "tool_call_step",
    "tool_name_for",
    "tool_run",
]
