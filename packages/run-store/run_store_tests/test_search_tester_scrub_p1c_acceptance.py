"""P1.C4 acceptance: the search-tester scrub on knowledge base and document delete.

Covers what test_search_tester_scrub.py does not: selection (only runs whose `tool_result` hits name
the document; runs of other tools, empty or failed searches and other knowledge bases are untouched),
usage rows and run totals kept, the scrubbed events holding only structural data, one transaction
(a forced failure rolls back the document delete too), and the lock order against a concurrent
event append."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store import EventLog, KnowledgeStore, RunStore
from eap_run_store.memory import scrub_event_data
from eap_schemas.tools import KNOWLEDGE_SEARCH_TOOL_ID

from .test_search_tester_scrub import CHUNK_TEXT, QUERY, _doc, _has_text, _kb, _state, _tester_run


@pytest.fixture
def tenant():
    return f"t_p1c_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def store(engine):
    return KnowledgeStore(engine)


def _totals(engine, tenant: str, run_id: str) -> tuple:
    with engine.connect() as conn:
        run = conn.execute(text("SELECT status, input_tokens, output_tokens FROM runs WHERE tenant_id = :t AND id = :r"),
                           {"t": tenant, "r": run_id}).one()
        usage = conn.execute(text("SELECT request_key, model_id, input_tokens FROM run_model_requests"
                                  " WHERE tenant_id = :t AND run_id = :r ORDER BY request_key"),
                             {"t": tenant, "r": run_id}).all()
    return tuple(run), tuple(tuple(u) for u in usage)


def _other_tool_run(engine, tenant: str, kb_id: str, doc_id: str) -> str:
    """A tool run of ANOTHER tool in the same knowledge base, whose result names the document."""
    run_id = uuid.uuid4()
    RunStore(engine).create_tool_run(tenant, "tickets.create", json.dumps({"q": QUERY}), run_id, knowledge_base_id=kb_id)
    log = EventLog(engine)
    log.append(tenant, run_id, "tool:result", "tool_result",
               {"status": "ok", "content": {"hits": [{"document_id": doc_id, "content": CHUNK_TEXT}]}})
    # loops D12: a terminal status needs an `end` event; finish_with_end writes both atomically.
    RunStore(engine).finish_with_end(tenant, run_id, "completed", CHUNK_TEXT, None)
    return str(run_id)


def _failed_search(engine, tenant: str, kb_id: str) -> str:
    runs, log = RunStore(engine), EventLog(engine)
    run_id = uuid.uuid4()
    runs.create_tool_run(tenant, KNOWLEDGE_SEARCH_TOOL_ID, json.dumps({"query": QUERY}), run_id, knowledge_base_id=kb_id)
    log.append(tenant, run_id, "start", "run_started", {"input": QUERY})
    log.append(tenant, run_id, "tool:result", "tool_result", {"status": "error", "error": f"no luck for {QUERY}"})
    log.append(tenant, run_id, "end", "run_failed", {"error": f"no luck for {QUERY}", "status": "failed"})
    runs.set_status(tenant, run_id, "failed", None, f"no luck for {QUERY}")
    return str(run_id)


def test_a_document_delete_scrubs_exactly_the_runs_that_name_it(engine, store, tenant):
    kb, other_kb = _kb(store, tenant), _kb(store, tenant, "Other")
    a, b = _doc(store, tenant, kb["id"], "a.md"), _doc(store, tenant, kb["id"], "b.md")
    names_a_first = _tester_run(engine, tenant, kb["id"], [a["id"], b["id"]])
    names_a_last = _tester_run(engine, tenant, kb["id"], [b["id"], b["id"], a["id"]])
    names_b = _tester_run(engine, tenant, kb["id"], [b["id"]])
    no_hits = _tester_run(engine, tenant, kb["id"], [])
    failed = _failed_search(engine, tenant, kb["id"])
    other_tool = _other_tool_run(engine, tenant, kb["id"], a["id"])
    # The same document id in another knowledge base's run (impossible in practice, but never touched).
    elsewhere = _tester_run(engine, tenant, other_kb["id"], [a["id"]])
    before = {r: _totals(engine, tenant, r) for r in (names_a_first, names_a_last)}

    assert store.delete_document(tenant, kb["id"], "a.md") is True

    for run in (names_a_first, names_a_last):
        state = _state(engine, tenant, run)
        assert state["input"] is None and state["output"] is None and not _has_text(state), run
        assert state["ledger"] == 0
        assert _totals(engine, tenant, run) == before[run]  # status, totals and usage rows are kept
        assert _totals(engine, tenant, run)[1], "a usage row exists"
    for run in (names_b, no_hits, failed, other_tool, elsewhere):
        state = _state(engine, tenant, run)
        assert state["output"] is not None or run == failed, run
        assert state["input"] is not None, run
        assert all(e.get("redacted") is not True for e in state["events"]), run
    assert _has_text(_state(engine, tenant, names_b)) and QUERY in _state(engine, tenant, failed)["input"]
    # The knowledge base's other document is still there, and its tester run answers as before.
    assert store.get_document(tenant, kb["id"], "b.md") is not None


def test_a_knowledge_base_delete_scrubs_every_search_run_of_it_and_nothing_else(engine, store, tenant):
    kb, other_kb = _kb(store, tenant), _kb(store, tenant, "Other")
    doc = _doc(store, tenant, kb["id"], "a.md")
    runs = [_tester_run(engine, tenant, kb["id"], [doc["id"]]), _tester_run(engine, tenant, kb["id"], []),
            _failed_search(engine, tenant, kb["id"])]
    other_tool = _other_tool_run(engine, tenant, kb["id"], doc["id"])
    keep = _tester_run(engine, tenant, other_kb["id"], [str(uuid.uuid4())])
    before = {r: _totals(engine, tenant, r) for r in runs}

    assert store.delete_knowledge_base(tenant, kb["id"]) is True

    for run in runs:
        state = _state(engine, tenant, run)
        assert state["input"] is None and state["output"] is None, run
        assert QUERY not in json.dumps(state, default=str) and "penguins" not in json.dumps(state, default=str)
        assert _totals(engine, tenant, run) == before[run]
    assert _state(engine, tenant, other_tool)["output"] == CHUNK_TEXT  # another tool: not a tester run
    assert _has_text(_state(engine, tenant, keep))


def test_scrubbed_events_hold_only_the_allowlisted_structure(engine, store, tenant):
    kb = _kb(store, tenant)
    doc = _doc(store, tenant, kb["id"], "a.md")
    run = _tester_run(engine, tenant, kb["id"], [doc["id"]])
    store.delete_knowledge_base(tenant, kb["id"])
    events = _state(engine, tenant, run)["events"]
    assert len(events) == 5
    for data in events:
        assert data == scrub_event_data(data)  # scrubbing again changes nothing: already minimal
        assert data["redacted"] is True
        for value in data.values():
            assert not isinstance(value, (list, dict)) or set(value) <= {
                "model_id", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "usage_measured", "request_key", "operation", "cost_usd", "measured"}
    [embedding] = [d for d in events if "usage" in d or d.get("model_id")]
    assert embedding  # the usage-bearing event keeps its numbers


def test_a_failed_document_delete_rolls_back_the_document_and_the_scrub(engine, store, tenant, monkeypatch):
    import eap_run_store.memory as memory

    kb = _kb(store, tenant)
    doc = _doc(store, tenant, kb["id"], "a.md")
    run = _tester_run(engine, tenant, kb["id"], [doc["id"]])
    snapshot = _state(engine, tenant, run)

    def boom(*args, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(memory, "_scrub_runs", boom)
    with pytest.raises(RuntimeError):
        store.delete_document(tenant, kb["id"], "a.md")
    assert store.get_document(tenant, kb["id"], "a.md") is not None  # the document is still there
    assert _state(engine, tenant, run) == snapshot  # and so is every byte of the run (input too)


def test_a_search_run_other_tenants_share_a_kb_id_with_is_untouched(engine, store, tenant):
    kb = _kb(store, tenant)
    doc = _doc(store, tenant, kb["id"], "a.md")
    mine = _tester_run(engine, tenant, kb["id"], [doc["id"]])
    foreign_tenant = f"{tenant}_f"
    foreign = _tester_run(engine, foreign_tenant, kb["id"], [doc["id"]])  # same kb and document ids
    store.delete_document(tenant, kb["id"], "a.md")
    assert not _has_text(_state(engine, tenant, mine))
    assert _has_text(_state(engine, foreign_tenant, foreign))
    store.delete_knowledge_base(tenant, kb["id"])
    assert _has_text(_state(engine, foreign_tenant, foreign))
