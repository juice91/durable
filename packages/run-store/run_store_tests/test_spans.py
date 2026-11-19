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


def test_materialise_spans_records_a_cancelled_run_once(engine, run_store, event_log, store, tenant):
    from eap_run_store.spans import materialise_spans

    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "cancel me", run_id)
    _seed(engine, event_log, tenant, run_id, AGENT_EVENTS[:3])
    run_store.mark_running(tenant, run_id)
    assert materialise_spans(engine, tenant, run_id) == 0  # not finished: nothing yet
    run_store.cancel_run_tree(tenant, run_id)
    written = materialise_spans(engine, tenant, run_id)
    assert written >= 2
    root = next(i for i in store.list(tenant, {}, 50, None)[0] if i["span_id"] == f"run:{run_id}")
    assert root["status"] == "error"
    assert materialise_spans(engine, tenant, run_id) == written  # idempotent
    assert len(store.list(tenant, {}, 50, None)[0]) == written
    assert materialise_spans(engine, f"{tenant}_other", run_id) == 0


def test_materialise_spans_skips_child_runs(engine, run_store, event_log, store, tenant):
    from eap_run_store.spans import materialise_spans

    parent, child = uuid4(), uuid4()
    run_store.create_run(tenant, uuid4(), 1, "parent", parent)
    run_store.create_child_agent_run(tenant, str(uuid4()), 1, "child", child, parent, "ask")
    run_store.mark_running(tenant, child)
    run_store.cancel_run_tree(tenant, parent)
    assert materialise_spans(engine, tenant, child) == 0


def test_list_bounds_the_limit_and_the_name_query_in_the_store(engine, run_store, event_log, store, tenant):
    run_id = _finished_agent_run(engine, run_store, event_log, tenant)
    store.replace_for_run(tenant, run_id, flat_spans(TraceReader(engine).read(tenant, run_id)))
    assert len(store.list(tenant, {}, 0, None)[0]) == 1  # clamped up to 1
    assert len(store.list(tenant, {}, -5, None)[0]) == 1
    assert len(store.list(tenant, {}, 10_000, None)[0]) == 4
    long_name = "a" * 200
    store.replace_for_run(tenant, run_id, [{
        "span_id": "long", "parent_id": None, "kind": "run", "name": long_name, "start": T0, "end": None,
        "status": "ok", "agent_id": None, "workflow_id": None, "model": None,
    }])
    # q is cut to 200 characters in the store: the over-long query matches the 200-character name
    assert [i["span_id"] for i in store.list(tenant, {"q": long_name + "zzz"}, 50, None)[0]] == ["long"]
    assert store.list(tenant, {"q": "b" * 300}, 50, None)[0] == []


# ---------------------------------------------------------------------------- external spans (0032)


def _external(trace_id: str, span_id: str, parent: str | None = None, *, at: int = 0, status: str = "ok",
              service: str | None = "svc", name: str = "op") -> dict:
    return {
        "trace_id": trace_id, "span_id": span_id, "parent_id": parent, "kind": "external", "name": name,
        "start": T0 + timedelta(seconds=at), "end": T0 + timedelta(seconds=at + 1), "status": status,
        "service_name": service, "attributes": {"a": 1}, "resource_attributes": {"service.name": service},
    }


def test_external_spans_are_grouped_by_trace_listed_and_tenant_scoped(store, tenant):
    t1, t2 = "1" * 32, "2" * 32
    other = f"{tenant}_other"
    assert store.insert_external(tenant, "key1", [
        _external(t1, "a" * 16, name="root", at=0), _external(t1, "b" * 16, "a" * 16, at=1, status="error"),
        _external(t2, "c" * 16, at=10, service="other-svc"),
    ]) == 3
    store.insert_external(tenant, "key1", [_external(t1, "a" * 16)])  # a retry stores nothing twice
    try:
        items, nxt = store.list_traces(tenant)
        assert [(i["trace_id"], i["span_count"], i["status"], i["name"]) for i in items] == [
            (t2, 1, "ok", "op"), (t1, 2, "error", "root")]
        assert nxt is None and items[1]["api_key_id"] == "key1"
        page, cursor = store.list_traces(tenant, limit=1)
        assert [i["trace_id"] for i in page] == [t2] and cursor
        assert [i["trace_id"] for i in store.list_traces(tenant, limit=1, before=SpanCursor.decode(cursor))[0]] == [t1]
        assert [i["trace_id"] for i in store.list_traces(tenant, {"service": "other-svc"})[0]] == [t2]
        assert [i["trace_id"] for i in store.list_traces(tenant, {"status": "error"})[0]] == [t1]
        assert [i["trace_id"] for i in store.list_traces(tenant, {"q": "ROOT"})[0]] == [t1]
        assert store.list_traces(other)[0] == [] and store.read_external_trace(other, t1) is None
        trace = store.read_external_trace(tenant, t1)
        assert [s["span_id"] for s in trace["spans"]] == ["a" * 16, "b" * 16] and not trace["truncated"]
        assert trace["resource_attributes"] == {"service.name": "svc"} and trace["spans"][0]["attributes"] == {"a": 1}
        spans = store.list(tenant, {"source": "external"})[0]
        assert {s["source"] for s in spans} == {"external"} and store.list(tenant, {"source": "platform"})[0] == []
        # a run's own materialisation never touches them
        store.replace_for_run(tenant, uuid4(), [])
        assert len(store.list(tenant, {})[0]) == 3
    finally:
        with store.engine.begin() as conn:
            conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": other})


def test_external_run_id_is_stable_and_differs_per_tenant():
    from eap_run_store.spans import external_run_id

    assert external_run_id("a", "1" * 32) == external_run_id("a", "1" * 32)
    assert external_run_id("a", "1" * 32) != external_run_id("b", "1" * 32)
    assert external_run_id("a", "1" * 32) != external_run_id("a", "2" * 32)
