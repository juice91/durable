"""P0.3: version compare and restore (rollback publishes a new version; history is never rewritten)."""

from __future__ import annotations

import uuid

import pytest

from eap_api.version_diff import compare_specs, instructions_diff, spec_changes
from eap_schemas import AgentSpec, ModelRef, canonical_spec_hash

from .conftest import get_auth_header

USER = "alice"


# --- pure diff -----------------------------------------------------------------------------------


def test_spec_changes_paths_and_kinds():
    before = {"a": 1, "m": {"x": "p", "y": True}, "l": [1, 2], "gone": 0}
    after = {"a": 2, "m": {"x": "p", "y": 1}, "l": [1], "new": "v"}
    assert spec_changes(before, after) == [
        {"path": "a", "kind": "changed", "before": 1, "after": 2},
        {"path": "gone", "kind": "removed", "before": 0, "after": None},
        {"path": "l[1]", "kind": "removed", "before": 2, "after": None},
        {"path": "m.y", "kind": "changed", "before": True, "after": 1},
        {"path": "new", "kind": "added", "before": None, "after": "v"},
    ]
    assert spec_changes(before, before) == []


def test_instructions_diff_lines():
    diff = instructions_diff("one\ntwo\nthree", "one\n2\nthree\nfour")
    assert [(d["op"], d["text"], d["base_line"], d["target_line"]) for d in diff] == [
        ("equal", "one", 1, 1),
        ("removed", "two", 2, None),
        ("added", "2", None, 2),
        ("equal", "three", 3, 3),
        ("added", "four", None, 4),
    ]


def test_compare_ignores_defaults_missing_from_an_old_stored_spec():
    full = AgentSpec(name="A", model=ModelRef(provider="test", name="ticket")).model_dump(mode="json")
    minimal = {"name": "A", "model": {"provider": "test", "name": "ticket"}}
    assert compare_specs(minimal, full)["changes"] == []


# --- API -----------------------------------------------------------------------------------------


def _spec(instructions: str = "", model: str = "gpt-4", provider: str = "openai") -> dict:
    return AgentSpec(
        name="Restore Agent", instructions=instructions, model=ModelRef(provider=provider, name=model)
    ).model_dump(mode="json")


def _agent_with_versions(client, tenant: str, *instructions: str) -> str:
    h = get_auth_header(tenant, USER)
    r = client.post("/api/v1/agents", json={"spec": _spec(instructions[0])}, headers=h)
    assert r.status_code == 201
    agent_id = r.json()["id"]
    for i, text in enumerate(instructions):
        if i:
            assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec(text)}, headers=h).status_code == 200
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


@pytest.fixture
def tenant_id():
    return f"t{uuid.uuid4().hex[:10]}"


def test_restore_v1_after_v3_publishes_v4(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "first", "second", "third")
    before = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()

    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
    assert r.status_code == 201, r.text
    v4 = r.json()
    v1 = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=h).json()
    assert v4["version"] == 4 and v4["restored_from"] == 1
    assert v4["spec"] == v1["spec"] and v4["spec_hash"] == v1["spec_hash"]
    assert v4["spec_hash"] == canonical_spec_hash(AgentSpec.model_validate(v4["spec"]))

    after = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    assert [v["version"] for v in after] == [4, 3, 2, 1]
    assert after[1:] == before  # v1-v3 untouched, restored_from null on them
    assert all(v["restored_from"] is None for v in before)

    agent = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()
    assert agent["latest_version"] == 4 and agent["draft_spec"] == v1["spec"]
    # The draft is clean again: publishing it is "no changes".
    r = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 409 and "no changes since version 4" in r.json()["detail"]


def test_restore_latest_or_equal_spec_is_409(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b", "a")
    r = client.post(f"/api/v1/agents/{agent_id}/versions/3/restore", headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "version 3 is already the latest version"
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "version 1 has the same spec as the latest version 3"


def test_restore_with_dirty_draft_needs_discard(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b")
    client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("unsaved")}, headers=h)

    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={}, headers=h)
    assert r.status_code == 409 and "unpublished changes since version 2" in r.json()["detail"]
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["latest_version"] == 2

    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h)
    assert r.status_code == 201 and r.json()["version"] == 3
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["draft_spec"]["instructions"] == "a"


def test_restore_rechecks_todays_model_policy(client, tenant_id, monkeypatch):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a")
    client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("b", provider="test", model="ticket")}, headers=h)
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201

    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")  # openai:gpt-4 (v1's model) no longer allowed
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h)
    assert r.status_code == 422 and "openai:gpt-4" in r.json()["detail"]
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["latest_version"] == 2


def test_publish_and_restore_check_placeholders(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a")
    client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("Hi {{context.who}}")}, headers=h)
    r = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 422 and "names no context field" in r.json()["detail"]


def test_restore_unknown_and_cross_tenant(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b")
    assert client.post(f"/api/v1/agents/{agent_id}/versions/9/restore", headers=h).status_code == 404
    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", USER)
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=other)
    assert r.status_code == 404 and r.json()["detail"] == "Agent not found"
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=2", headers=other)
    assert r.status_code == 404 and r.json()["detail"] == "Agent not found"


