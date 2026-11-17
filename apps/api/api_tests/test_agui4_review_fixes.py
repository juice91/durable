"""AGUI.4 review fixes: each reviewer probe (agui4-review.md, P1-P4 and minors 5-8) as a regression test."""

from __future__ import annotations

import json
import time
import tomllib
from typing import Any

from eap_api.ag_ui.conformance import lifecycle_errors, parse_frames
from eap_api.ag_ui.translate import STREAM_INTERRUPTED_MESSAGE, AgUiFormatter, AgUiStreamContext
from eap_api.streams.native import ChildScope, DurableEvent, Heartbeat
from eap_api.streams.resume import LAST_EVENT_ID_DETAIL, plan_stream
from eap_schemas import RunEvent

from .test_agui4_translate import (
    NATIVE,
    ROOT,
    TS,
    Log,
    agent_ctx,
    call_message,
    check,
    delta,
    drive,
    status,
    types,
)

A = ChildScope("a", "6c1f7d8e-0000-4000-8000-0000000000a1")
B = ChildScope("b", "6c1f7d8e-0000-4000-8000-0000000000b1")


def ordered(events: list[dict[str, Any]]) -> list[str]:
    """START/tool call/result ids in stream order."""
    out = []
    for e in events:
        if e["type"] == "TEXT_MESSAGE_START":
            out.append(e["messageId"])
        elif e["type"] == "TOOL_CALL_START":
            out.append(f"call:{e['toolCallId']}")
        elif e["type"] == "TOOL_CALL_RESULT":
            out.append(f"result:{e['toolCallId']}")
    return out


# --------------------------------------------------------------------------- MAJOR 1 (P1)


def test_next_request_deltas_wait_for_a_tool_only_response_and_its_results():
    log = Log()
    items = [
        status(), log.add("run_started", input="hi"),
        delta(1, "The answer"),  # request 1 streams before the poll reads model:0:final
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "calc"))),
        log.add("tool_call", tool_call_id="c1", tool_name="calc", args={}),
        log.add("tool_result", tool_call_id="c1", tool_name="calc", content="4"),
        delta(1, " is 4"),
        log.add("model_message", model_request=1, text="The answer is 4", message={"kind": "response", "parts": []}),
        log.add("run_completed", output="The answer is 4"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert ordered(events) == ["run-1-m0", "call:c1", "result:c1", "run-1-m1"]
    assert "MESSAGES_SNAPSHOT" not in types(events)
    assert [e["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"] == ["The answer", " is 4"]


def test_next_request_deltas_wait_for_every_result_of_the_previous_response():
    log = Log()
    items = [
        log.add("run_started"),
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "t"), ("c2", "t"))),
        log.add("tool_call", tool_call_id="c1", tool_name="t", args={}),
        log.add("tool_call", tool_call_id="c2", tool_name="t", args={}),
        log.add("tool_result", tool_call_id="c1", tool_name="t", content="1"),
        delta(1, "Both"),  # c2 has no result yet
        log.add("tool_result", tool_call_id="c2", tool_name="t", content="2"),
        log.add("model_message", model_request=1, text="Both", message={}),
        log.add("run_completed", output="Both"),
    ]
    events = check(drive(agent_ctx(), items)[0])
    assert ordered(events) == ["run-1-m0", "call:c1", "call:c2", "result:c1", "result:c2", "run-1-m1"]


# --------------------------------------------------------------------------- MAJOR 2 (P2)


def approval_log() -> tuple[Log, list[DurableEvent], int, list[Any]]:
    log = Log()
    first = [
        log.add("run_started", input="hi"),
        log.add("model_message", model_request=0, text="", message=call_message(("c1", "refund"))),
        log.add("tool_call", tool_call_id="c1", tool_name="refund", args={}),
        log.add("tool_approval_waiting", tool_call_id="c1", tool_name="refund", args={},
                deadline="2026-11-16T00:00:00Z", batch=["c1"]),
    ]
    later = [
        status("waiting_approval"), Heartbeat(), status("waiting_approval"),
        log.add("tool_approved", tool_call_id="c1"),
        log.add("tool_result", tool_call_id="c1", tool_name="refund", content="ok"),
        log.add("model_message", model_request=1, text="Refunded", message={"kind": "response", "parts": []}),
        log.add("run_completed", output="Refunded"),
    ]
    return log, first, first[-1].seq, later


