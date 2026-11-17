"""`secrets.request` is always gated: the API refuses a spec that lists it without an approvals entry."""

from __future__ import annotations

from .conftest import create_agent, get_auth_header
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures

SPEC = {"name": "a", "model": {"provider": "openai", "name": "gpt-4"}, "tools": ["secrets.request"]}


def test_secrets_request_needs_its_approval_entry_on_any_agent(client, tenant):
    h = get_auth_header(tenant, "alice")
    r = client.post("/api/v1/agents", json={"spec": SPEC}, headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "approval_required" and r.json()["tools"] == ["secrets.request"]
    agent_id = create_agent(client, tenant, "alice")
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": SPEC}, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "approval_required"
    gated = {**SPEC, "approvals": {"tools": ["secrets.request"]}}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": gated}, headers=h).status_code == 200
