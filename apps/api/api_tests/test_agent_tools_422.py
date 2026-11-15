"""POST /agents and PATCH with `tools: [1]` answer 422, not 500 (tester finding, follow-up batch)."""

import pytest

from .conftest import create_agent, get_auth_header

SPEC = {"name": "a", "model": {"provider": "openai", "name": "gpt-4"}}


@pytest.mark.parametrize("tools", [[1], ["calculator", None], "calculator"])
def test_create_with_non_string_tools_is_422(client, tenant, tools):
    r = client.post("/api/v1/agents", json={"spec": {**SPEC, "tools": tools}}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 422, r.text
    assert "tool" in str(r.json()["detail"])


def test_patch_with_non_string_tools_is_422(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**SPEC, "tools": [1]}}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 422, r.text
    assert "tool" in str(r.json()["detail"])
    # The same body with valid tools is accepted: the 422 above is the tools check, not the shape.
    ok = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**SPEC, "tools": ["calculator"]}}, headers=get_auth_header(tenant, "alice"))
    assert ok.status_code == 200, ok.text
