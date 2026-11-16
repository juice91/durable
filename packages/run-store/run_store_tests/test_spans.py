"""Materialised spans (0022): flat_spans from a trace, SpanStore.replace_for_run and list."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from eap_run_store.spans import InvalidSpanCursor, SpanCursor, SpanStore
from eap_run_store.trace import TraceReader, flat_spans
from run_store_tests.test_trace import AGENT_EVENTS, T0, _finish, _seed, _usage


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        for table in ("spans", "run_model_requests", "execution_events"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def store(engine):
    return SpanStore(engine)


def _finished_agent_run(engine, run_store, event_log, tenant, status="completed"):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "printer is on fire", run_id)
    events = AGENT_EVENTS if status == "completed" else AGENT_EVENTS[:3]
    _seed(engine, event_log, tenant, run_id, events,
          {"model:0:final": _usage("model:0", 10, 5, "0.001"), "model:1:final": _usage("model:1", 20, 7, "0.002")})
    _finish(engine, run_store, tenant, run_id, status=status)
    return run_id


def test_flat_spans_has_the_kinds_the_trace_shows(engine, run_store, event_log, tenant):
    run_id = _finished_agent_run(engine, run_store, event_log, tenant)
    trace = TraceReader(engine).read(tenant, run_id)
    rows = flat_spans(trace)
    assert [r["kind"] for r in rows] == [s["kind"] for s in trace["spans"]]
    assert [r["kind"] for r in rows] == ["run", "model_request", "tool_call", "model_request"]
    root = rows[0]
    assert root["span_id"] == f"run:{run_id}" and root["parent_id"] is None
    assert root["start"] == T0 and root["end"] == T0 + timedelta(seconds=100) and root["status"] == "ok"
    assert {r["parent_id"] for r in rows[1:]} == {f"run:{run_id}"}
    assert rows[1]["model"] == "test:ticket"
    assert set(rows[0]) == {
        "span_id", "parent_id", "kind", "name", "start", "end", "status", "agent_id", "workflow_id", "model",
    }


def test_replace_for_run_is_idempotent_and_failed_runs_record(engine, run_store, event_log, store, tenant):
    ok = _finished_agent_run(engine, run_store, event_log, tenant)
    bad = _finished_agent_run(engine, run_store, event_log, tenant, status="failed")
    for run_id in (ok, bad, ok):  # the second pass over `ok` must not duplicate rows
        rows = flat_spans(TraceReader(engine).read(tenant, run_id))
        store.replace_for_run(tenant, run_id, rows)
    items, _ = store.list(tenant, {}, 50, None)
    assert sum(1 for i in items if i["run_id"] == str(ok)) == 4
    failed_root = next(i for i in items if i["span_id"] == f"run:{bad}")
    assert failed_root["status"] == "error"
    assert len(items) == 4 + len([i for i in items if i["run_id"] == str(bad)])


def test_replace_for_run_is_tenant_scoped(engine, run_store, event_log, store, tenant):
    run_id = _finished_agent_run(engine, run_store, event_log, tenant)
    store.replace_for_run(tenant, run_id, flat_spans(TraceReader(engine).read(tenant, run_id)))
    assert store.list(f"{tenant}_other", {}, 50, None) == ([], None)
    store.replace_for_run(f"{tenant}_other", run_id, [])  # another tenant's replace deletes nothing of ours
    assert len(store.list(tenant, {}, 50, None)[0]) == 4


def test_list_pages_newest_first_and_filters_by_span_kind(engine, run_store, event_log, store, tenant):
    run_id = _finished_agent_run(engine, run_store, event_log, tenant)
    store.replace_for_run(tenant, run_id, flat_spans(TraceReader(engine).read(tenant, run_id)))
    first, nxt = store.list(tenant, {}, 2, None)
    assert nxt is not None
    second, nxt2 = store.list(tenant, {}, 2, SpanCursor.decode(nxt))
    assert store.list(tenant, {}, 2, SpanCursor.decode(nxt2)) == ([], None)  # a full last page still offers a cursor
    seen = first + second
    assert [(i["start"], i["span_id"]) for i in seen] == sorted(
        ((i["start"], i["span_id"]) for i in seen), reverse=True
    )
    assert len({i["span_id"] for i in seen}) == 4
    tools, _ = store.list(tenant, {"span_kind": "tool_call"}, 50, None)
    assert [i["kind"] for i in tools] == ["tool_call"]
    named, _ = store.list(tenant, {"q": "TICKETS"}, 50, None)
    assert [i["kind"] for i in named] == ["tool_call"]
    windowed, _ = store.list(tenant, {"from": T0 + timedelta(seconds=3), "to": T0 + timedelta(seconds=4)}, 50, None)
    assert {i["kind"] for i in windowed} == {"model_request"}
    by_run_status, _ = store.list(tenant, {"status": ["failed"]}, 50, None)
    assert by_run_status == []
    by_model, _ = store.list(tenant, {"model": "test:ticket"}, 50, None)
    assert len(by_model) == 4


def test_cursor_round_trip_and_rejects_garbage():
    cursor = SpanCursor(datetime(2026, 11, 13, tzinfo=timezone.utc), "run:x")
    assert SpanCursor.decode(cursor.encode()) == cursor
    for bad in ("", "x" * 400, "!!!", "e30"):
        with pytest.raises(InvalidSpanCursor):
            SpanCursor.decode(bad)
