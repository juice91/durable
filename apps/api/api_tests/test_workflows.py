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


# --- archive, unarchive and restore (shared with the Architect's tools: eap_run_store.definitions) ---


def _published(client, headers, name: str = "Test Workflow") -> str:
    spec = {**_spec(), "name": name}
    wid = client.post(BASE, json={"spec": spec}, headers=headers).json()["id"]
    assert client.post(f"{BASE}/{wid}/versions", headers=headers).status_code == 201
    return wid


def _publish_edit(client, headers, wid: str, name: str) -> dict:
    """Edit the draft (the name differs, so the hash does) and publish it."""
    spec = {**_spec(), "name": name}
    assert client.patch(f"{BASE}/{wid}", json={"spec": spec}, headers=headers).status_code == 200
    resp = client.post(f"{BASE}/{wid}/versions", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_archive_hides_the_workflow_and_is_idempotent(client, auth_header):
    wid = _published(client, auth_header)
    resp = client.post(f"{BASE}/{wid}/archive", headers=auth_header)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == wid and body["archived"] is True and body["latest_version"] == 1
    assert body["paused_schedules"] == []
    assert wid not in [w["id"] for w in client.get(BASE, headers=auth_header).json()]
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["archived"] is True  # still readable
    assert client.post(f"{BASE}/{wid}/archive", headers=auth_header).json()["archived"] is True

    back = client.post(f"{BASE}/{wid}/unarchive", headers=auth_header)
    assert back.status_code == 200 and back.json()["archived"] is False
    assert wid in [w["id"] for w in client.get(BASE, headers=auth_header).json()]
    assert client.post(f"{BASE}/{wid}/unarchive", headers=auth_header).json()["archived"] is False


def test_archived_workflow_cannot_be_published_or_restored(client, auth_header):
    wid = _published(client, auth_header)
    _publish_edit(client, auth_header, wid, "Second")
    client.post(f"{BASE}/{wid}/archive", headers=auth_header)
    _set_draft(wid, {**_spec(), "name": "Third"})
    assert client.post(f"{BASE}/{wid}/versions", headers=auth_header).status_code == 409
    resp = client.post(f"{BASE}/{wid}/versions/1/restore", json={"discard_draft": True}, headers=auth_header)
    assert resp.status_code == 409
    assert "archived" in resp.json()["detail"]
    client.post(f"{BASE}/{wid}/unarchive", headers=auth_header)
    assert client.post(f"{BASE}/{wid}/versions", headers=auth_header).status_code == 201


def test_archive_pauses_active_schedules_and_unarchive_leaves_them_paused(client, auth_header):
    wid = _published(client, auth_header)
    sched = client.post(
        f"{BASE}/{wid}/schedules", json={"name": "nightly", "cron": "0 3 * * *", "input": {}}, headers=auth_header
    )
    assert sched.status_code == 201, sched.text
    sid = sched.json()["id"]
    resp = client.post(f"{BASE}/{wid}/archive", headers=auth_header)
    assert resp.json()["paused_schedules"] == [sid]
    listed = client.get(f"{BASE}/schedules", headers=auth_header).json()["items"]
    assert [(i["id"], i["active"]) for i in listed] == [(sid, False)]
    client.post(f"{BASE}/{wid}/unarchive", headers=auth_header)
    listed = client.get(f"{BASE}/schedules", headers=auth_header).json()["items"]
    assert [(i["id"], i["active"]) for i in listed] == [(sid, False)]


def test_restore_publishes_an_old_spec_as_the_next_version(client, auth_header):
    wid = _published(client, auth_header, "First")
    _publish_edit(client, auth_header, wid, "Second")
    resp = client.post(f"{BASE}/{wid}/versions/1/restore", headers=auth_header)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["version"] == 3 and body["restored_from"] == 1 and body["warnings"] == []
    versions = client.get(f"{BASE}/{wid}/versions", headers=auth_header).json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert versions[0]["spec_hash"] == versions[2]["spec_hash"] and versions[0]["spec"]["name"] == "First"
    got = client.get(f"{BASE}/{wid}", headers=auth_header).json()
    assert got["name"] == "First" and got["draft_spec"]["name"] == "First"  # the draft became the restored spec


def test_restore_refusals(client, auth_header):
    wid = _published(client, auth_header, "First")
    _publish_edit(client, auth_header, wid, "Second")
    latest = client.post(f"{BASE}/{wid}/versions/2/restore", headers=auth_header)
    assert latest.status_code == 409 and "already the latest" in latest.json()["detail"]
    assert client.post(f"{BASE}/{wid}/versions/9/restore", headers=auth_header).status_code == 404
    # A draft with unpublished changes needs discard_draft.
    client.patch(f"{BASE}/{wid}", json={"spec": {**_spec(), "name": "Unpublished"}}, headers=auth_header)
    dirty = client.post(f"{BASE}/{wid}/versions/1/restore", headers=auth_header)
    assert dirty.status_code == 409 and "unpublished changes" in dirty.json()["detail"]
    ok = client.post(f"{BASE}/{wid}/versions/1/restore", json={"discard_draft": True}, headers=auth_header)
    assert ok.status_code == 201 and ok.json()["version"] == 3


def test_restore_is_revalidated_under_todays_policy(client, auth_header, monkeypatch):
    wid = _published(client, auth_header, "First")
    _publish_edit(client, auth_header, wid, "Second")
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "calc.add")  # clock.now is no longer allowed
    resp = client.post(f"{BASE}/{wid}/versions/1/restore", headers=auth_header)
    assert resp.status_code == 422 and "clock.now" in resp.json()["detail"]
    versions = client.get(f"{BASE}/{wid}/versions", headers=auth_header).json()
    assert [v["version"] for v in versions] == [2, 1]  # nothing written