def test_continuation_at_the_interrupt_seq_resends_the_interrupt_and_closes():
    log, first, k, later = approval_log()
    chunks, fmt = drive(agent_ctx(prior_events=[e.event for e in first]), later, resume_after=k)
    events = check(chunks, continuation=True, run_id="run-1", known_tool_calls=["c1"])
    assert types(events) == ["CUSTOM pydanticos.run", "MESSAGES_SNAPSHOT", "RUN_FINISHED"]
    assert events[-1]["outcome"]["type"] == "interrupt"
    assert parse_frames(chunks)[-1].id == f"run-1:{k}"
    assert fmt.closed
    assert "approval_decided" not in "".join(chunks) and "Refunded" not in "".join(chunks)


def test_continuation_given_events_past_the_interrupt_still_ends_at_the_interrupt():
    log, first, k, later = approval_log()
    # Even if the caller passes the whole durable log, this run's events stop at its interrupt.
    chunks, _ = drive(agent_ctx(prior_events=log.events), [status()], resume_after=log.events[-1].seq)
    events = check(chunks, continuation=True, run_id="run-1", known_tool_calls=["c1"])
    assert events[-1]["outcome"]["type"] == "interrupt"
    assert parse_frames(chunks)[-1].id == f"run-1:{k}"
    assert "Refunded" not in "".join(chunks)


def test_last_event_id_one_past_the_interrupt_is_400():
    log, _first, k, _later = approval_log()
    max_seq = log.events[-1].seq
    common = dict(run_id="run-1", recorded=True, created_now=False, same_fingerprint=True, from_seq=-1,
                  max_seq=max_seq, end_seq=k)
    at = plan_stream(last_event_id=f"run-1:{k}", **common)
    assert (at.status, at.mode, at.after_seq) == (200, "continue", k)
    past = plan_stream(last_event_id=f"run-1:{k + 1}", **common)
    assert (past.status, past.detail) == (400, LAST_EVENT_ID_DETAIL)
    # Without an end seq (the run's own end is the durable end) the durable max bounds it.
    assert plan_stream(last_event_id=f"run-1:{max_seq}", **{**common, "end_seq": None}).status == 200


# --------------------------------------------------------------------------- MAJOR 3 (P3)


def child(scope: ChildScope, type_: str, seq: int, **data: Any) -> DurableEvent:
    return DurableEvent(RunEvent(run_id=scope.run_id, seq=seq, type=type_, data=data, ts=TS), scope)


def wf_snapshot(a_messages: int) -> list[str]:
    """Child a announces `a_messages` messages, then child b diverges; returns the snapshot ids."""
    log = Log()
    items: list[Any] = [status(), log.add("run_started"), log.add("step_started", step_id="a", step_type="agent"),
                        log.add("step_started", step_id="b", step_type="agent")]
    seq = 1
    for i in range(a_messages):
        if i % 2 == 0:
            items.append(child(A, "model_message", seq, model_request=i, text=f"A{i}",
                               message=call_message((f"ca{i}", "t"), text=f"A{i}")))
            seq += 1
            items.append(child(A, "tool_call", seq, tool_call_id=f"ca{i}", tool_name="t", args={}))
        else:
            items.append(child(A, "tool_result", seq, tool_call_id=f"ca{i - 1}", tool_name="t", content="r"))
        seq += 1
    items += [delta(0, "B pa", scope=B), delta(0, "B full", attempt="z", scope=B),
              child(B, "model_message", 1, model_request=0, text="B full", message={"kind": "response", "parts": []})]
    ctx = AgUiStreamContext(thread_id="t", run_id="wf-1", native_run_id=NATIVE, kind="workflow")
    events = check(drive(ctx, items)[0])
    snaps = [e for e in events if e["type"] == "MESSAGES_SNAPSHOT"]
    assert len(snaps) == 1
    return [m["id"] for m in snaps[0]["messages"]]


def test_child_correction_snapshot_keeps_every_turn_odd_count():
    # a: m0 (with a call), its tool result, m2; then b's message: 4 messages in announced order.
    assert wf_snapshot(3) == ["wf-1-a-m0", "wf-1-a-t-ca0", "wf-1-a-m2", "wf-1-b-m0"]


def test_child_correction_snapshot_keeps_every_turn_even_count():
    assert wf_snapshot(2) == ["wf-1-a-m0", "wf-1-a-t-ca0", "wf-1-b-m0"]
    assert wf_snapshot(4) == ["wf-1-a-m0", "wf-1-a-t-ca0", "wf-1-a-m2", "wf-1-a-t-ca2", "wf-1-b-m0"]


def test_agent_correction_snapshot_keeps_history_first():
    log = Log()
    history = [{"id": "u0", "role": "user", "content": "hi"}]
    items = [log.add("run_started"), delta(0, "x", "a"), delta(0, "y", "b"),
             log.add("model_message", model_request=0, text="y", message={}), log.add("run_completed", output="y")]
    events = check(drive(agent_ctx(history=history), items)[0])
    snap = next(e for e in events if e["type"] == "MESSAGES_SNAPSHOT")
    assert [m["id"] for m in snap["messages"]] == ["u0", "run-1-m0"]


