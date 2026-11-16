"""POST /runs/{run_id}/cancel (loops D12, LP.8): focused implementer tests of the route contract
(a fake DBOSClient inside the real DBOSRunLauncher; no worker)."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.routes import workflow_runs

from .test_workflow_runs_api import FakeClient, api, fake, publish_demo  # noqa: F401 - fixtures
from .run_helpers import requires_pg, hard_timeout

pytestmark = requires_pg


class CancelClient(FakeClient):
    """Records DBOS cancels; `during_cancel` runs inside the cancel call (the cascade)."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[tuple[str, bool]] = []
        self.fail_cancel = False
        self.during_cancel: Any = None

    def cancel_workflow(self, workflow_id: str, *, cancel_children: bool = False, shard_key: str | None = None) -> None:
        if self.fail_cancel:
            raise ConnectionError("system database unreachable")
        if self.during_cancel is not None:
            self.during_cancel()
        self.cancels.append((workflow_id, cancel_children))


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> CancelClient:  # noqa: F811 - overrides the imported fixture
    return CancelClient()


def _start(client: TestClient, headers: dict[str, str]) -> str:
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers)
    assert r.status_code == 202, r.text
    return run_id


def _events(client: TestClient, run_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    return client.get(f"/api/v1/runs/{run_id}/events", headers=headers).json()


def test_cancel_ends_the_run_tree_once_and_is_idempotent(api, fake):  # noqa: F811
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = _start(client, headers)
    run_store, _ = app.state.run_stores
    with app.state.database_engine.connect() as conn:
        agent_id = conn.execute(text("SELECT id FROM agents WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    child_id = uuid.uuid4()
    run_store.create_child_agent_run(tenant, agent_id, 1, "hi", child_id, run_id, "ask")

    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 202, r.text
    assert r.json() == {
        "run_id": run_id, "kind": "workflow", "status": "failed", "display_status": "failed",
        "error_code": "run_cancelled", "cancelled": True, "requested_by": "alice", "approvals_closed": 0,
    }
    assert fake.cancels == [(run_id, True)]
    for rid in (run_id, str(child_id)):
        run = client.get(f"/api/v1/runs/{rid}", headers=headers).json()
        assert run["status"] == "failed", rid
        ends = [e for e in _events(client, rid, headers) if e["type"] in ("run_failed", "run_completed")]
        assert len(ends) == 1 and ends[0]["data"]["error_code"] == "run_cancelled"
    cancels = [e for e in _events(client, run_id, headers) if e["type"] == "cancel_requested"]
    assert [e["data"] for e in cancels] == [{"requested_by": "alice"}]

    # A retry: 202 with the same status, the cascade again, no second end or cancel event.
    again = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert again.status_code == 202 and again.json()["status"] == "failed" and again.json()["cancelled"] is True
    assert fake.cancels == [(run_id, True), (run_id, True)]
    types = [e["type"] for e in _events(client, run_id, headers)]
    assert types.count("cancel_requested") == 1 and types.count("run_failed") == 1


def test_refusals(api, fake):  # noqa: F811
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_store, _ = app.state.run_stores
    # Not a workflow run.
    tool_run = uuid.uuid4()
    run_store.create_tool_run(tenant, "clock.now", "{}", tool_run)
    r = client.post(f"/api/v1/runs/{tool_run}/cancel", headers=headers)
    assert r.status_code == 409 and r.json() == {"detail": "only workflow runs can be cancelled", "code": "cancel_not_supported"}
    # Unknown, and another tenant's.
    assert client.post(f"/api/v1/runs/{uuid.uuid4()}/cancel", headers=headers).status_code == 404
    run_id = _start(client, headers)
    _, other = tenant_of()
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=other).status_code == 404
    # Ended on its own before any cancel: 409, nothing appended, no cascade.
    assert run_store.finish_with_end(tenant, uuid.UUID(run_id), "completed", output="{}")
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 409 and r.json() == {"detail": "the run has already ended"}
    assert fake.cancels == []
    assert "cancel_requested" not in [e["type"] for e in _events(client, run_id, headers)]
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "completed"


def test_failed_cascade_leaves_the_durable_request_and_a_retry_finishes(api, fake):  # noqa: F811
    client, _app, tenant_of = api
    _, headers = tenant_of()
    run_id = _start(client, headers)
    fake.fail_cancel = True
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 503
    types = [e["type"] for e in _events(client, run_id, headers)]
    assert types == ["cancel_requested"]
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "queued"
    fake.fail_cancel = False
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).json()["status"] == "failed"


def test_crash_after_the_cascade_is_finished_by_a_retry(api, fake, monkeypatch):  # noqa: F811
    client, _app, tenant_of = api
    _, headers = tenant_of()
    run_id = _start(client, headers)

    real = workflow_runs._finish_cancel
    crashing = [True]

    def crash(*args: Any) -> Any:
        if crashing[0]:
            raise RuntimeError("API died after the cascade")
        return real(*args)

    monkeypatch.setattr(workflow_runs, "_finish_cancel", crash)
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 503
    assert fake.cancels == [(run_id, True)]
    assert [e["type"] for e in _events(client, run_id, headers)] == ["cancel_requested"]
    crashing[0] = False
    r = client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "failed"


def test_no_lock_is_held_across_the_cascade(api, fake):  # noqa: F811
    """A worker append during the DBOS cancel call (run-row lock) completes at once."""
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = _start(client, headers)
    _, event_log = app.state.run_stores
    finished: list[bool] = []

    def worker_append() -> None:
        thread = threading.Thread(
            target=lambda: finished.append(
                event_log.append(tenant, uuid.UUID(run_id), "start", "run_started", {}).created
            )
        )
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "the worker append blocked on a lock held by the cancel route"

    fake.during_cancel = worker_append
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 202
    assert finished == [True]
