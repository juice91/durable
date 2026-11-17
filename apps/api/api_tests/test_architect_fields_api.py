"""Architect fields (migration 0025): `builtin` on agents, `created_via` on versions, the archive
refusal for builtin agents and the `architect.*` tool-id guard."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .conftest import create_agent, get_auth_header
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures

SPEC = {"name": "a", "model": {"provider": "openai", "name": "gpt-4"}}


def _sql(client, statement: str, **params):
    from .run_helpers import PG_URL
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    try:
        with engine.begin() as conn:
            return conn.execute(text(statement), params)
    finally:
        engine.dispose()


def test_agent_json_carries_builtin_false_by_default(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    h = get_auth_header(tenant, "alice")
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["builtin"] is False
    assert [a["builtin"] for a in client.get("/api/v1/agents", headers=h).json()] == [False]


def test_archive_of_a_builtin_agent_is_409_builtin_agent(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    h = get_auth_header(tenant, "alice")
    _sql(client, "UPDATE agents SET builtin = true WHERE id = :i", i=agent_id)
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["builtin"] is True
    for params in ({}, {"force": "true"}):
        r = client.post(f"/api/v1/agents/{agent_id}/archive", params=params, headers=h)
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "builtin_agent"
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["archived"] is False


@pytest.mark.parametrize("tools", [["architect.save_agent"], ["calculator", "architect.publish_agent"]])
def test_architect_tools_are_refused_on_non_builtin_agents(client, tenant, tools):
    h = get_auth_header(tenant, "alice")
    r = client.post("/api/v1/agents", json={"spec": {**SPEC, "tools": tools}}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "tool_not_allowed"
    agent_id = create_agent(client, tenant, "alice")
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**SPEC, "tools": tools}}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "tool_not_allowed"
    # The same tools are accepted on a builtin row (a gated one with its approvals entry).
    _sql(client, "UPDATE agents SET builtin = true WHERE id = :i", i=agent_id)
    gated = [t for t in tools if t == "architect.publish_agent"]
    spec = {**SPEC, "tools": tools, **({"approvals": {"tools": gated}} if gated else {})}
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=h)
    assert r.status_code == 200, r.text


def test_gated_architect_tools_need_their_approval_entry_even_on_builtin_agents(client, tenant):
    """Nothing publishes, restores, archives or schedules without a recorded approval: a builtin spec that
    lists a gated tool without `approvals.tools` is refused (the worker refuses such a run too)."""
    from eap_schemas.architect import ARCHITECT_GATED_TOOL_IDS

    h = get_auth_header(tenant, "alice")
    agent_id = create_agent(client, tenant, "alice")
    _sql(client, "UPDATE agents SET builtin = true WHERE id = :i", i=agent_id)
    tools = ["architect.save_agent", *ARCHITECT_GATED_TOOL_IDS]
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**SPEC, "tools": tools}}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "approval_required" and r.json()["tools"] == list(ARCHITECT_GATED_TOOL_IDS)
    partial = {**SPEC, "tools": tools, "approvals": {"tools": list(ARCHITECT_GATED_TOOL_IDS[:-1])}}
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": partial}, headers=h)
    assert r.status_code == 422 and r.json()["tools"] == [ARCHITECT_GATED_TOOL_IDS[-1]]
    full = {**SPEC, "tools": tools, "approvals": {"tools": list(ARCHITECT_GATED_TOOL_IDS)}}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": full}, headers=h).status_code == 200


def test_version_json_carries_created_via(client, tenant):
    h = get_auth_header(tenant, "alice")
    agent_id = create_agent(client, tenant, "alice")
    r = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["created_via"] is None
    _sql(client, "UPDATE agent_versions SET created_via = 'architect-page' WHERE agent_id = :i", i=agent_id)
    [v] = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    assert v["created_via"] == "architect-page"
    got = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=h).json()
    assert got["created_via"] == "architect-page"


def test_workflow_version_json_carries_created_via(api, fake):
    from .test_workflow_runs_api import publish_demo

    client, _app, tenant_of = api
    _, h = tenant_of()
    workflow_id = publish_demo(client, h)
    versions = client.get(f"/api/v1/workflows/{workflow_id}/versions", headers=h).json()
    items = versions["items"] if isinstance(versions, dict) else versions
    assert items[0]["created_via"] is None
    _sql(client, "UPDATE workflow_versions SET created_via = 'architect:run-1' WHERE workflow_id = :i", i=workflow_id)
    versions = client.get(f"/api/v1/workflows/{workflow_id}/versions", headers=h).json()
    items = versions["items"] if isinstance(versions, dict) else versions
    assert items[0]["created_via"] == "architect:run-1"
