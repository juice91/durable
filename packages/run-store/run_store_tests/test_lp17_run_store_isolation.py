"""LP.17: tenant isolation at the run-store layer for the four methods loops add or lean on.

Two tenants hold a workflow run of the same shape: a workflow id, a `cancel_requested` event, a
loop with three iteration events (one of them also written as a `step_completed`) and a model
request row (recorded cost). Each row's data carries the tenant's marker. The test calls the four
methods tenant B uses on A's run and asserts no row of A's changes and nothing of A's is returned:

- `cancel_run_tree` raises RunNotFound (the whole guarded terminal write of LP.8 / loops D12);
- `find_unfinished_cancels` returns rows across tenants by design (the reconciler sweeps every
  tenant), so we assert the per-tenant filter leaves A's cancel alone when another tenant reads;
- `read_outputs` (the immutable iteration read, loops D6) returns {} for another tenant and never
  carries another tenant's data;
- `run_tree_cost` (the loop tick's cost cap, D11) returns Decimal(0) for another tenant.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from eap_run_store import RunNotFound


@pytest.fixture
def tenants(engine):
    a = f"iso_lp17_a_{uuid4().hex[:8]}"
    b = f"iso_lp17_b_{uuid4().hex[:8]}"
    yield a, b
    with engine.begin() as conn:
        for t in (a, b):
            conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": t})
            conn.execute(
                text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": t}
            )
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _usage(key: str, cost: str | None, marker: str) -> dict:
    return {
        "request_key": key,
        "model_id": "test:priced",
        "provider_model_name": marker,
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost),
        "price_source": "test",
    }


def _seed(run_store, event_log, tenant: str, marker: str) -> tuple[UUID, UUID]:
    """A workflow run (plus one child agent run of a loop iteration) with: a `cancel_requested`
    event, three iteration events of loop `lp`, a `step_completed` for iteration 1, and a priced
    model request. Each payload carries `marker`."""
    workflow_id = uuid4()
    run_id = uuid4()
    run_store.create_workflow_run(tenant, workflow_id, 1, f'{{"marker": "{marker}"}}', run_id)
    assert run_store.mark_running(tenant, run_id)
    child_id = uuid4()
    run_store.create_child_agent_run(tenant, uuid4(), 1, marker, child_id, run_id, "lp")
    assert run_store.mark_running(tenant, child_id)

    event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": marker})
    event_log.append(tenant, run_id, "step:lp:started", "step_started",
                     {"step_id": "lp", "step_type": "loop", "scope": []})
    for i in range(3):
        event_log.append(
            tenant, run_id, f"loop:lp.{i}:started", "loop_iteration_started",
            {"step_id": "lp", "scope": [], "index": i, "state": {"marker": marker, "n": i}},
        )
        event_log.append(
            tenant, run_id, f"loop:lp.{i}:completed", "loop_iteration_completed",
            {"step_id": "lp", "scope": [], "index": i, "last_key": "k",
             "state": {"marker": marker, "n": i + 1}, "output": f"{marker}:{i}"},
        )
    # A top-level step on the parent, with the marker text as its output.
    event_log.append(tenant, run_id, "step:done:completed", "step_completed",
                     {"step_id": "done", "output": {"marker": marker}})
    # The child's billed model request (counts in the run tree cost).
    event_log.append(tenant, child_id, "model:0:final", "model_message",
                     {"text": marker}, _usage("model:0", "0.25", marker))
    return run_id, child_id


def _snapshot(engine, tenant: str) -> dict[str, list]:
    out: dict[str, list] = {}
    tables = (
        ("runs", "id"),
        ("execution_events", "seq, run_id"),
        ("run_model_requests", "run_id, request_key"),
        ("side_effects", "key"),
    )
    with engine.connect() as conn:
        for table, order in tables:
            out[table] = [
                r[0]
                for r in conn.execute(
                    text(f"SELECT to_jsonb(x) FROM {table} x WHERE tenant_id = :t ORDER BY {order}"),
                    {"t": tenant},
                )
            ]
    return out


def test_cancel_run_tree_raises_and_changes_nothing_cross_tenant(engine, run_store, event_log, tenants):
    """B's `cancel_run_tree` on A's run raises RunNotFound: no `end`, no `cancel`, no rows change."""
    a_t, b_t = tenants
    a_run, _ = _seed(run_store, event_log, a_t, "MA")
    b_run, _ = _seed(run_store, event_log, b_t, "MB")
    before_a = _snapshot(engine, a_t)
    before_b = _snapshot(engine, b_t)

    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree(b_t, a_run)

    assert _snapshot(engine, a_t) == before_a, "A's rows must not change"
    assert _snapshot(engine, b_t) == before_b, "B's rows must not change"

    # And the reverse direction also raises (symmetry of the guard).
    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree(a_t, b_run)
    assert _snapshot(engine, a_t) == before_a
    assert _snapshot(engine, b_t) == before_b


def test_cancel_run_tree_on_own_tenant_leaves_the_other_untouched(engine, run_store, event_log, tenants):
    """A's cancel ends A's tree. B's snapshot is unchanged (no cross-tenant write of any kind)."""
    a_t, b_t = tenants
    a_run, a_child = _seed(run_store, event_log, a_t, "MA")
    b_run, _ = _seed(run_store, event_log, b_t, "MB")
    before_b = _snapshot(engine, b_t)

    result = run_store.cancel_run_tree(a_t, a_run)
    assert result.status == "failed"
    # Both the parent and the child got an `end`:
    assert set(result.ended) == {str(a_run), str(a_child)}
    assert _snapshot(engine, b_t) == before_b


