"""Review blocker: archiving an agent that published workflows run as a step (eap_api.agent_archive).

409 with the referencing workflows unless force=true; POST /workflows/{id}/runs then refuses a run
whose agent step's agent is archived, before anything is recorded or enqueued."""

from __future__ import annotations

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake, publish_demo  # noqa: F401 - fixtures

pytestmark = requires_pg


def _agent_of(client, h, workflow_id: str) -> str:
    versions = client.get(f"/api/v1/workflows/{workflow_id}/versions", headers=h).json()
    items = versions["items"] if isinstance(versions, dict) else versions
    return next(s["agent_id"] for s in items[0]["spec"]["steps"] if s["type"] == "agent")


def test_archive_lists_referencing_workflows_and_force_archives(api, fake):
    client, _app, tenant_of = api
    _, h = tenant_of()
    workflow_id = publish_demo(client, h)
    agent_id = _agent_of(client, h, workflow_id)

    r = client.post(f"/api/v1/agents/{agent_id}/archive", headers=h)
    assert r.status_code == 409, r.text
    body = r.json()
    assert "force=true" in body["detail"] and "Triage" in body["detail"]
    [ref] = body["workflows"]
    assert ref["workflow_id"] == workflow_id and ref["workflow_name"] == "Triage"
    assert (ref["versions"], ref["latest_version"]) == ([1], 1)
    # Not archived by the refused call.
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["archived"] is False

    r = client.post(f"/api/v1/agents/{agent_id}/archive", params={"force": "true"}, headers=h)
    assert r.status_code == 200 and r.json()["archived"] is True
    # Idempotent once archived, with or without force.
    assert client.post(f"/api/v1/agents/{agent_id}/archive", headers=h).status_code == 200


def test_workflow_run_with_an_archived_agent_step_is_409_before_anything_runs(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    workflow_id = publish_demo(client, h)
    agent_id = _agent_of(client, h, workflow_id)
    assert client.post(f"/api/v1/agents/{agent_id}/archive", params={"force": "true"}, headers=h).status_code == 200

    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "step 'ask': agent 'Helper' is archived; unarchive it to run this workflow"
    assert fake.calls == []  # nothing enqueued
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0

    # Unarchived: the run is accepted again.
    assert client.post(f"/api/v1/agents/{agent_id}/unarchive", headers=h).status_code == 200
    assert client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=h).status_code == 202


def test_unreferenced_agent_and_archived_workflow_do_not_block(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    workflow_id = publish_demo(client, h)
    agent_id = _agent_of(client, h, workflow_id)
    other = client.post("/api/v1/agents", json={"spec": {"name": "Solo", "model": {"provider": "test", "name": "echo"}}}, headers=h)
    assert client.post(f"/api/v1/agents/{other.json()['id']}/archive", headers=h).status_code == 200
    # Another tenant cannot archive it (404), and its workflows never count.
    _, h2 = tenant_of()
    assert client.post(f"/api/v1/agents/{agent_id}/archive", headers=h2).status_code == 404
    # An archived workflow cannot run, so it does not block archiving its agent.
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE workflows SET archived = true WHERE id = :w AND tenant_id = :t"), {"w": workflow_id, "t": tenant})
    assert client.post(f"/api/v1/agents/{agent_id}/archive", headers=h).status_code == 200


def test_retry_of_a_recorded_run_after_a_forced_archive_is_not_refused(api, fake):
    """Re-review 1: the up-front checks apply to new runs only; a retry with the same run_id gets
    the normal retry handling (202, re-enqueued) and the worker reports the step as agent_archived."""
    import uuid

    client, _app, tenant_of = api
    _, h = tenant_of()
    workflow_id = publish_demo(client, h)
    agent_id = _agent_of(client, h, workflow_id)
    run_id = str(uuid.uuid4())
    body = {"run_id": run_id, "input": {"q": "hi"}}
    assert client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=h).status_code == 202
    assert client.post(f"/api/v1/agents/{agent_id}/archive", params={"force": "true"}, headers=h).status_code == 200

    retry = client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=h)
    assert retry.status_code == 202, retry.text
    assert retry.json()["run_id"] == run_id
    assert len(fake.calls) == 2  # enqueued again (DBOS attaches it to the existing workflow)
    # A new run_id is still refused up front.
    fresh = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=h)
    assert fresh.status_code == 409


def test_publish_waits_for_an_archive_in_progress_and_then_refuses(api, fake):
    """Re-review 2: publish locks the referenced agents FOR SHARE before its archived check."""
    import threading

    client, app, tenant_of = api
    _, h = tenant_of()
    agent = client.post("/api/v1/agents", json={"spec": {"name": "Solo", "model": {"provider": "test", "name": "echo"}}}, headers=h)
    agent_id = agent.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    spec = {"name": "One", "input_schema": {"type": "object"}, "start": "a",
            "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "input": {}}]}
    workflow_id = client.post("/api/v1/workflows", json={"spec": spec}, headers=h).json()["id"]

    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("r", client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=h))
    )
    with app.state.database_engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text("SELECT 1 FROM agents WHERE id = :a FOR NO KEY UPDATE"), {"a": agent_id})
        thread.start()
        thread.join(0.7)
        assert thread.is_alive()  # waiting for the archive's lock
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :a"), {"a": agent_id})
        tx.commit()
    thread.join(10)
    assert result["r"].status_code == 422 and "was not found" in result["r"].json()["detail"]


def test_another_tenants_workflows_never_block_an_archive(api, fake):
    """Re-review 3: a same-named agent's workflow in another tenant, and even another tenant's
    workflow version naming this agent's id, never block (or appear in) an archive."""
    client, app, tenant_of = api
    _, h = tenant_of()
    other_tenant, h2 = tenant_of()
    mine = client.post("/api/v1/agents", json={"spec": {"name": "Helper", "model": {"provider": "test", "name": "echo"}}}, headers=h)
    agent_id = mine.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    # The other tenant's workflow runs ITS "Helper" (same name)...
    other_workflow = publish_demo(client, h2)
    other_agent = _agent_of(client, h2, other_workflow)
    assert other_agent != agent_id
    # ...and, written directly, a version that names this tenant's agent id.
    with app.state.database_engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_versions SET spec = CAST(replace(CAST(spec AS text), :old, :new) AS jsonb)"
                 " WHERE tenant_id = :t"),
            {"old": other_agent, "new": agent_id, "t": other_tenant},
        )
    r = client.post(f"/api/v1/agents/{agent_id}/archive", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is True
