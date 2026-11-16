"""Step processors (guardrails on tool calls), parity round 2.

`StepProcessorCapability.wrap_tool_execute` runs the spec's `step_processors` (deterministic chain,
eap_worker.processors) over the JSON text of a tool call's arguments before the tool runs, and over
its result after. Like `RetrieveNumbering` it wraps the call in the WORKFLOW, around the DBOS call_tool
step: the processor segments are their own `eap.step_processors` steps and the outcomes are written
with `eap.append_event`, so a replay re-reads checkpointed results and nothing is recorded twice.

A block never raises: the tool result becomes `Blocked by processor {type}: {reason}` and the run
continues (the model sees the message and may answer or try something else). A redaction replaces the
arguments the tool sees, or the result the model sees. Events (`processor_result`, phase `step`):
`processor:step:{i}:tool:{n}:args` and `...:result`, `n` the call's position among the run's tool calls.
Only specs with step processors take any of these steps, so other specs keep their step layout.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturn
from pydantic_ai.tools import ToolDefinition
from pydantic_core import to_jsonable_python

from eap_schemas.processors import ModerationProcessor
from eap_worker.processors import ChainRun, run_chain
from eap_worker.workflow_eval import canonical_json

ARGS_INVALID_REASON = "args_invalid_after_processing"


def blocked_message(chain: ChainRun) -> str:
    """The text that replaces a blocked call's result. Carries the processor type and reason code only."""
    record = chain.blocked_by
    processor_type = str(record.data.get("processor_type")) if record else "unknown"
    reason = (record.data.get("reason") if record else None) or "blocked"
    return f"Blocked by processor {processor_type}: {reason}"


def tool_call_ordinal(messages: list[Any], run_id: str | None, tool_call_id: str | None) -> int:
    """The position (from 0) of `tool_call_id` among the run's own tool calls, in message order.
    Deterministic across a replay: the messages are rebuilt from checkpointed responses."""
    n = 0
    for message in messages:
        if run_id is not None and getattr(message, "run_id", None) != run_id:
            continue
        if not isinstance(message, ModelResponse):
            continue
        for part in message.parts:
            if isinstance(part, ToolCallPart):
                if part.tool_call_id == tool_call_id:
                    return n
                n += 1
    return n


def _result_text(result: Any) -> tuple[str, bool]:
    """(the text the chain checks, whether the result was already a plain string)."""
    value = result.return_value if isinstance(result, ToolReturn) else result
    if isinstance(value, str):
        return value, True
    return canonical_json(to_jsonable_python(value, fallback=str)), False


def _with_text(result: Any, text: str, was_str: bool) -> Any:
    value: Any = text
    if not was_str:
        try:
            value = json.loads(text)
        except ValueError:
            value = text
    if isinstance(result, ToolReturn):
        return ToolReturn(return_value=value, content=result.content, metadata=result.metadata)
    return value


class StepProcessorCapability(AbstractCapability[Any]):
    """Step processors around every tool execution of a spec that lists them (no-op otherwise)."""

    async def wrap_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, handler: Any
    ) -> Any:
        spec = getattr(ctx.deps, "spec", None)
        specs = tuple(getattr(spec, "step_processors", ()) or ())
        if not specs:
            return await handler(args)
        # Moderation is not valid in this phase (schema); skipped defensively.
        specs = tuple(s for s in specs if not isinstance(s, ModerationProcessor))
        from eap_worker.workflow import _record_processor_results  # workflow imports this module

        run_id, tenant_id = str(ctx.deps.run_id), ctx.deps.tenant_id
        n = tool_call_ordinal(ctx.messages, getattr(ctx, "run_id", None), call.tool_call_id)

        # Arguments: the JSON text the model sent, before the tool runs.
        args_text = canonical_json(to_jsonable_python(args, fallback=str))
        chain = await run_chain(run_id, tenant_id, "step", specs, args_text, moderation=False)
        await _record_processor_results(run_id, tenant_id, chain, f":tool:{n}:args")
        if chain.error is not None:
            raise chain.error
        if chain.text is None:
            return blocked_message(chain)
        if chain.text != args_text:
            try:
                new_args = json.loads(chain.text)
            except ValueError:
                new_args = None
            if not isinstance(new_args, dict):  # a redaction that broke the JSON: the call cannot run
                return f"Blocked by processor {chain.records[-1].data.get('processor_type')}: {ARGS_INVALID_REASON}"
            args = new_args

        result = await handler(args)

        # Result: the text the model would see.
        text, was_str = _result_text(result)
        chain = await run_chain(run_id, tenant_id, "step", specs, text, moderation=False)
        await _record_processor_results(run_id, tenant_id, chain, f":tool:{n}:result")
        if chain.error is not None:
            raise chain.error
        if chain.text is None:
            return blocked_message(chain)
        if chain.text != text:
            return _with_text(result, chain.text, was_str)
        return result

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None


__all__ = ["StepProcessorCapability", "blocked_message", "tool_call_ordinal"]
