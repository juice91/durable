"""LP.17: API-level tenant isolation of the four methods loops add or lean on.

Two tenants A and B each publish the same workflow and start a workflow run; A's run has a loop
with iteration events and a priced model request on a child run. Each of the four surfaces behaves
the same: A's data never flows to B through this API call, and B calling the operation with A's
run id answers 404 (unknown run in B's tenant) without touching A.

- `cancel` (POST /runs/{id}/cancel) — B answers 404 on A's run id; no event written for A.
- `reconcile` (`RunStore.find_unfinished_cancels` + `_finish_cancel`) — the reconciler's seam (used
  by the cancel route to drive the terminal write) refuses to finish A's run from B's tenant.
- `read_outputs` (the EventLog read loops use at iteration begin, loops D6) — a cross-tenant read
  returns {}.
- `run_tree_cost` (loops D11) — a cross-tenant read returns Decimal(0).

The route uses the real `cancel_run_tree`/`find_unfinished_cancels`; the DBOS client is a seam
(the imported fake records cascades but does nothing to the DB).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_run_store import RunNotFound

from .test_workflow_run_cancel_api import CancelClient, _events, _start  # noqa: F401 - route seam
from .test_workflow_runs_api import api, publish_demo  # noqa: F401 - fixtures
from .run_helpers import requires_pg, hard_timeout

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> CancelClient:
    """Override the imported `fake` fixture with a cancel-recording client."""
    return CancelClient()


def _seed_loop_and_cost(app, tenant: str, run_id: str, marker: str) -> str:
    """Append a `cancel_requested`, three loop iteration events, a top-level step completed, and a
    priced child model request to `run_id`. Returns the child run id."""
    run_store, _ = app.state.run_stores
    engine = app.state.database_engine
    with engine.connect() as conn:
        agent_id = conn.execute(
            text("SELECT id FROM agents WHERE tenant_id = :t"), {"t": tenant}
        ).scalar_one()
    child_id = uuid.uuid4()
    run_store.create_child_agent_run(tenant, agent_id, 1, marker, child_id, run_id, "lp")

    _, event_log = app.state.run_stores
    event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": marker})
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
    event_log.append(
        tenant, run_id, "step:done:completed", "step_completed",
        {"step_id": "done", "output": {"marker": marker}},
    )
    usage = {
        "request_key": "model:0", "model_id": "test:priced", "provider_model_name": marker,
        "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "usage_measured": True, "cost_usd": Decimal("0.25"), "price_source": "test",
    }
    event_log.append(tenant, child_id, "model:0:final", "model_message", {"text": marker}, usage)
    return str(child_id)


def _snapshot_rows(engine, tenant: str) -> dict[str, list]:
    tables = (
        ("runs", "id"),
        ("execution_events", "seq, run_id"),
        ("run_model_requests", "run_id, request_key"),
    )
    out: dict[str, list] = {}
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


def test_cancel_returns_404_and_writes_nothing_cross_tenant(api, fake):
    client, app, tenant_of = api
    a_t, a_headers = tenant_of()
    b_t, b_headers = tenant_of()
    a_run = _start(client, a_headers)
    b_run = _start(client, b_headers)
    _seed_loop_and_cost(app, a_t, a_run, "MARK_A")
    _seed_loop_and_cost(app, b_t, b_run, "MARK_B")
    engine = app.state.database_engine
    before_a = _snapshot_rows(engine, a_t)

    # B cannot cancel A's run.
    r = client.post(f"/api/v1/runs/{a_run}/cancel", headers=b_headers)
    assert r.status_code == 404, r.text
    # Nor can A cancel B's run.
    assert client.post(f"/api/v1/runs/{b_run}/cancel", headers=a_headers).status_code == 404
    # No DBOS cascade was triggered for the cross-tenant attempts.
    assert fake.cancels == []
    assert _snapshot_rows(engine, a_t) == before_a, "A's rows must not change on a cross-tenant cancel"


def test_cancel_in_own_tenant_does_not_see_or_touch_the_other(api, fake):
    """A's cancel ends A's tree; B's rows are unchanged, and B still cancels its own run normally."""
    client, app, tenant_of = api
    a_t, a_headers = tenant_of()
    b_t, b_headers = tenant_of()
    a_run = _start(client, a_headers)
    b_run = _start(client, b_headers)
    _seed_loop_and_cost(app, a_t, a_run, "MARK_A")
    _seed_loop_and_cost(app, b_t, b_run, "MARK_B")
    engine = app.state.database_engine
    before_b = _snapshot_rows(engine, b_t)

    r = client.post(f"/api/v1/runs/{a_run}/cancel", headers=a_headers)
    assert r.status_code == 202, r.text
    assert r.json()["cancelled"] is True
    assert fake.cancels == [(a_run, True)]
    assert _snapshot_rows(engine, b_t) == before_b, "B's rows must not change when A cancels its own run"
    # B's cancel still works, independently.
    fake.cancels.clear()
    rb = client.post(f"/api/v1/runs/{b_run}/cancel", headers=b_headers)
    assert rb.status_code == 202 and rb.json()["cancelled"] is True
    assert fake.cancels == [(b_run, True)]


