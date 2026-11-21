"""LP.8 acceptance for POST /runs/{id}/cancel on the API side (fake DBOS client, no worker).

The implementer's tests stay as they are; these add the refusals for every run kind, the crash
window with a launcher stub (a child run, a waiting approval), concurrent cancels and isolation.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_api.services import workflow_runs

from .run_helpers import hard_timeout, requires_pg
from .test_workflow_run_cancel_api import CancelClient, _events, _start
from .test_workflow_runs_api import api, publish_demo  # noqa: F401 - fixtures

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> CancelClient:
    return CancelClient()


def _agent_id(app, tenant) -> str:
    with app.state.database_engine.connect() as conn:
        return str(conn.execute(text("SELECT id FROM agents WHERE tenant_id = :t"), {"t": tenant}).scalar_one())


def _ends(client, rid, headers):
    return [e for e in _events(client, rid, headers) if e["type"] in ("run_completed", "run_failed", "run_needs_attention")]


def test_every_non_workflow_kind_is_cancel_not_supported(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_store, _ = app.state.run_stores
    workflow_id = publish_demo(client, headers)  # also creates an agent for the tenant
    agent = uuid.UUID(_agent_id(app, tenant))
    agent_run, tool_run, child = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run_store.create_run(tenant, agent, 1, "hi", agent_run)
    run_store.create_tool_run(tenant, "clock.now", "{}", tool_run)
    wf_run = _start(client, headers)
    run_store.create_child_agent_run(tenant, agent, 1, "hi", child, wf_run, "ask")
    for rid in (agent_run, tool_run, child):
        r = client.post(f"/api/v1/runs/{rid}/cancel", headers=headers)
        assert r.status_code == 409, (rid, r.text)
        assert r.json() == {"detail": "only workflow runs can be cancelled", "code": "cancel_not_supported"}
        assert run_store.get_run(tenant, rid)["status"] == "queued"
    assert fake.cancels == []  # no cascade for a refused run
    assert not any(e["type"] == "cancel_requested" for rid in (agent_run, tool_run, child)
                   for e in _events(client, str(rid), headers))
    assert workflow_id


def test_cross_tenant_cancel_is_404_and_changes_nothing(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    _, other = tenant_of()
    run_id = _start(client, headers)
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=other)
    assert r.status_code == 404 and "cancel" not in r.text.lower().replace("cancel_", "")
    assert fake.cancels == []
    assert [e["type"] for e in _events(client, run_id, headers)] == []
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "queued"


def test_a_run_that_failed_on_its_own_is_409(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_store, _ = app.state.run_stores
    run_id = _start(client, headers)
    assert run_store.finish_with_end(tenant, uuid.UUID(run_id), "failed", None, "boom")
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 409 and r.json() == {"detail": "the run has already ended"}
    assert fake.cancels == [] and len(_ends(client, run_id, headers)) == 1
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "failed"


def test_concurrent_cancels_all_inside_the_cascade_write_one_end_per_run(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_store, _ = app.state.run_stores
    run_id = _start(client, headers)
    child = uuid.uuid4()
    run_store.create_child_agent_run(tenant, uuid.UUID(_agent_id(app, tenant)), 1, "hi", child, run_id, "ask")
    n = 6
    barrier = threading.Barrier(n, timeout=30)
    fake.during_cancel = lambda: barrier.wait()  # every request is inside the cascade at once
    out: list[tuple[int, dict[str, Any]]] = []

    def post() -> None:
        r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
        out.append((r.status_code, r.json()))

    threads = [threading.Thread(target=post) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert [s for s, _ in out] == [202] * n, out
    assert {b["status"] for _, b in out} == {"failed"}
    assert len(fake.cancels) == n and all(c == (run_id, True) for c in fake.cancels)
    for rid in (run_id, str(child)):
        events = _events(client, rid, headers)
        assert len(_ends(client, rid, headers)) == 1
        assert client.get(f"/api/v1/runs/{rid}", headers=headers).json()["status"] == "failed"
        assert [e["seq"] for e in events] == list(range(len(events)))
    assert [e["type"] for e in _events(client, run_id, headers)].count("cancel_requested") == 1


def test_crash_window_leaves_the_tree_unended_until_a_retry_ends_every_run(api, fake, monkeypatch):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_store, _ = app.state.run_stores
    run_id = _start(client, headers)
    child = uuid.uuid4()
    run_store.create_child_agent_run(tenant, uuid.UUID(_agent_id(app, tenant)), 1, "hi", child, run_id, "ask")
    real = workflow_runs._finish_cancel
    crashed: list[bool] = []

    def crash(*a: Any) -> Any:
        if not crashed:
            crashed.append(True)
            raise SystemError("process died between the cascade and the end write")
        return real(*a)

    monkeypatch.setattr(workflow_runs, "_finish_cancel", crash)
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 503
    assert fake.cancels == [(run_id, True)]
    for rid in (run_id, str(child)):
        assert _ends(client, rid, headers) == []
        assert client.get(f"/api/v1/runs/{rid}", headers=headers).json()["status"] == "queued"
    # The unfinished cancel is what the reconciler's sweep lists (cross-tenant; ours must be in it).
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE execution_events SET ts = ts - interval '120 seconds' WHERE run_id = :r "
                          "AND event_key = 'cancel'"), {"r": run_id})
    listed = {r["run_id"] for r in run_store.find_unfinished_cancels(limit=1000)}
    assert run_id in listed
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "failed"
    for rid in (run_id, str(child)):
        assert len(_ends(client, rid, headers)) == 1
        assert client.get(f"/api/v1/runs/{rid}", headers=headers).json()["status"] == "failed"
    assert run_id not in {x["run_id"] for x in run_store.find_unfinished_cancels(limit=1000)}
