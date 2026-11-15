"""LP.4 acceptance: terminal guards, the cancel tree, concurrency races, cost and approvals
(loops D6, D11, D12). Adds to the implementer's test_terminal_guards.py without duplicating it."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import (
    RUN_CANCELLED_ERROR,
    ApprovalStore,
    PostgresSideEffectStore,
    RunNotFound,
)
from eap_run_store.approvals import CANCELLED_DECIDED_BY
from eap_schemas import status_for_end

ROUNDS = 50
TERMINALS = {
    "completed": ("run_completed", {"output": "ok"}),
    "failed": ("run_failed", {"status": "failed", "error": "E: boom"}),
    "needs_attention": ("run_failed", {"status": "needs_attention", "error": "E: unsure"}),
}
NON_TERMINAL = ("queued", "running")


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        for table in ("pending_approvals", "run_model_requests", "execution_events", "side_effects"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _usage(key: str, cost: str | None) -> dict:
    return {
        "request_key": key, "model_id": "test:priced", "provider_model_name": None,
        "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "usage_measured": True, "cost_usd": None if cost is None else Decimal(cost), "price_source": "test",
    }


def _workflow(run_store, tenant, running=True):
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    if running:
        assert run_store.mark_running(tenant, run_id)
    return run_id


def _child(run_store, tenant, parent, step="s1", running=True):
    run_id = uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, "x", run_id, parent, step)
    if running:
        assert run_store.mark_running(tenant, run_id)
    return run_id


def _end_run(run_store, event_log, tenant, run_id, status):
    etype, data = TERMINALS[status]
    event_log.append(tenant, run_id, "end", etype, data)
    assert run_store.set_status(tenant, run_id, status, data.get("output"), data.get("error"))


def _snap(run_store, tenant, run_id):
    run = run_store.get_run(tenant, run_id)
    keys = ("status", "error", "output", "finished_at", "input_tokens", "output_tokens", "cost_usd", "heartbeat_at")
    return {k: run[k] for k in keys}


def _race(*fns):
    """Run the callables in threads released by one barrier; return the raised exceptions."""
    barrier, errors = threading.Barrier(len(fns)), []

    def wrap(fn):
        def go():
            try:
                barrier.wait()
                fn()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return go

    threads = [threading.Thread(target=wrap(fn)) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


# ------------------------------------------------------------------ every illegal transition


@pytest.mark.parametrize("terminal", sorted(TERMINALS))
def test_a_terminal_run_refuses_every_non_terminal_transition(run_store, event_log, tenant, terminal):
    run_id = _workflow(run_store, tenant)
    _end_run(run_store, event_log, tenant, run_id, terminal)
    before = _snap(run_store, tenant, run_id)
    assert before["status"] == terminal and before["finished_at"] is not None
    for status in NON_TERMINAL:
        assert run_store.set_status(tenant, run_id, status) is False, status
    assert run_store.mark_running(tenant, run_id) is False
    assert run_store.heartbeat(tenant, run_id) is False
    assert run_store.mark_enqueue_failed(tenant, run_id) is False
    assert _snap(run_store, tenant, run_id) == before


@pytest.mark.parametrize("terminal", sorted(TERMINALS))
@pytest.mark.parametrize("other", sorted(TERMINALS))
def test_a_terminal_status_that_disagrees_with_end_is_refused(run_store, event_log, tenant, terminal, other):
    run_id = _workflow(run_store, tenant)
    etype, data = TERMINALS[terminal]
    event_log.append(tenant, run_id, "end", etype, data)
    assert status_for_end(etype, data) == terminal
    assert run_store.set_status(tenant, run_id, other, "late", "E: late") is (other == terminal)
    run = run_store.get_run(tenant, run_id)
    if other == terminal:
        assert run["status"] == terminal
    else:
        assert (run["status"], run["finished_at"]) == ("running", None)
        # A disagreeing write after the matching one changes nothing either.
        assert run_store.set_status(tenant, run_id, terminal, data.get("output"), data.get("error"))
        before = _snap(run_store, tenant, run_id)
        assert run_store.set_status(tenant, run_id, other, "late", "E: late") is False
        assert _snap(run_store, tenant, run_id) == before


def test_end_without_status_field_means_failed(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_failed", {"error": "E: x"})
    assert run_store.set_status(tenant, run_id, "needs_attention", None, "E: x") is False
    assert run_store.set_status(tenant, run_id, "failed", None, "E: x") is True


# ------------------------------------------------------------------ concurrency races (50 rounds)


def _assert_one_end_matching_status(run_store, event_log, tenant, run_id):
    ends = [e for e in event_log.list_events(tenant, run_id) if e.type in ("run_completed", "run_failed")]
    assert len(ends) == 1
    assert run_store.get_run(tenant, run_id)["status"] == status_for_end(ends[0].type, ends[0].data)
    assert [e.seq for e in event_log.list_events(tenant, run_id)] == list(range(len(event_log.list_events(tenant, run_id))))


def test_end_versus_heartbeat_race(run_store, event_log, tenant):
    for _ in range(ROUNDS):
        run_id = _workflow(run_store, tenant)
        errors = _race(
            lambda: (event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"}),
                     run_store.set_status(tenant, run_id, "completed", "ok")),
            lambda: run_store.heartbeat(tenant, run_id),
        )
        assert errors == []
        _assert_one_end_matching_status(run_store, event_log, tenant, run_id)
        frozen = _snap(run_store, tenant, run_id)
        assert run_store.heartbeat(tenant, run_id) is False
        assert _snap(run_store, tenant, run_id) == frozen


def test_end_versus_mark_running_race(run_store, event_log, tenant):
    for _ in range(ROUNDS):
        run_id = _workflow(run_store, tenant, running=False)
        errors = _race(
            lambda: (event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"}),
                     run_store.set_status(tenant, run_id, "completed", "ok")),
            lambda: run_store.mark_running(tenant, run_id),
        )
        assert errors == []
        _assert_one_end_matching_status(run_store, event_log, tenant, run_id)
        assert run_store.get_run(tenant, run_id)["status"] == "completed"


def test_cancel_versus_end_race(run_store, event_log, tenant):
    for _ in range(ROUNDS):
        run_id = _workflow(run_store, tenant)
        child = _child(run_store, tenant, run_id)
        errors = _race(
            lambda: (event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"}),
                     run_store.set_status(tenant, run_id, "completed", "ok")),
            lambda: run_store.cancel_run_tree(tenant, run_id),
        )
        assert errors == []
        _assert_one_end_matching_status(run_store, event_log, tenant, run_id)
        assert run_store.get_run(tenant, child)["status"] == "failed"
        assert event_log.get_event(tenant, child, "end").data["error_code"] == "run_cancelled"


def test_double_cancel_race(run_store, event_log, tenant):
    for _ in range(ROUNDS):
        run_id = _workflow(run_store, tenant)
        child = _child(run_store, tenant, run_id)
        results = []
        errors = _race(
            lambda: results.append(run_store.cancel_run_tree(tenant, run_id)),
            lambda: results.append(run_store.cancel_run_tree(tenant, run_id)),
        )
        assert errors == [] and len(results) == 2
        # Exactly one call wrote each run's end; both report the same final statuses.
        assert sorted(r for res in results for r in res.ended) == sorted([str(run_id), str(child)])
        assert results[0].statuses == results[1].statuses == {str(run_id): "failed", str(child): "failed"}
        for rid in (run_id, child):
            _assert_one_end_matching_status(run_store, event_log, tenant, rid)


# ------------------------------------------------------------------ cancel tree: depth 3, mixed states


def _tree(run_store, event_log, tenant, engine):
    """root -> a (running), b (completed), c (queued) ; a -> a1 (running, reserved effect) ; a1 -> a2 (failed)."""
    root = _workflow(run_store, tenant)
    a, b = _child(run_store, tenant, root, "a"), _child(run_store, tenant, root, "b")
    c = _child(run_store, tenant, root, "c", running=False)
    a1 = _child(run_store, tenant, a, "a1")
    a2 = _child(run_store, tenant, a1, "a2")
    _end_run(run_store, event_log, tenant, b, "completed")
    _end_run(run_store, event_log, tenant, a2, "failed")
    PostgresSideEffectStore(engine, tenant).reserve(f"{a1}:call_1")
    return root, a, b, c, a1, a2


def test_cancel_depth_three_tree_with_mixed_states_is_idempotent(run_store, event_log, tenant, engine):
    root, a, b, c, a1, a2 = _tree(run_store, event_log, tenant, engine)
    first = run_store.cancel_run_tree(tenant, root)
    expected = {root: "failed", a: "failed", b: "completed", c: "failed", a1: "needs_attention", a2: "failed"}
    assert first.statuses == {str(k): v for k, v in expected.items()}
    assert set(first.ended) == {str(x) for x in (root, a, c, a1)}  # b and a2 already ended
    assert event_log.get_event(tenant, a2, "end").data["error"] == "E: boom"  # a2 keeps its own end
    assert run_store.get_run(tenant, b)["output"] == "ok"
    snaps = {x: _snap(run_store, tenant, x) for x in expected}
    seqs = {x: len(event_log.list_events(tenant, x)) for x in expected}
    for _ in range(3):
        again = run_store.cancel_run_tree(tenant, root)
        assert again.statuses == first.statuses and again.ended == () and again.approvals_closed == 0
    assert {x: _snap(run_store, tenant, x) for x in expected} == snaps
    assert {x: len(event_log.list_events(tenant, x)) for x in expected} == seqs
    assert run_store.get_run(tenant, root)["error"] == RUN_CANCELLED_ERROR


def test_cancel_of_a_subtree_leaves_the_rest_alone(run_store, event_log, tenant, engine):
    root, a, b, c, a1, a2 = _tree(run_store, event_log, tenant, engine)
    result = run_store.cancel_run_tree(tenant, a)
    assert set(result.statuses) == {str(a), str(a1), str(a2)}
    assert run_store.get_run(tenant, root)["status"] == "running"
    assert run_store.get_run(tenant, c)["status"] == "queued"
    assert event_log.get_event(tenant, root, "end") is None


def test_cancel_unknown_run_raises_and_changes_nothing(run_store, tenant):
    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree(tenant, uuid4())


# ------------------------------------------------------------------ recovery: find_unfinished_cancels


def _age_cancel(engine, run_id, seconds):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = now() - make_interval(secs => :s) WHERE run_id = :r AND event_key = 'cancel'"),
            {"s": seconds, "r": str(run_id)},
        )


def _mine(run_store, tenant, older_than=timedelta(seconds=30)):
    return [r["run_id"] for r in run_store.find_unfinished_cancels(older_than, limit=10000) if r["tenant_id"] == tenant]


def test_an_interrupted_cancel_is_found_and_then_resolved(run_store, event_log, tenant, engine):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    # The API wrote the durable request, then crashed before cancel_run_tree wrote any `end`.
    event_log.append(tenant, root, "cancel", "cancel_requested", {"requested_by": "u"})
    _age_cancel(engine, root, 120)
    assert _mine(run_store, tenant) == [str(root)]  # the child has no cancel event of its own
    # The reconciler's step 3 resolves the whole tree and the run drops out of the list.
    result = run_store.cancel_run_tree(tenant, root)
    assert result.statuses == {str(root): "failed", str(child): "failed"}
    assert _mine(run_store, tenant) == []
    # A cancel event is not an end: the run's end is the run_cancelled one.
    assert event_log.get_event(tenant, root, "end").data["error_code"] == "run_cancelled"


def test_find_unfinished_cancels_threshold_kind_and_limit(run_store, event_log, tenant, engine):
    older, younger, agent_run = _workflow(run_store, tenant), _workflow(run_store, tenant), uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", agent_run)
    for rid in (older, younger, agent_run):
        event_log.append(tenant, rid, "cancel", "cancel_requested", {})
    _age_cancel(engine, older, 600)
    _age_cancel(engine, younger, 10)
    _age_cancel(engine, agent_run, 600)
    assert _mine(run_store, tenant) == [str(older)]  # too young and non-workflow runs are skipped
    assert sorted(_mine(run_store, tenant, timedelta(seconds=5))) == sorted([str(older), str(younger)])
    assert len(run_store.find_unfinished_cancels(timedelta(seconds=5), limit=1)) <= 1
    mine = [r for r in run_store.find_unfinished_cancels(timedelta(seconds=5), limit=10000) if r["tenant_id"] == tenant]
    assert [r["run_id"] for r in mine] == [str(older), str(younger)]  # oldest request first
    assert all(isinstance(r["requested_at"], datetime) for r in mine)


# ------------------------------------------------------------------ after `end`


def test_after_end_usage_is_kept_and_seq_stays_gap_free(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "start", "run_started", {})
    event_log.append(tenant, run_id, "end", "run_failed", {"status": "failed"})
    for i in range(3):
        res = event_log.append(tenant, run_id, f"step:s{i}:completed", "step_completed", {}, _usage(f"model:{i}", "0.1"))
        assert res.dropped and res.event.type == "run_failed"
        # Re-dropping the same key writes no second usage row (first write wins).
        assert event_log.append(tenant, run_id, f"step:s{i}:completed", "step_completed", {}, _usage(f"model:{i}", "9")).dropped
    assert run_store.run_tree_cost(tenant, run_id) == Decimal("0.3")
    # Post-turn keys keep appending with the next consecutive seq, including their usage rows.
    for i, key in enumerate(("memory:observe:t1", "memory:reflect:t1", "embed:index:t1")):
        res = event_log.append(tenant, run_id, key, "memory_updated", {}, _usage(f"post:{i}", "0.01"))
        assert res.outcome == "created" and res.event.seq == 2 + i
    assert [e.seq for e in event_log.list_events(tenant, run_id)] == [0, 1, 2, 3, 4]
    assert run_store.run_tree_cost(tenant, run_id) == Decimal("0.33")


def test_end_again_returns_the_existing_event_not_dropped(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    first = event_log.append(tenant, run_id, "end", "run_completed", {"output": "a"})
    again = event_log.append(tenant, run_id, "end", "run_failed", {"status": "failed"})
    assert first.outcome == "created" and again.outcome == "exists" and not again.dropped
    assert again.event.type == "run_completed" and again.event.data == {"output": "a"}


def test_a_run_without_end_still_appends_normally(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    res = [event_log.append(tenant, run_id, f"step:{i}:completed", "step_completed", {}) for i in range(3)]
    assert [r.outcome for r in res] == ["created"] * 3 and [r.event.seq for r in res] == [0, 1, 2]


def test_failed_handoff_without_end_is_revived_but_cancelled_run_is_not(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert run_store.mark_enqueue_failed(tenant, run_id)
    assert run_store.mark_running(tenant, run_id)
    assert run_store.cancel_run_tree(tenant, run_id).status == "failed"
    assert run_store.mark_running(tenant, run_id) is False
    assert run_store.get_run(tenant, run_id)["error"] == RUN_CANCELLED_ERROR


# ------------------------------------------------------------------ run_tree_cost and read_outputs


def test_tree_cost_sums_priced_rows_and_counts_unpriced_as_zero(run_store, event_log, tenant):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    grand = _child(run_store, tenant, child, "g")
    sibling_root = _workflow(run_store, tenant)  # an unrelated tree
    event_log.append(tenant, root, "model:0:final", "model_message", {}, _usage("model:0", "0.10"))
    event_log.append(tenant, root, "judge:j:0", "model_message", {}, _usage("judge:j:0", "0.20"))
    event_log.append(tenant, root, "judge:j:1", "model_message", {}, _usage("judge:j:1", None))
    event_log.append(tenant, child, "model:0:final", "model_message", {}, _usage("model:0", "0.30"))
    event_log.append(tenant, grand, "model:0:final", "model_message", {}, _usage("model:0", "0.40"))
    event_log.append(tenant, grand, "model:1:final", "model_message", {}, _usage("model:1", None))
    event_log.append(tenant, sibling_root, "model:0:final", "model_message", {}, _usage("model:0", "5"))
    assert run_store.run_tree_cost(tenant, root) == Decimal("1.00")
    assert run_store.run_tree_cost(tenant, child) == Decimal("0.70")
    assert run_store.run_tree_cost(tenant, grand) == Decimal("0.40")


def test_tree_cost_of_unpriced_only_or_empty_run_is_zero(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    assert run_store.run_tree_cost(tenant, run_id) == Decimal(0)
    event_log.append(tenant, run_id, "model:0:final", "model_message", {}, _usage("model:0", None))
    assert run_store.run_tree_cost(tenant, run_id) == Decimal(0)
    assert run_store.run_tree_cost(tenant, uuid4()) == Decimal(0)


def test_read_outputs_edge_cases(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    for k in ("a", "b"):
        event_log.append(tenant, run_id, f"step:{k}:completed", "step_completed", {"k": k})
    assert event_log.read_outputs(tenant, run_id, []) == {}
    got = event_log.read_outputs(tenant, run_id, ("step:a:completed", "step:a:completed", "nope"))
    assert list(got) == ["step:a:completed"] and got["step:a:completed"].data == {"k": "a"}
    assert event_log.read_outputs(tenant, uuid4(), ["step:a:completed"]) == {}
    # A dropped (post-end) key is never readable.
    event_log.append(tenant, run_id, "end", "run_completed", {})
    event_log.append(tenant, run_id, "step:late:completed", "step_completed", {})
    assert event_log.read_outputs(tenant, run_id, ["step:late:completed"]) == {}


# ------------------------------------------------------------------ approvals closed on cancel


def _wait(event_log, tenant, run_id, kind, key):
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    if kind == "tool":
        data = {"tool_name": "send", "args": {}, "deadline": deadline}
    else:
        data = {"step_id": key, "prompt": "ok?", "deadline": deadline}
    assert event_log.append_approval_waiting(tenant, run_id, kind, key, data).outcome == "created"


def test_cancel_closes_approvals_as_denied_by_system_and_inbox_shows_them_decided(run_store, event_log, tenant, engine):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    _wait(event_log, tenant, root, "step", "gate")
    _wait(event_log, tenant, child, "tool", "call_1")
    store = ApprovalStore(engine)
    assert len(store.list(tenant, "pending")[0]) == 2
    result = run_store.cancel_run_tree(tenant, root)
    assert result.approvals_closed == 2
    pending, _ = store.list(tenant, "pending")
    decided, _ = store.list(tenant, "decided")
    assert pending == []  # the inbox no longer offers them
    assert {(r["decision"], r["decided_by"], r["status"]) for r in decided} == {("denied", CANCELLED_DECIDED_BY, "decided")}
    assert CANCELLED_DECIDED_BY == "system:run_cancelled" and all(r["decided_at"] for r in decided)
    assert {r["run_id"] for r in decided} == {str(root), str(child)}
    # Nothing can be decided afterwards, and a repeated cancel closes nothing more.
    assert event_log.append_tool_decision(tenant, child, "call_1", "tool_approved", {})[0] == "not_waiting"
    assert run_store.cancel_run_tree(tenant, root).approvals_closed == 0


def test_cancel_keeps_a_previously_decided_approval_and_other_trees(run_store, event_log, tenant, engine):
    root, other = _workflow(run_store, tenant), _workflow(run_store, tenant)
    _wait(event_log, tenant, root, "step", "gate1")
    _wait(event_log, tenant, root, "step", "gate2")
    _wait(event_log, tenant, other, "step", "gate")
    res = event_log.append_step_decision(tenant, root, "gate1", "step_approved", {"decided_by": "alice"})
    assert res[0] == "created"
    before = {r["approval_key"]: (r["decision"], r["decided_by"]) for r in ApprovalStore(engine).list_for_run(tenant, str(root))}
    run_store.cancel_run_tree(tenant, root)
    after = {r["approval_key"]: (r["decision"], r["decided_by"]) for r in ApprovalStore(engine).list_for_run(tenant, str(root))}
    assert after["gate1"] == before["gate1"] and after["gate1"][1] != CANCELLED_DECIDED_BY
    assert after["gate2"] == ("denied", CANCELLED_DECIDED_BY)
    assert [r["decided_at"] for r in ApprovalStore(engine).list_for_run(tenant, str(other))] == [None]


# ------------------------------------------------------------------ tenant isolation of the new methods
# test_rm_store_isolation enumerates only KnowledgeStore and MemoryStore (no RunStore/EventLog), so the
# LP.4 methods are covered here.


def _state(engine, tenant):
    with engine.connect() as conn:
        runs = conn.execute(text("SELECT id, status, error, finished_at, heartbeat_at, updated_at FROM runs WHERE tenant_id = :t ORDER BY id"), {"t": tenant}).all()
        events = conn.execute(text("SELECT run_id, seq, event_key FROM execution_events WHERE tenant_id = :t ORDER BY run_id, seq"), {"t": tenant}).all()
        appr = conn.execute(text("SELECT run_id, approval_key, decision, decided_by FROM pending_approvals WHERE tenant_id = :t ORDER BY run_id, approval_key"), {"t": tenant}).all()
    return runs, events, appr


def test_tenant_b_cannot_touch_tenant_as_tree_with_the_new_methods(run_store, event_log, tenant, engine):
    intruder = "intruder_" + tenant
    try:
        root = _workflow(run_store, tenant)
        child = _child(run_store, tenant, root)
        _wait(event_log, tenant, child, "tool", "call_1")
        event_log.append(tenant, root, "step:a:completed", "step_completed", {"x": 1}, _usage("model:0", "1"))
        mine = _workflow(run_store, intruder)
        _wait(event_log, intruder, mine, "step", "gate")
        before_a, before_b = _state(engine, tenant), _state(engine, intruder)
        for rid in (root, child):
            with pytest.raises(RunNotFound):
                run_store.cancel_run_tree(intruder, rid)
            with pytest.raises(RunNotFound):
                run_store.heartbeat(intruder, rid)
            with pytest.raises(RunNotFound):
                run_store.mark_running(intruder, rid)
            with pytest.raises(RunNotFound):
                run_store.set_status(intruder, rid, "failed", None, "E: x")
            assert run_store.run_tree_cost(intruder, rid) == Decimal(0)
            assert event_log.read_outputs(intruder, rid, ["step:a:completed", "end"]) == {}
            with pytest.raises(RunNotFound):
                event_log.append(intruder, rid, "end", "run_failed", {"status": "failed"})
        assert _state(engine, tenant) == before_a
        assert _state(engine, intruder) == before_b
        # Tenant A's own cancel then touches nothing of B's.
        run_store.cancel_run_tree(tenant, root)
        assert _state(engine, intruder) == before_b
        assert run_store.get_run(intruder, mine)["status"] == "running"
    finally:
        with engine.begin() as conn:
            for table in ("pending_approvals", "run_model_requests", "execution_events", "side_effects", "runs"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": intruder})


def test_find_unfinished_cancels_reports_the_owning_tenant(run_store, event_log, tenant, engine):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "cancel", "cancel_requested", {})
    _age_cancel(engine, run_id, 300)
    rows = [r for r in run_store.find_unfinished_cancels(timedelta(seconds=30), limit=10000) if r["run_id"] == str(run_id)]
    assert [r["tenant_id"] for r in rows] == [tenant]
