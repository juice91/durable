"""The migration report and apply (`scripts/iam/backfill_agent_grants.py`, eap_api.services.agent_grants_backfill): per agent and
per workflow, what its published versions use today; the three news workflows (http.request with `secret:` refs and an agent
step) must be in it; apply writes the grants once and a second run is a no-op."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from eap_run_store import AuditStore
from eap_secrets import KEY_ENV, generate_key
from sqlalchemy import text

from eap_api.services.agent_grants_backfill import apply_report, build_report, render_table
from eap_api.services.authz import AuthzService

from .iam_model_engine import AllowAll
from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401
from .test_workflow_runs_api import AGENT_SPEC

SECRET = "fake-not-a-real-credential-0001"


@pytest.fixture
def world(_wave1_world):  # noqa: F811
    yield _wave1_world
    with _wave1_world.app.state.database_engine.begin() as conn:
        for table in ("tenant_secrets", "mcp_server_secrets", "mcp_servers"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": _wave1_world.tenant})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())


def _news_workflow(name: str, agent_id: str, secrets: tuple[str, ...]) -> dict:
    """Shaped like the news workflows: an agent step that picks, then an http.request that posts with `secret:` refs."""
    return {
        "name": name, "input_schema": {"type": "object"}, "start": "pick",
        "steps": [
            {"id": "pick", "type": "agent", "agent_id": agent_id, "input": {"p": "go"}, "next": "post_feed"},
            {"id": "post_feed", "type": "tool", "tool_id": "http.request", "input": {
                "method": "POST", "url": "https://bsky.example.com/xrpc/com.atproto.repo.createRecord",
                "headers": {"Authorization": f"Bearer secret:{secrets[0]}"},
                "json": {"password": f"secret:{secrets[1]}" if len(secrets) > 1 else "x"},
            }},
        ],
    }


def test_the_report_lists_what_published_versions_use_the_news_workflows_included_and_apply_is_idempotent(world: World):
    c, ann = world.client, world.h("ann")
    world.use(AllowAll())  # the legacy world: publishing writes no grants
    for name in ("bsky_main", "bsky_second", "bsky_third", "bsky_pwd"):
        assert c.put(f"{API}/secrets/{name}", json={"value": SECRET}, headers=ann).status_code == 200
    assert c.put(f"{API}/secrets/never_used", json={"value": SECRET}, headers=ann).status_code == 200
    assert c.post(f"{API}/mcp-servers", json={"name": "tickets", "url": "https://example.com/mcp"}, headers=ann).status_code in (200, 201)
    assert c.post(f"{API}/mcp-servers", json={"name": "idle_server", "url": "https://example.com/idle"}, headers=ann).status_code in (200, 201)

    picker = c.post(f"{API}/agents", json={"spec": {**AGENT_SPEC, "name": "Picker"}}, headers=ann).json()["id"]
    assert c.post(f"{API}/agents/{picker}/versions", headers=ann).status_code == 201
    desk_spec = {**AGENT_SPEC, "name": "Desk", "mcp_tools": [{"server": "tickets", "tool": "list"}]}
    desk = c.post(f"{API}/agents", json={"spec": desk_spec}, headers=ann).json()["id"]
    assert c.post(f"{API}/agents/{desk}/versions", headers=ann).status_code == 201
    # The draft of Desk adds a server the published versions do not use: reported as draft-only, never granted.
    c.patch(f"{API}/agents/{desk}", json={"spec": {**desk_spec, "mcp_tools": [{"server": "tickets", "tool": "list"}, {"server": "idle_server", "tool": "x"}]}}, headers=ann)

    workflows = {}
    for name, secrets in (("News One", ("bsky_main", "bsky_pwd")), ("News Two", ("bsky_second",)), ("News Three", ("bsky_third", "ghost_secret"))):
        r = c.post(f"{API}/workflows", json={"spec": _news_workflow(name, picker, secrets)}, headers=ann)
        assert r.status_code == 201, r.text
        workflows[name] = r.json()["id"]
        assert c.post(f"{API}/workflows/{workflows[name]}/versions", headers=ann).status_code == 201

    world.use(world.engine)
    authz = AuthzService(world.engine)
    db = world.app.state.database_engine
    report = asyncio.run(build_report(db, authz, world.tenant))
    by_name = {(i["kind"], i["name"]): i for i in report["items"]}

    # The three news workflows are in the report, each with the secrets its http.request names, and their agent step's agent.
    for name, expected in (("News One", {"bsky_main", "bsky_pwd"}), ("News Two", {"bsky_second"}), ("News Three", {"bsky_third"})):
        item = by_name[("workflow", name)]
        assert {g["name"] for g in item["grants"] if g["kind"] == "secret"} == expected, name
        assert all(g["status"] == "to_grant" and g["versions"] == [1] and g["source"] == "published" for g in item["grants"])
        assert [a["name"] for a in item["agents"]] == ["Picker"]
    assert [d["name"] for d in by_name[("workflow", "News Three")]["dangling"]] == ["ghost_secret"]  # reported, not granted
    desk_item = by_name[("agent", "Desk")]
    assert [(g["kind"], g["name"]) for g in desk_item["grants"]] == [("mcp_server", "tickets")]
    assert [(d["kind"], d["name"]) for d in desk_item["draft_only"]] == [("mcp_server", "idle_server")]
    assert by_name[("agent", "Picker")]["grants"] == [] and "agent Picker" in report["summary"]["nothing_to_grant"]
    assert report["unused"]["secrets"] == ["never_used"] and report["supported"] is True

    table = render_table(report)
    assert "dry run, nothing written" in table and "News One" in table and "secret     bsky_main" in table
    assert "DANGLING" in table and "ghost_secret" in table and "only in the latest draft" in table
    json.dumps(report)  # plain data: the JSON output

    # A dry run wrote nothing.
    assert not [t for t in world.server.tuples if t[1] == "granted_to"]

    applied = asyncio.run(apply_report(db, authz, world.tenant, report, "op1", AuditStore(db)))
    assert applied["verified"] is True and applied["written"] == 5  # 4 secrets of the workflows + the Desk's server
    granted = {(t[0].split("/")[0].split(":")[0], t[2].split(":")[0]) for t in world.server.tuples if t[1] == "granted_to"}
    assert granted == {("workflow", "secret"), ("agent", "mcp_server")}
    assert not [t for t in world.server.tuples if t[1] == "granted_to" and t[2].endswith("/ghost_secret")]
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_subject, detail FROM audit_log WHERE tenant_id = :t AND action = 'grant_agent_use'"), {"t": world.tenant}
        ).mappings().all()
    assert len(rows) == 5 and {r["actor_subject"] for r in rows} == {"backfill:op1"} and all(r["detail"]["reason"] == "backfill" for r in rows)

    again = asyncio.run(build_report(db, authz, world.tenant))
    assert again["summary"]["to_grant"] == 0 and again["summary"]["already_granted"] == 5
    second = asyncio.run(apply_report(db, authz, world.tenant, again, "op1", AuditStore(db)))
    assert second["written"] == 0 and second["verified"] is True  # idempotent


def test_a_dry_run_works_before_the_engine_keeps_grants_and_apply_refuses(world: World):
    world.use(AllowAll())
    authz = AuthzService(AllowAll())
    db = world.app.state.database_engine
    report = asyncio.run(build_report(db, authz, world.tenant))
    assert report["supported"] is False and "keeps no grants" in render_table(report)
    with pytest.raises(RuntimeError, match="keeps no agent grants"):
        asyncio.run(apply_report(db, authz, world.tenant, report, "op1"))
