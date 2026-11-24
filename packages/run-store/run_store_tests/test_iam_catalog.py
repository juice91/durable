"""The resource catalog a policy plug-in's reconcile reads (eap_run_store.iam_catalog): every kind, tenant-scoped, the
creator of an agent or workflow from its first version, archived rows included, nothing written."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import ScheduleStore
from eap_run_store.api_keys import ApiKeyStore
from eap_run_store.iam_catalog import RunStoreCatalog

T0 = datetime(1970, 1, 1, tzinfo=timezone.utc)
TABLES = ("workflow_schedules", "mcp_servers", "knowledge_bases", "agent_versions", "workflow_versions", "agents", "workflows", "tenant_secrets", "api_keys")


@pytest.fixture
def tenants(engine):
    a, b = f"cat_a_{uuid4().hex[:8]}", f"cat_b_{uuid4().hex[:8]}"
    yield a, b
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b})


def _seed(engine, tenant: str, creator: str) -> dict[str, str]:
    ids = {k: str(uuid4()) for k in ("agent", "agent_draft", "agent_archived", "workflow", "kb", "mcp")}
    with engine.begin() as conn:
        for key, archived in (("agent", False), ("agent_draft", False), ("agent_archived", True)):
            conn.execute(
                text("INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, created_at, updated_at)"
                     " VALUES (:i, :t, :n, NULL, :a, false, now(), now())"),
                {"i": ids[key], "t": tenant, "n": key, "a": archived},
            )
        for version, by in ((2, "someone-later@example.com"), (1, creator)):  # the first version's author is the creator
            conn.execute(
                text("INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by)"
                     " VALUES (:id, :a, :t, :v, CAST(:s AS jsonb), 'h', now(), :by)"),
                {"id": str(uuid4()), "a": ids["agent"], "t": tenant, "v": version, "s": json.dumps({}), "by": by},
            )
        conn.execute(text("INSERT INTO workflows (id, tenant_id, name, draft_spec, created_at, updated_at)"
                          " VALUES (:i, :t, 'w', NULL, now(), now())"), {"i": ids["workflow"], "t": tenant})
        conn.execute(
            text("INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at)"
                 " VALUES (:i, :t, 'srv', 'http://127.0.0.1:1/mcp', 'streamable_http', true, now(), now())"),
            {"i": ids["mcp"], "t": tenant},
        )
        conn.execute(
            text("INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking, created_at, updated_at)"
                 " VALUES (:i, :t, 'kb', 'm', 1024, CAST('{}' AS jsonb), now(), now())"),
            {"i": ids["kb"], "t": tenant},
        )
    ids["schedule"] = ScheduleStore(engine).create(tenant, ids["workflow"], name="n", cron="* * * * *", timezone="UTC", input={}, next_fire_at=T0)["id"]
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO tenant_secrets (tenant_id, name, ciphertext, key_id, created_by) VALUES (:t, 'crm_token', :c, 'k', :by)"),
            {"t": tenant, "c": b"x", "by": creator},
        )
    ids["api_key"] = ApiKeyStore(engine).create(tenant, name="k1", created_by=creator).record.id
    return ids


def test_every_kind_is_listed_with_its_tenant_and_creator(engine, tenants):
    a, _ = tenants
    ids = _seed(engine, a, "boss@example.com")
    rows = {(r.kind, r.id): r for r in RunStoreCatalog(engine).entries(a)}
    expected = {
        ("agent", ids["agent"]): "boss@example.com",
        ("agent", ids["agent_draft"]): None,  # never published: no creator recorded
        ("agent", ids["agent_archived"]): None,  # archived rows stay in the catalog: their tuples stay
        ("workflow", ids["workflow"]): None,
        ("knowledge_base", ids["kb"]): None,
        ("mcp_server", ids["mcp"]): None,
        ("schedule", ids["schedule"]): None,
        ("secret", "crm_token"): "boss@example.com",
        ("api_key", ids["api_key"]): "boss@example.com",
    }
    assert {k: v.creator for k, v in rows.items()} == expected
    assert {r.tenant_id for r in rows.values()} == {a}


def test_the_tenant_filter_and_the_unfiltered_read(engine, tenants):
    a, b = tenants
    _seed(engine, a, "a@example.com")
    _seed(engine, b, "b@example.com")
    catalog = RunStoreCatalog(engine)
    assert {r.tenant_id for r in catalog.entries(a)} == {a} and len(catalog.entries(a)) == 9
    assert {a, b} <= {r.tenant_id for r in catalog.entries()}
    assert catalog.entries(f"no_such_{uuid4().hex}") == []


def test_reading_writes_nothing(engine, tenants):
    a, _ = tenants
    _seed(engine, a, "a@example.com")
    with engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM agents WHERE tenant_id = :t"), {"t": a}).scalar()
    RunStoreCatalog(engine).entries(a)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agents WHERE tenant_id = :t"), {"t": a}).scalar() == before
