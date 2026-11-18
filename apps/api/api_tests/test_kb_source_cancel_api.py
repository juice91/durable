"""POST /runs/{parent}/cancel also cancels the knowledge-base source syncs the parent workflow started
(design: Conflicts 8, "Cancellation"): `cancel_requested` on every child sync run whose
`kb_source_syncs.parent_run_id` is the parent, a DBOS cancel of each child's tree, and the guarded terminal
write. A fake DBOS client inside the real launcher (no worker)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore

from .run_helpers import hard_timeout, requires_pg
from .test_workflow_run_cancel_api import CancelClient, _events, _start  # noqa: F401
from .test_workflow_runs_api import api  # noqa: F401 - fixture

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> CancelClient:
    return CancelClient()


@pytest.fixture
def env(api):  # noqa: F811
    client, app, tenant_of = api
    engine = app.state.database_engine
    tenant, headers = tenant_of()
    kb = KnowledgeStore(engine).create_knowledge_base(
        tenant, name="docs", embedding_model="test:hash", dimensions=1536, description="")
    conn = ConnectionStore(engine).create(
        tenant, name="c", kind="oauth2_client_credentials", base_url=None, config={}, credential_refs={})
    source = SourceStore(engine).create(
        tenant, knowledge_base_id=kb["id"], connection_id=conn.id, name="parts", kind="product_catalog",
        provider="fake")
    yield client, app, engine, tenant, headers, source
    with engine.begin() as c:
        c.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
        c.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": tenant})


def child_sync(app, engine, tenant, source, parent, *, status=None) -> str:
    """A sync run of `source` started by `parent` (as kb.source.sync does), optionally already ended."""
    run_store, _ = app.state.run_stores
    rid = uuid.uuid4()
    run_store.create_kb_source_sync_run(tenant, rid, source.id, {"mode": "full", "part_numbers": []})
    SyncStore(engine).create(tenant, str(uuid.uuid4()), source.id, str(rid), "full", "h" * 64,
                             parent_run_id=str(parent) if parent else None)
    run_store.mark_running(tenant, rid)
    if status:
        run_store.finish_with_end(tenant, rid, status, "{}", None, end_data={"report": {}})
    return str(rid)


def test_cancelling_the_parent_cancels_its_child_syncs(env, fake):
    client, app, engine, tenant, headers, source = env
    parent = _start(client, headers)
    running = child_sync(app, engine, tenant, source, parent)
    done = child_sync(app, engine, tenant, source, parent, status="completed")
    unrelated = child_sync(app, engine, tenant, source, None)

    r = client.post(f"/api/v1/runs/{parent}/cancel", headers=headers)
    assert r.status_code == 202, r.text
    assert (parent, True) in fake.cancels and (running, True) in fake.cancels
    assert (unrelated, True) not in fake.cancels and (done, True) not in fake.cancels

    child = client.get(f"/api/v1/runs/{running}", headers=headers).json()
    assert child["status"] == "failed"
    types = [e["type"] for e in _events(client, running, headers)]
    assert types.count("cancel_requested") == 1 and types.count("run_failed") == 1
    [cancel] = [e for e in _events(client, running, headers) if e["type"] == "run_failed"]
    assert cancel["data"]["error_code"] == "run_cancelled"
    # the finished child keeps its end; the unrelated sync is untouched
    assert "cancel_requested" not in [e["type"] for e in _events(client, done, headers)]
    assert client.get(f"/api/v1/runs/{done}", headers=headers).json()["status"] == "completed"
    assert "cancel_requested" not in [e["type"] for e in _events(client, unrelated, headers)]
    assert client.get(f"/api/v1/runs/{unrelated}", headers=headers).json()["status"] == "running"


def test_a_retried_cancel_is_idempotent_for_the_children(env, fake):
    client, app, engine, tenant, headers, source = env
    parent = _start(client, headers)
    child = child_sync(app, engine, tenant, source, parent)
    assert client.post(f"/api/v1/runs/{parent}/cancel", headers=headers).status_code == 202
    assert client.post(f"/api/v1/runs/{parent}/cancel", headers=headers).status_code == 202
    types = [e["type"] for e in _events(client, child, headers)]
    assert types.count("cancel_requested") == 1 and types.count("run_failed") == 1


def test_a_failed_child_cascade_leaves_the_requests_for_the_retry(env, fake):
    client, app, engine, tenant, headers, source = env
    parent = _start(client, headers)
    child = child_sync(app, engine, tenant, source, parent)
    fake.fail_cancel = True
    assert client.post(f"/api/v1/runs/{parent}/cancel", headers=headers).status_code == 503
    fake.fail_cancel = False
    r = client.post(f"/api/v1/runs/{parent}/cancel", headers=headers)
    assert r.status_code == 202
    assert client.get(f"/api/v1/runs/{child}", headers=headers).json()["status"] == "failed"


def test_another_tenants_sync_naming_the_parent_is_not_touched(env, fake):
    client, app, engine, tenant, headers, source = env
    parent = _start(client, headers)
    # a forged row: the sync row of another tenant carries this parent id
    other = f"t{uuid.uuid4().hex[:10]}"
    run_store, _ = app.state.run_stores
    kb = KnowledgeStore(engine).create_knowledge_base(
        other, name="docs", embedding_model="test:hash", dimensions=1536, description="")
    conn = ConnectionStore(engine).create(
        other, name="c", kind="oauth2_client_credentials", base_url=None, config={}, credential_refs={})
    src = SourceStore(engine).create(
        other, knowledge_base_id=kb["id"], connection_id=conn.id, name="parts", kind="product_catalog", provider="fake")
    rid = uuid.uuid4()
    run_store.create_kb_source_sync_run(other, rid, src.id, {"mode": "full", "part_numbers": []})
    SyncStore(engine).create(other, str(uuid.uuid4()), src.id, str(rid), "full", "h" * 64, parent_run_id=parent)
    run_store.mark_running(other, rid)
    try:
        assert client.post(f"/api/v1/runs/{parent}/cancel", headers=headers).status_code == 202
        assert (str(rid), True) not in fake.cancels
        assert run_store.get_run(other, rid)["status"] == "running"
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": other})
            c.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": other})
            c.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": other})
            c.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": other})
