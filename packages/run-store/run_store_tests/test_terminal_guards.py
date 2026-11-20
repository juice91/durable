"""LP.4: run-store terminal guards, the cancel tree and the loop reads (loops D6, D11, D12).
Implementer's focused tests; the Sonnet tester adds the acceptance suite."""

from __future__ import annotations

import pickle
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import (
    RUN_CANCELLED_ERROR,
    AppendResult,
    ApprovalStore,
    PostgresSideEffectStore,
    RunNotFound,
)


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _usage(key: str, cost: str | None) -> dict:
    return {
        "request_key": key, "model_id": "test:priced", "provider_model_name": None,
        "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "usage_measured": True, "cost_usd": None if cost is None else Decimal(cost), "price_source": "test",
    }


def _workflow(run_store, tenant):
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    assert run_store.mark_running(tenant, run_id)
    return run_id


def _child(run_store, tenant, parent, step="s1"):
    run_id = uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, "x", run_id, parent, step)
    assert run_store.mark_running(tenant, run_id)
    return run_id


def _row(run_store, tenant, run_id):
    run = run_store.get_run(tenant, run_id)
    return {k: run[k] for k in ("status", "error", "output", "finished_at", "input_tokens", "cost_usd", "heartbeat_at")}


# ------------------------------------------------------------------ status guards


