"""RM.19: RAG and memory spans of the event-derived trace (pure: build_trace on in-memory events).

Covers the span kinds retrieve, embedding, memory_recall, memory_index, observer, reflector,
working_memory and ingest, the kb_ingest run span, the new totals, and that no memory or chunk text
reaches a span attribute.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from eap_run_store.trace import RunData, TraceInput, build_trace, knowledge_refs_of, load_knowledge_refs

T0 = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
SECRET = "SECRET-MEMORY-TEXT"
CHUNK = "SECRET-CHUNK-TEXT"


def _run(**kw):
    base = {
        "id": "r1", "kind": "agent", "status": "completed", "display_status": "completed", "agent_id": "a1",
        "agent_version": 3, "created_at": T0, "started_at": T0, "finished_at": T0 + timedelta(seconds=30),
        "updated_at": T0 + timedelta(seconds=30), "conversation_id": "c1", "turn": 2, "input": "hello",
        "output": "done",
    }
    base.update(kw)
    return base


def _events(rows):
    return [
        {"seq": i + 1, "event_key": k, "type": t, "data": d, "ts": T0 + timedelta(seconds=i + 1)}
        for i, (k, t, d) in enumerate(rows)
    ]


def _row(key, op="chat", inp=10, out=0, cost="0.001", model="test:m"):
    return {
        "run_id": "r1", "request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": inp,
        "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": "override", "operation": op,
    }


SOURCES = [
    {"n": 1, "title": "Install", "url": "https://docs.example/install#setup", "ref": "https://docs.example/install#setup",
     "kb_id": "kb1", "document_id": "d1", "document_key": "install", "chunk_id": "ch1", "doc_version": 1, "score": 0.91},
    {"n": 2, "title": "FAQ", "url": None, "ref": "kb:faq", "kb_id": "kb1", "document_id": "d2",
     "document_key": "faq", "chunk_id": "ch2", "doc_version": 2, "score": 0.7},
]

AGENT_EVENTS = [
    ("start", "run_started", {"input": "hello"}),
    ("embed:recall:2", "embedding", {"model_id": "test:hash", "inputs": 1, "input_tokens": 3, "cost_usd": "0.00003",
                                     "dimensions": 1024, "input_type": "query", "requests": 1, "stage": "recall",
                                     "hits": 2, "turns": 1, "groups": 1, "messages": 2}),
    ("model:0:final", "model_message", {"model_request": 0, "text": "", "message": {"parts": [
        {"part_kind": "tool-call", "tool_name": "retrieve", "tool_call_id": "c1", "args": {"search_query": "install"}},
    ]}}),
    ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "retrieve",
                                   "args": {"search_query": "install", "knowledge_base": "Docs"}}),
    ("embed:retrieve:c1", "embedding", {"model_id": "test:hash", "inputs": 1, "input_tokens": 2, "cost_usd": "0.00002",
                                        "dimensions": 1024, "input_type": "query", "requests": 1}),
    ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "retrieve", "status": "ok",
                                       "content": f"[1] Install\n> {CHUNK}", "sources": SOURCES}),
    ("model:1:final", "model_message", {"model_request": 1, "text": "", "message": {"parts": [
        {"part_kind": "tool-call", "tool_name": "update_working_memory", "tool_call_id": "c2",
         "args": {"redacted": True, "fields": ["name"]}},
    ]}}),
    ("tool:c2:call", "tool_call", {"tool_call_id": "c2", "tool_name": "update_working_memory",
                                   "args": {"redacted": True, "fields": ["name"]}}),
    ("memory:wm:c2", "memory_updated", {"scope": "resource", "format": "json", "version": 4, "changed_keys": ["name"],
                                        "tool_call_id": "c2", "redacted": 0}),
    ("tool:c2:result", "tool_result", {"tool_call_id": "c2", "tool_name": "update_working_memory", "status": "ok",
                                       "content": "Working memory updated (version 4)."}),
    ("model:2:final", "model_message", {"model_request": 2, "text": "Done [1]", "message": {"parts": []}}),
    ("end", "run_completed", {"output": "Done [1]"}),
    ("embed:index:2", "embedding", {"model_id": "test:hash", "inputs": 2, "input_tokens": 9, "cost_usd": "0.00009",
                                    "dimensions": 1024, "input_type": "document", "requests": 1, "stage": "index",
                                    "turn": 2, "inserted": 2, "existing": 0, "redacted": 1}),
    ("memory:observe:2", "memory_updated", {"kind": "observation", "covers_from_turn": 0, "covers_to_turn": 2,
                                            "approx_tokens": 120, "stage": "observe", "notes": 3, "redacted": 0,
                                            "prompt_version": 1, "model_id": "test:observer",
                                            "usage": {"input_tokens": 50, "output_tokens": 20}, "cost_usd": "0.002"}),
    ("memory:reflect:2:failed", "memory_observe_failed", {"stage": "reflect", "error_code": "observer_output_invalid",
                                                          "covers_from_turn": 0, "covers_to_turn": 2,
                                                          "prompt_version": 1}),
]

REQUESTS = {
    k: r for k, r in (
        ("model:0", _row("model:0", inp=100, out=10, cost="0.01")),
        ("model:1", _row("model:1", inp=120, out=12, cost="0.012")),
        ("model:2", _row("model:2", inp=130, out=13, cost="0.013")),
        ("embed:recall:2", _row("embed:recall:2", op="embedding", inp=3, cost="0.00003", model="test:hash")),
        ("embed:retrieve:c1", _row("embed:retrieve:c1", op="embedding", inp=2, cost="0.00002", model="test:hash")),
        ("embed:index:2", _row("embed:index:2", op="embedding", inp=9, cost="0.00009", model="test:hash")),
        ("memory:observe:2", _row("memory:observe:2", inp=50, out=20, cost="0.002", model="test:observer")),
    )
}


def _agent_trace(refs=None):
    data = TraceInput(root=RunData(run=_run(), events=_events(AGENT_EVENTS), requests=dict(REQUESTS)))
    if refs is not None:
        data.knowledge_refs = refs
    return build_trace(data)


def _by_kind(trace):
    out: dict[str, list] = {}
    for s in trace["spans"]:
        out.setdefault(s["kind"], []).append(s)
    return out


def test_retrieve_span_carries_query_hits_and_top_k():
    trace = _agent_trace({("a1", 3): [{"id": "kb1", "top_k": 5, "min_score": 0.3}]})
    (span,) = _by_kind(trace)["retrieve"]
    a = span["attributes"]
    assert span["id"] == "r1:tool:c1" and span["name"] == "retrieve" and span["status"] == "ok"
    assert a["query"] == "install" and a["knowledge_base"] == "Docs"
    assert a["top_k"] == 5 and a["knowledge_bases"] == [{"id": "kb1", "top_k": 5, "min_score": 0.3}]
    assert a["hit_count"] == 2
    assert a["hits"][0] == SOURCES[0]
    assert span["input"] == {"search_query": "install", "knowledge_base": "Docs"}
    # The output is the tool result the model saw (unchanged from the tool_call span).
    assert CHUNK in span["output"]
    assert trace["totals"]["tool_calls"] == 2


def test_retrieve_top_k_absent_without_spec_or_with_several_kbs():
    a = _by_kind(_agent_trace())["retrieve"][0]["attributes"]
    assert a["top_k"] is None and "knowledge_bases" not in a
    refs = {("a1", 3): [{"id": "kb1", "top_k": 5, "min_score": None}, {"id": "kb2", "top_k": 8, "min_score": None}]}
    a = _by_kind(_agent_trace(refs))["retrieve"][0]["attributes"]
    assert a["top_k"] is None and len(a["knowledge_bases"]) == 2


def test_embedding_span_nests_under_retrieve_with_usage():
    spans = _by_kind(_agent_trace())
    (emb,) = spans["embedding"]
    assert emb["parent_id"] == "r1:tool:c1"
    a = emb["attributes"]
    assert a["model_id"] == "test:hash" and a["dimensions"] == 1024 and a["inputs"] == 1
    assert a["usage"]["input_tokens"] == 2 and a["usage"]["output_tokens"] == 0
    assert Decimal(a["cost_usd"]) == Decimal("0.00002") and a["request_key"] == "embed:retrieve:c1"


def test_recall_index_observer_reflector_and_working_memory_spans():
    spans = _by_kind(_agent_trace())
    (recall,) = spans["memory_recall"]
    assert recall["attributes"]["hits"] == 2 and recall["attributes"]["messages"] == 2
    assert recall["attributes"]["usage"]["input_tokens"] == 3
    (index,) = spans["memory_index"]
    assert index["attributes"]["inserted"] == 2 and index["attributes"]["redacted"] == 1
    (obs,) = spans["observer"]
    assert obs["status"] == "ok" and obs["attributes"]["memory_kind"] == "observation"
    assert obs["attributes"]["notes"] == 3 and obs["attributes"]["usage"]["output_tokens"] == 20
    assert obs["attributes"]["request_key"] == "memory:observe:2"
    (ref,) = spans["reflector"]
    assert ref["status"] == "error" and ref["attributes"]["error_code"] == "observer_output_invalid"
    (wm,) = spans["working_memory"]
    assert wm["parent_id"] == "r1:tool:c2" and wm["attributes"]["changed_keys"] == ["name"]
    assert wm["attributes"]["version"] == 4


def test_failed_recall_embedding_is_an_error_span():
    events = _events([
        ("start", "run_started", {}),
        ("embed:recall:1:failed", "embedding", {"stage": "recall", "status": "failed", "error_code": "embedding_timeout",
                                                "model_id": "test:hash", "inputs": 1, "input_tokens": 0}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests={})))
    (span,) = _by_kind(trace)["memory_recall"]
    assert span["status"] == "error" and span["attributes"]["error_code"] == "embedding_timeout"
    assert span["attributes"]["usage"] is None


def test_totals_split_embeddings_and_memory():
    t = _agent_trace()["totals"]
    assert t["model_requests"] == 7
    assert t["embedding_requests"] == 3 and t["embedding_input_tokens"] == 14
    assert Decimal(t["embedding_cost_usd"]) == Decimal("0.00014")
    assert t["memory_requests"] == 1


def test_totals_split_the_chat_model_from_embedding_memory_and_moderation_tokens():
    """The chat model's tokens are its own requests only (what the chat turn shows), with embedding
    and observer tokens apart; the groups add up to the all-request totals."""
    requests = {**REQUESTS, "moderation:input:0": _row("moderation:input:0", inp=7, out=1, cost=None, model="test:mod")}
    events = _events(AGENT_EVENTS)
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests=requests)))
    t = trace["totals"]
    assert t["chat_requests"] == 3
    assert (t["chat_input_tokens"], t["chat_output_tokens"]) == (350, 35)  # model:0..2 only
    assert Decimal(t["chat_cost_usd"]) == Decimal("0.035")
    assert (t["memory_input_tokens"], t["memory_output_tokens"]) == (50, 20)
    assert Decimal(t["memory_cost_usd"]) == Decimal("0.002")
    assert (t["moderation_input_tokens"], t["moderation_output_tokens"], t["moderation_cost_usd"]) == (7, 1, None)
    assert t["embedding_input_tokens"] == 14
    groups_in = t["chat_input_tokens"] + t["memory_input_tokens"] + t["moderation_input_tokens"] + t["embedding_input_tokens"]
    groups_out = t["chat_output_tokens"] + t["memory_output_tokens"] + t["moderation_output_tokens"]
    assert (groups_in, groups_out) == (t["input_tokens"], t["output_tokens"]) == (421, 56)
    # The run span carries the same token split for its own requests.
    (root,) = _by_kind(trace)["run"]
    a = root["attributes"]
    assert (a["chat_input_tokens"], a["chat_output_tokens"], a["embedding_input_tokens"]) == (350, 35, 14)
    assert (a["memory_input_tokens"], a["memory_output_tokens"]) == (50, 20)
    assert "chat_cost_usd" not in a


def test_usage_split_without_usage_rows_is_null_for_the_unrecorded_groups():
    """Runs recorded before usage existed: chat tokens unknown (null), not zero; an empty group is 0."""
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=_events(AGENT_EVENTS), requests={})))
    t = trace["totals"]
    assert t["chat_requests"] == 3 and t["chat_input_tokens"] is None and t["chat_cost_usd"] is None
    assert t["memory_input_tokens"] == 0 and t["moderation_input_tokens"] == 0


def test_no_memory_text_in_span_attributes():
    events = [dict(e) for e in _events(AGENT_EVENTS)]
    # A forged event carrying text must not have it copied into attributes (allowlists only).
    for e in events:
        if e["type"] in ("memory_updated", "embedding", "memory_observe_failed"):
            e["data"] = {**e["data"], "content": SECRET, "text": SECRET, "notes_text": SECRET}
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=events, requests=dict(REQUESTS))))
    for span in trace["spans"]:
        if span["kind"] in ("embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory"):
            assert SECRET not in json.dumps(span, default=str)
            assert span["input"] is None and span["output"] is None
        if span["kind"] == "retrieve":
            assert CHUNK not in json.dumps(span["attributes"], default=str)


KB_EVENTS = [
    ("start", "run_started", {"kind": "kb_ingest", "mode": "ingest", "knowledge_base_id": "kb1",
                              "document_id": "d1", "doc_version": 2}),
    ("chunk:0", "ingest_progress", {"phase": "chunked", "generation": 1, "embedded": 0, "total": 3,
                                    "pending_tokens": 900, "message": "0 of 3 sections embedded"}),
    ("embed:batch:0", "ingest_progress", {"model_id": "test:hash", "inputs": 3, "input_tokens": 900,
                                          "cost_usd": "0.0009", "dimensions": 768, "input_type": "document",
                                          "requests": 1, "phase": "embedding", "batch": 0, "chunks": 3,
                                          "embedded": 3, "total": 3, "message": "3 of 3 sections embedded"}),
    ("finalize:0", "ingest_progress", {"phase": "finalized", "generation": 1, "document_id": "d1", "doc_version": 2}),
    ("end", "run_completed", {"output": "{}", "result": {"status": "ready", "chunk_count": 3}}),
]


def test_kb_ingest_run_trace():
    run = _run(kind="kb_ingest", agent_id=None, agent_version=None, conversation_id=None, turn=None,
               knowledge_base_id="kb1")
    requests = {"embed:batch:0": {**_row("embed:batch:0", op="embedding", inp=900, cost="0.0009", model="test:hash"), "run_id": "r1"}}
    trace = build_trace(TraceInput(root=RunData(run=run, events=_events(KB_EVENTS), requests=requests)))
    spans = _by_kind(trace)
    root = spans["run"][0]
    assert root["name"] == "knowledge base kb1 ingest"
    assert root["attributes"]["knowledge_base_id"] == "kb1" and root["attributes"]["ingest_mode"] == "ingest"
    assert [s["name"] for s in spans["ingest"]] == ["chunked", "finalized"]
    (batch,) = spans["embedding"]
    assert batch["name"] == "embedding batch 0" and batch["attributes"]["chunks"] == 3
    assert batch["attributes"]["usage"]["input_tokens"] == 900 and batch["attributes"]["dimensions"] == 768
    assert trace["totals"]["embedding_requests"] == 1 and Decimal(trace["totals"]["embedding_cost_usd"]) == Decimal("0.0009")


def test_knowledge_search_tool_run_is_a_retrieve_span_without_chunk_text():
    run = _run(kind="tool", agent_id=None, agent_version=None, conversation_id=None, turn=None, tool_id="knowledge.search")
    content = {"hits": [{**SOURCES[0], "content": CHUNK, "heading_path": ["Install"], "metadata": {}}], "text": CHUNK}
    events = _events([
        ("start", "run_started", {"kind": "tool", "tool_id": "knowledge.search", "input": {"query": "install"}}),
        ("tool:tool_run:call", "tool_call", {"tool_call_id": "tool_run", "tool_name": "knowledge_search",
                                             "tool_id": "knowledge.search",
                                             "args": {"query": "install", "top_k": 4, "kb_id": "kb1"}}),
        ("embed:retrieve:tool_run", "embedding", {"model_id": "test:hash", "inputs": 1, "input_tokens": 2,
                                                  "cost_usd": "0.00002", "dimensions": 1024}),
        ("tool:tool_run:result", "tool_result", {"tool_call_id": "tool_run", "tool_name": "knowledge_search",
                                                 "tool_id": "knowledge.search", "status": "ok", "content": content}),
        ("end", "run_completed", {"output": "{}", "result": content}),
    ])
    trace = build_trace(TraceInput(root=RunData(run=run, events=events, requests={})))
    spans = _by_kind(trace)
    (span,) = spans["retrieve"]
    assert span["attributes"]["query"] == "install" and span["attributes"]["top_k"] == 4
    assert span["attributes"]["hits"] == [SOURCES[0]]
    assert CHUNK not in json.dumps(span["attributes"])
    assert spans["embedding"][0]["parent_id"] == span["id"]


def test_load_knowledge_refs_only_for_runs_that_retrieved():
    calls = []

    def loader(agent_id, version):
        calls.append((agent_id, version))
        return {"knowledge_bases": [{"id": "kb1", "top_k": 6}, {"bad": 1}]}

    data = TraceInput(root=RunData(run=_run(), events=_events(AGENT_EVENTS), requests={}))
    assert load_knowledge_refs(data, loader) == {("a1", 3): [{"id": "kb1", "top_k": 6, "min_score": None}]}
    assert calls == [("a1", 3)]
    plain = TraceInput(root=RunData(run=_run(), events=_events([("start", "run_started", {})]), requests={}))
    calls.clear()
    assert load_knowledge_refs(plain, loader) == {} and calls == []
    assert knowledge_refs_of(None) == [] and knowledge_refs_of({"knowledge_bases": "x"}) == []


def test_retrieve_spans_of_one_turn_show_run_wide_numbers():
    """Two parallel retrieve calls: the second call's sources continue from the first's (`numbering`
    "run"), and each span's hits carry those numbers, as the model saw them."""
    first = [{**SOURCES[0], "n": 1, "numbering": "run"}, {**SOURCES[1], "n": 2, "numbering": "run"}]
    second = [{**SOURCES[0], "n": 3, "numbering": "run", "chunk_id": "ch9", "title": "Limits"}]
    rows = [
        ("start", "run_started", {"input": "refunds and limits"}),
        ("model:0:final", "model_message", {"model_request": 0, "text": "", "message": {"parts": []}}),
        ("tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "retrieve", "args": {"search_query": "refunds"}}),
        ("tool:c3:call", "tool_call", {"tool_call_id": "c3", "tool_name": "retrieve", "args": {"search_query": "limits"}}),
        ("tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "retrieve", "status": "ok",
                                           "content": "[1] Install\n> a\n\n[2] FAQ\n> b", "sources": first}),
        ("tool:c3:result", "tool_result", {"tool_call_id": "c3", "tool_name": "retrieve", "status": "ok",
                                           "content": "[3] Limits\n> c", "sources": second}),
        ("model:1:final", "model_message", {"model_request": 1, "text": "Refunds [1], limits [3]", "message": {"parts": []}}),
        ("end", "run_completed", {"output": "Refunds [1], limits [3]"}),
    ]
    trace = build_trace(TraceInput(root=RunData(run=_run(), events=_events(rows), requests={})))
    spans = {s["id"]: s for s in _by_kind(trace)["retrieve"]}
    assert [h["n"] for h in spans["r1:tool:c1"]["attributes"]["hits"]] == [1, 2]
    assert [h["n"] for h in spans["r1:tool:c3"]["attributes"]["hits"]] == [3]
    assert spans["r1:tool:c3"]["attributes"]["hits"][0]["numbering"] == "run"
    assert spans["r1:tool:c3"]["attributes"]["hits"][0]["title"] == "Limits"
