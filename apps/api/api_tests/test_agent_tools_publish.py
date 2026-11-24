"""Agents as tools: the publish-time checks (unknown / foreign / archived target, missing version, name
conflict, cycle) with stable error codes, and the spec round trip (the field is carried as sent)."""

from __future__ import annotations

import pytest

from eap_schemas import AgentSpec, canonical_spec_hash

from .conftest import get_auth_header


def _h(tenant: str) -> dict[str, str]:
    return get_auth_header(tenant, "alice")


def _spec(name: str, *refs: dict, tools: list[str] | None = None) -> dict:
    spec = {"name": name, "instructions": f"I am {name}.", "model": {"provider": "test", "name": "echo"}}
    if refs:
        spec["agent_tools"] = list(refs)
    if tools:
        spec["tools"] = tools
    return spec


def _agent(client, tenant: str, spec: dict) -> str:
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=_h(tenant))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client, tenant: str, agent_id: str):
    return client.post(f"/api/v1/agents/{agent_id}/versions", headers=_h(tenant))


def _published(client, tenant: str, name: str, *refs: dict) -> str:
    agent_id = _agent(client, tenant, _spec(name, *refs))
    assert _publish(client, tenant, agent_id).status_code == 201
    return agent_id


def _set_draft(client, tenant: str, agent_id: str, spec: dict):
    return client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=_h(tenant))


def test_a_spec_with_agent_tools_publishes_and_round_trips_with_defaults_left_out(client, tenant):
    helper = _published(client, tenant, "Helper")
    spec = _spec("Boss", {"agent_id": helper})
    boss = _agent(client, tenant, spec)
    r = _publish(client, tenant, boss)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["spec"]["agent_tools"] == [{"agent_id": helper}]  # every default is left out
    assert body["spec_hash"] == canonical_spec_hash(AgentSpec.model_validate(spec))
    pinned = _spec("Boss", {"agent_id": helper, "version": 1, "timeout_seconds": 60, "max_calls_per_run": 2,
                            "name": "ask_helper", "description": "Ask it."})
    assert _set_draft(client, tenant, boss, pinned).status_code == 200
    stored = _publish(client, tenant, boss).json()["spec"]["agent_tools"]
    assert stored == [{"agent_id": helper, "version": 1, "timeout_seconds": 60, "max_calls_per_run": 2,
                       "name": "ask_helper", "description": "Ask it."}]


def test_a_spec_without_agent_tools_keeps_its_shape(client, tenant):
    body = _publish(client, tenant, _agent(client, tenant, _spec("Plain"))).json()
    assert "agent_tools" not in body["spec"]


@pytest.mark.parametrize(
    "ref",
    [
        {"agent_id": "not-a-uuid"},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "name": "Bad Name"},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "timeout_seconds": 0},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "timeout_seconds": 3601},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "max_calls_per_run": 21},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "version": "oldest"},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "version": 0},
        {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab", "extra": 1},
    ],
)
def test_a_malformed_ref_is_a_422_when_the_draft_is_saved(client, tenant, ref):
    r = client.post("/api/v1/agents", json={"spec": _spec("Boss", ref)}, headers=_h(tenant))
    assert r.status_code == 422, r.text


def test_an_unknown_agent_is_refused_at_publish(client, tenant):
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab"}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_unknown_agent", r.text


def test_another_tenants_agent_is_refused_like_an_unknown_one(client, tenant):
    foreign = _published(client, f"{tenant}x", "Foreign")
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": foreign}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_unknown_agent", r.text


def test_an_archived_agent_is_refused(client, tenant):
    helper = _published(client, tenant, "Helper")
    assert client.post(f"/api/v1/agents/{helper}/archive", headers=_h(tenant)).status_code == 200
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": helper}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_agent_archived", r.text


def test_an_agent_with_no_published_version_is_refused(client, tenant):
    draft_only = _agent(client, tenant, _spec("Draft only"))
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": draft_only}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_not_published", r.text


def test_a_pinned_version_that_does_not_exist_is_refused(client, tenant):
    helper = _published(client, tenant, "Helper")
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": helper, "version": 7}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_version_not_found", r.text


def test_two_refs_that_share_a_derived_name_are_refused(client, tenant):
    a = _published(client, tenant, "Research Helper")
    b = _published(client, tenant, "research helper")  # the same derived name
    boss = _agent(client, tenant, _spec("Boss", {"agent_id": a}, {"agent_id": b}))
    r = _publish(client, tenant, boss)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_name_conflict", r.text
    ok = _set_draft(client, tenant, boss, _spec("Boss", {"agent_id": a}, {"agent_id": b, "name": "other"}))
    assert ok.status_code == 200
    assert _publish(client, tenant, boss).status_code == 201