def test_non_terminal_writes_after_terminal_are_refused(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"})
    assert run_store.set_status(tenant, run_id, "completed", "ok")
    before = _row(run_store, tenant, run_id)
    assert run_store.set_status(tenant, run_id, "running") is False
    assert run_store.mark_running(tenant, run_id) is False
    assert run_store.heartbeat(tenant, run_id) is False
    assert run_store.mark_enqueue_failed(tenant, run_id) is False
    assert _row(run_store, tenant, run_id) == before


def test_terminal_status_must_match_end(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_failed", {"status": "needs_attention", "error": "E: x"})
    assert run_store.set_status(tenant, run_id, "completed", "late") is False
    assert run_store.set_status(tenant, run_id, "failed", None, "E: x") is False
    assert run_store.get_run(tenant, run_id)["status"] == "running"
    assert run_store.set_status(tenant, run_id, "needs_attention", None, "E: x") is True
    finished = run_store.get_run(tenant, run_id)["finished_at"]
    # A re-executed finish step is a no-op now: nothing changes.
    assert run_store.set_status(tenant, run_id, "needs_attention", None, "E: other") is False
    run = run_store.get_run(tenant, run_id)
    assert (run["status"], run["error"], run["finished_at"]) == ("needs_attention", "E: x", finished)


def test_skipped_is_a_terminal_status_fixed_by_the_end_outcome(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_completed", {"output": "{}", "outcome": "skipped"})
    assert run_store.set_status(tenant, run_id, "completed", "{}") is False  # the end fixes `skipped`
    assert run_store.set_status(tenant, run_id, "skipped", "{}") is True
    run = run_store.get_run(tenant, run_id)
    assert run["status"] == "skipped" and run["finished_at"] is not None
    assert run_store.set_status(tenant, run_id, "running") is False  # terminal: never revived
    assert run_store.mark_running(tenant, run_id) is False


def test_finish_with_end_skipped_writes_a_completed_event_with_the_outcome(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    assert run_store.finish_with_end(tenant, run_id, "skipped", output='{"status":"outside_slot"}')
    end = event_log.get_event(tenant, run_id, "end")
    assert (end.type, end.data) == ("run_completed", {"output": '{"status":"outside_slot"}', "outcome": "skipped"})
    assert run_store.get_run(tenant, run_id)["status"] == "skipped"


def test_terminal_status_needs_an_end(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert run_store.set_status(tenant, run_id, "running")
    assert run_store.set_status(tenant, run_id, "completed", output="a") is False  # no end
    assert run_store.get_run(tenant, run_id)["status"] == "running"
    assert run_store.finish_with_end(tenant, run_id, "completed", output="a")
    end = event_log.get_event(tenant, run_id, "end")
    assert (end.type, end.data) == ("run_completed", {"output": "a"})
    assert run_store.finish_with_end(tenant, run_id, "failed", error="E: b") is False
    assert run_store.get_run(tenant, run_id)["status"] == "completed"
    with pytest.raises(ValueError):
        run_store.finish_with_end(tenant, run_id, "running")
    with pytest.raises(RunNotFound):
        run_store.set_status("intruder_" + tenant, run_id, "completed")
    with pytest.raises(RunNotFound):
        run_store.finish_with_end("intruder_" + tenant, run_id, "completed")
    with pytest.raises(RunNotFound):
        run_store.heartbeat("intruder_" + tenant, run_id)


def test_failed_handoff_is_still_revived(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert run_store.mark_enqueue_failed(tenant, run_id)
    assert run_store.mark_running(tenant, run_id)
    assert run_store.get_run(tenant, run_id)["status"] == "running"
    # ...but not once it has an end event.
    other = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", other)
    assert run_store.mark_enqueue_failed(tenant, other)
    event_log.append(tenant, other, "end", "run_failed", {"status": "failed"})
    assert run_store.mark_running(tenant, other) is False


# ------------------------------------------------------------------ appends after end


def test_appends_after_end_are_dropped_without_seq(run_store, event_log, tenant):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "start", "run_started", {})
    end, created = event_log.append(tenant, run_id, "end", "run_failed", {"status": "failed"})
    assert created and end.seq == 1
    again = event_log.append(tenant, run_id, "end", "run_completed", {})
    assert again.outcome == "exists" and again.event.seq == 1 and again.event.type == "run_failed"
    dropped = event_log.append(tenant, run_id, "step:a:completed", "step_completed", {}, _usage("model:9", "0.5"))
    assert isinstance(dropped, AppendResult) and dropped.dropped and dropped.outcome == "dropped"
    event, created = dropped
    assert (event.seq, event.type, created) == (1, "run_failed", False)
    assert [e.seq for e in event_log.list_events(tenant, run_id)] == [0, 1]
    assert event_log.get_event(tenant, run_id, "step:a:completed") is None
    assert run_store.run_tree_cost(tenant, run_id) == Decimal("0.5")  # the usage row is kept
    assert pickle.loads(pickle.dumps(dropped)).dropped is True


def test_post_turn_keys_still_append_after_end(run_store, event_log, tenant):
    run_id = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    event_log.append(tenant, run_id, "end", "run_completed", {"output": "x"})
    for key in ("memory:observe:1", "memory:reflect:1:failed", "embed:index:1"):
        result = event_log.append(tenant, run_id, key, "memory_updated", {})
        assert result.outcome == "created", key


def test_approval_wait_after_end_is_dropped(run_store, event_log, tenant, engine):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_failed", {"status": "failed"})
    result = event_log.append_approval_waiting(
        tenant, run_id, "step", "gate",
        {"step_id": "gate", "prompt": "ok?", "deadline": datetime.now(timezone.utc) + timedelta(hours=1)},
    )
    assert result.dropped
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM pending_approvals WHERE run_id = :r"), {"r": str(run_id)}).scalar() == 0


# ------------------------------------------------------------------ cancel_run_tree


def test_cancel_tree_ends_root_and_children(run_store, event_log, tenant, engine):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    done = _child(run_store, tenant, root, "s2")
    event_log.append(tenant, done, "end", "run_completed", {"output": "fine"})
    assert run_store.set_status(tenant, done, "completed", "fine")
    PostgresSideEffectStore(engine, tenant).reserve(f"{child}:call_1")  # outcome unknown

    result = run_store.cancel_run_tree(tenant, root)
    assert result.status == "failed"
    assert result.statuses == {str(root): "failed", str(child): "needs_attention", str(done): "completed"}
    assert set(result.ended) == {str(root), str(child)}
    end = event_log.get_event(tenant, root, "end")
    assert end.type == "run_failed" and end.data["error_code"] == "run_cancelled"
    assert run_store.get_run(tenant, root)["error"] == RUN_CANCELLED_ERROR
    assert run_store.get_run(tenant, root)["finished_at"] is not None
    assert event_log.get_event(tenant, child, "end").data["status"] == "needs_attention"

    again = run_store.cancel_run_tree(tenant, root)
    assert again.ended == () and again.statuses == result.statuses and again.approvals_closed == 0
    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree("intruder_" + tenant, root)


def test_cancel_tree_follows_an_existing_end(run_store, event_log, tenant):
    root = _workflow(run_store, tenant)
    event_log.append(tenant, root, "end", "run_completed", {"output": "{\"a\":1}", "result": {"a": 1}})
    result = run_store.cancel_run_tree(tenant, root)  # worker cut off before finish_run
    assert result.status == "completed" and result.ended == ()
    run = run_store.get_run(tenant, root)
    assert (run["status"], run["output"]) == ("completed", "{\"a\":1}")


def test_cancel_tree_closes_approvals_by_root(run_store, event_log, tenant, engine):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    deadline = datetime.now(timezone.utc) + timedelta(hours=1)
    event_log.append_approval_waiting(tenant, child, "tool", "call_1", {"tool_name": "send", "args": {}, "deadline": deadline})
    event_log.append_approval_waiting(tenant, root, "step", "gate", {"step_id": "gate", "prompt": "?", "deadline": deadline})
    result = run_store.cancel_run_tree(tenant, root)
    assert result.approvals_closed == 2
    rows = ApprovalStore(engine).list_for_run(tenant, str(child)) + ApprovalStore(engine).list_for_run(tenant, str(root))
    assert {r["status"] for r in rows} == {"decided"}
    assert event_log.append_tool_decision(tenant, child, "call_1", "tool_approved", {})[0] == "not_waiting"


def test_finish_against_cancel_race(run_store, event_log, tenant):
    for _ in range(20):
        run_id = _workflow(run_store, tenant)
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def finish():
            try:
                barrier.wait()
                event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"})
                run_store.set_status(tenant, run_id, "completed", "ok")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def cancel():
            try:
                barrier.wait()
                run_store.cancel_run_tree(tenant, run_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=finish), threading.Thread(target=cancel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        events = [e for e in event_log.list_events(tenant, run_id) if e.type in ("run_completed", "run_failed")]
        assert len(events) == 1
        from eap_schemas import status_for_end

        assert run_store.get_run(tenant, run_id)["status"] == status_for_end(events[0].type, events[0].data)


# ------------------------------------------------------------------ reads


def test_find_unfinished_cancels(run_store, event_log, tenant, engine):
    old, fresh, ended = _workflow(run_store, tenant), _workflow(run_store, tenant), _workflow(run_store, tenant)
    for run_id in (old, fresh, ended):
        event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "u"})
    event_log.append(tenant, ended, "end", "run_failed", {"status": "failed"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = now() - interval '5 minutes' WHERE run_id IN (:a, :b) AND event_key = 'cancel'"),
            {"a": str(old), "b": str(ended)},
        )
    found = [r for r in run_store.find_unfinished_cancels(timedelta(seconds=30), limit=1000) if r["tenant_id"] == tenant]
    assert [r["run_id"] for r in found] == [str(old)]


def test_read_outputs_and_tree_cost_are_tenant_scoped(run_store, event_log, tenant):
    root = _workflow(run_store, tenant)
    child = _child(run_store, tenant, root)
    event_log.append(tenant, root, "step:a:completed", "step_completed", {"output": 1})
    event_log.append(tenant, root, "step:b:completed", "step_completed", {"output": 2})
    got = event_log.read_outputs(tenant, root, ["step:a:completed", "step:x:completed"])
    assert list(got) == ["step:a:completed"] and got["step:a:completed"].data == {"output": 1}
    assert event_log.read_outputs("intruder_" + tenant, root, ["step:a:completed"]) == {}

    event_log.append(tenant, child, "model:0:final", "model_message", {}, _usage("model:0", "0.25"))
    event_log.append(tenant, root, "judge:j:0", "model_message", {}, _usage("judge:j:0", "0.5"))
    event_log.append(tenant, root, "judge:j:1", "model_message", {}, _usage("judge:j:1", None))
    assert run_store.run_tree_cost(tenant, root) == Decimal("0.75")
    assert run_store.run_tree_cost(tenant, child) == Decimal("0.25")
    assert run_store.run_tree_cost("intruder_" + tenant, root) == Decimal(0)


def test_dropped_append_keeps_ac5_row_lists(run_store, event_log, tenant, engine):
    """Merge of AC.5 into loops: a dropped-after-end append with AC.5's row list (the answering
    model's row plus failed fallback attempts) keeps every row, and run_tree_cost agrees with
    recorded_cost_usd (eap.check_budget's quantity) on a run without children."""
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_failed", {"status": "cancelled"})
    rows = [_usage("model:3", "0.5"), _usage("model:3:failed:0", "0.125"), _usage("model:3:failed:1", None)]
    dropped = event_log.append(tenant, run_id, "model:3:final", "model_message", {}, rows)
    assert dropped.dropped
    with engine.connect() as conn:
        keys = set(conn.execute(
            text("SELECT request_key FROM run_model_requests WHERE tenant_id = :t AND run_id = :r"),
            {"t": tenant, "r": str(run_id)},
        ).scalars())
    assert keys == {"model:3", "model:3:failed:0", "model:3:failed:1"}
    assert run_store.recorded_cost_usd(tenant, run_id) == Decimal("0.625")
    assert run_store.run_tree_cost(tenant, run_id) == run_store.recorded_cost_usd(tenant, run_id)


# ------------------------------------------------------------------ review fixes (LP.4 review)


def _age(engine, run_ids, minutes=5):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = now() - make_interval(mins => :m)"
                 " WHERE run_id = ANY(:r) AND event_key = 'cancel'"),
            {"m": minutes, "r": [str(r) for r in run_ids]},
        )


def test_cancelling_a_failed_handoff_finalises_it(run_store, event_log, tenant, engine):
    run_id = uuid4()
    run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
    assert run_store.mark_enqueue_failed(tenant, run_id)
    event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "u"})
    _age(engine, [run_id])
    assert [r["run_id"] for r in run_store.find_unfinished_cancels() if r["tenant_id"] == tenant] == [str(run_id)]

    result = run_store.cancel_run_tree(tenant, run_id)
    assert (result.status, result.ended) == ("failed", (str(run_id),))
    end = event_log.get_event(tenant, run_id, "end")
    assert (end.type, end.data["status"], end.data["error_code"]) == ("run_failed", "failed", "run_cancelled")
    run = run_store.get_run(tenant, run_id)
    assert (run["status"], run["error"]) == ("failed", RUN_CANCELLED_ERROR)
    assert run_store.mark_running(tenant, run_id) is False
    assert run_store.requeue_after_enqueue_failure(tenant, run_id) is False
    assert [r for r in run_store.find_unfinished_cancels() if r["tenant_id"] == tenant] == []
    assert run_store.cancel_run_tree(tenant, run_id).ended == ()


def test_cancelled_handoffs_never_starve_a_real_cancel(run_store, event_log, tenant, engine):
    handoffs = []
    for _ in range(105):
        run_id = uuid4()
        run_store.create_workflow_run(tenant, uuid4(), 1, "{}", run_id)
        run_store.mark_enqueue_failed(tenant, run_id)
        event_log.append(tenant, run_id, "cancel", "cancel_requested", {})
        handoffs.append(run_id)
    real = _workflow(run_store, tenant)
    event_log.append(tenant, real, "cancel", "cancel_requested", {})
    _age(engine, handoffs, minutes=10)  # older than the real one: listed first
    _age(engine, [real], minutes=5)

    # The reconciler loop: list (limit 100), then cancel_run_tree each.
    passes = 0
    while True:
        batch = [r for r in run_store.find_unfinished_cancels(limit=100) if r["tenant_id"] == tenant]
        if not batch:
            break
        passes += 1
        assert passes <= 2
        for row in batch:
            run_store.cancel_run_tree(row["tenant_id"], row["run_id"])
    assert passes == 2
    assert run_store.get_run(tenant, real)["status"] == "failed"
    assert event_log.get_event(tenant, real, "end").data["error_code"] == "run_cancelled"


def test_child_creation_is_refused_after_cancel_or_end(run_store, event_log, tenant):
    from eap_run_store import ParentRunStopped

    root = _workflow(run_store, tenant)
    early = _child(run_store, tenant, root)  # created before the cancel
    event_log.append(tenant, root, "cancel", "cancel_requested", {})
    late = uuid4()
    with pytest.raises(ParentRunStopped) as caught:
        run_store.create_child_agent_run(tenant, uuid4(), 1, "x", late, root, "s9")
    assert caught.value.reason == "cancel" and "being cancelled" in str(caught.value)
    assert pickle.loads(pickle.dumps(caught.value)).parent_run_id == str(root)
    assert run_store.get_run(tenant, late) is None
    # A replayed step finds its existing child row as before.
    assert run_store.create_child_agent_run(tenant, uuid4(), 1, "x", early, root, "s1") == (early, False)

    ended = _workflow(run_store, tenant)
    event_log.append(tenant, ended, "end", "run_completed", {"output": "x"})
    with pytest.raises(ParentRunStopped) as caught:
        run_store.create_child_agent_run(tenant, uuid4(), 1, "x", uuid4(), ended, "s1")
    assert caught.value.reason == "end"


def test_reconciler_sweeps_a_child_left_unfinished_after_the_tree_snapshot(run_store, event_log, tenant, engine):
    root = _workflow(run_store, tenant)
    event_log.append(tenant, root, "cancel", "cancel_requested", {})
    _age(engine, [root])
    run_store.cancel_run_tree(tenant, root)
    assert [r for r in run_store.find_unfinished_cancels() if r["tenant_id"] == tenant] == []
    # A child the refusal could not stop (inserted directly, as by a pre-guard worker).
    orphan = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
                 " parent_step_id, created_at, updated_at, model_overridden)"
                 " VALUES (:id, :t, 'agent', :a, 1, 'queued', 'x', :p, 's1', now(), now(), false)"),
            {"id": str(orphan), "t": tenant, "a": str(uuid4()), "p": str(root)},
        )
    assert [r["run_id"] for r in run_store.find_unfinished_cancels() if r["tenant_id"] == tenant] == [str(root)]
    result = run_store.cancel_run_tree(tenant, root)
    assert result.ended == (str(orphan),) and result.statuses[str(orphan)] == "failed"
    assert [r for r in run_store.find_unfinished_cancels() if r["tenant_id"] == tenant] == []


def _messages(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one()


def test_a_finish_after_cancel_followed_its_end_keeps_messages(run_store, event_log, tenant, engine):
    run_id = _workflow(run_store, tenant)
    event_log.append(tenant, run_id, "end", "run_completed", {"output": "ok"})
    assert run_store.cancel_run_tree(tenant, run_id).status == "completed"
    finished = run_store.get_run(tenant, run_id)["finished_at"]
    # The worker's finish runs late: refused (status already set) but its messages are kept.
    assert run_store.set_status(tenant, run_id, "completed", "ok", None, [{"kind": "request"}], "text") is False
    run = run_store.get_run(tenant, run_id)
    assert (run["status"], run["output"], run["finished_at"]) == ("completed", "ok", finished)
    assert _messages(engine, run_id) == [{"kind": "request"}] and run["output_format"] == "text"
    # Written once: a second finish never replaces them.
    run_store.set_status(tenant, run_id, "completed", "ok", None, [{"kind": "other"}])
    assert _messages(engine, run_id) == [{"kind": "request"}]
