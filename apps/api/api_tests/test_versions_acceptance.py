"""P0.3: version compare and restore acceptance tests.

Restore never rewrites history, and restored_from is set; the restore 409s; compare with the draft on either side.
"""

from __future__ import annotations

import uuid

import pytest

from eap_schemas import AgentSpec, ModelRef

from .conftest import get_auth_header
from .test_runs import SPEC


def _agent_with_versions(client, tenant: str, *instructions: str) -> str:
    h = get_auth_header(tenant, "alice")
    r = client.post("/api/v1/agents", json={"spec": _spec(instructions[0])}, headers=h)
    assert r.status_code == 201
    agent_id = r.json()["id"]
    for i, text in enumerate(instructions):
        if i:
            assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec(text)}, headers=h).status_code == 200
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


def _spec(instructions: str = "", model: str = "gpt-4", provider: str = "openai") -> dict:
    return AgentSpec(
        name="Restore Agent", instructions=instructions, model=ModelRef(provider=provider, name=model)
    ).model_dump(mode="json")


@pytest.fixture
def tenant_id():
    return f"t{uuid.uuid4().hex[:10]}"


class TestRestoreHistoryPreservation:
    """Restore creates N+1 with the same spec_hash, never updates or deletes a row."""

    def test_restore_v1_after_v3_publishes_v4_with_history_intact(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "first", "second", "third")
        before = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()

        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
        assert r.status_code == 201
        v4 = r.json()
        v1 = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=h).json()

        assert v4["version"] == 4
        assert v4["restored_from"] == 1
        assert v4["spec"] == v1["spec"]
        assert v4["spec_hash"] == v1["spec_hash"]

        after = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
        assert [v["version"] for v in after] == [4, 3, 2, 1]

        assert after[1:] == before
        assert all(v["restored_from"] is None for v in before)


class TestRestoreIdempotentAndConflict:
    """Restore 409s appropriately."""

    def test_restore_latest_is_409(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a", "b", "c")

        r = client.post(f"/api/v1/agents/{agent_id}/versions/3/restore", headers=h)
        assert r.status_code == 409
        assert "already the latest version" in r.json()["detail"]

    def test_restore_same_spec_as_latest_is_409(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a", "b", "a")

        # v1 has same spec as v3 (latest), so v3 restore gives "already latest" error
        r = client.post(f"/api/v1/agents/{agent_id}/versions/3/restore", headers=h)
        assert r.status_code == 409
        assert "already the latest version" in r.json()["detail"]

        # But restoring v1 (which has same spec as latest) should give same-spec error
        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
        assert r.status_code == 409
        assert "has the same spec as the latest version" in r.json()["detail"]

    def test_restore_with_dirty_draft_needs_discard(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a", "b")
        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("unsaved")}, headers=h)

        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={}, headers=h)
        assert r.status_code == 409
        assert "unpublished changes" in r.json()["detail"]

        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h)
        assert r.status_code == 201


class TestRestoreRechecksPolicy:
    """A restored version is re-validated under today's policy."""

    def test_restore_rechecks_model_policy(self, client, tenant_id, monkeypatch):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a")
        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("b", provider="test", model="ticket")}, headers=h)
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201

        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
        assert r.status_code == 422
        assert "openai:gpt-4" in r.json()["detail"]


class TestCompareWithDraft:
    """Compare with the draft on either side."""

    def test_compare_version_to_draft(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "one\ntwo", "one\n2")

        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("one\n2", provider="test", model="ticket")}, headers=h)
        r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=2&target=draft", headers=h)

        assert r.status_code == 200
        body = r.json()
        assert body["target"]["version"] == "draft"
        assert body["target"]["created_at"] is None
        assert {c["path"] for c in body["changes"]} == {"model.provider", "model.name"}
        assert all(d["op"] == "equal" for d in body["instructions_diff"])


class TestCrossTenantIsolation:
    """Cross-tenant compare and restore are 404."""

    def test_cross_tenant_restore(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a", "b")
        other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "alice")

        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=other)
        assert r.status_code == 404
        assert r.json()["detail"] == "Agent not found"

    def test_cross_tenant_compare(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "a", "b")
        other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "alice")

        r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=2", headers=other)
        assert r.status_code == 404
        assert r.json()["detail"] == "Agent not found"


class TestDraftBecomesRestored:
    """The draft becomes the restored spec after restore."""

    def test_draft_is_overwritten(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "first", "second", "third")

        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
        assert r.status_code == 201

        agent = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()
        assert agent["latest_version"] == 4
        v1 = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=h).json()
        assert agent["draft_spec"] == v1["spec"]

        publishing_again = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
        assert publishing_again.status_code == 409
        assert "no changes since version 4" in publishing_again.json()["detail"]


class TestCompareDraftAsBase:
    """The draft can be the base side too, and a restore that discarded the draft leaves nothing to compare."""

    def test_compare_draft_as_base_and_target_swap(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "one", "two")
        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("three")}, headers=h)
        r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=draft&target=2", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["base"]["version"] == "draft" and body["target"]["version"] == 2
        ops = {d["op"] for d in body["instructions_diff"]}
        assert ops - {"equal"}  # three -> two is a real change
        assert any("two" in str(d) for d in body["instructions_diff"])

    def test_compare_unknown_version_is_404(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "one")
        assert client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=9", headers=h).status_code == 404


class TestPublishAfterRestore:
    """A restore consumes the draft: publishing right after it has nothing new (409), and the
    restored version is exactly one new row, never two."""

    def test_publish_after_restore_is_409_and_adds_no_version(self, client, tenant_id):
        h = get_auth_header(tenant_id, "alice")
        agent_id = _agent_with_versions(client, tenant_id, "first", "second")
        client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("unpublished edit")}, headers=h)
        r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h)
        assert r.status_code == 201, r.text
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 409
        versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
        assert [v["version"] for v in versions] == [3, 2, 1]
        draft = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["draft_spec"]
        assert draft["instructions"] == "first"  # the discarded edit was never published
