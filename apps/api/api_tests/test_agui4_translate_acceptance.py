"""AGUI.4 acceptance: every translated AG-UI stream over a corpus of event logs passes the 1.0
schema; ordering invariants hold (checked here independently of the lifecycle checker); the D9
mapping per native event type; resume from every event id (D7); no leaks; bounded memory."""

from __future__ import annotations

import json
import tracemalloc
from datetime import datetime, timezone
from typing import Any

import pytest

from eap_api.ag_ui.conformance import events_of, lifecycle_errors, parse_frames, schema_errors
from eap_api.ag_ui.translate import AgUiFormatter, AgUiStreamContext
from eap_api.streams.native import Delta, DurableEvent, Status
from eap_schemas import RunEvent

from .agui4_corpus import AGENT_CORPUS, RUN, SECRET, B, approval_cycle, calls, workflow_run
from .agui4_harness import SCHEMA, agui_chunks, assert_resume_is_a_suffix, conformance

TERMINAL = {"run_completed", "run_failed"}
TS_ = datetime(2026, 11, 15, tzinfo=timezone.utc)


def upto(events: list[RunEvent], seq: int) -> list[RunEvent]:
    return [e for e in events if e.seq <= seq]


def types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] if e["type"] != "CUSTOM" else f"CUSTOM {e['name']}" for e in events]


def invariants(events: list[dict[str, Any]]) -> None:
    """Independent of conformance.lifecycle_errors: pairs matched, well nested, one terminal."""
    open_text: str | None = None
    open_call: str | None = None
    open_steps: list[str] = []
    for e in events:
        t = e["type"]
        if t == "TEXT_MESSAGE_START":
            assert open_text is None, "nested TEXT_MESSAGE_START"
            open_text = e["messageId"]
        elif t == "TEXT_MESSAGE_CONTENT":
            assert open_text == e["messageId"]
        elif t == "TEXT_MESSAGE_END":
            assert open_text == e["messageId"]
            open_text = None
        elif t == "TOOL_CALL_START":
            assert open_call is None, "nested TOOL_CALL_START"
            open_call = e["toolCallId"]
        elif t == "TOOL_CALL_ARGS":
            assert open_call == e["toolCallId"]
        elif t == "TOOL_CALL_END":
            assert open_call == e["toolCallId"]
            open_call = None
        elif t == "STEP_STARTED":
            open_steps.append(e["stepName"])
        elif t == "STEP_FINISHED":
            assert e["stepName"] in open_steps
            open_steps.remove(e["stepName"])
    assert open_text is None and open_call is None and open_steps == []
    kinds = types(events)
    assert kinds[0] == "RUN_STARTED"
    assert sum(k in {"RUN_FINISHED", "RUN_ERROR"} for k in kinds) == 1 and kinds[-1] in {"RUN_FINISHED", "RUN_ERROR"}
    assert kinds.count("RUN_STARTED") == 1


def end_seq(events: list[RunEvent]) -> int:
    return next(e.seq for e in events if e.type in TERMINAL)


@pytest.mark.parametrize("name", list(AGENT_CORPUS))
def test_every_agent_log_translates_to_a_conformant_ordered_stream(name):
    events = AGENT_CORPUS[name]().events
    out = conformance(agui_chunks(RUN, events))
    invariants(out)
    again = agui_chunks(RUN, events)
    assert again == agui_chunks(RUN, events)  # ids are stable across replays
    assert out[-1]["type"] == ("RUN_ERROR" if events[end_seq(events)].type == "run_failed" else "RUN_FINISHED")