def test_reconcile_seam_refuses_to_finish_a_run_from_another_tenant(api, fake):
    """The route's `_finish_cancel` seam calls `RunStore.cancel_run_tree(tenant, run_id)`. With the
    wrong tenant it raises RunNotFound (the same guard as the reconciler scheduled on the worker).
    The reconciler reads `find_unfinished_cancels()` rows that carry the owning tenant, and only
    calls the terminal write with that tenant; this guards against a careless tenant swap."""
    client, app, tenant_of = api
    a_t, a_headers = tenant_of()
    b_t, b_headers = tenant_of()
    a_run = _start(client, a_headers)
    _start(client, b_headers)
    _seed_loop_and_cost(app, a_t, a_run, "MARK_A")
    run_store, _ = app.state.run_stores
    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree(b_t, a_run)
    with pytest.raises(RunNotFound):
        run_store.cancel_run_tree(f"intruder_{a_t}", a_run)


def test_read_outputs_is_tenant_scoped_through_the_app_event_log(api, fake):
    """The immutable iteration read loops do (loops D6) is tenant-scoped: another tenant's call
    returns {}, and A's own call never leaks B's data. The API never exposes this read directly,
    so we assert through the shared EventLog on app state (the one the worker uses)."""
    client, app, tenant_of = api
    a_t, a_headers = tenant_of()
    b_t, b_headers = tenant_of()
    a_run = _start(client, a_headers)
    _start(client, b_headers)
    _seed_loop_and_cost(app, a_t, a_run, "MARK_A")
    _, event_log = app.state.run_stores
    keys = ["loop:lp.0:completed", "loop:lp.2:completed", "step:done:completed"]
    got = event_log.read_outputs(a_t, a_run, keys)
    assert set(got) == set(keys)
    assert all("MARK_A" in repr(v.data) and "MARK_B" not in repr(v.data) for v in got.values())
    # Cross-tenant: empty, even with the same run id.
    assert event_log.read_outputs(b_t, a_run, keys) == {}
    assert event_log.read_outputs(f"intruder_{a_t}", a_run, keys) == {}


def test_run_tree_cost_is_tenant_scoped_through_the_app_store(api, fake):
    """The loop tick's cost cap quantity (D11) is tenant-scoped: another tenant on A's run id gets
    Decimal(0), and A's own call sees A's rows only."""
    client, app, tenant_of = api
    a_t, a_headers = tenant_of()
    b_t, b_headers = tenant_of()
    a_run = _start(client, a_headers)
    b_run = _start(client, b_headers)
    _seed_loop_and_cost(app, a_t, a_run, "MARK_A")
    _seed_loop_and_cost(app, b_t, b_run, "MARK_B")
    run_store, _ = app.state.run_stores
    assert run_store.run_tree_cost(a_t, a_run) == Decimal("0.25")
    assert run_store.run_tree_cost(b_t, b_run) == Decimal("0.25")
    assert run_store.run_tree_cost(b_t, a_run) == Decimal(0)
    assert run_store.run_tree_cost(a_t, b_run) == Decimal(0)
    assert run_store.run_tree_cost(f"intruder_{a_t}", a_run) == Decimal(0)