# --------------------------------------------------------------------------- MINOR 4 (P4)


def test_continuation_at_a_structured_end_resends_only_run_finished():
    log = Log()
    evs = [log.add("run_started", input="hi"),
           log.add("model_message", model_request=0, text='{"a":1}', output_candidate=True,
                   message={"kind": "response", "parts": []}),
           log.add("run_completed", output='{"a":1}', output_json={"a": 1})]
    chunks, _ = drive(agent_ctx(prior_events=[e.event for e in evs]), [status("completed")], resume_after=evs[-1].seq)
    events = check(chunks, continuation=True, run_id="run-1")
    assert types(events) == ["CUSTOM pydanticos.run", "MESSAGES_SNAPSHOT", "RUN_FINISHED"]
    assert [m["id"] for m in events[1]["messages"]] == ["run-1-m0"]
    assert events[-1]["result"] == {"a": 1}


# --------------------------------------------------------------------------- MINOR 5


def _frames(*events: dict[str, Any]):
    return parse_frames([f"data: {json.dumps(e)}\n\n" for e in events])


def test_checker_rejects_a_reused_message_id_and_out_of_order_requests():
    start = {"type": "RUN_STARTED", "threadId": "t", "runId": "r", "protocolVersion": "1.0"}
    reused = _frames(start, {"type": "TEXT_MESSAGE_START", "messageId": "r-m0"},
                     {"type": "TEXT_MESSAGE_END", "messageId": "r-m0"},
                     {"type": "TEXT_MESSAGE_START", "messageId": "r-m0"},
                     {"type": "TEXT_MESSAGE_END", "messageId": "r-m0"})
    assert any("already started" in e for e in lifecycle_errors(reused))
    in_snapshot = _frames(start, {"type": "MESSAGES_SNAPSHOT", "messages": [{"id": "r-m0", "role": "assistant"}]},
                          {"type": "TEXT_MESSAGE_START", "messageId": "r-m0"},
                          {"type": "TEXT_MESSAGE_END", "messageId": "r-m0"})
    assert any("snapshot" in e for e in lifecycle_errors(in_snapshot))
    # The reviewer's P1 output before the fix: m1 opens, then m0.
    p1 = _frames(start, {"type": "TEXT_MESSAGE_START", "messageId": "r-m1"},
                 {"type": "TEXT_MESSAGE_START", "messageId": "r-m0"},
                 {"type": "TEXT_MESSAGE_END", "messageId": "r-m0"},
                 {"type": "TEXT_MESSAGE_END", "messageId": "r-m1"})
    assert any("later request" in e for e in lifecycle_errors(p1))


# --------------------------------------------------------------------------- MINOR 6


def test_expired_or_vanished_stream_ends_with_stream_interrupted():
    for reason in ("expired", "gone"):
        fmt = AgUiFormatter(agent_ctx())
        chunks = fmt.start() + fmt.on_item(delta(0, "par")) + fmt.finish(reason)
        events = check(chunks)
        assert types(events)[-2:] == ["TEXT_MESSAGE_END", "RUN_ERROR"]
        assert events[-1] == {"type": "RUN_ERROR", "message": STREAM_INTERRUPTED_MESSAGE, "code": "stream_interrupted"}
        assert fmt.closed and fmt.finish(reason) == []


# --------------------------------------------------------------------------- MINOR 7


def test_worker_tests_declare_the_packages_they_import():
    project = tomllib.loads((ROOT / "apps/worker/pyproject.toml").read_text())
    dev = project["dependency-groups"]["dev"]
    assert "ag-ui-protocol==1.0.0" in dev and "eap-api" in dev
    assert project["tool"]["uv"]["sources"]["eap-api"] == {"workspace": True}


# --------------------------------------------------------------------------- MINOR 8


def test_long_streams_are_linear():
    def run(n: int) -> float:
        fmt = AgUiFormatter(agent_ctx())
        fmt.start()
        started = time.perf_counter()
        for _ in range(n):
            fmt.on_item(delta(0, "x" * 64))
        frames = fmt.on_item(DurableEvent(Log().add("model_message", model_request=0, text="x" * 64 * n,
                                                    message={}).event))
        assert "MESSAGES_SNAPSHOT" not in "".join(frames)
        return time.perf_counter() - started

    # best of three: a noisy neighbour inflates one run, a quadratic blow-up inflates all of them
    small, large = min(run(2000) for _ in range(3)), min(run(20000) for _ in range(3))
    assert large < small * 30  # 10x the deltas: linear-ish, never quadratic (100x)