@pytest.mark.parametrize("name", list(AGENT_CORPUS))
def test_resume_from_every_event_id_has_no_duplicates_and_no_gaps(name):
    events = AGENT_CORPUS[name]().events
    full = agui_chunks(RUN, events)
    full_frames = parse_frames(full)
    ids = [f.id for f in full_frames if f.id]
    assert len(ids) == len(set(ids)), "frame ids must be unique"
    for n in range(-1, end_seq(events)):
        assert_resume_is_a_suffix(RUN, events, full, n)
        chunks = agui_chunks(RUN, events, resume_after=n)
        tools = [e.data["tool_call_id"] for e in upto(events, n) if "tool_call_id" in e.data]
        out = conformance(chunks, continuation=True, run_id="agui-run", known_tool_calls=tools)
        assert "RUN_STARTED" not in types(out)


@pytest.mark.parametrize("name", list(AGENT_CORPUS))
def test_resume_after_the_end_event_resends_the_terminal_frames(name):
    events = AGENT_CORPUS[name]().events
    chunks = agui_chunks(RUN, events, resume_after=end_seq(events))
    out = conformance(chunks, continuation=True, run_id="agui-run",
                      known_tool_calls=[e.data["tool_call_id"] for e in events if "tool_call_id" in e.data])
    assert out[-1]["type"] in {"RUN_FINISHED", "RUN_ERROR"}


# --------------------------------------------------------------------------- D9 mapping per native type


def one(events_log: B, **kw: Any) -> list[dict[str, Any]]:
    return conformance(agui_chunks(RUN, events_log.events, **kw))


def test_d9_every_native_event_type():
    # run_started -> RUN_STARTED only; embedding / memory_updated / memory_observe_failed -> nothing.
    out = one(AGENT_CORPUS["plain"]())
    assert [t for t in types(out) if t != "CUSTOM pydanticos.status"] == [
        "RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED"]
    for name in ("retrieve", "memory"):
        dumped = json.dumps(one(AGENT_CORPUS[name]()))
        assert "embedding" not in dumped and "memory_updated" not in dumped and "observe" not in dumped
    # tool_call / tool_result -> TOOL_CALL_* and TOOL_CALL_RESULT; an error result keeps its content.
    out = one(AGENT_CORPUS["tool_chain"]())
    assert [e["content"] for e in out if e["type"] == "TOOL_CALL_RESULT"] and "TOOL_CALL_END" in types(out)
    assert {e["toolCallId"] for e in out if e["type"] == "TOOL_CALL_START"} == {"c1", "c2"}
    # model_message -> one text message per request, id {run}-m{n}.
    assert {e["messageId"] for e in one(AGENT_CORPUS["tool_chain"]()) if e["type"] == "TEXT_MESSAGE_START"} == {
        "agui-run-m0", "agui-run-m1"}
    # processor_result -> CUSTOM with counts only, never the reason.
    value = next(e["value"] for e in one(AGENT_CORPUS["processors"]()) if e.get("name") == "pydanticos.processor_result")
    assert value == {"phase": "input", "type": "pii", "action": "redact", "matches": {"email": 2}}