def test_a_derived_name_that_equals_a_builtin_tool_is_refused(client, tenant):
    helper = _published(client, tenant, "Tickets  Create")  # derived name tickets_create
    spec = _spec("Boss", {"agent_id": helper}, tools=["tickets.create"])
    boss = _agent(client, tenant, spec)  # explicit names are checked on save; a derived one at publish
    # tickets.create is `tickets__create` for the model, so `tickets_create` does not collide.
    assert _publish(client, tenant, boss).status_code == 201
    other = _published(client, tenant, "Tickets  Create ")
    del other
    reserved = _published(client, tenant, "Retrieve")  # derived name `retrieve`: a reserved platform tool
    boss2 = _agent(client, tenant, _spec("Boss two", {"agent_id": reserved}))
    r = _publish(client, tenant, boss2)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_name_conflict", r.text


def test_an_explicit_name_equal_to_a_builtin_tool_is_a_422_on_save(client, tenant):
    helper = _published(client, tenant, "Helper")
    spec = _spec("Boss", {"agent_id": helper, "name": "tickets__create"}, tools=["tickets.create"])
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=_h(tenant))
    assert r.status_code == 422, r.text


def test_a_direct_cycle_is_refused(client, tenant):
    a = _published(client, tenant, "A")
    b = _published(client, tenant, "B", {"agent_id": a})  # B -> A, published
    assert _set_draft(client, tenant, a, _spec("A", {"agent_id": b})).status_code == 200  # A -> B closes the loop
    r = _publish(client, tenant, a)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_cycle", r.text
    assert "A" in r.json()["detail"] and "B" in r.json()["detail"]


def test_a_longer_cycle_and_a_self_reference_are_refused(client, tenant):
    a = _published(client, tenant, "A")
    b = _published(client, tenant, "B", {"agent_id": a})
    c = _published(client, tenant, "C", {"agent_id": b})
    assert _set_draft(client, tenant, a, _spec("A", {"agent_id": c})).status_code == 200
    r = _publish(client, tenant, a)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_cycle", r.text
    assert _set_draft(client, tenant, a, _spec("A", {"agent_id": a})).status_code == 200
    r = _publish(client, tenant, a)
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_cycle", r.text


def test_a_diamond_is_not_a_cycle(client, tenant):
    leaf = _published(client, tenant, "Leaf")
    left = _published(client, tenant, "Left", {"agent_id": leaf})
    right = _published(client, tenant, "Right", {"agent_id": leaf})
    top = _agent(client, tenant, _spec("Top", {"agent_id": left}, {"agent_id": right}))
    assert _publish(client, tenant, top).status_code == 201


def test_a_pinned_ref_to_an_older_version_without_the_loop_is_allowed(client, tenant):
    a = _published(client, tenant, "A")  # version 1 calls nothing
    b = _published(client, tenant, "B", {"agent_id": a})
    # A@2 pins A@1 and calls B, which calls A (latest = this very publish): a cycle through B.
    assert _set_draft(client, tenant, a, _spec("A", {"agent_id": b})).status_code == 200
    assert _publish(client, tenant, a).status_code == 422
    # B pinned to A's version 1 breaks the loop: B@2 -> A@1 (which calls nothing).
    assert _set_draft(client, tenant, b, _spec("B", {"agent_id": a, "version": 1})).status_code == 200
    assert _publish(client, tenant, b).status_code == 201
    assert _publish(client, tenant, a).status_code == 201


def test_restore_runs_the_same_checks(client, tenant):
    b = _published(client, tenant, "B")
    a = _published(client, tenant, "A", {"agent_id": b})  # A@1 -> B
    assert _set_draft(client, tenant, a, _spec("A")).status_code == 200
    assert _publish(client, tenant, a).status_code == 201  # A@2 calls nothing
    assert _set_draft(client, tenant, b, _spec("B", {"agent_id": a})).status_code == 200
    assert _publish(client, tenant, b).status_code == 201  # B@2 -> A@2: fine
    r = client.post(f"/api/v1/agents/{a}/versions/1/restore", json={"discard_draft": True}, headers=_h(tenant))
    assert r.status_code == 422 and r.json()["code"] == "agent_tool_cycle", r.text  # A@3 would be A@1 -> B -> A
