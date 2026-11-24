"""Agents as tools (supervisor pattern) in the worker; spec: eap_schemas.agent_tools.

A spec's `agent_tools` refs become model-visible tools `<name>(input: str) -> {output, run_id}`. Like a
workflow agent step (eap_worker.workflow_run), a call starts a CHILD run of the target agent as a child
DBOS workflow `eap.agent_run`: workflow id = child run id = uuid5(`{run_id}:agent_tool:{tool_call_id}`),
a `runs` row with `parent_run_id` = the caller's run and `parent_step_id` = the tool call id. The child
has its own events, trace, usage, approvals and recovery; nothing runs in-process. The caller waits for the
child's result inside its own workflow, so a worker kill while the child runs resumes both and the child
is never started twice (the id is deterministic and DBOS records the start).

The call is made WORKFLOW-side, by `AgentToolsCapability.wrap_tool_execute` (the hook never calls the
toolset's own call step: there is none for these tools), like the approval wait and the retrieve
numbering. Steps of one call, in order (a spec without `agent_tools` has none of them):

    eap.agent_tool_resolve     depth and max-calls guards, the target version resolved ONCE ('latest' or the
                               pin) and the child run row created (idempotent: a re-executed step finds it
                               and reloads the version that row names). Checkpointed.
    eap.append_event           agent_tool:{call}:started    agent_tool_started {child_run_id, ...}
    DBOS.start_workflow        child eap.agent_run, SetWorkflowID(child), SetWorkflowTimeout(timeout_seconds)
    DBOS.getResult             the child's result (a timeout or cancel raises "awaited workflow cancelled")
    eap.agent_tool_cancel      only after a cancel: the child's run tree is ended `run_cancelled`
    eap.append_event           agent_tool:{call}:completed  agent_tool_completed {child_run_id, status, ...}

The tool result is `{"output": <child's final output, cut at MAX_OUTPUT_BYTES with a marker>, "run_id":
<child run id>}`. A refused call (depth, max calls, unknown or archived target), a failed child and a
timeout are model-visible tool errors (ToolFailed with a stable code in the message), never a run failure.

Guards: depth (EAP_AGENT_TOOL_MAX_DEPTH, default 3: a run's depth is the number of agent runs above it
linked by agent-tool calls); `max_calls_per_run` per ref (the child rows of this run for that agent); the
child runs ITS OWN published spec; the caller's request context, model override and conversation never reach
the child, only the `input` string.

Crash points (crash.py): `after_agent_tool_start` (the child workflow was started, the caller has recorded
nothing yet) and `after_agent_tool_result` (the child finished, the caller has not recorded the
completion). Both are filtered by `EAP_CRASH_EVENT_KEY` against `agent_tool:{call}:started`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any

from dbos import DBOS, SetWorkflowID, SetWorkflowTimeout
from dbos._error import DBOSAwaitedWorkflowCancelledError
from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema
from sqlalchemy import text

from eap_run_store import ParentRunStopped
from eap_schemas.agent_tools import (
    AGENT_TOOL_AGENT_ARCHIVED,
    AGENT_TOOL_CANCELLED,
    AGENT_TOOL_DEPTH_EXCEEDED,
    AGENT_TOOL_FAILED,
    AGENT_TOOL_MAX_CALLS,
    AGENT_TOOL_NOT_PUBLISHED,
    AGENT_TOOL_TIMEOUT,
    AGENT_TOOL_UNKNOWN_AGENT,
    AGENT_TOOL_VERSION_NOT_FOUND,
    LATEST,
    AgentToolRef,
    agent_tool_child_run_id,
    agent_tool_default_name,
)
from eap_worker.crash import crash_point
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.agent_tools")

METADATA_KEY = "agent_tool"  # ToolDefinition.metadata: the ref's index in spec.agent_tools
MAX_OUTPUT_BYTES = 32 * 1024  # the child's output in a tool result (the workflow step output cap)
TRUNCATION_MARKER = "\n[output truncated: {omitted} bytes omitted]"
MAX_DESCRIPTION_SNIPPET = 300
DEFAULT_MAX_DEPTH = 3
MAX_DEPTH_WALK = 16
GRACE_SECONDS = 3.0  # how long past its timeout the caller lets DBOS cancel the child first
ARGS_VALIDATOR = SchemaValidator(
    core_schema.typed_dict_schema(
        {"input": core_schema.typed_dict_field(core_schema.str_schema())}, extra_behavior="forbid"
    )
)


def max_depth() -> int:
    """EAP_AGENT_TOOL_MAX_DEPTH (default 3): the deepest agent-tool chain a run may start. Read inside
    `eap.agent_tool_resolve`, so a replay decides as the first attempt did."""
    try:
        return max(1, int(os.environ.get("EAP_AGENT_TOOL_MAX_DEPTH", DEFAULT_MAX_DEPTH)))
    except ValueError:
        return DEFAULT_MAX_DEPTH


def child_run_id(run_id: str, tool_call_id: str) -> str:
    """The child run id and DBOS workflow id of one tool call (stable across replays)."""
    return agent_tool_child_run_id(run_id, tool_call_id)


def bounded_output(output: Any) -> tuple[Any, bool]:
    """The child's output for the tool result, bounded by MAX_OUTPUT_BYTES (UTF-8).

    A string stays text (cut with a marker saying how much was left out). A structured value (an agent with
    `output.json_schema` answers an object) stays that object when its JSON fits, so the model reads real
    JSON; one that does not fit becomes its JSON text, cut with the same marker."""
    if output is None:
        return "", False
    if isinstance(output, str):
        value = output
    else:
        try:
            value = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(output)
        else:
            if len(value.encode("utf-8")) <= MAX_OUTPUT_BYTES:
                return output, False
    raw = value.encode("utf-8")
    if len(raw) <= MAX_OUTPUT_BYTES:
        return value, False
    kept = raw[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
    omitted = len(raw) - len(kept.encode("utf-8"))
    return kept + TRUNCATION_MARKER.format(omitted=omitted), True


# --------------------------------------------------------------------------------- the toolset


def _snippet(instructions: str | None) -> str:
    one_line = re.sub(r"\s+", " ", instructions or "").strip()
    return one_line[: MAX_DESCRIPTION_SNIPPET - 1] + "…" if len(one_line) > MAX_DESCRIPTION_SNIPPET else one_line


def _read_targets(engine: Any, tenant_id: str, refs: tuple[AgentToolRef, ...]) -> list[dict[str, Any]]:
    """Per ref: {name, instructions} of the target (the agent row's name, the pinned or latest version's
    instructions); both None for a target that no longer exists in the tenant. Read inside get_tools."""
    out: list[dict[str, Any]] = []
    with engine.connect() as conn:
        for ref in refs:
            agent = conn.execute(
                text("SELECT name FROM agents WHERE id = :a AND tenant_id = :t"), {"a": ref.agent_id, "t": tenant_id}
            ).first()
            if agent is None:
                out.append({"name": None, "instructions": None})
                continue
            if ref.version == LATEST:
                row = conn.execute(
                    text(
                        "SELECT spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t"
                        " ORDER BY version DESC LIMIT 1"
                    ),
                    {"a": ref.agent_id, "t": tenant_id},
                ).first()
            else:
                row = conn.execute(
                    text("SELECT spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t AND version = :v"),
                    {"a": ref.agent_id, "t": tenant_id, "v": ref.version},
                ).first()
            spec = row[0] if row is not None else None
            out.append({"name": agent[0], "instructions": (spec or {}).get("instructions")})
    return out


def tool_definition(index: int, ref: AgentToolRef, target: dict[str, Any]) -> ToolDefinition:
    name = ref.name or agent_tool_default_name(target.get("name"), ref.agent_id)
    if ref.description:
        description = ref.description
    elif target.get("name") is None:
        description = "Delegate a task to another agent. That agent is no longer available."
    else:
        description = f"Delegate a task to the agent '{target['name']}' and get its answer."
        if snippet := _snippet(target.get("instructions")):
            description += f" What it does: {snippet}"
    return ToolDefinition(
        name=name,
        description=description,
        parameters_json_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "The task or question for the agent, as plain text."}
            },
            "required": ["input"],
            "additionalProperties": False,
        },
        metadata={METADATA_KEY: index, "agent_id": ref.agent_id},
    )


class AgentToolsToolset(AbstractToolset[Any]):
    """The run's agent tools. Built without I/O; the target names and descriptions are read inside the
    `get_tools` step (recorded, so a replay never reads them again). It never executes a call:
    AgentToolsCapability handles it workflow-side."""

    def __init__(self, refs: tuple[AgentToolRef, ...]) -> None:
        self.refs = refs

    @property
    def id(self) -> str | None:
        return None

    @staticmethod
    def owns(tool_def: ToolDefinition) -> bool:
        return (tool_def.metadata or {}).get(METADATA_KEY) is not None

    def _tool(self, tool_def: ToolDefinition) -> ToolsetTool[Any]:
        return ToolsetTool(toolset=self, tool_def=tool_def, max_retries=1, args_validator=ARGS_VALIDATOR)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        rt = get_runtime()
        targets = await asyncio.to_thread(_read_targets, rt.engine, ctx.deps.tenant_id, self.refs)
        defs = [tool_definition(i, ref, targets[i]) for i, ref in enumerate(self.refs)]
        return {d.name: self._tool(d) for d in defs}

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        return self._tool(tool_def)  # no I/O: the recorded definition is all the hook needs

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]) -> Any:
        raise RuntimeError("an agent tool is called by AgentToolsCapability, never by the toolset")


def agent_toolset_for_run(spec: Any) -> AgentToolsToolset | None:
    refs = tuple(getattr(spec, "agent_tools", ()) or ())
    return AgentToolsToolset(refs) if refs else None


# --------------------------------------------------------------------------------- steps


def _depth_of(rt: Any, tenant_id: str, run_id: str) -> int:
    """How many agent runs, linked by agent-tool calls, sit above `run_id` (0 for a top-level run, a
    workflow's or eval's child run, a tool run): each parent of kind `agent` is one link."""
    depth = 0
    current = rt.run_store.get_run(tenant_id, uuid.UUID(run_id)) or {}
    for _ in range(MAX_DEPTH_WALK):
        parent_id = current.get("parent_run_id")
        if parent_id is None:
            break
        parent = rt.run_store.get_run(tenant_id, uuid.UUID(str(parent_id))) or {}
        if parent.get("kind") != "agent":
            break
        depth += 1
        current = parent
    return depth


def _refusal(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "code": code, "message": message}


@DBOS.step(name="eap.agent_tool_resolve")
async def agent_tool_resolve_step(
    run_id: str, tenant_id: str, tool_call_id: str, tool_name: str, agent_id: str, version: str | int,
    max_calls: int, input_text: str,
) -> dict[str, Any]:
    """Guards, version resolution and the child run row. Returns {"ok": False, "code", "message"} or
    {"ok": True, "child_run_id", "version", "spec_json", "depth"}; checkpointed, so a replay never
    re-resolves 'latest' or re-counts. A re-executed step (killed after the insert, before the
    checkpoint) finds the child row and reloads the version it names, even if the agent was archived since."""
    import json

    rt = get_runtime()
    child = child_run_id(run_id, tool_call_id)

    def pinned(agent_version: Any, depth: int) -> dict[str, Any]:
        with rt.engine.connect() as conn:
            row = conn.execute(
                text("SELECT version, spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t AND version = :v"),
                {"a": agent_id, "t": tenant_id, "v": agent_version},
            ).first()
        if row is None:
            return _refusal(AGENT_TOOL_VERSION_NOT_FOUND, f"The agent has no version {agent_version}.")
        return {"ok": True, "child_run_id": child, "version": row.version, "spec_json": json.dumps(row.spec), "depth": depth}

    def work() -> dict[str, Any]:
        depth = _depth_of(rt, tenant_id, run_id) + 1
        existing = rt.run_store.get_run(tenant_id, uuid.UUID(child))
        if existing is not None:
            return pinned(existing.get("agent_version"), depth)
        cap = max_depth()
        if depth > cap:
            return _refusal(
                AGENT_TOOL_DEPTH_EXCEEDED,
                f"Agent tool calls may be nested at most {cap} deep; this call would be level {depth}.",
            )
        with rt.engine.connect() as conn:
            agent = conn.execute(
                text("SELECT archived FROM agents WHERE id = :a AND tenant_id = :t"), {"a": agent_id, "t": tenant_id}
            ).first()
            if agent is None:
                return _refusal(AGENT_TOOL_UNKNOWN_AGENT, "The agent no longer exists.")
            if agent.archived:
                return _refusal(AGENT_TOOL_AGENT_ARCHIVED, "The agent is archived.")
            calls = conn.execute(
                text(
                    "SELECT count(*) FROM runs WHERE tenant_id = :t AND parent_run_id = :r AND kind = 'agent'"
                    " AND agent_id = :a"
                ),
                {"t": tenant_id, "r": run_id, "a": agent_id},
            ).scalar_one()
            if calls >= max_calls:
                return _refusal(
                    AGENT_TOOL_MAX_CALLS,
                    f"{tool_name} was already called {calls} times in this run (the limit is {max_calls}).",
                )
            if version == LATEST:
                row = conn.execute(
                    text(
                        "SELECT version, spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t"
                        " ORDER BY version DESC LIMIT 1"
                    ),
                    {"a": agent_id, "t": tenant_id},
                ).first()
            else:
                row = conn.execute(
                    text("SELECT version, spec FROM agent_versions WHERE agent_id = :a AND tenant_id = :t AND version = :v"),
                    {"a": agent_id, "t": tenant_id, "v": version},
                ).first()
        if row is None:
            code = AGENT_TOOL_NOT_PUBLISHED if version == LATEST else AGENT_TOOL_VERSION_NOT_FOUND
            return _refusal(code, "The agent has no published version." if version == LATEST else f"The agent has no version {version}.")
        parent = rt.run_store.get_run(tenant_id, uuid.UUID(run_id)) or {}
        try:
            _, created = rt.run_store.create_child_agent_run(
                tenant_id, agent_id, row.version, input_text, uuid.UUID(child), run_id, tool_call_id,
                traceparent=parent.get("traceparent"),
            )
        except ParentRunStopped:
            return _refusal("run_cancelled", "The run was cancelled or has ended.")
        if not created:  # lost a race with a concurrent attempt: that row's version wins
            existing = rt.run_store.get_run(tenant_id, uuid.UUID(child)) or {}
            if existing.get("agent_version") != row.version:
                return pinned(existing.get("agent_version"), depth)
        return {"ok": True, "child_run_id": child, "version": row.version, "spec_json": json.dumps(row.spec), "depth": depth}

    return await asyncio.to_thread(work)


@DBOS.step(name="eap.agent_tool_cancel")
async def agent_tool_cancel_step(tenant_id: str, child_run: str) -> dict[str, Any]:
    """End the cancelled child's run tree (`end` run_failed `run_cancelled`, status failed, open approvals
    closed): the DBOS cancel left its rows `running`. Idempotent (RunStore.cancel_run_tree)."""
    from eap_run_store.store import CANCEL_EVENT_KEY

    rt = get_runtime()

    def work() -> dict[str, Any]:
        rt.event_log.append(
            tenant_id, uuid.UUID(child_run), CANCEL_EVENT_KEY, "cancel_requested", {"requested_by": "agent_tool"}
        )
        result = rt.run_store.cancel_run_tree(tenant_id, uuid.UUID(child_run))
        return {"status": result.status}

    return await asyncio.to_thread(work)


# --------------------------------------------------------------------------------- the call


def _failed(code: str, message: str, run: str | None = None) -> ToolFailed:
    suffix = f" (run {run})" if run else ""
    return ToolFailed(f"[{code}] {message}{suffix}")


async def _stop_child(child: str, tenant_id: str) -> None:
    """Cancel the child's workflow tree and wake it, so a child parked in an approval wait ends now."""
    from eap_run_store.approvals import tool_approval_topic

    try:
        await DBOS.cancel_workflow_async(child, cancel_children=True)
        await DBOS.send_async(child, {"cancelled": True}, tool_approval_topic(child))
    except Exception as exc:  # noqa: BLE001 - the run tree is ended below either way
        log.warning("agent tool: child %s not woken after cancel (%s)", child, type(exc).__name__)