def test_find_unfinished_cancels_filters_rows_by_tenant_on_read(engine, run_store, event_log, tenants):
    """The reconciler enumerates tenants by design (no tenant argument), but a caller filters by
    `row["tenant_id"]`. We assert that the per-tenant filter yields each tenant's own row only."""
    a_t, b_t = tenants
    a_run, _ = _seed(run_store, event_log, a_t, "MA")
    b_run, _ = _seed(run_store, event_log, b_t, "MB")
    # Age the cancels past the default threshold.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = now() - interval '5 minutes' WHERE event_key = 'cancel'"
                 " AND tenant_id IN (:a, :b)"),
            {"a": a_t, "b": b_t},
        )

    rows = run_store.find_unfinished_cancels(older_than=timedelta(seconds=30), limit=1000)
    mine_a = [r for r in rows if r["tenant_id"] == a_t]
    mine_b = [r for r in rows if r["tenant_id"] == b_t]
    assert [r["run_id"] for r in mine_a] == [str(a_run)], mine_a
    assert [r["run_id"] for r in mine_b] == [str(b_run)], mine_b
    # No row crosses tenants (the per-tenant rows reference only their own run id).
    assert all(r["run_id"] != str(b_run) for r in mine_a)
    assert all(r["run_id"] != str(a_run) for r in mine_b)


def test_read_outputs_is_tenant_scoped_for_loop_iteration_reads(engine, run_store, event_log, tenants):
    """The immutable iteration read (loops D6) answers {} for the wrong tenant, and never leaks
    another tenant's data. Keys include a loop iteration key and a top-level step key."""
    a_t, b_t = tenants
    a_run, _ = _seed(run_store, event_log, a_t, "MARKER_A")
    _b_run, _ = _seed(run_store, event_log, b_t, "MARKER_B")

    keys = ["loop:lp.0:completed", "loop:lp.1:completed", "step:done:completed", "missing:key"]
    got_a = event_log.read_outputs(a_t, a_run, keys)
    # A's read sees A's events only (MARKER_A, never MARKER_B).
    assert set(got_a) == {"loop:lp.0:completed", "loop:lp.1:completed", "step:done:completed"}
    dump = repr({k: v.data for k, v in got_a.items()})
    assert "MARKER_A" in dump and "MARKER_B" not in dump

    # B's read of A's run returns {} (unknown run in B's tenant).
    assert event_log.read_outputs(b_t, a_run, keys) == {}
    # An intruder tenant also gets {}; nothing leaks.
    assert event_log.read_outputs(f"intruder_{a_t}", a_run, keys) == {}
    # An empty keys list is {} (and never a full scan).
    assert event_log.read_outputs(a_t, a_run, []) == {}


def test_run_tree_cost_is_tenant_scoped(engine, run_store, event_log, tenants):
    """run_tree_cost (D11, the loop tick's cost cap) returns the tenant's own cost only; another
    tenant's call on the same run id gives Decimal(0), even when both tenants seeded priced rows."""
    a_t, b_t = tenants
    a_run, a_child = _seed(run_store, event_log, a_t, "MA")
    b_run, _ = _seed(run_store, event_log, b_t, "MB")
    # A has one 0.25 charge on the child (which is in the tree), B has one 0.25 charge.
    assert run_store.run_tree_cost(a_t, a_run) == Decimal("0.25")
    assert run_store.run_tree_cost(b_t, b_run) == Decimal("0.25")

    # Cross-tenant: B asking about A's run (same id, wrong tenant) sees nothing.
    assert run_store.run_tree_cost(b_t, a_run) == Decimal(0)
    assert run_store.run_tree_cost(a_t, b_run) == Decimal(0)
    # An intruder tenant: Decimal(0).
    assert run_store.run_tree_cost(f"intruder_{a_t}", a_run) == Decimal(0)
    # Even the child id alone from the wrong tenant: Decimal(0).
    assert run_store.run_tree_cost(b_t, a_child) == Decimal(0)
