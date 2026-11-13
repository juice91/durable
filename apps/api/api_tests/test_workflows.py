"""Tests for /api/v1/workflows CRUD, publish, and tenant isolation."""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from eap_schemas.workflows import WorkflowSpec

from .conftest import get_auth_header
from .run_helpers import PG_URL, requires_pg

pytestmark = requires_pg
BASE = "/api/v1/workflows"


def _spec(agent_id: str | None = None, tool_id: str = "clock.now") -> dict:
    """Build a minimal valid spec. If agent_id given, includes an agent step."""
    steps: list[dict[str, Any]] = []
    if agent_id:
        steps.append({
            "id": "ask",
            "type": "agent",
            "agent_id": agent_id,
            "input": {"q": {"$ref": "input.q"}},
            "next": "now",
        })
    steps.append({"id": "now", "type": "tool", "tool_id": tool_id, "input": {}})
    return {
        "name": "Test Workflow",
        "input_schema": {"type": "object"},
        "start": steps[0]["id"],
        "steps": steps,
    }


def _set_draft(workflow_id: str, spec: dict | None) -> None:
    """Write draft_spec directly. The API refuses an invalid spec, and it has no way to clear a draft."""
    engine = create_engine(PG_URL)
    try:
        with engine.begin() as conn:
            if spec is None:
                conn.execute(text("UPDATE workflows SET draft_spec = NULL WHERE id = :id"), {"id": workflow_id})
            else:
                conn.execute(
                    text("UPDATE workflows SET draft_spec = CAST(:spec AS jsonb) WHERE id = :id"),
                    {"id": workflow_id, "spec": json.dumps(spec)},
                )
    finally:
        engine.dispose()


def test_post_draft_creates_201(client, auth_header):
    """POST /workflows with valid spec returns 201 and latest_version=null."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "id" in data
    assert data["name"] == "Test Workflow"
    assert data["latest_version"] is None

    got = client.get(f"{BASE}/{data['id']}", headers=auth_header)
    assert got.status_code == 200, got.text
    assert got.json()["id"] == data["id"]
    assert got.json()["draft_spec"]["start"] == "now"
    assert got.json()["latest_version"] is None


def test_list_workflows_returns_tenant_isolation(client, auth_header):
    """Other tenants' workflows are not visible."""
    # Create workflow for this tenant
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    assert resp.status_code == 201
    wid = resp.json()["id"]

    # List shows only this tenant's workflows
    resp = client.get(BASE, headers=auth_header)
    assert resp.status_code == 200
    ids = [w["id"] for w in resp.json()]
    assert wid in ids

    # Another tenant cannot see it
    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.get(BASE, headers=other)
    assert resp.status_code == 200
    assert wid not in [w["id"] for w in resp.json()]


def test_get_workflow_404_other_tenant(client, auth_header):
    """GET for another tenant's workflow is 404."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.get(f"{BASE}/{wid}", headers=other)
    assert resp.status_code == 404


def test_patch_draft_404_other_tenant(client, auth_header):
    """PATCH for another tenant's workflow is 404."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.patch(f"{BASE}/{wid}", json={"spec": _spec()}, headers=other)
    assert resp.status_code == 404


def test_patch_draft_replaces(client, auth_header):
    """PATCH replaces draft and updates latest_version remains null."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]
    assert resp.json()["latest_version"] is None

    spec = _spec()
    spec["name"] = "Renamed"
    resp = client.patch(f"{BASE}/{wid}", json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest_version"] is None
    assert body["name"] == "Renamed"
    assert body["draft_spec"]["name"] == "Renamed"


def test_publish_creates_version_1(client, auth_header):
    """POST /versions creates version 1 with spec_hash."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["version"] == 1
    assert "spec_hash" in data
    assert len(data["spec"]) > 0


