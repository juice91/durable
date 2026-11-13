"""Acceptance tests for POST /tools/{tool_id}/runs (tool test console).

Covers the API-side contract not already tested in test_tool_runs_api.py.
"""

from __future__ import annotations

import uuid

from .run_helpers import requires_pg
from .test_runs import fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures

pytestmark = requires_pg


def _post(client, headers, tool_id: str, body: dict):
    return client.post(f"/api/v1/tools/{tool_id}/runs", json=body, headers=headers)


def test_202_shape_and_visibility(pg, tenant_of, fake_dbos):
    """A successful run returns 202 with correct shape and is visible via GET /runs/{id}, /runs?kind=tool&tool_id=."""
    client, _app = pg
    tenant, headers = tenant_of()
    run_id = str(uuid.uuid4())
    r = _post(
        client, headers, "tickets.create",
        {"input": {"title": "Test", "body": "Test body"}, "run_id": run_id},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["run_id"] == run_id
    assert body["kind"] == "tool"
    assert body["tool_id"] == "tickets.create"
    assert body["status"] == "queued"
    assert "display_status" in body
    assert "trace_id" in body

    # Visible via GET /runs/{id}
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["kind"] == "tool"
    assert run["tool_id"] == "tickets.create"
    assert run["agent_id"] is None
    assert run["version"] is None
    assert run["status"] == "queued"

    # Visible via GET /runs?kind=tool&tool_id=
    listed = client.get(f"/api/v1/runs?kind=tool&tool_id=tickets.create", headers=headers).json()["items"]
    assert len(listed) == 1
    assert listed[0]["run_id"] == run_id
    # agent runs are not included
    assert client.get("/api/v1/runs?kind=agent", headers=headers).json()["items"] == []
    # The run is visible through its events (none yet: the worker has not started).
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=headers)
    assert events.status_code == 200
    assert events.json() == []


def test_404_for_unknown_tool(pg, tenant_of):
    """Unknown tool returns 404."""
    client, _app = pg
    _, headers = tenant_of()
    r = _post(client, headers, "no.such.tool", {"input": {}})
    assert r.status_code == 404
    assert r.json()["detail"] == "Tool not found"


def test_422_disallowed_tool(pg, tenant_of, monkeypatch):
    """Disallowed tool (not in EAP_ALLOWED_TOOLS) returns 422 with clear message."""
    client, _app = pg
    _, headers = tenant_of()
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    r = _post(client, headers, "tickets.create", {"input": {"title": "x", "body": "y"}})
    assert r.status_code == 422
    assert "tickets.create' is not allowed on this platform" in r.json()["detail"]


def test_422_invalid_input_no_values_exposed(pg, tenant_of):
    """Invalid input returns 422 with FastAPI-style errors, without exposing the rejected input."""
    client, _app = pg
    _, headers = tenant_of()
    r = _post(client, headers, "tickets.create", {"input": {"title": "only title"}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    # Errors have loc, msg, type but not the input values
    assert all("loc" in e and "msg" in e and "type" in e for e in detail)
    # No input values in the error detail
    text = str(detail)
    assert "only title" not in text
    # Missing body field
    assert any("body" in str(e["loc"]) for e in detail)


def test_422_input_over_32_kib(pg, tenant_of):
    """Input over 32 KiB as canonical JSON returns 422."""
    client, _app = pg
    _, headers = tenant_of()
    large_body = {"title": "x", "body": "y" * (33 * 1024)}  # > 32 KiB
    r = _post(client, headers, "tickets.create", {"input": large_body})
    assert r.status_code == 422
    assert "at most 32 KiB" in r.json()["detail"]


def test_422_extra_body_fields(pg, tenant_of):
    """Extra body fields (beyond input and run_id) return 422."""
    client, _app = pg
    _, headers = tenant_of()
    r = _post(client, headers, "tickets.create", {"input": {"title": "a", "body": "b"}, "run_id": str(uuid.uuid4()), "extra": 1})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert all(set(e) == {"loc", "msg", "type"} for e in detail)
    assert any(e["type"] == "extra_forbidden" for e in detail)


def test_409_run_id_reuse_different_input(pg, tenant_of):
    """Same run_id with different input returns 409."""
    client, _app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())
    # First request succeeds
    r1 = _post(client, headers, "tickets.create", {"input": {"title": "a", "body": "b"}, "run_id": run_id})
    assert r1.status_code == 202
    # Same run_id, different input -> 409
    r2 = _post(client, headers, "tickets.create", {"input": {"title": "c", "body": "d"}, "run_id": run_id})
    assert r2.status_code == 409
    assert "already used by a different run request" in r2.json()["detail"]


def test_409_another_tenants_run_id(pg, tenant_of):
    """Another tenant's run_id returns 409."""
    client, _app = pg
    _, headers = tenant_of()
    # First tenant creates a run
    run_id = str(uuid.uuid4())
    r1 = _post(client, headers, "tickets.create", {"input": {"title": "x", "body": "y"}, "run_id": run_id})
    assert r1.status_code == 202
    # Second tenant tries to reuse the same run_id -> 409
    _, other_headers = tenant_of()
    r2 = _post(client, other_headers, "tickets.create", {"input": {"title": "z", "body": "w"}, "run_id": run_id})
    assert r2.status_code == 409
    assert "already in use" in r2.json()["detail"]


def test_202_idempotent_same_run_id_and_input(pg, tenant_of):
    """Same run_id and input again returns 202 (idempotent)."""
    client, _app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())
    body = {"input": {"title": "a", "body": "b"}, "run_id": run_id}
    # First request
    r1 = _post(client, headers, "tickets.create", body)
    assert r1.status_code == 202
    # Same request again -> 202, not 409
    r2 = _post(client, headers, "tickets.create", body)
    assert r2.status_code == 202