async def run_agent_tool(
    ctx: RunContext[Any], call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
) -> Any:
    """One agent-tool call (see the module docstring). Raises ToolFailed for every outcome that is not an answer."""
    from eap_worker.workflow import agent_run, append_event_step

    spec = ctx.deps.spec
    index = int((tool_def.metadata or {})[METADATA_KEY])
    ref = spec.agent_tools[index]
    run_id, tenant_id = str(ctx.deps.run_id), ctx.deps.tenant_id
    call_id, name = call.tool_call_id, call.tool_name
    key = f"agent_tool:{call_id}"
    input_text = str(args.get("input", ""))

    found = await agent_tool_resolve_step(
        run_id, tenant_id, call_id, name, ref.agent_id, ref.version, ref.max_calls_per_run, input_text
    )
    if not found.get("ok"):
        await append_event_step(
            run_id, tenant_id, f"{key}:completed", "agent_tool_completed",
            {"tool_call_id": call_id, "tool_name": name, "agent_id": ref.agent_id, "status": "refused",
             "error_code": found["code"]},
        )
        raise _failed(found["code"], found["message"])
    child = found["child_run_id"]
    await append_event_step(
        run_id, tenant_id, f"{key}:started", "agent_tool_started",
        {"tool_call_id": call_id, "tool_name": name, "agent_id": ref.agent_id, "version": found["version"],
         "child_run_id": child, "depth": found["depth"], "timeout_seconds": ref.timeout_seconds},
    )
    from eap_worker.workflow_run import clock_step  # `eap.workflow_clock`; workflow_run imports workflow

    started_at = await clock_step()  # recorded: a recovered caller waits only the time that is left
    with SetWorkflowID(child), SetWorkflowTimeout(ref.timeout_seconds):
        handle = await DBOS.start_workflow_async(
            agent_run, child, tenant_id, ref.agent_id, found["version"], found["spec_json"], input_text
        )
    crash_point("after_agent_tool_start", event_key=f"{key}:started")
    result: dict[str, Any] | None = None
    cancelled = False
    # DBOS cancels the child at its deadline (SetWorkflowTimeout), but a child parked in a long wait (an
    # approval) notices only when it wakes. So the caller bounds its own wait too, a little past the
    # deadline, and then cancels the child itself.
    waiting = asyncio.ensure_future(handle.get_result())
    left = max(started_at + ref.timeout_seconds - time.time(), 0.0) + GRACE_SECONDS
    try:
        done, _ = await asyncio.wait({waiting}, timeout=left)
        if waiting in done:
            result = waiting.result()
        else:
            cancelled = True
    except DBOSAwaitedWorkflowCancelledError:
        cancelled = True  # the timeout (or an outside cancel) stopped the child
    finally:
        if not waiting.done():
            waiting.cancel()
    crash_point("after_agent_tool_result", event_key=f"{key}:started")
    if cancelled or result is None:
        await _stop_child(child, tenant_id)
        await agent_tool_cancel_step(tenant_id, child)
        code = AGENT_TOOL_TIMEOUT
        await append_event_step(
            run_id, tenant_id, f"{key}:completed", "agent_tool_completed",
            {"tool_call_id": call_id, "tool_name": name, "child_run_id": child, "status": "timeout", "error_code": code},
        )
        raise _failed(
            code, f"The agent did not finish within {ref.timeout_seconds} seconds and was cancelled.", child
        )
    status = str(result.get("status") or "failed")
    if status != "completed":
        code = str(result.get("error_code") or AGENT_TOOL_FAILED)
        await append_event_step(
            run_id, tenant_id, f"{key}:completed", "agent_tool_completed",
            {"tool_call_id": call_id, "tool_name": name, "child_run_id": child, "status": status, "error_code": code},
        )
        detail = str(result.get("error") or "the run did not complete")
        raise _failed(AGENT_TOOL_FAILED, f"The agent run {status}: {detail} ({code})", child)
    # A structured child (output.json_schema) reports its validated object as `output_json` (`output` is its
    # JSON text): the model gets the object itself, not text and never a Python repr.
    structured = result.get("output_json")
    output, truncated = bounded_output(structured if structured is not None else result.get("output"))
    await append_event_step(
        run_id, tenant_id, f"{key}:completed", "agent_tool_completed",
        {"tool_call_id": call_id, "tool_name": name, "child_run_id": child, "status": "completed",
         **({"truncated": True} if truncated else {})},
    )
    return {"output": output, "run_id": child}


class AgentToolsCapability(AbstractCapability[Any]):
    """Runs an agent-tool call workflow-side (see the module docstring); every other tool passes through."""

    async def wrap_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, handler: Any
    ) -> Any:
        if not AgentToolsToolset.owns(tool_def):
            return await handler(args)
        return await run_agent_tool(ctx, call, tool_def, args)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None


__all__ = [
    "AgentToolsCapability",
    "AgentToolsToolset",
    "MAX_OUTPUT_BYTES",
    "agent_toolset_for_run",
    "bounded_output",
    "child_run_id",
    "max_depth",
    "run_agent_tool",
]
