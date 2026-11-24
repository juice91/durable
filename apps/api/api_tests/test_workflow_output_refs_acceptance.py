"""AC.9 acceptance: publish `$ref` checks into agent output schemas and the schema helper route."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from .conftest import get_auth_header

BASE = "/api/v1/workflows"
AGENTS = "/api/v1/agents"
# Closed objects: an unlisted field cannot exist, so a pinned ref to one is a 422 (review fix:
# the check follows additionalProperties; an open object only warns, see the unit tests).
SCHEMA = {
    "type": "object",
    "properties": {
        "priority": {"type": "string"},
        "owner": {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False},
        "tags": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}},
                                            "additionalProperties": False}},
    },
    "additionalProperties": False,
}


def _h(tenant: str = "tenant-ac9a") -> dict[str, str]:
    return get_auth_header(tenant, "alice")


def _spec(schema: dict[str, Any] | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": "Triage", "model": {"provider": "test", "name": "structured"}}
    if schema is not None:
        spec["output"] = {"json_schema": schema}
    return spec


def _publish_agent(client, spec, agent_id: str | None = None, tenant: str = "tenant-ac9a") -> str:
    if agent_id is None:
        resp = client.post(AGENTS, json={"spec": spec}, headers=_h(tenant))
        assert resp.status_code == 201, resp.text
        agent_id = resp.json()["id"]
    else:
        assert client.patch(f"{AGENTS}/{agent_id}", json={"spec": spec}, headers=_h(tenant)).status_code == 200
    assert client.post(f"{AGENTS}/{agent_id}/versions", headers=_h(tenant)).status_code == 201
    return agent_id


def _wf(agent_id: str, refs: list[str], version: int | None = 1) -> dict[str, Any]:
    """Step `a` (the agent), `b` (a tool whose inputs read `refs`) and `c` (a branch on the last ref)."""
    return {
        "name": "Refs",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [
            {"id": "a", "type": "agent", "agent_id": agent_id, "version": version, "input": {"p": "x"}, "next": "b"},
            {"id": "b", "type": "tool", "tool_id": "tickets.create",
             "input": {"title": "t", "body": {"$ref": refs[0]}}, "next": "c_gate"},
            {"id": "c_gate", "type": "branch",
             "cases": [{"when": {"op": "exists", "left": {"$ref": refs[-1]}}, "next": "d"}], "default": "d"},
            {"id": "d", "type": "agent", "agent_id": agent_id, "version": version, "input": {"p": "y"}},
        ],
    }


def _publish(client, spec, tenant: str = "tenant-ac9a"):
    created = client.post(BASE, json={"spec": spec}, headers=_h(tenant))
    assert created.status_code == 201, created.text
    return client.post(f"{BASE}/{created.json()['id']}/versions", headers=_h(tenant))


def test_nested_and_array_refs_publish_clean(client):
    agent_id = _publish_agent(client, _spec(SCHEMA))
    for ref in ("steps.a.output.owner.name", "steps.a.output.tags.2.label", "steps.a.output"):
        resp = _publish(client, _wf(agent_id, [ref]))
        assert resp.status_code == 201, (ref, resp.text)
        assert resp.json()["warnings"] == []


def test_bad_paths_are_422_and_name_the_step(client):
    agent_id = _publish_agent(client, _spec(SCHEMA))
    cases = {
        "steps.a.output.owner.nam": ("step 'b'", "'nam' is not a field of owner"),
        "steps.a.output.tags.label": ("step 'b'", "is an array (use a numeric index)"),
        "steps.a.output.priority.x": ("step 'b'", "'x' does not exist: priority is a string"),
        "steps.a.output.ownr": ("step 'b'", "'ownr' is not a field of the output"),
    }
    for ref, (who, why) in cases.items():
        resp = _publish(client, _wf(agent_id, [ref]))
        assert resp.status_code == 422, (ref, resp.text)
        detail = resp.json()["detail"]
        assert who in detail and f"$ref '{ref}'" in detail and "agent step 'a'" in detail and why in detail, detail


def test_bad_ref_in_a_branch_condition_names_the_branch_and_problems_are_joined(client):
    agent_id = _publish_agent(client, _spec(SCHEMA))
    resp = _publish(client, _wf(agent_id, ["steps.a.output.nope1", "steps.a.output.nope2"]))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "step 'b': $ref 'steps.a.output.nope1'" in detail and "; step 'c_gate': $ref 'steps.a.output.nope2'" in detail


def test_pinned_version_is_checked_against_that_version_not_the_latest(client):
    agent_id = _publish_agent(client, _spec({"type": "object", "properties": {"old": {"type": "string"}}}))
    _publish_agent(client, _spec(SCHEMA), agent_id=agent_id)  # version 2 has no `old`
    ok = _publish(client, _wf(agent_id, ["steps.a.output.old"], version=1))
    assert ok.status_code == 201 and ok.json()["warnings"] == []
    bad = _publish(client, _wf(agent_id, ["steps.a.output.old"], version=2))
    assert bad.status_code == 422
    # Unpinned: checked against the latest (v2) and only warns.
    soft = _publish(client, _wf(agent_id, ["steps.a.output.old"], version=None))
    assert soft.status_code == 201, soft.text
    assert {w["code"] for w in soft.json()["warnings"]} == {"unpinned_agent_output_ref"}


def test_pinned_version_without_output_warns_and_never_blocks(client):
    agent_id = _publish_agent(client, _spec(None))  # v1: text
    _publish_agent(client, _spec(SCHEMA), agent_id=agent_id)  # v2: structured
    resp = _publish(client, _wf(agent_id, ["steps.a.output.priority"], version=1))
    assert resp.status_code == 201, resp.text
    warnings = resp.json()["warnings"]
    # Steps b (tool input) and c (branch condition) both read the field: one warning each.
    assert {w["code"] for w in warnings} == {"text_agent_output_ref"}
    assert [w["path"] for w in warnings] == ["steps[1]", "steps[2]"]
    assert "step 'b'" in warnings[0]["message"] and "step 'c_gate'" in warnings[1]["message"]
    # Unpinned: the latest (v2) matches, but an unpinned field ref always warns (review fix).
    latest = _publish(client, _wf(agent_id, ["steps.a.output.priority"], version=None))
    assert latest.status_code == 201, latest.text
    soft_warnings = latest.json()["warnings"]
    assert [w["code"] for w in soft_warnings] == ["unpinned_agent_output_ref"] * 2
    assert all("matches the latest version's output schema today" in w["message"] for w in soft_warnings)


def test_refs_into_non_agent_steps_are_not_checked(client):
    agent_id = _publish_agent(client, _spec(SCHEMA))
    spec = _wf(agent_id, ["steps.b.output.anything"])  # b is a tool step
    spec["steps"][1]["input"]["body"] = "x"
    spec["steps"][3]["input"] = {"p": {"$ref": "steps.b.output.anything"}}
    resp = _publish(client, spec)
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []


def test_the_agent_of_another_tenant_cannot_be_referenced(client):
    other = _publish_agent(client, _spec(SCHEMA), tenant="tenant-ac9b")
    resp = _publish(client, _wf(other, ["steps.a.output.priority"]))
    assert resp.status_code == 422, resp.text


def test_agent_output_schema_isolation_and_errors(client):
    agent_id = _publish_agent(client, _spec(SCHEMA))
    url = f"{BASE}/agent-output-schema"
    mine = client.get(url, params={"agent_id": agent_id}, headers=_h())
    assert mine.status_code == 200 and mine.json()["agent_id"] == agent_id
    other = client.get(url, params={"agent_id": agent_id}, headers=_h("tenant-ac9b"))
    assert other.status_code == 404 and other.json()["detail"] == "Agent not found"
    assert "json_schema" not in other.text
    pinned = client.get(url, params={"agent_id": agent_id, "version": 1}, headers=_h("tenant-ac9b"))
    assert pinned.status_code == 404
    for params in ({"agent_id": str(uuid.uuid4())}, {"agent_id": agent_id, "version": 2}):
        r = client.get(url, params=params, headers=_h())
        assert r.status_code == 404, r.text
    assert client.get(url, params={"agent_id": agent_id, "version": 2}, headers=_h()).json()["detail"] == "Version not found"
    for params in ({"agent_id": "nope"}, {"agent_id": agent_id, "version": 0}, {}):
        assert client.get(url, params=params, headers=_h()).status_code == 422, params
    assert client.get(url, params={"agent_id": agent_id}).status_code in (401, 403)


def test_agent_output_schema_picks_latest_and_hides_archived_agents(client):
    agent_id = _publish_agent(client, _spec(None))
    _publish_agent(client, _spec(SCHEMA), agent_id=agent_id)
    url = f"{BASE}/agent-output-schema"
    latest = client.get(url, params={"agent_id": agent_id}, headers=_h()).json()
    assert latest["version"] == 2 and latest["structured"] is True and latest["pinned"] is False
    v1 = client.get(url, params={"agent_id": agent_id, "version": 1}, headers=_h()).json()
    assert v1["structured"] is False and v1["json_schema"] is None and v1["fields"] == []
    with client.app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :i"), {"i": agent_id})
    gone = client.get(url, params={"agent_id": agent_id}, headers=_h())
    assert gone.status_code == 404 and gone.json()["detail"] == "Agent not found"
