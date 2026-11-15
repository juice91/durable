"""AGUI.2 acceptance: AgUiStore idempotency (sequential and concurrent), the D4 fingerprint contract,
tenant isolation on every method, pending_client_tool_calls before AGUI.7, and old-row compatibility."""

from __future__ import annotations

import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import AgUiStore, RunNotFound


def _fp(seed="x"):
    return hashlib.sha256(seed.encode()).hexdigest()


def _clean(engine, tenant):
    with engine.begin() as conn:
        for sql in ("DELETE FROM ag_ui_runs", "DELETE FROM execution_events", "DELETE FROM runs",
                    "DELETE FROM conversations"):
            conn.execute(text(f"{sql} WHERE tenant_id = :t"), {"t": tenant})


@pytest.fixture
def tenants(engine):
    a, b = f"test_tenant_{uuid4().hex[:8]}", f"test_tenant_{uuid4().hex[:8]}"
    yield a, b
    _clean(engine, a)
    _clean(engine, b)


@pytest.fixture
def ag_ui(engine):
    return AgUiStore(engine)


def _run(run_store, tenant):
    rid = uuid4()
    run_store.create_run(tenant, "agent-1", 1, "hi", rid)
    return str(rid)


KEY = dict(scope_kind="agent", scope_id="agent-1", external_thread_id="th", external_run_id="r1")


def test_public_methods_are_exactly_the_four_tested_here():
    public = {n for n, m in inspect.getmembers(AgUiStore, callable) if not n.startswith("_")}
    assert public == {"record_ag_ui_run", "get_ag_ui_run", "pending_client_tool_calls", "set_end_order"}


def test_set_end_order_first_writer_wins_and_is_tenant_scoped(run_store, ag_ui, tenants):
    """AGUI.8: a workflow AG-UI run's end (root_order) is recorded once and never re-derived."""
    from sqlalchemy.exc import NoResultFound

    a, b = tenants
    r = _run(run_store, a)
    ag_ui.record_ag_ui_run(a, run_id=r, fingerprint=_fp("1"), from_seq=3, from_order=7, **KEY)
    assert ag_ui.get_ag_ui_run(a, **KEY).from_order == 7 and ag_ui.get_ag_ui_run(a, **KEY).end_order is None
    assert ag_ui.set_end_order(a, end_order=12, **KEY) == 12
    assert ag_ui.set_end_order(a, end_order=99, **KEY) == 12  # the first value stands
    assert ag_ui.get_ag_ui_run(a, **KEY).end_order == 12
    with pytest.raises(NoResultFound):
        ag_ui.set_end_order(b, end_order=1, **KEY)  # another tenant never reaches A's row
    assert ag_ui.get_ag_ui_run(a, **KEY).end_order == 12


def test_repeat_returns_the_stored_row_and_a_different_fingerprint_does_not_overwrite(run_store, ag_ui, tenants):
    a, _ = tenants
    r1, r2 = _run(run_store, a), _run(run_store, a)
    first, created = ag_ui.record_ag_ui_run(a, run_id=r1, fingerprint=_fp("1"), from_seq=-1,
                                            input_message_ids=["m1"], **KEY)
    assert created
    again, created = ag_ui.record_ag_ui_run(a, run_id=r2, fingerprint=_fp("2"), from_seq=7,
                                            input_message_ids=["zz"], **KEY)
    # D4: the caller compares fingerprints (409); the store keeps the first writer's row untouched.
    assert not created and again == first
    assert again.fingerprint == _fp("1") and again.run_id == r1 and again.from_seq == -1
    assert ag_ui.get_ag_ui_run(a, **KEY) == first


def test_concurrent_duplicate_inserts_make_one_row_and_one_creator(engine, run_store, ag_ui, tenants):
    a, _ = tenants
    rid = _run(run_store, a)

    def call(_):
        return ag_ui.record_ag_ui_run(a, run_id=rid, fingerprint=_fp(), **KEY)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(call, range(6)))
    assert sum(1 for _, created in results if created) == 1
    assert len({rec for rec, _ in results}) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ag_ui_runs WHERE tenant_id = :t"), {"t": a}).scalar_one() == 1