def test_compare_versions_and_draft(client, tenant_id):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "one\ntwo", "one\n2")
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=2", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base"]["version"] == 1 and body["target"]["version"] == 2
    assert body["identical"] is False and body["truncated"] is False
    assert body["changes"] == [{"path": "instructions", "kind": "changed", "before": "one\ntwo", "after": "one\n2"}]
    assert [d["op"] for d in body["instructions_diff"]] == ["equal", "removed", "added"]

    client.patch(
        f"/api/v1/agents/{agent_id}", json={"spec": _spec("one\n2", provider="test", model="ticket")}, headers=h
    )
    body = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=2&target=draft", headers=h).json()
    assert body["target"]["version"] == "draft" and body["target"]["created_at"] is None
    assert {c["path"] for c in body["changes"]} == {"model.provider", "model.name"}
    assert all(d["op"] == "equal" for d in body["instructions_diff"])

    same = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=2&target=2", headers=h).json()
    assert same["identical"] is True and same["changes"] == []


@pytest.mark.parametrize(
    "query,status,detail",
    [
        ("base=1&target=7", 404, "Version 7 not found"),
        ("base=x&target=1", 422, "base must be a version number or 'draft'"),
        ("base=0&target=1", 422, "base must be a version number or 'draft'"),
        ("base=1&target=-1", 422, "target must be a version number or 'draft'"),
    ],
)
def test_compare_bad_sides(client, tenant_id, query, status, detail):
    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a")
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?{query}", headers=h)
    assert r.status_code == status and r.json()["detail"] == detail


def test_concurrent_restores_have_one_winner(client, tenant_id):
    import threading

    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b")
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def restore() -> None:
        barrier.wait()
        statuses.append(client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=h).status_code)

    threads = [threading.Thread(target=restore) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [201, 409]
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    assert [(v["version"], v["restored_from"]) for v in versions] == [(3, 1), (2, None), (1, None)]


def _pause_inside_lock(monkeypatch):
    """Make the first request that reads the latest version after this call (publish and restore
    both do it right after locking the agent row) stop there until `go` is set; later calls pass
    through. Returns (entered, go) events. TestClient runs sync routes on worker threads, so the
    gate is call order, not the caller's thread."""
    import threading

    import eap_api.services.agents as routes

    entered, go = threading.Event(), threading.Event()
    real = routes._latest_version
    first = threading.Lock()

    def paused(db, agent_id):
        if first.acquire(blocking=False):
            entered.set()
            assert go.wait(10), "test never released the paused request"
        return real(db, agent_id)

    monkeypatch.setattr(routes, "_latest_version", paused)
    return entered, go


def _run(name: str, fn, out: dict):
    import threading

    t = threading.Thread(target=lambda: out.__setitem__(name, fn()), name=name)
    t.start()
    return t


def test_publish_waits_for_restore_and_never_publishes_the_discarded_draft(client, tenant_id, monkeypatch):
    import time

    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b")
    client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("DISCARDED")}, headers=h)
    entered, go = _pause_inside_lock(monkeypatch)
    out: dict = {}

    t_restore = _run("restore", lambda: client.post(
        f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h
    ), out)
    assert entered.wait(10)  # restore holds the agent lock, paused before its reads
    t_publish = _run("publish", lambda: client.post(f"/api/v1/agents/{agent_id}/versions", headers=h), out)
    time.sleep(0.5)
    assert "publish" not in out, "publish must wait for the agent lock held by restore"
    go.set()
    t_restore.join(10)
    t_publish.join(10)

    assert out["restore"].status_code == 201 and out["restore"].json()["version"] == 3
    assert out["publish"].status_code == 409
    assert out["publish"].json()["detail"] == "no changes since version 3"
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    assert [(v["version"], v["restored_from"]) for v in versions] == [(3, 1), (2, None), (1, None)]
    assert all(v["spec"]["instructions"] != "DISCARDED" for v in versions)


def test_restore_waits_for_publish_and_judges_the_committed_state(client, tenant_id, monkeypatch):
    """Publish first: it commits the draft as v3, then restore runs on that state (the draft is
    clean again) and publishes v4 from v1. Serialized, not lost: no IntegrityError, no 409."""
    import time

    h = get_auth_header(tenant_id, USER)
    agent_id = _agent_with_versions(client, tenant_id, "a", "b")
    client.patch(f"/api/v1/agents/{agent_id}", json={"spec": _spec("c")}, headers=h)
    entered, go = _pause_inside_lock(monkeypatch)
    out: dict = {}

    t_publish = _run("publish", lambda: client.post(f"/api/v1/agents/{agent_id}/versions", headers=h), out)
    assert entered.wait(10)
    t_restore = _run("restore", lambda: client.post(
        f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h
    ), out)
    time.sleep(0.5)
    assert "restore" not in out, "restore must wait for the agent lock held by publish"
    go.set()
    t_publish.join(10)
    t_restore.join(10)

    assert out["publish"].status_code == 201 and out["publish"].json()["version"] == 3
    assert out["restore"].status_code == 201
    assert (out["restore"].json()["version"], out["restore"].json()["restored_from"]) == (4, 1)
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    assert [v["spec"]["instructions"] for v in versions] == ["a", "c", "b", "a"]