def test_d9_structured_output_is_result_plus_one_text_message_and_output_retry_is_custom():
    out = one(structured_log())
    assert out[-1]["type"] == "RUN_FINISHED" and out[-1]["result"] == {"age": 3}
    texts = [e for e in out if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert len(texts) == 1 and json.loads(texts[0]["delta"]) == {"age": 3}
    assert sum(e["type"] == "TEXT_MESSAGE_START" for e in out) == 1
    assert "TOOL_CALL_START" not in types(out) and "final_result" not in json.dumps(out)
    retry = [e for e in out if e.get("name") == "pydanticos.output_retry"]
    assert [r["value"] for r in retry] == [{"model_request": 0, "errors": ["/age"]}]


def test_d9_fallback_is_a_custom_model_event():
    out = one(AGENT_CORPUS["fallback"]())
    model = next(e for e in out if e.get("name") == "pydanticos.model")["value"]
    assert model["model_id"] == "anthropic:b" and model["fallback_index"] == 1
    assert all("message" not in a for a in model["failed_attempts"])


def test_d9_needs_attention_is_run_error_with_code_and_failures_are_run_error():
    out = one(AGENT_CORPUS["needs_attention"]())
    assert out[-1]["type"] == "RUN_ERROR" and out[-1]["code"] == "needs_attention"
    failed = one(AGENT_CORPUS["failed"]())
    assert failed[-1]["type"] == "RUN_ERROR" and failed[-1]["code"] == "model_error"
    assert failed[-1]["message"] == "Model unavailable."


def structured_log() -> B:
    return AGENT_CORPUS["structured"]()


# --------------------------------------------------------------------------- approvals and workflows (D7, D9)


@pytest.mark.parametrize("batch", [("g1",), ("g1", "g2")])
def test_approval_run_ends_in_one_interrupt_and_the_resume_run_covers_the_rest(batch):
    log, waiting = approval_cycle(batch)
    first = conformance(agui_chunks(RUN, upto(log.events, waiting)))
    invariants(first)
    outcome = first[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    assert [i["toolCallId"] for i in outcome["interrupts"]] == list(batch)
    assert all(i["reason"] == "tool_call" for i in outcome["interrupts"])
    # The interrupted run's stream is the same whether or not the later events already exist.
    assert agui_chunks(RUN, log.events)[: len(agui_chunks(RUN, upto(log.events, waiting)))] == \
        agui_chunks(RUN, upto(log.events, waiting))
    # The resume run: fresh AG-UI run over the events after the interrupt.
    rest = [e for e in log.events if e.seq > waiting]
    resumed = conformance(_resume_run(log.events, waiting), known_tool_calls=list(batch))
    invariants(resumed)
    decided = [e["value"]["decision"] for e in resumed if e.get("name") == "pydanticos.approval_decided"]
    assert decided == (["approved"] if len(batch) == 1 else ["approved", "denied"])
    assert resumed[-1]["type"] == "RUN_FINISHED" and rest[-1].type == "run_completed"


def _resume_run(events: list[RunEvent], waiting: int) -> list[str]:
    """A new AG-UI run (the interrupt's resume) over the durable events after `waiting`."""
    import asyncio

    from eap_api.streams.formatter import format_stream
    from eap_api.streams.native import run_items

    from .agui4_harness import SETTINGS, _collect, _loaders, no_deltas

    load_events, load_run = _loaders(RUN, events)
    ctx = AgUiStreamContext(thread_id="thread-x", run_id="agui-run-2", native_run_id=RUN, from_seq=waiting,
                            prior_events=upto(events, waiting))
    items = run_items(run_id=RUN, after_seq=waiting, load_events=load_events, load_run=load_run,
                      delta_source=no_deltas, settings=SETTINGS)
    return asyncio.run(_collect(format_stream(items, AgUiFormatter(ctx))))


def test_workflow_interrupts_at_the_approval_step_and_resumes():
    log, waiting = workflow_run()
    ctx = {"kind": "workflow"}
    first = conformance(agui_chunks(RUN, upto(log.events, waiting), **ctx))
    invariants(first)
    assert first[-1]["outcome"]["type"] == "interrupt"
    assert first[-1]["outcome"]["interrupts"][0]["id"] == f"approval:{RUN}:ok"
    assert [e["stepName"] for e in first if e["type"] == "STEP_STARTED"] == ["lookup", "route", "fan", "a/b", "ok"]
    for n in range(-1, waiting + 1):
        chunks = agui_chunks(RUN, upto(log.events, waiting), resume_after=n, **ctx)
        frames = parse_frames(chunks)
        out = events_of(frames)
        assert schema_errors(out, SCHEMA) == [] and "RUN_STARTED" not in types(out)
        # Steps the client saw start before N (and not finish) may be finished by the continuation.
        started = {e.data["step_id"] for e in log.events if e.seq <= n and e.type == "step_started"}
        done = {e.data["step_id"] for e in log.events if e.seq <= n and e.type in ("step_completed", "step_failed")}
        # At the interrupt's own seq the client also holds that event's STEP_FINISHED frames.
        known_open = set() if n >= waiting else started - done
        errors = lifecycle_errors(frames, continuation=True, run_id="agui-run", known_open_steps=known_open)
        assert errors == [], (n, errors)
        finished = [e["stepName"] for e in out if e["type"] == "STEP_FINISHED"]
        starts = [e["stepName"] for e in out if e["type"] == "STEP_STARTED"]
        assert set(finished) <= set(starts) | started
    ids = [f.id for f in parse_frames(agui_chunks(RUN, upto(log.events, waiting), **ctx)) if f.id]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- leaks


def secret_logs() -> list[B]:
    return [AGENT_CORPUS[n]() for n in AGENT_CORPUS] + [approval_cycle(("g1", "g2"))[0], workflow_run()[0]]


def test_no_secret_or_decider_leaks_into_any_translated_chunk():
    for log in secret_logs():
        kind = "workflow" if log.events[0].data.get("kind") == "workflow" else "agent"
        text = "".join(agui_chunks(RUN, log.events, kind=kind))
        for n in range(-1, 3):
            text += "".join(agui_chunks(RUN, log.events, resume_after=n, kind=kind))
        assert SECRET not in text and "user-secret" not in text and "SECRETVALUE" not in text


def test_internal_native_fields_do_not_cross_the_boundary():
    text = "".join(agui_chunks(RUN, retrieve_log().events))
    assert "sources" not in text and "https://x.test/a" not in text and "embedding" not in text


def retrieve_log() -> B:
    return AGENT_CORPUS["retrieve"]()


# --------------------------------------------------------------------------- load


def test_a_large_run_with_tens_of_thousands_of_deltas_translates_with_bounded_memory():
    n = 50_000
    fmt = AgUiFormatter(AgUiStreamContext(thread_id="t", run_id="r", native_run_id=RUN))
    fmt.start()
    first = RunEvent(run_id=RUN, seq=0, type="run_started", data={}, ts=TS_)
    fmt.on_item(DurableEvent(first))
    tracemalloc.start()
    frames = 0
    for i in range(n):
        out = fmt.on_item(Delta({"run_id": RUN, "model_request": 0, "attempt": "a", "delta": "t%d " % (i % 10)}, None))
        frames += len(out)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert frames >= n  # one TEXT_MESSAGE_CONTENT per delta (+ the start)
    assert peak < 20 * 1024 * 1024, f"peak {peak}"
    text = "".join("t%d " % (i % 10) for i in range(n))
    end = RunEvent(run_id=RUN, seq=1, type="model_message", data={"model_request": 0, "text": text, "message": {}}, ts=TS_)
    done = RunEvent(run_id=RUN, seq=2, type="run_completed", data={"output": text}, ts=TS_)
    tail = fmt.on_item(DurableEvent(end)) + fmt.on_item(DurableEvent(done))
    out = events_of(parse_frames(tail))
    assert out[-1]["type"] == "RUN_FINISHED"
    assert not any(e["type"] == "MESSAGES_SNAPSHOT" for e in out), "a matching stream needs no correction snapshot"


def test_many_events_with_many_tool_calls_stay_conformant():
    b = B().add("run_started")
    ids = [f"c{i}" for i in range(300)]
    b.add("model_message", model_request=0, text="", message=calls(*[(c, "t") for c in ids]))
    for c in ids:
        b.add("tool_call", tool_call_id=c, tool_name="t", args={"i": c})
    for c in ids:
        b.add("tool_result", tool_call_id=c, tool_name="t", status="ok", content="r")
    b.add("model_message", model_request=1, text="done", message={}).add("run_completed", output="done")
    out = conformance(agui_chunks(RUN, b.events))
    invariants(out)
    assert sum(e["type"] == "TOOL_CALL_RESULT" for e in out) == 300