def test_tenant_isolation(pg, tenant_of):
    """Another tenant gets 404 on a run and its events."""
    client, _app = pg
    tenant1, headers1 = tenant_of()
    _, headers2 = tenant_of()
    run_id = str(uuid.uuid4())
    # Tenant 1 creates a run
    r = _post(client, headers1, "tickets.create", {"input": {"title": "x", "body": "y"}, "run_id": run_id})
    assert r.status_code == 202
    # Tenant 2 cannot see it
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers2).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}/events", headers=headers2).status_code == 404
    assert client.get(f"/api/v1/runs?kind=tool", headers=headers2).json()["items"] == []


def test_agent_run_kind_is_agent(pg, tenant_of, fake_dbos):
    """Agent runs still have kind 'agent' and their agent fields populated."""
    from .test_runs import make_agent, start

    client, _app = pg
    _, headers = tenant_of()
    agent_id = make_agent(client, headers)
    run_id = str(uuid.uuid4())
    created = start(client, headers, agent_id, run_id)
    assert created.status_code == 201, created.text
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["kind"] == "agent"
    assert run["agent_id"] == agent_id
    assert run["version"] == 1
    assert run["tool_id"] is None


def test_failed_enqueue_retry_clears_stale_error(pg, tenant_of, fake_dbos):
    """A failed enqueue is 503 and the run is failed with ENQUEUE_FAILED_ERROR.

    The same run_id and input again is 202 queued, and the stored error is cleared.
    """
    from eap_run_store import ENQUEUE_FAILED_ERROR

    client, _app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())
    body = {"input": {"title": "a", "body": "b"}, "run_id": run_id}
    fake_dbos.fail_times = 1
    failed = _post(client, headers, "tickets.create", body)
    assert failed.status_code == 503, failed.text
    stored = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (stored["status"], stored["error"]) == ("failed", ENQUEUE_FAILED_ERROR)

    retry = _post(client, headers, "tickets.create", body)
    assert retry.status_code == 202, retry.text
    assert retry.json()["status"] == "queued"
    healed = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (healed["status"], healed["error"]) == ("queued", None)


def test_202_never_reports_status_failed(pg, tenant_of, fake_dbos):
    """A successful enqueue's 202 says queued even when a concurrent owner marked the row failed."""
    client, app = pg
    tenant, headers = tenant_of()
    run_id = str(uuid.uuid4())
    run_store = app.state.run_stores[0]
    original = fake_dbos.enqueue

    def enqueue_while_owner_fails(options, *args):
        original(options, *args)
        assert run_store.mark_enqueue_failed(tenant, uuid.UUID(run_id))

    fake_dbos.enqueue = enqueue_while_owner_fails
    response = _post(client, headers, "clock.now", {"input": {}, "run_id": run_id})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert body["display_status"] == "queued"