def test_concurrent_racers_with_different_fingerprints_all_see_the_winner(run_store, ag_ui, tenants):
    a, _ = tenants
    rid = _run(run_store, a)

    def call(i):
        return ag_ui.record_ag_ui_run(a, run_id=rid, fingerprint=_fp(str(i)), **KEY)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(call, range(6)))
    assert sum(1 for _, c in results if c) == 1
    assert len({rec.fingerprint for rec, _ in results}) == 1


def test_get_unknown_is_none_and_input_ids_round_trip_as_tuple(run_store, ag_ui, tenants):
    a, _ = tenants
    assert ag_ui.get_ag_ui_run(a, **KEY) is None
    rec, _ = ag_ui.record_ag_ui_run(a, run_id=_run(run_store, a), fingerprint=_fp(), input_message_ids=["b", "a"], **KEY)
    assert rec.input_message_ids == ("b", "a") and isinstance(rec.from_seq, int)


def test_tenant_isolation_on_every_method(run_store, event_log, ag_ui, tenants):
    a, b = tenants
    rid = _run(run_store, a)
    ag_ui.record_ag_ui_run(a, run_id=rid, fingerprint=_fp(), **KEY)
    # B cannot read A's row, nor name A's run.
    assert ag_ui.get_ag_ui_run(b, **KEY) is None
    with pytest.raises(RunNotFound):
        ag_ui.record_ag_ui_run(b, run_id=rid, fingerprint=_fp(), **KEY)
    assert ag_ui.get_ag_ui_run(b, **KEY) is None
    # B's own row under the same external ids is a distinct row; A's stays unchanged.
    brid = _run(run_store, b)
    brec, created = ag_ui.record_ag_ui_run(b, run_id=brid, fingerprint=_fp("b"), **KEY)
    assert created and brec.run_id == brid
    assert ag_ui.get_ag_ui_run(a, **KEY).run_id == rid
    # pending calls: A's conversation with a pending call is invisible to B.
    conv, _ = run_store.create_conversation(a, "agent-1")
    t = uuid4()
    run_store.create_turn(a, conv["id"], 1, "hi", t)
    run_store.set_status(a, t, "running")
    event_log.append(a, str(t), "tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "p", "args": {}})
    event_log.append(a, str(t), "end", "run_completed", {"pending_tool_call_ids": ["c1"]})
    run_store.set_status(a, t, "completed")
    assert len(ag_ui.pending_client_tool_calls(a, conv["id"])) == 1
    assert ag_ui.pending_client_tool_calls(b, conv["id"]) == []


def test_pending_is_empty_until_run_completed_names_pending_ids(run_store, event_log, ag_ui, tenants):
    a, _ = tenants
    conv, _ = run_store.create_conversation(a, "agent-1")
    assert ag_ui.pending_client_tool_calls(a, conv["id"]) == []
    t = uuid4()
    run_store.create_turn(a, conv["id"], 1, "hi", t)
    run_store.set_status(a, t, "running")
    # A call event with no pending_tool_call_ids (today's worker output) is not pending.
    event_log.append(a, str(t), "tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "p", "args": {}})
    event_log.append(a, str(t), "end", "run_completed", {"output": "ok"})
    run_store.set_status(a, t, "completed")
    assert ag_ui.pending_client_tool_calls(a, conv["id"]) == []


def test_old_conversations_and_runs_read_unchanged(run_store, tenants):
    a, _ = tenants
    conv, created = run_store.create_conversation(a, "agent-1")
    assert created and conv["external_thread_id"] is None
    t = uuid4()
    run_store.create_turn(a, conv["id"], 1, "hello", t)
    run = run_store.get_run(a, t)
    assert run["input"] == "hello" and run["client_tools"] is None and run["client_tool_results"] is None
    (turn,) = run_store.list_turns(a, conv["id"])
    assert turn["client_tools"] is None
    assert run_store.get_conversation(a, conv["id"])["external_thread_id"] is None


def test_the_same_thread_two_agents_get_distinct_conversations(run_store, tenants):
    a, b = tenants
    c1, _ = run_store.create_conversation(a, "ag1", external_thread_id="th")
    c2, _ = run_store.create_conversation(a, "ag2", external_thread_id="th")
    c3, _ = run_store.create_conversation(b, "ag1", external_thread_id="th")
    assert len({c1["id"], c2["id"], c3["id"]}) == 3
