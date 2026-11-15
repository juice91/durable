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

    eap.start_run                       ensure the runs row, status -> running; returns the turn link,
                                        the stored request traceparent and the run options
    --- from here on, inside the `eap.run` span (child of the API request span) ---
    eap.append_event   start            run_started
    eap.load_history                    only for a conversation turn: the earlier COMPLETED turns'
                                        stored messages, passed to the agent as message_history
    eap-agent__dynamic_toolset__eap-spec-tools.get_tools
    eap-agent__model.request_stream     model request n (deltas NOTIFY'd live inside)
    eap.append_event   model:{n}:final  model_message (from the checkpointed response), with its
                                        usage and cost and, in the same transaction, its
                                        run_model_requests row (eap_worker.pricing)
    eap.append_event   tool:{id}:call   tool_call (before the tool runs)
    eap-agent__dynamic_toolset__eap-spec-tools.call_tool   tickets.create, MCP tools (mcp__<server>__<tool>),
                                        platform tools (retrieve)
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

Run options (milestone P0, P0.8 API / P0.11 worker): the effective model (`runs.model_id`, set when a
caller overrides the spec's model), the request context (`runs.request_context`) and whether the run
is a draft test run (`runs.draft_spec` set) also come back through `eap.start_run`'s recorded output,
so a replay uses the model and context of the first attempt even if the row changed since. A
checkpoint recorded before run options existed has no such keys and runs as before: the spec's model
and an empty context. The effective model is re-checked against the allowlist before the first model
request (and again by the resolver inside the model step). The request context is rendered into the
instructions (`eap_schemas.render_instructions`, `{{context.<name>}}`) and given to tools as the
read-only `RunDeps.request_context`; its values never go to spans or logs. A draft run is handed off
with version 0 and its snapshot as spec_json, so the spec it runs is fixed at enqueue time.

Processors (milestone P0, P0.17; eap_worker.processors): only for a spec with processors, so a spec
without them keeps the exact step layout above. Input processors run on the turn's input after
`eap.start_run` and BEFORE `start` (their steps write no events), so run_started, the model, the
stored messages and the history all see the processed text; one `processor_result` event per
processor that ran (`processor:input:{i}`) follows `start`. Output processors run on every model
response that has text, after its model step and before its `model:{n}:final` event: the deterministic
ones on every such response, and all of them, moderation included, on the final answer (text and no
tool calls); events `processor:output:{i}:model:{n}`. A response without text (tool calls only, or an
empty or thinking-only answer the agent retries) is not checked. Thinking parts are dropped from
every stored response of such a spec (event and runs.messages). A moderation outcome's event
carries the moderation request's usage row (request key `moderation:input:{i}`, or
`moderation:output:{i}:model:{n}`). A block ends the run failed with `input_blocked` (no history load, no
model request) or `output_blocked` (the blocked response's model_message keeps only its usage and
`"blocked": true`, no text; nothing is stored as output or messages). Failed turns never enter a
later turn's history. A spec with output processors publishes no NOTIFY deltas.

    eap.start_run
    eap.input_processors | eap-moderator__model.request ...   (input chain, list order)
    eap.append_event   start                   run_started (input = the processed text)
    eap.append_event   processor:input:{i}     processor_result, one per processor that ran
    ... (history, model request n) ...
    eap.output_processors | eap-moderator__model.request ...  (on the response's text)
    eap.append_event   processor:output:{i}:model:{n}   processor_result
    eap.append_event   model:{n}:final         model_message with the processed text

Retrieval (RAG and memory milestone, RM.11; eap_worker.platform_tools): a spec that attaches knowledge
bases gets the platform tool `retrieve` as the third member of its RunToolset. It adds NO step: its
knowledge base names are read inside the existing get_tools step, and the search (query embedding,
its `embedding` event and usage row under `embed:retrieve:{tool_call_id}`, then the SQL search)
runs inside the existing call_tool step, whose result is checkpointed. Its `tool_result` event also
carries the structured `sources`. A spec without knowledge bases keeps its exact toolset and steps.
Source numbers are unique across the run: each call continues from the sources the run's earlier
retrieve calls returned (eap_worker.platform_tools.retrieve_offset; the RetrieveNumbering capability
is a workflow-side hook around the call_tool step, so it adds no step either).

Working memory and history limits (RM.12; eap_worker.memory): only for a spec with `memory` AND a
conversation turn. `eap.start_run` also records the conversation's `resource_id` (None for a
standalone run; a checkpoint recorded before RM.12 has no such key). Such a turn does NOT run
`eap.load_history`; `eap.load_memory` takes its place in the sequence, after the model and tool
checks (so a failure there ends the run with a durable run_failed) and never for a blocked input:

    eap.start_run
    ... input processors, `start`, processor events ...
    eap.load_memory                     snapshot: resource_id, working memory note, history after
                                        memory.history (last_turns, max_tokens), then the byte cap
    eap-agent__dynamic_toolset__eap-spec-tools.get_tools   (+ update_working_memory when writable)
    ...
    eap-agent__dynamic_toolset__eap-spec-tools.call_tool   update_working_memory: its write, the
                                        `memory_updated` event (`memory:wm:{tool_call_id}`) and the
                                        ledger row in one transaction

The recorded note is rendered into the instructions as a delimited "Working memory" block, fixed
for the turn. It is never stored: runs.messages keep the base instructions only, and
update_working_memory's arguments are a placeholder there and in the model_message and tool_call
events (eap_worker.memory.scrub_memory_messages; history re-renders the block from eap.load_memory). A spec without `memory`, a standalone run and a workflow child run keep their exact
step layout, toolset and full history (up to EAP_MAX_HISTORY_BYTES).

Observational memory (RM.13; eap_worker.observational): only for a spec with `memory.observational`
AND a conversation turn that completed. eap.load_memory then also reads the active observations
(the history starts after the turns they cover) and the instructions get an "Earlier in this
conversation" block (never stored). After `eap.finish_run`, so the answer is never delayed and the
run's status is already `completed`:

    eap.finish_run
    <enqueue eap.memory_observe_run>    child workflow `{run_id}:memory-observe` on the partitioned
                                        queue `eap-memory-observe` (key: conversation id, one
                                        running per conversation), then DBOS.getResult; its steps:
    eap.memory_observe                  snapshot; decides observe / reflect / nothing (no write)
    eap-observer__model.request         only to observe: the observer request (model per call,
                                        allowlist checked inside the step)
    eap.memory_observe_write            the observation, its `memory_updated` event and usage row
                                        (`memory:observe:{turn}`) in one transaction, or one
                                        `memory_observe_failed` event
    eap-observer__model.request         only to reflect
    eap.memory_reflect                  the reflection (`memory:reflect:{turn}`), or the failure event

A failure there never changes the run's status. Without `memory.observational` the layout is the
RM.12 one exactly (no post-turn step).

Semantic recall (RM.14; eap_worker.recall): only for a spec with `memory.semantic_recall` AND a
conversation turn. Pre-turn, right after eap.load_memory (so never for a blocked input), one step
embeds the processed input, searches, widens the hits and checkpoints the groups, which the
instructions show as a "Related earlier conversations" block (never stored); post-turn, for a
completed turn, one step indexes the processed input and the final answer:

    eap.load_memory
    eap.memory_recall                   query embedding (`embed:recall:{turn}`), search excluding the
                                        turns in context, neighbours, the 8 KiB fit; its `embedding`
                                        event and usage row
    ... the turn ...
    eap.finish_run
    eap.memory_index                    redact, embed (`embed:index:{turn}`), MemoryStore.index_turn
                                        with its `embedding` event and usage row in one transaction
    eap.memory_observe_run ...          (RM.13, when configured: the observer child workflow)

A recall or index failure (embedding model no longer allowed, provider error, redaction) is one
text-free `embedding` event `embed:{recall|index}:{turn}:failed` and never fails the turn. Without
`memory.semantic_recall` the layout is the RM.12 / RM.13 one exactly.

Structured output (agent capabilities milestone, AC.4; eap_worker.capabilities): only for a spec with
`output`. `_drive_agent` passes a per-run `output_type` (`final_result` tool, native or prompted
`StructuredDict`) and `retries={"output": max_retries}`; the construction-time SpecCapability checks
every candidate against the JSON Schema workflow-side (no step) and asks the model again with
value-free errors. A spec without `output` passes neither and keeps its exact step layout. Inside
`_drive_agent` only:

    eap-agent__model.request_stream     the candidate (no live deltas for a structured spec)
    eap.output_processors_structured    only with output processors: string leaves of the candidate
    eap.append_event   processor:output:{i}:model:{n}
    eap.append_event   model:{n}:final  model_message with "output_candidate": true
    eap-agent__event_stream_handler     (tool mode) the output tool's call and result events
    eap.append_event   output:{n}:retry output_retry {model_request, errors: [json paths], cause},
                                        per rejected candidate (no `tool:{id}` events for final_result)
    ... (next model request) ...
    eap.output_processors_structured | eap-moderator__model.request   only with output processors:
                                        the final pass over the validated object (list order;
                                        moderation once, on its canonical JSON), then re-validation
    eap.append_event   processor:output:{i}:final   (moderation outcomes, or a block)
    eap.append_event   end              run_completed {output: canonical JSON, output_json: object}
    eap.finish_run                      runs.output = canonical JSON, runs.output_format = json

Retries exhausted end the run `output_invalid`; native mode on a model without JSON-schema output
ends it `output_mode_unsupported`; a processed object that breaks the schema ends it
`output_blocked` with reason `schema_invalid_after_processing`. Memory consumes the canonical JSON
text (eap.memory_index) and the observer renders the object as the answer (observational.turn_text).

Limits and fallback (AC.5): only for a spec with `limits` or `fallback_models`. Native limits
(`max_requests`, `max_tool_calls`, token caps) go to the run as `usage_limits` (no argument without
them, so Pydantic AI's default of 50 requests stays); a breach ends the run `usage_limit_exceeded`
with `{limit, value}`. `limits.max_cost_usd` adds one step before every model request n >= 1:

    eap.check_budget                    SUM(cost_usd) of the run's recorded run_model_requests rows
                                        (unpriced = 0); at or over the cap: run_budget_exceeded
    eap-agent__model.request_stream     model request n

Fallback adds no step: the resolver builds a FallbackModel inside the model step (every request
starts at the primary). The fallback models are re-checked against the allowlist at run start. A
per-run model override disables fallback (`run_started.fallback: "disabled_by_override"`). The
`model_message` event of a request on a chain adds `requested_model_id`, `fallback_index` and
`failed_attempts: [{model_id, outcome, error_type}]`; its usage row is the ANSWERING model's, and
failed attempts that reported usage get rows `model:{n}:failed:{k}` in the same transaction. Every
model failing ends the run `fallback_exhausted` (`cause`: the first failure's code). A failed
attempt's error text is reduced to its type and HTTP status (pricing.safe_attempt_error) in every
event and in runs.messages; spans keep only the type (instrumentation without content, plus the
redacting exporter). The one place it stays verbatim is the model step's own DBOS checkpoint in
the system database (the recorded ModelResponse, like the model's output itself), which is
operator-only and never served to tenants. Token caps (`max_input_tokens`, `max_output_tokens`,
`max_total_tokens`) are checked by the workflow on each response, before its output processors run
(so a response over a cap never triggers a billed moderation request); the run fails right after
the response's model_message event and usage row are written (so a billed request is never lost)
and before its tools run. With output processors that event carries no text (`"withheld":
"usage_limit_exceeded"`), since the processors never checked it.

Failures: exception text never leaves the worker. `eap_worker.errors.classify` turns the exception
into a stable code and a fixed user-safe message (stored as runs.error and in run_failed); logs and
the run span get only the code, the exception type and the run/tenant ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from dbos import DBOS
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ResolveModelId
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryFeedbackPart,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from pydantic_ai.toolsets import AbstractToolset, DynamicToolset, FunctionToolset
from pydantic_core import to_jsonable_python

from eap_observability import log_context, log_failure, run_span, set_run_outcome
from eap_runtime import RunDeps, SideEffectOutcomeUnknown, check_spec_tools, toolset_for_spec
from eap_run_store import ENQUEUE_FAILED_ERROR, KnowledgeStore, MemoryStore
from eap_schemas import AgentSpec
from eap_schemas.limits import RunBudgetExceeded
from eap_schemas.output import OUTPUT_TOOL_NAME, OutputInvalid
from eap_schemas.tools import RETRIEVE_TOOL_NAME
from eap_schemas.request_context import RequestContextError, render_instructions, validate_request_context
from eap_worker.capabilities import SpecCapability, error_paths, output_type_for, retries_for, schema_errors
from eap_worker.config import WORKFLOW_NAME
from eap_worker.crash import crash_point
from eap_worker.errors import (
    SCHEMA_INVALID_AFTER_PROCESSING,
    describe_usage_limit,
    token_limit_breach,
    InputBlocked,
    OutputBlocked,
    OutputModeUnsupported,
    RequestContextInvalid,
    classify,
    native_output_unsupported,
)
from eap_worker.mcp import RunToolset, mcp_toolset_for_run
from eap_worker.memory import (
    instructions_with_memory,
    load_memory_step,
    memory_context,
    memory_of,
    scrub_memory_message,
    scrub_memory_messages,
    scrub_memory_tool_args,
    with_memory,
)
from eap_worker.observational import observe_after_turn
from eap_worker.recall import index_after_turn, recall_before_turn
from eap_worker.platform_tools import RetrieveNumbering, check_no_hidden_tools, platform_toolset_for_run
from eap_worker.models import PLACEHOLDER_MODEL, fallback_chain, make_resolver, model_id_for
from eap_worker.pricing import (
    failed_attempt_rows,
    fallback_usage,
    request_usage_input,
    scrub_failed_attempts,
    scrub_failed_attempts_message,
    usage_record,
)
from eap_worker.processors import (
    ChainRun,
    describe,
    processed_message,
    processed_messages,
    processed_output_call,
    replace_responses,
    run_chain,
    run_structured_chain,
    scrub_retry_inputs,
    strip_thinking,
)
from eap_worker.runtime import WorkerDeps, declared_context, get_runtime
from eap_worker.workflow_eval import canonical_json, non_finite_path

log = logging.getLogger("eap_worker")

AGENT_NAME = "eap-agent"
TOOLSET_ID = "eap-spec-tools"
HEARTBEAT_SECONDS = 5.0


# --------------------------------------------------------------------------- agent (one, generic)


def _instructions(ctx: RunContext[WorkerDeps]) -> str:
    # Rendered workflow-side from the recorded context (and, for a memory turn, the recorded
    # eap.load_memory output), so every (re)execution sends the same text.
    spec = ctx.deps.spec
    if not spec.context_fields:
        base = spec.instructions
    else:
        base = render_instructions(spec.instructions, ctx.deps.request_context, spec.context_fields)
    memory = memory_of(ctx.deps)
    return base if memory is None else instructions_with_memory(base, memory)


def _knowledge_store() -> KnowledgeStore:
    """The worker's KnowledgeStore (cheap: a sessionmaker on the runtime's engine)."""
    return KnowledgeStore(get_runtime().engine)


def _memory_store() -> MemoryStore:
    """The worker's MemoryStore (cheap: a sessionmaker on the runtime's engine)."""
    return MemoryStore(get_runtime().engine)


async def _toolset_for_run(ctx: RunContext[WorkerDeps]) -> AbstractToolset[Any]:
    """Only the published spec's tools: built-ins checked against registry + allowlist, plus its MCP
    tools (eap_worker.mcp), plus the platform tools it uses (eap_worker.platform_tools: `retrieve`
    when it attaches knowledge bases, `update_working_memory` for a memory turn with writable
    working memory). No I/O here: MCP server rows and secrets, and knowledge base
    names, are read later, inside the get_tools step (and MCP secrets inside a newly reserved call),
    so a replayed step needs neither the database nor the platform key. A spec without MCP tools and
    platform tools gets exactly the FunctionToolset it always did."""
    rt = get_runtime()
    spec = ctx.deps.spec
    check_no_hidden_tools(spec)
    builtin: FunctionToolset[RunDeps] = toolset_for_spec(spec, rt.registry, ctx.deps.allowed_tools)
    mcp = mcp_toolset_for_run(rt.engine, ctx.deps.tenant_id, spec.mcp_tools) if spec.mcp_tools else None
    platform = platform_toolset_for_run(ctx.deps, knowledge=_knowledge_store, memory=_memory_store)
    if mcp is None and platform is None:
        return builtin
    return RunToolset(builtin, mcp, platform)


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
    # Output processors (P0.17): unchecked text must never reach the client, so no live deltas; the
    # UI waits for the checked model_message. Structured output (AC.4): no deltas either (native and
    # prompted modes would stream raw JSON fragments). The crash point stays where the delta would go.
    live = not ctx.deps.spec.output_processors and ctx.deps.spec.output is None
    async for event in events:
        text = _text_of(event)
        if text:
            if live:
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
        # AC.5: the spec's fallback chain for requests to its primary model (eap_worker.models).
        ResolveModelId(make_resolver(lambda: get_runtime().settings, fallback=True)),
        # Run-wide source numbers for retrieve (a workflow-side hook around the call_tool step: no step).
        RetrieveNumbering(),
        # The run spec's behaviour (eap_worker.capabilities; AC.4: the output schema check). Workflow-side
        # hooks only: no step. Before DBOSDurability, which stays last.
        SpecCapability(),
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


DRAFT_VERSION = 0  # the hand-off `version` of a draft test run (published versions start at 1)
# run_started's `input` when the input processors blocked the input (or failed): never the raw text.
WITHHELD_INPUT = "[withheld: blocked by the agent's input processors]"


@DBOS.step(name="eap.start_run")
async def start_run_step(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: int,
    input_text: str,
    spec_json: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Returns {"conversation_id", "turn", "resource_id", "traceparent", "superseded", "model_id",
    "request_context", "is_draft"}; conversation_id/turn/resource_id are None for a standalone run
    (RM.12: the conversation row's resource_id, read only for a spec with `memory`; else None; it is
    informational: eap.load_memory's snapshot is authoritative for the working memory key, and RM.14's
    recall store calls read the resource from the conversation row themselves), traceparent
    is None when the API had telemetry off. `superseded` is True when this turn must not run (see
    mark_running); the decision is part of this step's recorded output, so a replayed workflow takes
    the same branch. `model_id` is the run's effective model (None: the spec's), `request_context`
    its validated context ({} when none), `is_draft` whether it is a draft test run."""
    rt = get_runtime()

    def work() -> dict[str, Any]:
        # The API inserts the row first (client-supplied run_id); this is an idempotent safety net.
        # Raises RunIdConflict if the run_id belongs to another tenant: nothing of theirs is touched.
        # A draft run (version 0) is stored like the API stores it: no version, the snapshot instead.
        draft = version == DRAFT_VERSION and spec_json is not None
        rt.run_store.create_run(
            tenant_id,
            UUID(agent_id),
            None if draft else version,
            input_text,
            UUID(run_id),
            draft_spec=_spec_dict(spec_json) if draft and spec_json is not None else None,
            model_id=_spec_model_id(spec_json),
        )
        # running, clearing an ENQUEUE_FAILED_ERROR in the same statement: the API marks a run failed
        # when its enqueue raised, yet DBOS may have accepted it, and then this workflow runs it.
        # A turn that a later turn has overtaken meanwhile stays failed and is not run (False).
        started = rt.run_store.mark_running(tenant_id, UUID(run_id))
        if started:
            rt.run_store.heartbeat(tenant_id, UUID(run_id))
        run = rt.run_store.get_run(tenant_id, UUID(run_id)) or {}
        resource_id = None
        # Only a memory spec needs it (RM.12): no extra read for any other turn.
        memory_spec = _spec_or_none(spec_json) if spec_json is not None else None
        if run.get("conversation_id") is not None and memory_spec is not None and memory_spec.memory is not None:
            conversation = rt.run_store.get_conversation(tenant_id, run["conversation_id"]) or {}
            resource_id = conversation.get("resource_id")
        return {
            "conversation_id": run.get("conversation_id"),
            "turn": run.get("turn"),
            "resource_id": resource_id,
            "traceparent": run.get("traceparent"),
            "superseded": not started,
            "model_id": run.get("model_id"),
            "request_context": run.get("request_context") or {},
            "is_draft": run.get("draft_spec") is not None,
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
async def append_event_step(
    run_id: str,
    tenant_id: str,
    event_key: str,
    type: str,
    data: dict[str, Any],
    usage: dict[str, Any] | None = None,
    request_key: str | None = None,
) -> int:
    """Write one durable event (idempotent per key). `usage` (pricing.request_usage_input) is given
    only for a model request's `model:{n}:final` event, or a moderation outcome's `processor_result`
    event (with `request_key` `moderation:input:{i}` or `moderation:output:{i}:model:{n}`; default: the event key without `:final`):
    the request is priced here, the price and tokens are added to the event's data, and its
    run_model_requests row is written in the event's transaction (first write wins, so a
    re-executed step never re-prices). Same step, same position: a run without usage and a
    checkpoint recorded before usage existed replay unchanged. AC.5: `usage["failed"]` (from
    pricing.fallback_usage) adds a row per failed fallback attempt that reported usage
    (`{request_key}:failed:{k}`), in the same transaction."""
    rt = get_runtime()
    rows: list[dict[str, Any]] | dict[str, Any] | None = None
    if usage is not None:
        key = request_key or event_key.removesuffix(":final")
        row, fields = usage_record(key, usage)
        data = {**data, **fields}
        failed = usage.get("failed") or []
        rows = [row, *failed_attempt_rows(key, failed, row["created_at"])] if failed else row
    event, _created = await asyncio.to_thread(
        rt.event_log.append, tenant_id, UUID(run_id), event_key, type, data, rows
    )
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
    output_format: str | None = None,
) -> None:
    """The terminal status. `output_format` is `json` for a structured output's canonical JSON text
    (AC.4); None leaves runs.output_format NULL (text)."""
    rt = get_runtime()
    await asyncio.to_thread(
        rt.run_store.set_status, tenant_id, UUID(run_id), status, output, error, messages, output_format
    )


@DBOS.step(name="eap.check_budget")
async def check_budget_step(run_id: str, tenant_id: str, n: int, cap_usd: str) -> str:
    """AC.5: before model request n >= 1 of a spec with `limits.max_cost_usd`. Reads the run's
    recorded cost (every run_model_requests row of THIS run so far: chat, failed fallback attempts,
    moderation, in-turn embeddings; unpriced rows count 0) and raises RunBudgetExceeded (pickle-safe,
    recorded by DBOS, so a replay decides identically) when it is at or over the cap. Returns the
    spent amount as a decimal string."""
    rt = get_runtime()
    spent = await asyncio.to_thread(rt.run_store.recorded_cost_usd, tenant_id, UUID(run_id))
    if spent >= Decimal(cap_usd):
        raise RunBudgetExceeded(spent, cap_usd)
    return format(spent, "f")


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


def _spec_dict(spec_json: str | dict[str, Any]) -> dict[str, Any]:
    return json.loads(spec_json) if isinstance(spec_json, str) else dict(spec_json)


def _spec_model_id(spec_json: str | dict[str, Any] | None) -> str | None:
    """The spec's model id for the safety-net insert, or None. Never raises: an invalid spec fails
    the run later, inside the run body, with a durable run_failed."""
    if spec_json is None:
        return None
    try:
        spec = _parse_spec(spec_json)
    except Exception:  # noqa: BLE001
        return None
    return model_id_for(spec.model.provider, spec.model.name)


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


def _tool_sources(part: ToolReturnPart) -> list[Any] | None:
    """The structured sources of a platform `retrieve` result (ToolReturn metadata, never sent to
    the model), for the tool_result event; None for every other tool."""
    if part.tool_name != RETRIEVE_TOOL_NAME or part.outcome != "success" or not isinstance(part.metadata, dict):
        return None
    sources = part.metadata.get("sources")
    return to_jsonable_python(sources, fallback=str) if isinstance(sources, list) else None


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
            sources = _tool_sources(part)
            if sources is not None:
                data["sources"] = sources
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


async def _record_processor_results(run_id: str, tenant_id: str, chain: ChainRun, key_suffix: str = "") -> None:
    """One `processor_result` event per processor that ran (`processor:{phase}:{i}{key_suffix}`); a
    moderation outcome's event also records its model request's usage
    (`moderation:{phase}:{i}{key_suffix}`). Output outcomes pass `:model:{n}`, so two responses of a
    run (e.g. an empty answer the agent retries, then the real one) never share a key."""
    for record in chain.records:
        phase = record.data["phase"]
        key = f"processor:{phase}:{record.index}{key_suffix}"
        if record.usage is not None:
            await append_event_step(
                run_id, tenant_id, key, "processor_result", record.data,
                usage=record.usage, request_key=f"moderation:{phase}:{record.index}{key_suffix}",
            )
        else:
            await append_event_step(run_id, tenant_id, key, "processor_result", record.data)
    if chain.blocked_by is not None:
        log.info("run %s: %s", run_id, describe(chain.blocked_by))


def _blocked_error(chain: ChainRun, phase: str) -> Exception:
    record = chain.blocked_by
    index, processor_type = (record.index, str(record.data.get("processor_type"))) if record else (-1, "?")
    if phase == "input":
        return InputBlocked(index, processor_type)
    # AC.4: two object keys that processing made equal: the object no longer fits its schema.
    reason = SCHEMA_INVALID_AFTER_PROCESSING if record and record.data.get("reason") == SCHEMA_INVALID_AFTER_PROCESSING else None
    return OutputBlocked(index, processor_type, reason)


def _spec_or_none(spec_json: str | dict[str, Any]) -> AgentSpec | None:
    """The spec for the processor decision, or None: an invalid spec fails later, in the run body."""
    try:
        return _parse_spec(spec_json)
    except Exception:  # noqa: BLE001
        return None


# Token caps are enforced by the workflow itself (check_token_limits), not by Pydantic AI: its check
# runs after a response but before the response is handed back, so the billed request would get no
# model_message event and no usage row.
TOKEN_LIMIT_FIELDS = ("max_input_tokens", "max_output_tokens", "max_total_tokens")


def usage_limits_for(spec: AgentSpec) -> UsageLimits | None:
    """AC.5: Pydantic AI's UsageLimits for the spec's request and tool-call caps, or None (no
    argument: the default UsageLimits, 50 requests, applies as before). An unset max_requests keeps
    50. Token caps are not passed (see token_limits_for)."""
    if spec.limits is None:
        return None
    kwargs = {
        kwarg: value
        for kwarg, value in spec.limits.usage_limits_kwargs().items()
        if kwarg in ("request_limit", "tool_calls_limit")
    }
    return UsageLimits(**kwargs) if kwargs else None


def token_limits_for(spec: AgentSpec) -> dict[str, int]:
    """AC.5: the spec's token caps `{field: cap}` (empty without any), checked by check_token_limits."""
    if spec.limits is None:
        return {}
    return {name: getattr(spec.limits, name) for name in TOKEN_LIMIT_FIELDS if getattr(spec.limits, name) is not None}


def fallback_disabled_by_override(spec: AgentSpec | None, link: dict[str, Any]) -> bool:
    """A spec with fallback models whose run has another (overridden) model: no fallback (AC.5)."""
    if spec is None or not spec.fallback_models:
        return False
    return len(fallback_chain(spec, effective_model_id(spec, link))) == 1


def effective_model_id(spec: AgentSpec, link: dict[str, Any]) -> str:
    """The run's model: the recorded `model_id` from eap.start_run, else (a checkpoint recorded
    before run options, or a row without one) the spec's model."""
    recorded = link.get("model_id")
    if isinstance(recorded, str) and recorded:
        return recorded
    return model_id_for(spec.model.provider, spec.model.name)


def _candidate_value(spec: AgentSpec, output_calls: list[ToolCallPart], text: str) -> Any:
    """The raw structured candidate of a response (tool arguments, or the parsed JSON text in
    native or prompted mode), or None when there is none or it does not parse. Pure. Used ONLY to
    recompute a rejected candidate's error paths (output_retry); never stored or processed, so a
    non-finite number is kept here (schema_errors names its path as "must be a finite number")."""
    if output_calls:
        try:
            return output_calls[0].args_as_dict(raise_if_invalid=True)
        except (ValueError, AssertionError):  # malformed or non-object JSON: Pydantic AI asks again
            return None
    if text:
        return _parse_json_text(text, allow_non_finite=True)
    return None


def _no_constants(name: str) -> Any:
    raise ValueError(name)


def _parse_json_text(text: str, *, allow_non_finite: bool = False) -> Any:
    """`text` as a JSON object or array (NaN and Infinity refused unless `allow_non_finite`), else None."""
    try:
        value = json.loads(text) if allow_non_finite else json.loads(text, parse_constant=_no_constants)
    except ValueError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _structured_args(call: ToolCallPart) -> dict[str, Any] | None:
    """An output call's arguments as a JSON object for leaf processing, or None when they are not
    one: malformed JSON, a non-object, or a non-finite number (NaN and Infinity are not JSON, JSONB
    rejects them, and SpecCapability asks the model again for them). Those are processed as text."""
    try:
        args = call.args_as_dict(raise_if_invalid=True)
    except (ValueError, AssertionError):
        return None
    return None if non_finite_path(args) is not None else args


def _args_text(call: ToolCallPart) -> str:
    """An output call's arguments as the model's text (a dict is dumped as JSON, NaN kept as a token)."""
    if isinstance(call.args, str):
        return call.args
    return json.dumps(call.args or {}, ensure_ascii=False, default=str)


def _is_output_retry(part: Any) -> bool:
    if isinstance(part, ToolReturnPart):
        return part.tool_name == OUTPUT_TOOL_NAME and part.outcome == "retried"
    if isinstance(part, RetryFeedbackPart):
        return True
    return isinstance(part, RetryPromptPart) and part.tool_name in (None, OUTPUT_TOOL_NAME)


def _candidate_errors(spec: AgentSpec, candidate: Any, content: Any = None) -> list[str]:
    """The JSON paths a rejected candidate broke: Pydantic's own error list when it rejected the
    candidate itself, else the schema check's (recomputed: pure). Never values."""
    paths = error_paths(content)
    if paths or spec.output is None or candidate is None:
        return paths
    return list(dict.fromkeys(path for path, _ in schema_errors(spec.output.json_schema, candidate)))


async def _record_output_retry(
    run_id: str, tenant_id: str, spec: AgentSpec, n: int, candidate: Any, parts: list[Any]
) -> list[str] | None:
    """One `output_retry` event (`output:{n}:retry`) when the request after response n asks the
    model again for its output; returns the error paths, or None when it does not."""
    retry = next((p for p in parts if _is_output_retry(p)), None)
    if retry is None:
        return None
    errors = _candidate_errors(spec, candidate, retry.content)
    data: dict[str, Any] = {"model_request": n, "errors": errors}
    info = getattr(retry, "retry", None)
    cause = getattr(info, "cause", None)
    if isinstance(cause, str):
        data["cause"] = cause
    await append_event_step(run_id, tenant_id, f"output:{n}:retry", "output_retry", data)
    return errors


async def _blocked_response(
    run_id: str, tenant_id: str, n: int, usage: Any, chain: ChainRun, extra: dict[str, Any] | None = None
) -> None:
    """A blocked (or failed) response: keep the request's usage, never its content; then fail.
    `extra` (AC.5): the fallback fields (requested_model_id, fallback_index, failed_attempts)."""
    await append_event_step(
        run_id, tenant_id, f"model:{n}:final", "model_message",
        {"model_request": n, "text": "", "blocked": True, **(extra or {})}, usage=usage,
    )
    raise chain.error if chain.error is not None else _blocked_error(chain, "output")


async def _process_structured_response(
    run_id: str,
    tenant_id: str,
    spec: AgentSpec,
    n: int,
    message: dict[str, Any],
    text: str,
    output_calls: list[ToolCallPart],
    candidate: bool,
    usage: Any,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Output processors over one response of a structured spec (AC.4), before anything of it is
    stored: the deterministic processors on every string leaf of each output candidate (tool
    arguments, or native/prompted JSON text), on plain text otherwise. No moderation here: it runs
    once on the final object. Returns the (message, text) to store; a block writes the blocked
    model_message and raises."""
    processors = spec.output_processors
    suffix = f":model:{n}"
    for k, call in enumerate(output_calls):
        key = suffix if k == 0 else f"{suffix}:{k}"
        args = _structured_args(call)
        if args is not None:
            chain = await run_structured_chain(
                run_id, tenant_id, processors, args, moderation=False, schema=spec.output.json_schema
            )
        else:  # not a finite JSON object: processed as text, stored as the (processed) string
            chain = await run_chain(run_id, tenant_id, "output", processors, _args_text(call), moderation=False)
        await _record_processor_results(run_id, tenant_id, chain, key)
        if chain.text is None:
            await _blocked_response(run_id, tenant_id, n, usage, chain, extra)
        message = processed_output_call(message, call.tool_call_id, chain.text)
    if not text:
        return message, text
    value = _parse_json_text(text) if candidate and not output_calls else None
    key = f"{suffix}:text" if output_calls else suffix
    if value is not None:
        chain = await run_structured_chain(
            run_id, tenant_id, processors, value, moderation=False, schema=spec.output.json_schema
        )
    else:
        chain = await run_chain(run_id, tenant_id, "output", processors, text, moderation=False)
    await _record_processor_results(run_id, tenant_id, chain, key)
    if chain.text is None:
        await _blocked_response(run_id, tenant_id, n, usage, chain, extra)
    text = canonical_json(chain.text) if value is not None else chain.text
    return processed_message(message, text), text


async def _final_structured_output(run_id: str, tenant_id: str, spec: AgentSpec, value: Any) -> Any:
    """The validated object after the output processors' final pass (AC.4): the whole chain in
    list order (moderation once, on the canonical JSON; the deterministic processors are pure, so
    they give what the stored candidate holds), then re-validation against the schema. Records the
    moderation outcomes (and a block) as `processor:output:{i}:final`."""
    assert spec.output is not None
    final = await run_structured_chain(
        run_id, tenant_id, spec.output_processors, value, moderation=True, schema=spec.output.json_schema
    )
    shown = [r for r in final.records if r.data.get("processor_type") == "moderation" or r.blocked]
    await _record_processor_results(run_id, tenant_id, ChainRun(final.text, shown, final.error), ":final")
    if final.text is None:
        raise final.error if final.error is not None else _blocked_error(final, "output")
    if schema_errors(spec.output.json_schema, final.text):
        raise OutputBlocked(-1, "output_schema", SCHEMA_INVALID_AFTER_PROCESSING)
    return final.text


async def _drive_agent(
    run_id: str,
    deps: WorkerDeps,
    input_text: str,
    history: list[ModelMessage] | None = None,
    model_id: str | None = None,
) -> tuple[str, list[dict[str, Any]], Any]:
    """Run the agent on `model_id` (default: the spec's model); returns (output, this run's new
    messages as JSON, output_json). For a structured spec (AC.4) `output_json` is the validated,
    processed object and `output` its canonical JSON text; otherwise `output_json` is None."""
    spec = deps.spec
    tenant_id = deps.tenant_id
    settings = ModelSettings(max_retries=0)
    if spec.model_settings.temperature is not None:
        settings["temperature"] = spec.model_settings.temperature
    if spec.model_settings.max_tokens is not None:
        settings["max_tokens"] = spec.model_settings.max_tokens

    if model_id is None:
        model_id = model_id_for(spec.model.provider, spec.model.name)
    # AC.4: only a structured spec passes output_type and retries (a spec without `output` runs as before).
    structured = spec.output is not None
    mode = spec.output.effective_mode() if spec.output is not None else None
    run_options: dict[str, Any] = {}
    if structured:
        run_options = {"output_type": output_type_for(spec), "retries": retries_for(spec)}
    # AC.5: only a spec with native limits passes usage_limits; the cost cap is ours (check_budget).
    limits = usage_limits_for(spec)
    if limits is not None:
        run_options["usage_limits"] = limits
    token_caps = token_limits_for(spec)
    cap_usd = spec.limits.max_cost_usd if spec.limits is not None else None
    # (model_id,) without fallback or with an override. Not `chain`: that name is a ChainRun below.
    model_chain = fallback_chain(spec, model_id)
    # RM.12: a memory turn stores no copy of the note (eap_worker.memory.scrub_memory_messages).
    memory = memory_of(deps)
    n = 0
    pending: list[ToolCallPart] = []
    processed: dict[int, str] = {}  # model request n -> its processed text (output processors only)
    stored: dict[int, dict[str, Any]] = {}  # structured + output processors: n -> the stored response
    candidates: dict[int, Any] = {}  # structured: n -> its raw candidate (for output_retry paths)
    awaiting_retry: int | None = None  # structured: the response whose retry (if any) comes next
    final_text: str | None = None
    active_run: Any = None  # AC.5: the run, for the limit a UsageLimitExceeded broke
    try:
        async with agent.iter(
            input_text,
            message_history=history or None,
            deps=deps,
            model=model_id,
            model_settings=settings,
            **run_options,
        ) as run:
            active_run = run
            async for node in run:
                request_parts = list(node.request.parts) if Agent.is_model_request_node(node) else []
                if pending:
                    await _record_tool_results(run_id, tenant_id, pending, request_parts, _all_parts(run.all_messages()))
                    pending = []
                if awaiting_retry is not None and request_parts:
                    await _record_output_retry(
                        run_id, tenant_id, spec, awaiting_retry, candidates.get(awaiting_retry), request_parts
                    )
                if Agent.is_model_request_node(node) or Agent.is_call_tools_node(node):
                    awaiting_retry = None
                if cap_usd is not None and n >= 1 and Agent.is_model_request_node(node):
                    # AC.5: before model request n (the node runs when the loop advances).
                    await check_budget_step(run_id, tenant_id, n, format(cap_usd, "f"))
                    crash_point("after_check_budget", event_key=f"budget:{n}")
                if Agent.is_call_tools_node(node):
                    # The model request that produced this response is already checkpointed.
                    response: ModelResponse = node.model_response
                    calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
                    # AC.4: the output tool is not a tool call (no tool events); its call is the candidate.
                    output_calls = [c for c in calls if mode == "tool" and c.tool_name == OUTPUT_TOOL_NAME]
                    if output_calls:
                        calls = [c for c in calls if c.tool_name != OUTPUT_TOOL_NAME]
                    text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
                    candidate = bool(output_calls) or (structured and mode != "tool" and not calls and bool(text))
                    if candidate:
                        candidates[n] = _candidate_value(spec, output_calls, text)
                    fallback_data: dict[str, Any] = {}
                    if len(model_chain) > 1:
                        # AC.5: priced as the model that answered; failed attempts with usage get rows.
                        # Streamed (model.request_stream): the answering model's impossible usage
                        # is corrected by the guard (request_usage_input).
                        usage, fallback_data, failed_usage = fallback_usage(
                            model_chain, response, streamed_response=response
                        )
                        if failed_usage:
                            usage["failed"] = failed_usage
                    else:
                        # Streamed (model.request_stream): a gateway's impossible usage is corrected here.
                        usage = request_usage_input(
                            model_id, response.model_name, response.usage, streamed_response=response
                        )
                    # AC.5: a fallback attempt's error text (provider body, request) is never stored.
                    message = scrub_failed_attempts_message(_message_json(response))
                    message = scrub_memory_message(message, memory)
                    # AC.5: token caps, from the run's recorded usage (this response included), decided
                    # BEFORE the output processors, so a response over a cap never triggers their
                    # (billed) moderation request. Its usage row is still written below.
                    breach = token_limit_breach(token_caps, run.usage) if token_caps else None
                    if breach is not None and spec.output_processors:
                        # The text was never checked by the processors: never stored. Usage kept.
                        await append_event_step(
                            run_id, tenant_id, f"model:{n}:final", "model_message",
                            {"model_request": n, "text": "", "withheld": "usage_limit_exceeded", **fallback_data},
                            usage=usage,
                        )
                        raise breach
                    if spec.output_processors:
                        # Thinking is model text no processor checks: never stored, for every response.
                        message = strip_thinking(message)
                    if spec.output_processors and structured:
                        message, text = await _process_structured_response(
                            run_id, tenant_id, spec, n, message, text, output_calls, candidate, usage,
                            fallback_data,
                        )
                        stored[n] = message
                    elif spec.output_processors and text:
                        # Only responses with text: an empty or thinking-only answer has nothing to check
                        # (the agent retries it). Before anything of this response is stored. Moderation
                        # only on the final answer (text and no tool calls).
                        final = not calls
                        chain = await run_chain(
                            run_id, tenant_id, "output", spec.output_processors, text, moderation=final
                        )
                        await _record_processor_results(run_id, tenant_id, chain, f":model:{n}")
                        if chain.text is None:
                            # Blocked (or failed): keep the request's usage, never its text.
                            await _blocked_response(run_id, tenant_id, n, usage, chain, fallback_data)
                        text = chain.text
                        message = processed_message(message, text)
                        processed[n] = text
                        if final:
                            final_text = text
                    data: dict[str, Any] = {"model_request": n, "text": text, "message": message, **fallback_data}
                    if candidate:
                        data["output_candidate"] = True
                        crash_point("output_candidate", event_key=f"model:{n}:final")
                    await append_event_step(run_id, tenant_id, f"model:{n}:final", "model_message", data, usage=usage)
                    if breach is not None:
                        # AC.5: raised HERE, after the billed response and its usage row are recorded
                        # (Pydantic AI would raise before either), and before its tools run.
                        raise breach
                    for call in calls:
                        await append_event_step(
                            run_id,
                            tenant_id,
                            f"tool:{call.tool_call_id}:call",
                            "tool_call",
                            {
                                "tool_call_id": call.tool_call_id,
                                "tool_name": call.tool_name,
                                "args": scrub_memory_tool_args(
                                    call.tool_name, to_jsonable_python(call.args_as_dict(), fallback=str), memory
                                ),
                            },
                        )
                    pending = calls
                    if structured:
                        awaiting_retry = n
                    n += 1
            if pending:
                await _record_tool_results(run_id, tenant_id, pending, _all_parts(run.all_messages()))
            assert run.result is not None
            new_messages = scrub_memory_messages(
                scrub_failed_attempts(ModelMessagesTypeAdapter.dump_python(run.result.new_messages(), mode="json")),
                memory,
            )
            result_output = run.result.output
    except Exception as exc:
        if isinstance(exc, UsageLimitExceeded) and active_run is not None:
            # AC.5: name the limit from the run's own counters, not from the message text.
            describe_usage_limit(exc, spec.limits, active_run.usage)
        if spec.output is not None:
            if isinstance(exc, UnexpectedModelBehavior) and "output retries" in str(exc):
                # Retries exhausted: the last candidate's error paths only, never its values.
                errors = _candidate_errors(spec, candidates.get(n - 1))
                raise OutputInvalid(spec.output.effective_max_retries(), tuple(errors)) from None
            if native_output_unsupported(exc):
                raise OutputModeUnsupported(str(mode)) from None
        raise
    if spec.output is not None:
        value = result_output
        if spec.output_processors:
            value = await _final_structured_output(run_id, tenant_id, spec, value)
            new_messages = scrub_retry_inputs(replace_responses(new_messages, stored))
        return canonical_json(value), new_messages, value
    if not spec.output_processors:
        return result_output, new_messages, None
    if final_text is None:  # the answer was never checked: fail closed (not expected to happen)
        raise OutputBlocked(-1, "unchecked")
    return final_text, processed_messages(new_messages, processed, strip_thinking_parts=True), None


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
    with log_context(tenant_id=tenant_id, run_id=run_id):  # records before run_span carry the ids too
        link = await start_run_step(run_id, tenant_id, agent_id, version, input_text, spec_json) or {}
        if link.get("superseded"):
            # Its hand-off failed (the run is failed with ENQUEUE_FAILED_ERROR) and a later turn has
            # started since (running it now would run two turns at once), or its conversation was
            # deleted meanwhile (a detached run must never write output again). No event, no model
            # call, no tool.
            log.warning("run %s not started: a later turn has started or its conversation was deleted", run_id)
            return {"status": "failed", "error": ENQUEUE_FAILED_ERROR, "error_code": "superseded"}
    with run_span(run_id, tenant_id, agent_id, version, traceparent=link.get("traceparent")) as span:
        log.info("run %s started: tenant=%s agent=%s version=%s", run_id, tenant_id, agent_id, version)
        result = await _run_body(run_id, tenant_id, agent_id, version, spec_json, input_text, link)
        set_run_outcome(span, result["status"], result.get("error_code"))
        log.info("run %s %s", run_id, result["status"])
        return result


def _check_request_context(spec: AgentSpec, request_context: dict[str, Any] | None) -> None:
    """The run's declared context must satisfy the spec's context fields before the model is called
    (no step: pure and deterministic). The API validates a caller's context, but a workflow agent
    step runs with an empty one, so an agent with a required field fails here. Undeclared keys are
    ignored as in RunDeps. RequestContextInvalid carries no values."""
    if not spec.context_fields:
        return
    try:
        validate_request_context(spec.context_fields, declared_context(spec, request_context))
    except RequestContextError:
        raise RequestContextInvalid() from None


async def _run_body(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    version: int,
    spec_json: str | dict[str, Any],
    input_text: str,
    link: dict[str, Any],
) -> dict[str, Any]:
    started: dict[str, Any] = {"agent_id": agent_id, "version": version, "input": input_text}
    if link.get("is_draft"):
        started["draft"] = True
    # Liveness from here on (not a step): a slow input moderation check must not look stalled.
    heartbeat = asyncio.create_task(_heartbeat(run_id, tenant_id))
    try:
        # Input processors (P0.17), only for a spec that has them: before `start`, so no event, log
        # or message ever holds text they redact or block. Their events follow `start`.
        pre: ChainRun | None = None
        pre_spec = _spec_or_none(spec_json)
        if fallback_disabled_by_override(pre_spec, link):
            started["fallback"] = "disabled_by_override"  # AC.5: the caller chose this model
        if pre_spec is not None and pre_spec.input_processors:
            pre = await run_chain(run_id, tenant_id, "input", pre_spec.input_processors, input_text)
            started["input"] = pre.text if pre.text is not None else WITHHELD_INPUT
            started["input_processed"] = True
        await append_event_step(run_id, tenant_id, "start", "run_started", started)
        if pre is not None:
            await _record_processor_results(run_id, tenant_id, pre)
            if pre.text is not None:
                input_text = pre.text
        history: list[ModelMessage] | None = None
        # A memory turn loads its history in eap.load_memory instead (below, after the checks).
        memory_turn = pre_spec is not None and pre_spec.memory is not None and link.get("conversation_id") is not None
        if link.get("conversation_id") is not None and (pre is None or pre.text is not None) and not memory_turn:
            history_json = await load_history_step(run_id, tenant_id, link["conversation_id"], link["turn"])
            history = ModelMessagesTypeAdapter.validate_python(history_json)
    except BaseException:
        heartbeat.cancel()
        raise
    try:
        if pre is not None and pre.text is None:  # blocked or failed: the main model is never called
            raise pre.error if pre.error is not None else _blocked_error(pre, "input")
        rt = get_runtime()
        spec = _parse_spec(spec_json)
        # Model governance: the effective model (an override recorded on the run, else the spec's)
        # is re-checked here, not only at publish or run start, because the policy may have changed
        # since. Fails before any model request is made.
        model_id = effective_model_id(spec, link)
        provider, _, model_name = model_id.partition(":")
        rt.settings.allowed_models.check(provider, model_name)
        for member in fallback_chain(spec, model_id)[1:]:
            # AC.5: a fallback removed from the allowlist after publish fails closed here.
            fallback_provider, _, fallback_name = member.partition(":")
            rt.settings.allowed_models.check(fallback_provider, fallback_name)
        check_no_hidden_tools(spec)  # knowledge.search is for tool runs only
        check_spec_tools(spec, rt.registry, rt.allowed_tools)
        request_context = link.get("request_context") or None
        _check_request_context(spec, request_context)
        deps = rt.deps_for(run_id, tenant_id, spec, request_context)
        if spec.memory is not None and link.get("conversation_id") is not None:
            # RM.12: the turn's memory snapshot (recorded), then its instructions block and tools.
            loaded = await load_memory_step(
                run_id, tenant_id, agent_id, str(link["conversation_id"]), int(link["turn"]),
                spec.memory.model_dump(mode="json"),
            )
            history = ModelMessagesTypeAdapter.validate_python(loaded["history"])
            recall = None
            if spec.memory.semantic_recall is not None:
                # RM.14: pre-turn recall on the processed input (recorded; never fails the turn).
                recall = await recall_before_turn(
                    run_id, tenant_id, agent_id, str(link["conversation_id"]), int(link["turn"]), spec,
                    input_text, loaded,
                )
            deps = with_memory(deps, memory_context(spec, agent_id, link["conversation_id"], loaded, recall))
        output, new_messages, output_json = await _drive_agent(run_id, deps, input_text, history, model_id)
    except Exception as exc:  # noqa: BLE001 - every failure ends the run with a durable run_failed
        failure = classify(exc)
        status = "needs_attention" if _is_side_effect_unknown(exc) else failure.status
        log_failure(
            log, f"run {status}", exc, code=failure.code, error_type=failure.error_type,
            run_id=run_id, tenant_id=tenant_id,
        )
        heartbeat.cancel()
        failed: dict[str, Any] = {"status": status, "error": failure.error, "error_code": failure.code}
        if failure.data:
            failed.update(failure.data)  # AC.5: {limit, value} / {limit, value, spent_usd} / {cause}
        if failure.reason is not None:
            failed["reason"] = failure.reason  # AC.4: e.g. output_blocked schema_invalid_after_processing
        await append_event_step(run_id, tenant_id, "end", "run_failed", failed)
        await finish_run_step(run_id, tenant_id, status, None, failure.error)
        return {"status": status, "error": failure.error, "error_code": failure.code}
    finally:
        heartbeat.cancel()
    # End event before the terminal status: a reader that sees a terminal status always finds the
    # complete event log.
    completed: dict[str, Any] = {"output": output}
    if spec.output is not None:  # AC.4: the canonical JSON text plus the object itself
        completed["output_json"] = output_json
    await append_event_step(run_id, tenant_id, "end", "run_completed", completed)
    await finish_run_step(
        run_id, tenant_id, "completed", output, None, new_messages,
        output_format="json" if spec.output is not None else None,
    )
    if spec.memory is not None and spec.memory.semantic_recall is not None and link.get("conversation_id") is not None:
        # RM.14: post-turn index of the processed input and final answer (never fails the turn).
        await index_after_turn(
            run_id, tenant_id, agent_id, str(link["conversation_id"]), int(link["turn"]), spec, input_text, output
        )
    if spec.memory is not None and spec.memory.observational is not None and link.get("conversation_id") is not None:
        # RM.13: post-turn observational memory, after the run is completed (never delays the answer,
        # never fails the turn).
        await observe_after_turn(run_id, tenant_id, agent_id, str(link["conversation_id"]), int(link["turn"]), spec)
    if spec.output is not None:
        return {"status": "completed", "output": output, "output_json": output_json}
    return {"status": "completed", "output": output}


__all__ = ["agent", "agent_run", "load_history_step", "AGENT_NAME", "TOOLSET_ID"]
