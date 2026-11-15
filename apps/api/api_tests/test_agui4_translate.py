"""AGUI.4: the AG-UI formatter over synthetic durable events (no database).

Every output is checked against the vendored AG-UI 1.0 schema and the lifecycle checker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from eap_api.ag_ui.conformance import events_of, lifecycle_errors, parse_frames, schema_errors
from eap_api.ag_ui.snapshot import Transcript, turn_messages
from eap_api.ag_ui.translate import (
    KEEP_ALIVE,
    NEEDS_ATTENTION_MESSAGE,
    RESPONSE_SCHEMA,
    AgUiFormatter,
    AgUiStreamContext,
    token_usage,
)
from eap_api.streams.native import ChildScope, Delta, DurableEvent, Heartbeat, RunSummary, Status
from eap_api.streams.resume import (
    LAST_EVENT_ID_DETAIL,
    LastEventIdError,
    frame_id,
    parse_last_event_id,
    plan_stream,
    with_frame_id,
)
from eap_schemas import RunEvent

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "tests/fixtures/ag-ui/schema-1.0.json").read_text())
NATIVE = "6c1f7d8e-0000-4000-8000-000000000001"
CHILD = "6c1f7d8e-0000-4000-8000-0000000000c1"
TS = datetime(2026, 11, 15, tzinfo=timezone.utc)
RUN_INFO = {"run_id": NATIVE, "conversation_id": "conv", "turn": 0}


class Log:
    """Builds a durable event log with gap-free seq."""

    def __init__(self, run_id: str = NATIVE) -> None:
        self.run_id = run_id
        self.events: list[RunEvent] = []

    def add(self, type_: str, **data: Any) -> DurableEvent:
        event = RunEvent(run_id=self.run_id, seq=len(self.events), type=type_, data=data, ts=TS)
        self.events.append(event)
        return DurableEvent(event)


def delta(n: int, text: str, attempt: str = "a", run_id: str = NATIVE, scope: ChildScope | None = None) -> Delta:
    return Delta({"run_id": run_id, "model_request": n, "attempt": attempt, "delta": text}, scope)


def status(display: str = "running") -> Status:
    return Status({"id": NATIVE, "status": "running", "display_status": display})


def call_message(*calls: tuple[str, str], text: str = "") -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"part_kind": "text", "content": text}] if text else []
    parts += [{"part_kind": "tool-call", "tool_name": name, "tool_call_id": cid, "args": {}} for cid, name in calls]
    return {"kind": "response", "parts": parts}


def drive(ctx: AgUiStreamContext, items: list[Any], *, resume_after: int | None = None) -> tuple[list[str], AgUiFormatter]:
    fmt = AgUiFormatter(ctx)
    chunks = fmt.start() if resume_after is None else fmt.resume_frames(resume_after)
    for item in items:
        chunks += fmt.on_item(item)
        if fmt.closed:
            break
    else:
        chunks += fmt.finish("terminal")
    return chunks, fmt


def agent_ctx(**kw: Any) -> AgUiStreamContext:
    return AgUiStreamContext(thread_id="thread-1", run_id=kw.pop("run_id", "run-1"), native_run_id=NATIVE,
                             run_info=RUN_INFO, **kw)


def check(chunks: list[str], **kw: Any) -> list[dict[str, Any]]:
    frames = parse_frames(chunks)
    events = events_of(frames)
    assert schema_errors(events, SCHEMA) == []
    assert lifecycle_errors(frames, **kw) == []
    return events


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] if e["type"] != "CUSTOM" else f"CUSTOM {e['name']}" for e in events]


# --------------------------------------------------------------------------- agent sequences


def chat_items() -> tuple[list[Any], Log]:
    log = Log()
    items = [
        status(),
        log.add("run_started", input="hi"),
        delta(0, "Hel"),
        Heartbeat(),
        status(),
        delta(0, "lo"),
        log.add("model_message", model_request=0, text="Hello", message={"kind": "response", "parts": []}),
        RunSummary({"model_requests": [
            {"request_key": "model:0", "model_id": "openai:gpt-x", "input_tokens": 5, "output_tokens": 2},
        ]}),
        log.add("run_completed", output="Hello"),
    ]
    return items, log


def test_chat():
    items, _ = chat_items()
    chunks, _ = drive(agent_ctx(), items)
    events = check(chunks)
    assert types(events) == [
        "RUN_STARTED", "CUSTOM pydanticos.run", "CUSTOM pydanticos.status", "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED",
    ]
    assert events[0] == {"type": "RUN_STARTED", "threadId": "thread-1", "runId": "run-1", "protocolVersion": "1.0"}
    assert events[1]["value"] == RUN_INFO
    assert events[3] == {"type": "TEXT_MESSAGE_START", "messageId": "run-1-m0", "role": "assistant"}
    assert events[-1]["outcome"] == {"type": "success"}
    assert events[-1]["usage"] == [
        {"provider": "openai", "model": "gpt-x", "inputTokens": 5, "outputTokens": 2, "totalTokens": 7}
    ]
    assert KEEP_ALIVE in chunks
    assert sum(1 for c in chunks if c.startswith("data:") and "pydanticos.status" in c) == 1  # changes only


def test_tool_chain():
    log = Log()
    items = [
        log.add("run_started", input="hi"),
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "tickets.create"))),
        log.add("tool_call", tool_call_id="c1", tool_name="tickets.create", args={"b": 2, "a": 1}),
        log.add("tool_result", tool_call_id="c1", tool_name="tickets.create", status="ok", content={"id": 7}),
        delta(1, "Done"),
        log.add("model_message", model_request=1, text="Done", message={"kind": "response", "parts": []}),
        log.add("run_completed", output="Done"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert types(events) == [
        "RUN_STARTED", "CUSTOM pydanticos.run", "TEXT_MESSAGE_START", "TEXT_MESSAGE_END", "TOOL_CALL_START",
        "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END", "RUN_FINISHED",
    ]
    assert events[4] == {"type": "TOOL_CALL_START", "toolCallId": "c1", "toolCallName": "tickets.create",
                         "parentMessageId": "run-1-m0"}
    assert events[5]["delta"] == '{"a":1,"b":2}'
    assert events[7] == {"type": "TOOL_CALL_RESULT", "messageId": "run-1-t-c1", "toolCallId": "c1",
                         "content": '{"id":7}', "role": "tool"}


def test_diverged_attempt_gets_a_correction_snapshot():
    log = Log()
    history = [{"id": "u-1", "role": "user", "content": "hi"}]
    items = [
        log.add("run_started", input="hi"),
        delta(0, "Hel", "a"),
        delta(0, "Bye", "b"),
        delta(0, "!", "a"),
        log.add("model_message", model_request=0, text="Bye", message={"kind": "response", "parts": []}),
        log.add("run_completed", output="Bye"),
    ]
    events = check(drive(agent_ctx(history=history), items)[0])
    assert types(events)[2:] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
                                 "MESSAGES_SNAPSHOT", "RUN_FINISHED"]
    assert [e.get("delta") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"] == ["Hel"]
    assert events[5]["messages"] == [history[0], {"id": "run-1-m0", "role": "assistant", "content": "Bye"}]


def test_streamed_text_equal_to_final_needs_no_snapshot_and_later_deltas_wait():
    log = Log()
    items = [
        log.add("run_started"),
        delta(0, "A"),
        delta(1, "B"),  # arrives before model:0:final
        log.add("model_message", model_request=0, text="A", message=call_message(("c1", "t"), text="A")),
        log.add("tool_call", tool_call_id="c1", tool_name="t", args={}),
        log.add("tool_result", tool_call_id="c1", tool_name="t", status="ok", content="ok"),
        log.add("model_message", model_request=1, text="B", message={"kind": "response", "parts": []}),
        log.add("run_completed", output="B"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert "MESSAGES_SNAPSHOT" not in types(events)
    starts = [e["messageId"] for e in events if e["type"] == "TEXT_MESSAGE_START"]
    assert starts == ["run-1-m0", "run-1-m1"]


def test_no_deltas_gives_one_start_content_end():
    log = Log()
    items = [log.add("run_started"), log.add("model_message", model_request=0, text="Hi", message={}),
             log.add("run_completed", output="Hi")]
    events = check(drive(agent_ctx(), items)[0])
    assert types(events)[2:] == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED"]


def test_failure_mid_text_closes_the_message_then_run_error():
    log = Log()
    items = [log.add("run_started"), delta(0, "par"),
             log.add("run_failed", status="failed", error="The model provider failed.", error_code="model_error")]
    events = check(drive(agent_ctx(), items)[0])
    assert types(events)[-2:] == ["TEXT_MESSAGE_END", "RUN_ERROR"]
    assert events[-1] == {"type": "RUN_ERROR", "message": "The model provider failed.", "code": "model_error"}


def test_needs_attention_is_a_run_error_with_its_code():
    log = Log()
    items = [log.add("run_started"),
             log.add("run_failed", status="needs_attention", error="raw text", error_code="side_effect_unknown")]
    events = check(drive(agent_ctx(), items)[0])
    assert events[-1] == {"type": "RUN_ERROR", "message": NEEDS_ATTENTION_MESSAGE, "code": "needs_attention"}


def test_pending_client_tools_and_client_results_are_not_re_emitted():
    log = Log()
    items = [
        log.add("run_started"),
        log.add("tool_result", tool_call_id="c0", tool_name="pick_color", status="ok", content="red", source="client"),
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "pick_color"))),
        log.add("tool_call", tool_call_id="c1", tool_name="pick_color", args={"q": "x"}),
        log.add("run_completed", output=None, pending_tool_call_ids=["c1"]),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert "TOOL_CALL_RESULT" not in types(events)
    assert events[-1]["outcome"] == {"type": "success", "pendingToolCallIds": ["c1"]}


def test_retrieve_result_is_an_ordinary_tool_result_and_silent_events_produce_nothing():
    log = Log()
    items = [
        log.add("run_started"),
        log.add("embedding", model_id="m", inputs=1, input_tokens=3, cost_usd=0),
        log.add("model_message", model_request=0, text="", message=call_message(("r1", "retrieve"))),
        log.add("tool_call", tool_call_id="r1", tool_name="retrieve", args={"query": "q"}),
        log.add("tool_result", tool_call_id="r1", tool_name="retrieve", status="ok", content="[1] text",
                sources=[{"n": 1, "doc": "d"}]),
        log.add("memory_updated", kind="working"),
        log.add("model_message", model_request=1, text="See [1]", message={}),
        log.add("run_completed", output="See [1]"),
        log.add("memory_observe_failed", error_code="x"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
    assert result["content"] == "[1] text" and "sources" not in json.dumps(events)
    assert "embedding" not in json.dumps(events) and "memory" not in json.dumps(events)


def test_processor_results_are_custom_without_text():
    log = Log()
    items = [log.add("run_started"),
             log.add("processor_result", phase="input", index=0, processor_type="pii", action="redact",
                     match_counts={"email": 1}),
             log.add("model_message", model_request=0, text="ok", message={}), log.add("run_completed", output="ok")]
    events = check(drive(agent_ctx(), items)[0])
    custom = next(e for e in events if e.get("name") == "pydanticos.processor_result")
    assert custom["value"] == {"phase": "input", "type": "pii", "action": "redact", "matches": {"email": 1}}


# --------------------------------------------------------------------------- AC sequences


def test_structured_run():
    log = Log()
    items = [
        log.add("run_started"),
        log.add("model_message", model_request=0, text="", output_candidate=True,
                message=call_message(("o1", "final_result"))),
        log.add("output_retry", model_request=0, errors=["/age"], cause="secret value"),
        log.add("model_message", model_request=1, text="", output_candidate=True,
                message=call_message(("o2", "final_result"))),
        log.add("run_completed", output='{"age":3}', output_json={"age": 3}),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert types(events)[2:] == ["CUSTOM pydanticos.output_retry", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
                                 "TEXT_MESSAGE_END", "RUN_FINISHED"]
    assert events[2]["value"] == {"model_request": 0, "errors": ["/age"]}
    assert events[3]["messageId"] == "run-1-m1" and events[4]["delta"] == '{"age":3}'
    assert events[-1]["result"] == {"age": 3}
    assert "final_result" not in json.dumps(events) and "secret" not in json.dumps(events)


def test_fallback_answer_gives_custom_model_and_per_model_usage():
    log = Log()
    items = [
        log.add("run_started"),
        delta(0, "x", "a1"),
        delta(0, "Hi", "a2"),
        log.add("model_message", model_request=0, text="Hi", message={}, model_id="anthropic:b",
                requested_model_id="openai:a", fallback_index=1,
                failed_attempts=[{"model_id": "openai:a", "outcome": "error", "error_type": "RateLimit", "x": 1}]),
        RunSummary({"model_requests": [
            {"request_key": "model:0:attempt:0", "model_id": "openai:a", "input_tokens": 4, "output_tokens": 0},
            {"request_key": "model:0", "model_id": "anthropic:b", "input_tokens": 4, "output_tokens": 1,
             "cache_read_tokens": 2},
            {"request_key": "embed:recall:0", "model_id": "openai:e", "input_tokens": 9, "operation": "embedding"},
            {"request_key": "moderation:output:0", "model_id": "openai:mod", "input_tokens": 9},
        ]}),
        log.add("run_completed", output="Hi"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    t = types(events)
    assert t.index("CUSTOM pydanticos.model") < t.index("TEXT_MESSAGE_END")
    model = next(e for e in events if e.get("name") == "pydanticos.model")["value"]
    assert model == {"model_request": 0, "model_id": "anthropic:b", "requested_model_id": "openai:a",
                     "fallback_index": 1,
                     "failed_attempts": [{"model_id": "openai:a", "outcome": "error", "error_type": "RateLimit"}]}
    assert [(u["provider"], u["model"]) for u in events[-1]["usage"]] == [("openai", "a"), ("anthropic", "b")]
    assert events[-1]["usage"][1]["cachedInputTokens"] == 2


def gated_items(batch: list[str]) -> tuple[list[Any], Log]:
    log = Log()
    items = [log.add("run_started"),
             log.add("model_message", model_request=0, text="", message=call_message(*[(b, "pay") for b in batch]))]
    for i, cid in enumerate(batch):
        if i == 0:
            items.append(log.add("tool_call", tool_call_id=cid, tool_name="pay", args={"amount": 5}))
        items.append(status("waiting_approval") if i == 0 else status("waiting_approval"))
        items.append(log.add("tool_approval_waiting", tool_call_id=cid, tool_name="pay", args={"amount": 5},
                             deadline="2026-11-16T00:00:00+00:00", workflow_id=NATIVE, batch=batch))
    items.append(log.add("tool_approved", tool_call_id=batch[0], decided_by="u", comment="c"))  # never reached
    return items, log


@pytest.mark.parametrize("batch", [["g1"], ["g1", "g2"]])
def test_gated_calls_end_with_one_interrupt_outcome(batch):
    items, _ = gated_items(batch)
    chunks, fmt = drive(agent_ctx(), items)
    events = check(chunks)
    assert fmt.closed
    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED"
    interrupts = finished["outcome"]["interrupts"]
    assert finished["outcome"]["type"] == "interrupt"
    assert [i["id"] for i in interrupts] == [f"approval:{NATIVE}:tool:{b}" for b in batch]
    assert interrupts[0] == {
        "id": f"approval:{NATIVE}:tool:g1", "reason": "tool_call", "toolCallId": "g1",
        "message": 'Approve pay({"amount":5})?', "responseSchema": RESPONSE_SCHEMA,
        "expiresAt": "2026-11-16T00:00:00+00:00", "metadata": {"pydanticos": {"run_id": NATIVE, "tool_name": "pay"}},
    }
    started = [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_START"]
    assert started == batch  # the second call is announced from its waiting event
    required = [e["value"] for e in events if e.get("name") == "pydanticos.approval_required"]
    assert [r["key"] for r in required] == batch and required[0]["kind"] == "tool"
    assert "CUSTOM pydanticos.status" in types(events)
    assert "pydanticos.approval_decided" not in json.dumps(events)


def test_resumed_run_with_approved_denied_and_timed_out_decisions():
    log = Log()
    prior = [
        log.add("run_started"),
        log.add("model_message", model_request=0, text="", message=call_message(("a", "pay"), ("b", "pay"), ("c", "pay"))),
        *[log.add("tool_approval_waiting", tool_call_id=x, tool_name="pay", args={}, deadline="2026-11-16T00:00:00Z",
                  batch=["a", "b", "c"]) for x in "abc"],
    ]
    from_seq = prior[-1].seq
    items = [
        status("running"),
        log.add("tool_approved", tool_call_id="a", decided_by="u"),
        log.add("tool_denied", tool_call_id="b", decided_by="u", comment="no"),
        log.add("tool_approval_timed_out", tool_call_id="c"),
        log.add("tool_result", tool_call_id="a", tool_name="pay", status="ok", content="paid"),
        log.add("tool_result", tool_call_id="b", tool_name="pay", status="denied", content="Denied: no"),
        log.add("tool_result", tool_call_id="c", tool_name="pay", status="denied", content="Timed out"),
        log.add("model_message", model_request=1, text="Done", message={}),
        log.add("run_completed", output="Done"),
    ]
    ctx = agent_ctx(run_id="run-2", from_seq=from_seq, prior_events=[p.event for p in prior])
    events = check(drive(ctx, items)[0], known_tool_calls=["a", "b", "c"])
    decided = [e["value"] for e in events if e.get("name") == "pydanticos.approval_decided"]
    assert decided == [{"kind": "tool", "key": "a", "decision": "approved"},
                       {"kind": "tool", "key": "b", "decision": "denied"},
                       {"kind": "tool", "key": "c", "decision": "timed_out"}]
    assert "decided_by" not in json.dumps(events) and '"no"' not in json.dumps(decided)
    assert "TOOL_CALL_START" not in types(events)  # the calls were shown in the interrupted run
    assert [e["content"] for e in events if e["type"] == "TOOL_CALL_RESULT"] == ["paid", "Denied: no", "Timed out"]
    assert events[0]["runId"] == "run-2"


# --------------------------------------------------------------------------- workflows


def wf_ctx(**kw: Any) -> AgUiStreamContext:
    return AgUiStreamContext(thread_id="wf-thread", run_id=kw.pop("run_id", "wrun-1"), native_run_id=NATIVE,
                             kind="workflow", run_info={"run_id": NATIVE}, **kw)


def workflow_log() -> tuple[list[Any], Log, Log]:
    log, child = Log(), Log(CHILD)
    scope = ChildScope("draft", CHILD)

    def c(type_: str, **data: Any) -> DurableEvent:
        return DurableEvent(child.add(type_, **data).event, scope)

    items = [
        log.add("run_started", kind="workflow", workflow_id="w", input={"x": 1}),
        log.add("step_started", step_id="draft", step_type="agent"),
        c("run_started", input="x"),
        Delta({"run_id": CHILD, "model_request": 0, "attempt": "a", "delta": "Dra"}, scope),
        c("model_message", model_request=0, text="Draft", message={}),
        c("run_completed", output="Draft"),
        log.add("step_completed", step_id="draft", output="Draft"),
        log.add("step_started", step_id="lookup", step_type="tool"),
        log.add("step_completed", step_id="lookup", output={"big": "x" * 5000}),
        log.add("step_started", step_id="route", step_type="branch"),
        log.add("step_completed", step_id="route", output={"case": 0, "next": "fan"}),
        log.add("step_started", step_id="fan", step_type="parallel"),
        log.add("step_started", step_id="a/b", step_type="tool"),
        log.add("step_completed", step_id="a/b", output=1),
        log.add("step_completed", step_id="fan", output=None),
        log.add("step_started", step_id="ok", step_type="approval"),
        log.add("step_waiting", step_id="ok", prompt="Ship it?", timeout_hours=1, workflow_id=NATIVE,
                deadline="2026-11-16T00:00:00+00:00"),
    ]
    return items, log, child


def test_workflow_with_agent_tool_branch_parallel_and_approval_steps():
    items, _, _ = workflow_log()
    chunks, fmt = drive(wf_ctx(), items)
    events = check(chunks)
    t = types(events)
    assert t[:3] == ["RUN_STARTED", "CUSTOM pydanticos.run", "STATE_SNAPSHOT"]
    assert [e["stepName"] for e in events if e["type"] == "STEP_STARTED"] == ["draft", "lookup", "route", "fan", "a/b", "ok"]
    text = [e for e in events if e["type"].startswith("TEXT_MESSAGE")]
    assert {e["messageId"] for e in text} == {"wrun-1-draft-m0"}
    deltas = [e for e in events if e["type"] == "STATE_DELTA"]
    flat = [op for e in deltas for op in e["delta"]]
    assert {"op": "add", "path": "/steps/a~1b", "value": {"status": "running", "type": "tool"}} in flat
    assert {"op": "add", "path": "/steps/lookup/output_truncated", "value": True} in flat
    assert {"op": "add", "path": "/steps/route/output", "value": {"case": 0, "next": "fan"}} in flat
    finished = events[-1]
    assert finished["outcome"]["interrupts"] == [{
        "id": f"approval:{NATIVE}:ok", "reason": "confirmation", "message": "Ship it?",
        "responseSchema": RESPONSE_SCHEMA, "expiresAt": "2026-11-16T00:00:00+00:00",
    }]
    assert t[-2] == "STEP_FINISHED" and events[-2]["stepName"] == "ok"
    assert fmt.closed
    # Child-run frames carry no frame id (their seq is not the workflow's).
    for chunk in chunks:
        if "wrun-1-draft-m0" in chunk:
            assert not chunk.startswith("id:")


def test_workflow_resume_run_reopens_the_waiting_step_and_finishes():
    items, log, _ = workflow_log()
    prior = [i.event for i in items if isinstance(i, DurableEvent) and i.scope is None]
    from_seq = prior[-1].seq
    rest = [
        log.add("step_approved", step_id="ok", decided_by="u", comment="fine"),
        log.add("step_completed", step_id="ok", output={"approved": True}),
        log.add("run_completed", output='{"done":true}', result={"done": True}),
    ]
    events = check(drive(wf_ctx(run_id="wrun-2", from_seq=from_seq, prior_events=prior), rest)[0])
    t = types(events)
    assert t[:4] == ["RUN_STARTED", "CUSTOM pydanticos.run", "STATE_SNAPSHOT", "STEP_STARTED"]
    assert events[2]["snapshot"]["steps"]["ok"]["status"] == "waiting"
    assert "CUSTOM pydanticos.approval_decided" in t
    assert events[-1]["result"] == {"done": True} and events[-1]["outcome"] == {"type": "success"}


def test_step_failure_closes_the_step_before_run_error():
    log = Log()
    items = [log.add("run_started"), log.add("step_started", step_id="s", step_type="tool"),
             log.add("step_failed", step_id="s", status="failed", error="Tool failed.", error_code="tool_error"),
             log.add("run_failed", status="failed", error="Tool failed.", error_code="tool_error")]
    events = check(drive(wf_ctx(), items)[0])
    t = types(events)
    assert t.index("STEP_FINISHED") < t.index("RUN_ERROR")
    assert events[-1]["code"] == "tool_error"


def test_workflow_agent_step_whose_child_waits_for_a_tool_approval():
    log, child = Log(), Log(CHILD)
    scope = ChildScope("draft", CHILD)
    items = [
        log.add("run_started"),
        log.add("step_started", step_id="draft", step_type="agent"),
        DurableEvent(child.add("run_started").event, scope),
        DurableEvent(child.add("model_message", model_request=0, text="", message=call_message(("g", "pay"))).event, scope),
        DurableEvent(child.add("tool_approval_waiting", tool_call_id="g", tool_name="pay", args={}, batch=["g"]).event, scope),
        log.add("step_waiting", step_id="draft"),  # the workflow's own view; never reached
    ]
    events = check(drive(wf_ctx(), items)[0])
    interrupt = events[-1]["outcome"]["interrupts"][0]
    assert interrupt["id"] == f"approval:{CHILD}:tool:g"
    assert interrupt["metadata"] == {"pydanticos": {"run_id": CHILD, "tool_name": "pay"}}


# --------------------------------------------------------------------------- ids, resume, replay


def test_ids_are_stable_across_replays_and_frame_ids_sit_on_the_last_frame_of_each_event():
    items, log = chat_items()
    first, _ = drive(agent_ctx(), items)
    second, _ = drive(agent_ctx(), items)
    assert first == second
    frames = parse_frames(first)
    ids = [f.id for f in frames if f.id is not None]
    assert ids == ["run-1:1", "run-1:2"]  # run_started yields nothing; model_message, run_completed
    # The id is on the TEXT_MESSAGE_END of model:0:final, not on frames before it.
    end = next(i for i, f in enumerate(frames) if f.event and f.event["type"] == "TEXT_MESSAGE_END")
    assert frames[end].id == "run-1:1"
    assert all(f.id is None for f in frames[:end])


def test_attach_from_start_sends_a_messages_snapshot():
    items, _ = chat_items()
    history = [{"id": "u", "role": "user", "content": "hi"}]
    events = check(drive(agent_ctx(snapshot_on_start=True, history=history), items)[0])
    assert types(events)[:3] == ["RUN_STARTED", "CUSTOM pydanticos.run", "MESSAGES_SNAPSHOT"]
    assert events[2]["messages"] == history


def test_continuation_has_no_run_started_snapshot_first_and_no_duplicates():
    log = Log()
    all_items = [
        log.add("run_started"),
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "t"))),
        log.add("tool_call", tool_call_id="c1", tool_name="t", args={}),
        log.add("tool_result", tool_call_id="c1", tool_name="t", status="ok", content="r"),
        log.add("model_message", model_request=1, text="Fin", message={}),
        log.add("run_completed", output="Fin"),
    ]
    n = 3  # the client holds frames through seq 3 (the tool result)
    prior = [i.event for i in all_items if i.seq <= n]
    after = [i for i in all_items if i.seq > n]
    chunks, _ = drive(agent_ctx(prior_events=prior), after, resume_after=n)
    events = check(chunks, continuation=True, run_id="run-1", known_tool_calls=["c1"])
    t = types(events)
    assert t[:2] == ["CUSTOM pydanticos.run", "MESSAGES_SNAPSHOT"] and "RUN_STARTED" not in t
    assert events[1]["messages"] == [
        {"id": "run-1-m0", "role": "assistant",
         "toolCalls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"id": "run-1-t-c1", "role": "tool", "toolCallId": "c1", "content": "r"},
    ]
    assert "TOOL_CALL_START" not in t and "TOOL_CALL_RESULT" not in t
    # After the snapshot, the continuation is exactly what one stream sends after frame `run-1:3`.
    full = parse_frames(drive(agent_ctx(), all_items)[0])
    cut = next(i for i, f in enumerate(full) if f.id == f"run-1:{n}")
    assert parse_frames(chunks)[2:] == full[cut + 1:]


def test_continuation_after_the_end_event_resends_the_terminal_event():
    items, log = chat_items()
    prior = log.events
    chunks, _ = drive(agent_ctx(prior_events=prior), [status()], resume_after=prior[-1].seq)
    events = check(chunks, continuation=True, run_id="run-1")
    assert events[-1]["type"] == "RUN_FINISHED"
    assert parse_frames(chunks)[-1].id == f"run-1:{prior[-1].seq}"


def test_source_failure_ends_with_stream_interrupted():
    fmt = AgUiFormatter(agent_ctx())
    chunks = fmt.start() + fmt.on_item(delta(0, "x")) + (fmt.fail() or [])
    events = check(chunks)
    assert events[-1]["code"] == "stream_interrupted" and types(events)[-2] == "TEXT_MESSAGE_END"


def test_frame_id_helpers():
    assert frame_id("a:b", 3) == "a:b:3"
    assert with_frame_id(["data: 1\n\n", "data: 2\n\n"], "r:1") == ["data: 1\n\n", "id: r:1\ndata: 2\n\n"]
    assert with_frame_id([], "r:1") == []
    with pytest.raises(ValueError):
        with_frame_id(["data: 1\n\n"], "bad\nid")


@pytest.mark.parametrize(
    "value,expected",
    [("run:1:5", 5), ("run:1:-1", -1), ("  run:1:0 ", 0), ("run:1:9", 9)],
)
def test_parse_last_event_id_accepts(value, expected):
    assert parse_last_event_id(value, "run:1", max_seq=9) == expected


@pytest.mark.parametrize("value", ["5", "other:5", "run:1:", "run:1:x", "run:1:-2", "run:1:10", "run:1:1.5", "run:1:+1"])
def test_parse_last_event_id_rejects(value):
    with pytest.raises(LastEventIdError) as info:
        parse_last_event_id(value, "run:1", max_seq=9)
    assert str(info.value) == LAST_EVENT_ID_DETAIL


def test_parse_last_event_id_without_events_accepts_only_minus_one():
    assert parse_last_event_id("r:-1", "r", max_seq=None) == -1
    with pytest.raises(LastEventIdError):
        parse_last_event_id("r:0", "r", max_seq=None)


@pytest.mark.parametrize(
    "kw,status,mode,after",
    [
        (dict(recorded=True, created_now=True, same_fingerprint=True, last_event_id=None), 200, "fresh", -1),
        (dict(recorded=True, created_now=False, same_fingerprint=True, last_event_id=None), 200, "attach", -1),
        (dict(recorded=True, created_now=False, same_fingerprint=True, last_event_id="r:3"), 200, "continue", 3),
        (dict(recorded=True, created_now=False, same_fingerprint=True, last_event_id="r:-1"), 200, "continue", -1),
        (dict(recorded=True, created_now=False, same_fingerprint=False, last_event_id=None), 409, None, -1),
        (dict(recorded=True, created_now=False, same_fingerprint=False, last_event_id="r:3"), 409, None, -1),
        (dict(recorded=False, created_now=False, same_fingerprint=False, last_event_id="r:3"), 404, None, -1),
        (dict(recorded=True, created_now=False, same_fingerprint=True, last_event_id="r:99"), 400, None, -1),
        (dict(recorded=True, created_now=False, same_fingerprint=True, last_event_id="x:1"), 400, None, -1),
    ],
)
def test_plan_stream_is_the_d7_table(kw, status, mode, after):
    plan = plan_stream(run_id="r", from_seq=-1, max_seq=5, **kw)
    assert (plan.status, plan.mode, plan.after_seq) == (status, mode, after)
    if status == 400:
        assert plan.detail == LAST_EVENT_ID_DETAIL


def test_plan_stream_continuation_never_goes_before_from_seq():
    plan = plan_stream(run_id="r", recorded=True, created_now=False, same_fingerprint=True, from_seq=4,
                       last_event_id="r:-1", max_seq=9)
    assert (plan.mode, plan.after_seq) == ("continue", 4)


# --------------------------------------------------------------------------- snapshots


def test_stored_messages_and_the_event_transcript_agree():
    stored = [
        {"kind": "request", "parts": [{"part_kind": "system-prompt", "content": "sys"},
                                      {"part_kind": "user-prompt", "content": "hi"}]},
        {"kind": "response", "parts": [{"part_kind": "text", "content": "Let me check."},
                                       {"part_kind": "tool-call", "tool_name": "t", "tool_call_id": "c1",
                                        "args": '{"b":1,"a":2}'}]},
        {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_name": "t", "tool_call_id": "c1",
                                       "content": {"v": 1}}]},
        {"kind": "response", "parts": [{"part_kind": "thinking", "content": "hmm"}, {"part_kind": "text", "content": "Done"}]},
    ]
    from_rows = turn_messages("run-1", stored, user_message_client_id="client-u")
    log = Log()
    transcript = Transcript("run-1")
    for event in [
        log.add("model_message", model_request=0, text="Let me check.", message=stored[1]).event,
        log.add("tool_call", tool_call_id="c1", tool_name="t", args={"a": 2, "b": 1}).event,
        log.add("tool_result", tool_call_id="c1", tool_name="t", content={"v": 1}).event,
        log.add("model_message", model_request=1, text="Done", message=stored[3]).event,
    ]:
        transcript.add(event)
    assert from_rows[0] == {"id": "client-u", "role": "user", "content": "hi"}
    assert from_rows[1:] == transcript.messages()
    assert from_rows[1]["toolCalls"][0]["function"]["arguments"] == '{"a":2,"b":1}'


def test_structured_stored_messages_hide_the_output_tool_and_rejected_candidates():
    stored = [
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "q"}]},
        {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "final_result", "tool_call_id": "o1",
                                        "args": {"age": "x"}}]},
        {"kind": "request", "parts": [{"part_kind": "retry-prompt", "tool_name": "final_result", "tool_call_id": "o1",
                                       "content": "bad"}]},
        {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "final_result", "tool_call_id": "o2",
                                        "args": {"age": 3}}]},
        {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_name": "final_result", "tool_call_id": "o2",
                                       "content": "Final result processed."}]},
    ]
    messages = turn_messages("run-1", stored, structured_output='{"age":3}')
    assert messages == [
        {"id": "run-1-u", "role": "user", "content": "q"},
        {"id": "run-1-m1", "role": "assistant", "content": '{"age":3}'},
    ]


def test_token_usage_skips_non_chat_rows():
    assert token_usage([{"request_key": "judge:0", "model_id": "openai:j", "input_tokens": 1}]) == []
