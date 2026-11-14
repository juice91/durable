"""Follow-up 1a: moderation requests are labelled with the moderation model, never the agent's chat
model, on their processor spans, and the run span's chat_* tokens exclude them."""

from __future__ import annotations

from eap_run_store.trace import RunData, TraceInput, build_trace

from run_store_tests.test_trace_rag_memory import _events, _row, _run


def _trace(input_event_usage: bool = True, with_rows: bool = True):
    mod_data = {"phase": "input", "index": 0, "processor_type": "moderation", "action": "allow", "match_counts": {}}
    if input_event_usage:
        mod_data |= {"model_id": "test:mod", "cost_usd": None, "usage": {
            "input_tokens": 7, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "measured": True}}
    events = _events([
        ("start", "run_started", {"input": "hi"}),
        ("processor:input:0", "processor_result", mod_data),
        ("model:0:final", "model_message", {"model_request": 0, "text": "ok", "message": {"parts": []}}),
        ("processor:output:0:model:0", "processor_result",
         {"phase": "output", "index": 0, "processor_type": "moderation", "action": "allow", "match_counts": {}}),
        ("end", "run_completed", {"output": "ok"}),
    ])
    requests = {"model:0": _row("model:0", inp=10, out=2, model="test:chat")}
    if with_rows:
        requests |= {
            "moderation:input:0": _row("moderation:input:0", inp=7, out=1, model="test:mod"),
            "moderation:output:0:model:0": _row("moderation:output:0:model:0", inp=5, out=1, model="test:mod"),
        }
    run = _run(model_id="test:chat", input_tokens=22, output_tokens=4)
    return build_trace(TraceInput(root=RunData(run=run, events=events, requests=requests)))


def test_moderation_spans_carry_the_moderation_model_and_their_own_tokens():
    spans = {s["id"]: s for s in _trace()["spans"]}
    mod_in = spans["r1:processor:input:0"]["attributes"]
    mod_out = spans["r1:processor:output:0:model:0"]["attributes"]
    chat = spans["r1:model:0"]["attributes"]
    assert (mod_in["model_id"], mod_in["usage"]["input_tokens"]) == ("test:mod", 7)
    assert (mod_out["model_id"], mod_out["usage"]["input_tokens"]) == ("test:mod", 5)
    assert (chat["model_id"], chat["usage"]["input_tokens"]) == ("test:chat", 10)
    run = spans["run:r1"]["attributes"]
    assert run["model_id"] == "test:chat"
    assert (run["chat_input_tokens"], run["chat_output_tokens"]) == (10, 2)
    assert (run["moderation_input_tokens"], run["moderation_output_tokens"]) == (12, 2)


def test_a_moderation_span_without_a_usage_row_never_falls_back_to_the_chat_model():
    spans = {s["id"]: s for s in _trace(with_rows=False)["spans"]}
    assert spans["r1:processor:input:0"]["attributes"]["model_id"] == "test:mod"  # the event's own model
    assert "model_id" not in spans["r1:processor:output:0:model:0"]["attributes"]  # no usage at all: no label


def test_the_run_span_flags_a_draft_run():
    """Follow-up 1c: a draft run has no agent_version; the run span says it is a draft."""
    events = _events([("start", "run_started", {"input": "hi"}), ("end", "run_completed", {"output": "ok"})])
    draft = _run(agent_version=None, draft_spec={"name": "a"})
    published = _run()
    span = build_trace(TraceInput(root=RunData(run=draft, events=events, requests={})))["spans"][0]["attributes"]
    assert (span["draft"], span["agent_version"]) == (True, None)
    span = build_trace(TraceInput(root=RunData(run=published, events=events, requests={})))["spans"][0]["attributes"]
    assert (span["draft"], span["agent_version"]) == (False, 3)