def test_publish_409_unchanged(client, auth_header):
    """Publishing identical draft returns 409."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 409


def test_publish_creates_version_2_after_patch(client, auth_header):
    """After PATCH, publish creates version 2."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    published = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert published.status_code == 201, published.text
    assert published.json()["version"] == 1

    # Patch to change spec
    spec = _spec()
    spec["name"] = "Updated"
    resp = client.patch(f"{BASE}/{wid}", json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 200

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 201
    assert resp.json()["version"] == 2


def test_invalid_patch_does_not_replace_the_draft(client, auth_header):
    """A 422 PATCH leaves the stored draft, so publish still succeeds."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.patch(
        f"{BASE}/{wid}",
        json={"spec": {"name": "x", "input_schema": {"type": "object"}, "start": "now", "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "next": "missing"}]}},
        headers=auth_header,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert set(detail[0]) == {"loc", "msg", "type"}
    assert "does not exist" in detail[0]["msg"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 201


def test_publish_422_when_stored_draft_is_invalid(client, auth_header):
    """A draft that bypassed request validation is still rejected at publish, as a string detail."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]
    _set_draft(wid, {"name": "x"})

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert isinstance(resp.json()["detail"], str)
    assert resp.json()["detail"].startswith("draft spec is invalid:")


def test_publish_400_when_no_draft(client, auth_header):
    """Publishing when the draft has been cleared returns 400."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]
    _set_draft(wid, None)

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "Workflow has no draft spec"


def test_publish_422_another_tenants_agent(client, auth_header, monkeypatch):
    """A real agent published by another tenant is still 'not found' for this tenant."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    from .conftest import create_agent, publish_version

    other_tenant = f"t{uuid.uuid4().hex[:10]}"
    agent_id = create_agent(client, other_tenant, "bob")
    published = publish_version(client, agent_id, other_tenant, "bob")
    assert published["version"] == 1

    resp = client.post(BASE, json={"spec": _spec(agent_id)}, headers=auth_header)
    assert resp.status_code == 201, resp.text
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert f"step 'ask': agent '{agent_id}' was not found" in detail


def test_publish_422_agent_never_published(client, auth_header, monkeypatch):
    """Publish fails for agent with no published version."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    from .conftest import create_agent, publish_version

    # Create but don't publish agent
    resp = client.post(
        "/api/v1/agents",
        json={
            "spec": {
                "name": "Test",
                "model": {"provider": "openai", "name": "gpt-4"},
            }
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    resp = client.post(BASE, json={"spec": _spec(agent_id)}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    assert f"step 'ask': agent '{agent_id}' has no published version" in resp.json()["detail"]


def test_publish_422_unknown_tool(client, auth_header, monkeypatch):
    """Publish fails for unknown tool."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    spec = _spec(tool_id="unknown.tool")
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    assert "step 'now': tool 'unknown.tool' does not exist" in resp.json()["detail"]


def test_publish_422_disallowed_tool(client, auth_header, monkeypatch):
    """Publish fails for tool not in EAP_ALLOWED_TOOLS."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    spec = _spec(tool_id="tickets.create")
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    assert "EAP_ALLOWED_TOOLS" in resp.json()["detail"]


def test_publish_422_multiple_problems_aggregated(client, auth_header, monkeypatch):
    """Multiple publish problems are joined with '; '."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    agent_id = str(uuid.uuid4())  # Fake
    spec = _spec(agent_id, tool_id="tickets.create")  # Both problems

    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "step 'ask'" in detail
    assert "step 'now'" in detail
    assert "; " in detail
    ask, now = detail.split("; ")
    assert ask.startswith("step 'ask'")
    assert now.startswith("step 'now'")


def test_list_versions_newest_first(client, auth_header):
    """Versions list is newest first."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    resp1 = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp1.status_code == 201
    assert resp1.json()["version"] == 1

    # Change draft first
    spec = _spec()
    spec["name"] = "Updated"
    client.patch(f"{BASE}/{wid}", json={"spec": spec}, headers=auth_header)

    resp2 = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp2.status_code == 201
    assert resp2.json()["version"] == 2

    resp = client.get(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 200
    versions = resp.json()
    assert [v["version"] for v in versions] == [2, 1]


def test_get_version_by_number(client, auth_header):
    """GET /versions/{n} returns specific version."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    client.post(f"{BASE}/{wid}/versions", headers=auth_header)

    resp = client.get(f"{BASE}/{wid}/versions/1", headers=auth_header)
    assert resp.status_code == 200
    assert resp.json()["version"] == 1

    resp = client.get(f"{BASE}/{wid}/versions/2", headers=auth_header)
    assert resp.status_code == 404


def test_get_version_404_other_tenant(client, auth_header):
    """GET version for another tenant's workflow is 404."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    client.post(f"{BASE}/{wid}/versions", headers=auth_header)

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.get(f"{BASE}/{wid}/versions/1", headers=other)
    assert resp.status_code == 404


def test_publish_404_other_tenant(client, auth_header):
    """POST /versions for another tenant's workflow is 404."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.post(f"{BASE}/{wid}/versions", headers=other)
    assert resp.status_code == 404


def test_list_versions_404_other_tenant(client, auth_header):
    """GET /versions for another tenant's workflow is 404."""
    resp = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    wid = resp.json()["id"]

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    resp = client.get(f"{BASE}/{wid}/versions", headers=other)
    assert resp.status_code == 404


def test_invalid_spec_returns_422_with_detail_msg(client, auth_header):
    """POST with invalid spec returns 422 with validator message in detail[].msg."""
    spec = _spec()
    spec["steps"][0]["next"] = "nonexistent"  # Dangling next

    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert set(detail[0]) == {"loc", "msg", "type"}
    assert "step 'now'" in detail[0]["msg"]
    assert "does not exist" in detail[0]["msg"]


def test_agent_step_with_pinned_version(client, auth_header, monkeypatch):
    """Publish with pinned agent version succeeds if version exists."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    from .conftest import create_agent, publish_version

    agent_id = create_agent(client, auth_header["Authorization"].split(":")[1], "alice")
    publish_version(client, agent_id, auth_header["Authorization"].split(":")[1], "alice")

    spec = _spec(agent_id)
    spec["steps"][0]["version"] = 1

    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 201


def test_agent_step_with_missing_pinned_version(client, auth_header, monkeypatch):
    """Publish with pinned version that doesn't exist returns 422."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    from .conftest import create_agent, publish_version

    agent_id = create_agent(client, auth_header["Authorization"].split(":")[1], "alice")
    publish_version(client, agent_id, auth_header["Authorization"].split(":")[1], "alice")

    spec = _spec(agent_id)
    spec["steps"][0]["version"] = 99  # Missing version

    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    assert "has no published version 99" in resp.json()["detail"]


def test_unpublished_agent_rejected(client, auth_header, monkeypatch):
    """Publish fails for agent that was created but never published."""
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    from .conftest import create_agent

    # Create agent but don't publish
    resp = client.post(
        "/api/v1/agents",
        json={
            "spec": {
                "name": "Unpublished",
                "model": {"provider": "openai", "name": "gpt-4"},
            }
        },
        headers=auth_header,
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]

    resp = client.post(BASE, json={"spec": _spec(agent_id)}, headers=auth_header)
    wid = resp.json()["id"]

    resp = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert resp.status_code == 422
    assert "has no published version" in resp.json()["detail"]


def test_post_draft_422_invalid_spec(client, auth_header):
    """POST with invalid spec returns 422."""
    spec = {
        "name": "Test",
        "input_schema": {"type": "object"},
        "start": "missing",
        "steps": [],  # No steps but start references missing
    }
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422


def test_create_rejects_branch_root_ref_to_its_parallel_step(client, auth_header):
    """A branch root cannot read its parallel step, so create is 422 and there is nothing to publish."""
    spec = {
        "name": "Parallel ref",
        "input_schema": {"type": "object"},
        "start": "p",
        "steps": [
            {"id": "p", "type": "parallel", "branches": ["a", "b"], "join": "j"},
            {"id": "a", "type": "tool", "tool_id": "clock.now", "input": {"x": {"$ref": "steps.p.output"}}},
            {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}},
            {"id": "j", "type": "tool", "tool_id": "clock.now", "input": {}},
        ],
    }
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert set(detail[0]) == {"loc", "msg", "type"}
    assert (
        "step 'a': $ref 'steps.p.output' refers to parallel step 'p', which has no output "
        "(refer to the steps inside its branches instead)"
    ) in detail[0]["msg"]
    assert "id" not in resp.json()


@pytest.mark.parametrize(("token", "label"), [("1e309", "inf"), ("NaN", "nan")])
def test_patch_non_finite_number_is_422_and_leaves_the_draft(client, auth_header, token, label):
    """PATCH of a non-finite number is a 422 that names the path and does not replace the draft."""
    created = client.post(BASE, json={"spec": _spec()}, headers=auth_header)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]

    spec = _spec()
    spec["steps"][0]["input"] = {"n": 424242}
    body = json.dumps({"spec": spec}).replace("424242", token).encode()
    assert token.encode() in body
    headers = {**auth_header, "Content-Type": "application/json"}
    resp = client.patch(f"{BASE}/{wid}", content=body, headers=headers)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail
    assert set(detail[0]) == {"loc", "msg", "type"}
    assert f"workflow spec contains a non-finite number ({label}) at steps.0.input.n" in detail[0]["msg"]

    kept = client.get(f"{BASE}/{wid}", headers=auth_header)
    assert kept.status_code == 200, kept.text
    assert kept.json()["name"] == "Test Workflow"
    assert kept.json()["draft_spec"]["steps"][0]["input"] == {}
