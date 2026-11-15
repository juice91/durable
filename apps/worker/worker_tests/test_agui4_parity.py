"""AGUI.4 parity: our AG-UI translator vs Pydantic AI's stock `AGUIEventStream` (the oracle, D6).

The same scripted model runs through the stock adapter in this process, and through our pipeline:
- **real worker** (Postgres + DBOS, in process) for chat, tool and structured-output runs: the
  worker writes the durable events and `AgUiFormatter` translates them;
- **worker-shaped events** for the frontend-tool and approval-gated runs, whose worker side is
  not built yet (AGUI.7, AC.6): the stock run's messages are written as the durable events the
  worker writes (`model:{n}:final`, `tool:{id}:call/result`, AC's `tool_approval_waiting` with
  `batch`, `run_completed {output: null, pending_tool_call_ids}`).

Normalisation (the plan's "AC events over AG-UI"): ids are ignored; text is compared per message
(chunks joined, empty messages dropped); tool calls by name and parsed args; `final_result` tool
events are dropped from the stock stream; interrupts are compared by `toolCallId` (stock
`int-<id>` vs ours `approval:{run}:tool:{id}`); structured output by `RUN_FINISHED.result`
(ours also sends the JSON as one text message, which the stock stream does not; it is dropped
from our side before comparing).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core import RunAgentInput
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import ExternalToolset
from pydantic_ai.ui.ag_ui import AGUIEventStream

from eap_api.ag_ui.conformance import lifecycle_errors, parse_frames, schema_errors
from eap_api.ag_ui.translate import AgUiFormatter, AgUiStreamContext
from eap_api.streams.native import DurableEvent
from eap_schemas import RunEvent

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "tests/fixtures/ag-ui/schema-1.0.json").read_text())
TS = datetime(2026, 11, 15, tzinfo=timezone.utc)
RUN_X, RUN_Y, RUN_Z = (str(uuid.UUID(int=i)) for i in (1, 2, 3))


# --------------------------------------------------------------------------- the stock side


def stock_events(agent: Agent, prompt: str, **run_kw: Any) -> tuple[list[dict[str, Any]], Any]:
    """Run `agent` through the stock AGUIEventStream; the AG-UI events as wire dicts, plus the
    agent result (for its messages)."""
    run_input = RunAgentInput(thread_id="t", run_id="r", messages=[], protocol_version="1.0")
    stream = AGUIEventStream(run_input=run_input, include_usage=False)
    captured: dict[str, Any] = {}

    async def go() -> list[dict[str, Any]]:
        out = []
        async with agent.run_stream_events(prompt, **run_kw) as native:

            async def tap() -> AsyncIterator[Any]:
                async for event in native:
                    if hasattr(event, "result"):
                        captured["result"] = event.result
                    yield event

            async for event in stream.transform_stream(tap()):
                out.append(json.loads(stream.encode_event(event)[len("data: "):]))
        assert not any(e["type"] == "RUN_ERROR" for e in out), out
        return out

    return asyncio.run(go()), captured.get("result")


# --------------------------------------------------------------------------- our side


def translate(events: list[RunEvent], run_id: str) -> list[dict[str, Any]]:
    formatter = AgUiFormatter(AgUiStreamContext(thread_id="t", run_id="r", native_run_id=run_id))
    chunks = formatter.start()
    for event in events:
        chunks += formatter.on_item(DurableEvent(event))
        if formatter.closed:
            break
    frames = parse_frames(chunks)
    assert lifecycle_errors(frames) == []
    out = [f.event for f in frames if f.event is not None]
    assert schema_errors(out, SCHEMA) == []
    return out


def worker_shaped_events(run_id: str, messages: list[ModelMessage], output: Any) -> list[RunEvent]:
    """The durable events the worker writes for these messages (its event keys and data shapes)."""
    rows: list[tuple[str, dict[str, Any]]] = [("run_started", {"input": "x"})]
    dumped = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    n = 0
    for message, raw in zip(messages, dumped, strict=True):
        if isinstance(message, ModelResponse):
            calls = [p for p in message.parts if isinstance(p, ToolCallPart)]
            text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
            data: dict[str, Any] = {"model_request": n, "text": text, "message": raw}
            if any(c.tool_name == "final_result" for c in calls):
                data["output_candidate"] = True
            rows.append(("model_message", data))
            for call in calls:
                if call.tool_name != "final_result":
                    rows.append(("tool_call", {"tool_call_id": call.tool_call_id, "tool_name": call.tool_name,
                                               "args": call.args_as_dict()}))
            n += 1
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name != "final_result":
                    rows.append(("tool_result", {"tool_call_id": part.tool_call_id, "tool_name": part.tool_name,
                                                 "status": "ok", "content": part.content}))
    if isinstance(output, DeferredToolRequests) and output.approvals:
        batch = [c.tool_call_id for c in output.approvals]
        for call in output.approvals:
            rows.append(("tool_approval_waiting", {
                "tool_call_id": call.tool_call_id, "tool_name": call.tool_name, "args": call.args_as_dict(),
                "deadline": "2026-11-16T00:00:00+00:00", "workflow_id": run_id, "batch": batch,
            }))
    elif isinstance(output, DeferredToolRequests):
        rows.append(("run_completed", {"output": None, "pending_tool_call_ids": [c.tool_call_id for c in output.calls]}))
    elif isinstance(output, BaseModel):
        value = output.model_dump(mode="json")
        rows.append(("run_completed", {"output": json.dumps(value, sort_keys=True, separators=(",", ":")),
                                       "output_json": value}))
    else:
        rows.append(("run_completed", {"output": output}))
    return [RunEvent(run_id=run_id, seq=i, type=t, data=d, ts=TS) for i, (t, d) in enumerate(rows)]


# --------------------------------------------------------------------------- normalisation


def normalize(events: list[dict[str, Any]], *, ours: bool, structured: bool = False) -> list[tuple]:
    texts: dict[str, list[str]] = {}
    order: list[tuple] = []
    calls: dict[str, dict[str, Any]] = {}
    hidden: set[str] = set()
    output_args: dict[str, str] = {}
    for event in events:
        kind = event["type"]
        if kind == "TEXT_MESSAGE_START":
            texts[event["messageId"]] = []
            order.append(("text", event["messageId"]))
        elif kind == "TEXT_MESSAGE_CONTENT":
            texts[event["messageId"]].append(event["delta"])
        elif kind == "TOOL_CALL_START":
            if event["toolCallName"] == "final_result":
                hidden.add(event["toolCallId"])
                continue
            calls[event["toolCallId"]] = {"name": event["toolCallName"], "args": ""}
            order.append(("call", event["toolCallId"]))
        elif kind == "TOOL_CALL_ARGS" and event["toolCallId"] in hidden:
            output_args[event["toolCallId"]] = output_args.get(event["toolCallId"], "") + event["delta"]
        elif kind == "TOOL_CALL_ARGS":
            calls[event["toolCallId"]]["args"] += event["delta"]
        elif kind == "TOOL_CALL_RESULT" and event["toolCallId"] not in hidden:
            order.append(("result", event["toolCallId"]))
        elif kind == "RUN_FINISHED":
            order.append(("finished", event))
        elif kind == "RUN_ERROR":
            order.append(("error",))
    call_index = {cid: i for i, cid in enumerate(c for k, c, *_ in [o + (None,) for o in order] if k == "call")}
    out: list[tuple] = []
    for item in order:
        if item[0] == "text":
            text = "".join(texts[item[1]])
            if text:
                out.append(("text", text))
        elif item[0] == "call":
            call = calls[item[1]]
            out.append(("call", call["name"], json.loads(call["args"] or "{}")))
        elif item[0] == "result":
            out.append(("result", call_index.get(item[1])))
        elif item[0] == "finished":
            outcome = item[1].get("outcome") or {"type": "success"}
            pending = [call_index.get(i) for i in outcome.get("pendingToolCallIds") or []]
            interrupts = [(i["reason"], call_index.get(i.get("toolCallId"))) for i in outcome.get("interrupts") or []]
            result = item[1].get("result")
            if result is None and output_args and not ours:
                # The stock stream has no `result`: its output is the last `final_result` call's args.
                result = json.loads(list(output_args.values())[-1])
            out.append(("finished", outcome["type"], pending, interrupts, result))
        else:
            out.append(item)
    if ours and structured:
        # Our one assistant text message with the canonical JSON (not in the stock stream).
        finished = out[-1]
        out = [o for o in out if not (o[0] == "text" and finished[4] is not None and json.loads(o[1]) == finished[4])]
    return out


def assert_parity(stock: list[dict[str, Any]], ours: list[dict[str, Any]], structured: bool = False) -> list[tuple]:
    a = normalize(stock, ours=False, structured=structured)
    b = normalize(ours, ours=True, structured=structured)
    assert b == a
    return a


# --------------------------------------------------------------------------- scripted models


def plan_model(plan) -> FunctionModel:
    """A FunctionModel from `plan(messages, info) -> list of ('text', str) | ('tool', name, args)`."""

    def parts_of(messages, info):
        return plan(messages, info)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = []
        for step in parts_of(messages, info):
            parts.append(TextPart(step[1]) if step[0] == "text" else ToolCallPart(step[1], step[2]))
        return ModelResponse(parts=parts)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        index = 0
        for step in parts_of(messages, info):
            if step[0] == "text":
                for i in range(0, len(step[1]), 4):
                    yield step[1][i : i + 4]
            else:
                yield {index: DeltaToolCall(name=step[1], json_args=json.dumps(step[2]))}
                index += 1

    return FunctionModel(respond, stream_function=stream)


def returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


# --------------------------------------------------------------------------- worker-shaped parity


def test_parity_frontend_tool_run():
    def plan(messages, info):
        return [("text", "Let me ask."), ("tool", "pick_color", {"palette": ["red", "blue"]})]

    agent = Agent(plan_model(plan), output_type=[str, DeferredToolRequests])
    toolset = ExternalToolset([ToolDefinition(name="pick_color", parameters_json_schema={
        "type": "object", "properties": {"palette": {"type": "array"}}})])
    stock, result = stock_events(agent, "choose", toolsets=[toolset])
    ours = translate(worker_shaped_events(RUN_X, result.new_messages(), result.output), RUN_X)
    normalized = assert_parity(stock, ours)
    assert normalized[-1][:3] == ("finished", "success", [0])


def test_parity_approval_gated_run_with_a_batch_of_two():
    def plan(messages, info):
        return [("tool", "pay", {"amount": 5}), ("tool", "pay", {"amount": 7})]

    agent = Agent(plan_model(plan), output_type=[str, DeferredToolRequests])

    @agent.tool_plain(requires_approval=True)
    def pay(amount: int) -> str:
        return f"paid {amount}"

    stock, result = stock_events(agent, "pay")
    assert isinstance(result.output, DeferredToolRequests) and len(result.output.approvals) == 2
    ours = translate(worker_shaped_events(RUN_Y, result.new_messages(), result.output), RUN_Y)
    normalized = assert_parity(stock, ours)
    assert normalized[-1][:4] == ("finished", "interrupt", [], [("tool_call", 0), ("tool_call", 1)])


def test_parity_chat_tool_and_structured_runs_from_worker_shaped_events():
    class Answer(BaseModel):
        answer: str
        score: int

    def tool_plan(messages, info):
        if not returns(messages):
            return [("tool", "lookup", {"q": "printer"})]
        return [("text", f"Found: {returns(messages)[-1].content}")]

    tool_agent = Agent(plan_model(tool_plan))

    @tool_agent.tool_plain
    def lookup(q: str) -> dict[str, Any]:
        return {"hit": q}

    def structured_plan(messages, info):
        return [("tool", "final_result", {"answer": "ok", "score": 3})]

    cases = [
        (Agent(plan_model(lambda m, i: [("text", "Hello there, friend.")])), False),
        (tool_agent, False),
        (Agent(plan_model(structured_plan), output_type=Answer), True),
    ]
    for agent, structured in cases:
        stock, result = stock_events(agent, "hi")
        ours = translate(worker_shaped_events(RUN_Z, result.new_messages(), result.output), RUN_Z)
        assert_parity(stock, ours, structured=structured)


# --------------------------------------------------------------------------- real worker parity


def _worker_events(worker, client, tenant, spec: dict[str, Any], prompt: str) -> tuple[str, list[RunEvent]]:
    from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal

    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt,
                         app_version=APP_VERSION)
    wait_terminal(worker.run_store, tenant, run_id)
    rows = event_rows(worker.engine, run_id)
    return str(run_id), [RunEvent(run_id=run_id, seq=s, type=t, data=d, ts=TS) for s, _k, t, d in rows]


@pytest.mark.usefixtures("worker")
def test_parity_with_the_real_worker_chat_tool_and_structured(worker, client, tenant):
    from eap_worker.models import scripted_structured_model, scripted_ticket_model
    from worker_tests.conftest import ticket_spec

    # Chat: test:ticket without its tool answers `Echo: <prompt>`.
    run_id, events = _worker_events(worker, client, tenant, ticket_spec(tools=()), "hello parity")
    stock, _ = stock_events(Agent(scripted_ticket_model()), "hello parity")
    assert_parity(stock, translate(events, run_id))

    # Tool: tickets.create, then the confirmation naming the ticket the worker created. The stock
    # agent's tool returns exactly what the worker's tool returned.
    run_id, events = _worker_events(worker, client, tenant, ticket_spec(), "printer broken")
    worker_result = next(e.data for e in events if e.type == "tool_result")
    agent = Agent(scripted_ticket_model())
    agent.tool_plain(name=worker_result["tool_name"])(lambda title, body: worker_result["content"])
    stock, _ = stock_events(agent, "printer broken")
    normalized = assert_parity(stock, translate(events, run_id))
    assert [n[0] for n in normalized] == ["call", "result", "text", "finished"]

    # Structured output (tool mode, AC.4): the worker accepts the candidate; result == output_json.
    from worker_tests.test_step_layout_ac import SCHEMA

    spec = ticket_spec(tools=(), model="structured")
    spec["output"] = {"json_schema": SCHEMA, "mode": "tool"}
    run_id, events = _worker_events(worker, client, tenant, spec, "rate this")
    completed = next(e for e in events if e.type == "run_completed")
    assert "output_json" in completed.data, [e.type for e in events]

    class Out(BaseModel):
        answer: str
        score: int

    stock, _ = stock_events(Agent(scripted_structured_model(), output_type=Out), "rate this")
    ours = translate(events, run_id)
    assert_parity(stock, ours, structured=True)
    assert ours[-1]["result"] == completed.data["output_json"]
    assert "final_result" not in json.dumps(ours)