def test_archive_unarchive_restore_404_other_tenant(client, auth_header):
    wid = _published(client, auth_header)
    _publish_edit(client, auth_header, wid, "Second")
    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    assert client.post(f"{BASE}/{wid}/archive", headers=other).status_code == 404
    assert client.post(f"{BASE}/{wid}/unarchive", headers=other).status_code == 404
    assert client.post(f"{BASE}/{wid}/versions/1/restore", headers=other).status_code == 404
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["archived"] is False


# --- archive contract: body, runs_active/force, archived_at, list filter, 409 workflow_archived ---


def _tenant(headers: dict) -> str:
    return headers["Authorization"].split(":")[1]


def _insert_run(headers: dict, wid: str, status: str) -> str:
    run_id = str(uuid.uuid4())
    engine = create_engine(PG_URL)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, tenant_id, kind, workflow_id, workflow_version, status, input, created_at, updated_at)"
                    " VALUES (:id, :t, 'workflow', :w, 1, :s, '{}', now(), now())"
                ),
                {"id": run_id, "t": _tenant(headers), "w": wid, "s": status},
            )
    finally:
        engine.dispose()
    return run_id


def _ids(client, headers, **params) -> list[str]:
    resp = client.get(BASE, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return [w["id"] for w in resp.json()]


def test_archive_body_defaults_to_true_and_false_unarchives(client, auth_header):
    wid = _published(client, auth_header)
    got = client.get(f"{BASE}/{wid}", headers=auth_header).json()
    assert got["archived"] is False and got["archived_at"] is None
    resp = client.post(f"{BASE}/{wid}/archive", json={}, headers=auth_header)  # default archived=true
    assert resp.status_code == 200 and resp.json()["archived"] is True
    stamp = resp.json()["archived_at"]
    assert stamp
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["archived_at"] == stamp
    again = client.post(f"{BASE}/{wid}/archive", json={"archived": True}, headers=auth_header)
    assert again.json()["archived_at"] == stamp  # idempotent: the first archive time stays
    back = client.post(f"{BASE}/{wid}/archive", json={"archived": False}, headers=auth_header)
    assert back.status_code == 200 and back.json()["archived"] is False and back.json()["archived_at"] is None
    assert back.json()["paused_schedules"] == []


def test_archive_refused_while_runs_are_active_unless_forced(client, auth_header):
    wid = _published(client, auth_header)
    _insert_run(auth_header, wid, "running")
    _insert_run(auth_header, wid, "queued")
    _insert_run(auth_header, wid, "succeeded")
    resp = client.post(f"{BASE}/{wid}/archive", headers=auth_header)
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "runs_active" and resp.json()["active_runs"] == 2
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["archived"] is False
    forced = client.post(f"{BASE}/{wid}/archive", json={"force": True}, headers=auth_header)
    assert forced.status_code == 200 and forced.json()["archived"] is True


def test_archive_allowed_when_only_finished_runs(client, auth_header):
    wid = _published(client, auth_header)
    for status in ("succeeded", "failed", "cancelled"):
        _insert_run(auth_header, wid, status)
    assert client.post(f"{BASE}/{wid}/archive", headers=auth_header).status_code == 200


def test_unarchive_is_not_blocked_by_runs(client, auth_header):
    wid = _published(client, auth_header)
    assert client.post(f"{BASE}/{wid}/archive", headers=auth_header).status_code == 200
    _insert_run(auth_header, wid, "running")
    assert client.post(f"{BASE}/{wid}/archive", json={"archived": False}, headers=auth_header).status_code == 200


def test_list_filter_archived_true_false_all(client, auth_header):
    live = _published(client, auth_header, "Live")
    gone = _published(client, auth_header, "Gone")
    client.post(f"{BASE}/{gone}/archive", headers=auth_header)
    assert _ids(client, auth_header) == [live]
    assert _ids(client, auth_header, archived="false") == [live]
    assert _ids(client, auth_header, archived="true") == [gone]
    assert _ids(client, auth_header, archived="all") == [live, gone]
    assert client.get(BASE, headers=auth_header, params={"archived": "maybe"}).status_code == 422
    archived_row = client.get(BASE, headers=auth_header, params={"archived": "true"}).json()[0]
    assert archived_row["archived"] is True and archived_row["archived_at"]


def test_archived_workflow_refuses_runs_and_schedules_with_409(client, auth_header):
    wid = _published(client, auth_header)
    sched = client.post(
        f"{BASE}/{wid}/schedules", json={"name": "n", "cron": "0 3 * * *", "input": {}}, headers=auth_header
    ).json()["id"]
    client.post(f"{BASE}/{wid}/archive", headers=auth_header)
    run = client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=auth_header)
    assert run.status_code == 409 and run.json()["code"] == "workflow_archived"
    new = client.post(
        f"{BASE}/{wid}/schedules", json={"name": "m", "cron": "0 4 * * *", "input": {}}, headers=auth_header
    )
    assert new.status_code == 409 and new.json()["code"] == "workflow_archived"
    # Resuming its paused schedule is refused too; reading and pausing still work.
    resume = client.patch(f"{BASE}/schedules/{sched}", json={"active": True}, headers=auth_header)
    assert resume.status_code == 409 and resume.json()["code"] == "workflow_archived"
    assert client.patch(f"{BASE}/schedules/{sched}", json={"active": False}, headers=auth_header).status_code == 200
    assert client.get(f"{BASE}/{wid}/schedules", headers=auth_header).status_code == 200
    # A workflow of another tenant stays an indistinguishable 404.
    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    assert client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=other).status_code == 404
