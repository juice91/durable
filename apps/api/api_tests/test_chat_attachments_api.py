"""W2.4: chat attachments (upload, fetch, AG-UI media parts) and message feedback."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from .conftest import get_auth_header
from .run_helpers import iter_frames, requires_pg
from .test_agui6_agent_endpoints import (  # noqa: F401 - fixtures and helpers
    ag,
    agui_settings,
    body,
    post,
    tenant,
    types,
    user,
    worker,
)
from .test_runs import SPEC

pytestmark = requires_pg

def make_agent(client, h, model: str = "echo") -> str:
    spec = {**SPEC, "model": {"provider": "test", "name": model}, "tools": []}
    agent_id = client.post("/api/v1/agents", json={"spec": spec}, headers=h).json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def upload(client, headers, name="a.png", data=PNG, content_type="image/png"):
    return client.post("/api/v1/attachments", files={"file": (name, data, content_type)}, headers=headers)


def part(attachment: dict[str, Any], kind: str = "image") -> dict[str, Any]:
    return {
        "type": kind,
        "source": {"type": "url", "value": f"/api/v1/attachments/{attachment['id']}", "mimeType": attachment["media_type"]},
    }


@pytest.fixture(autouse=True)
def _clean(ag):
    yield
    with ag[1].state.database_engine.begin() as conn:
        for t in ag[1].state.test_tenants:
            conn.execute(text("DELETE FROM chat_attachments WHERE tenant_id = :t"), {"t": t})


def test_upload_and_fetch(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    r = upload(client, h)
    assert r.status_code == 201, r.text
    meta = r.json()
    assert meta["name"] == "a.png" and meta["media_type"] == "image/png" and meta["size"] == len(PNG)
    got = client.get(f"/api/v1/attachments/{meta['id']}", headers=h)
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["content-type"] == "image/png"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("inline; filename=\"a.png\"")


def test_text_attachment_downloads_not_inline(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    meta = upload(client, h, "n.txt", b"hello", "text/plain").json()
    got = client.get(f"/api/v1/attachments/{meta['id']}", headers=h)
    assert got.headers["content-disposition"].startswith("attachment;")


def test_cross_tenant_and_unknown_are_404(ag, tenant):
    client, _ = ag
    meta = upload(client, get_auth_header(tenant, "alice")).json()
    other = get_auth_header(f"t{uuid.uuid4().hex[:8]}", "bob")
    assert client.get(f"/api/v1/attachments/{meta['id']}", headers=other).status_code == 404
    assert client.get(f"/api/v1/attachments/{uuid.uuid4()}", headers=other).status_code == 404
    assert client.get("/api/v1/attachments/not-a-uuid", headers=other).status_code == 404


def test_size_limit_is_413(ag, tenant, monkeypatch):
    client, _ = ag
    monkeypatch.setenv("EAP_CHAT_ATTACHMENT_MAX_BYTES", "100")
    h = get_auth_header(tenant, "alice")
    assert upload(client, h, "big.txt", b"x" * 101, "text/plain").status_code == 413
    assert upload(client, h, "ok.txt", b"x" * 100, "text/plain").status_code == 201


def test_media_type_allowlist_and_sniffing(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    assert upload(client, h, "x.exe", b"MZ", "application/x-msdownload").status_code == 415
    assert upload(client, h, "x.svg", b"<svg/>", "image/svg+xml").status_code == 415
    assert upload(client, h, "fake.png", b"not a png", "image/png").status_code == 415
    assert upload(client, h, "empty.txt", b"", "text/plain").status_code == 422
    assert upload(client, h, "d.pdf", b"%PDF-1.4 x", "application/pdf").status_code == 201


def test_agui_input_with_file_parts_runs_and_reloads(ag, tenant, worker):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    meta = upload(client, h).json()
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "what is this?"}, part(meta)]}
    status, detail, frames = post(client, agent_id, body("t1", "r1", msg), h)
    assert status == 200, detail
    assert worker.calls[0][1] == "what is this?"
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t1", headers=h).json()
    first = thread["messages"][0]
    assert first["role"] == "user" and first["content"][0] == {"type": "text", "text": "what is this?"}
    image = first["content"][1]
    assert image["type"] == "image" and image["metadata"]["attachmentId"] == meta["id"]
    assert image["source"]["value"] == f"/api/v1/attachments/{meta['id']}"
    # The attach snapshot carries the same parts.
    status, _, frames = post(client, agent_id, body("t1", "r1", msg), h)
    snap = next(f for f in frames if f.data and f.data["type"] == "MESSAGES_SNAPSHOT")
    assert snap.data["messages"][0]["content"][1]["type"] == "image"


def test_agui_refuses_inline_data_unknown_and_foreign_attachments(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    inline = {"type": "image", "source": {"type": "data", "value": "AAAA", "mimeType": "image/png"}}
    status, detail, _ = post(client, agent_id, body("t", "r1", {"id": "u", "role": "user", "content": [inline]}), h)
    assert status == 422 and detail["detail"][0]["type"] == "unsupported_content"
    ghost = part({"id": str(uuid.uuid4()), "media_type": "image/png"})
    status, detail, _ = post(client, agent_id, body("t", "r2", {"id": "u", "role": "user", "content": [ghost]}), h)
    assert status == 422 and detail["detail"][0]["type"] == "attachment_not_found"
    other = upload(client, get_auth_header(f"t{uuid.uuid4().hex[:8]}", "bob")).json()
    status, detail, _ = post(client, agent_id, body("t", "r3", {"id": "u", "role": "user", "content": [part(other)]}), h)
    assert status == 422 and detail["detail"][0]["type"] == "attachment_not_found"
    assert worker.calls == []


def test_attachment_only_message_gets_a_text_turn(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    meta = upload(client, h, "n.txt", b"notes", "text/plain").json()
    msg = {"id": "u1", "role": "user", "content": [part(meta, "document")]}
    status, detail, _ = post(client, agent_id, body("t", "r1", msg), h)
    assert status == 200, detail
    assert worker.calls[0][1].startswith("Attached:")


def test_pdf_refused_for_a_model_that_cannot_take_it(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    # make_agent's scripted model (test:*) takes only echo/history/vision as binary.
    agent_id = make_agent(client, h, "ticket")
    meta = upload(client, h, "d.pdf", b"%PDF-1.4 x", "application/pdf").json()
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "read"}, part(meta, "document")]}
    status, detail, _ = post(client, agent_id, body("t", "r1", msg), h)
    assert status == 422 and detail["detail"][0]["type"] == "attachment_unsupported_by_model"
    text_file = upload(client, h, "n.txt", b"ok", "text/plain").json()
    msg = {"id": "u2", "role": "user", "content": [{"type": "text", "text": "read"}, part(text_file, "document")]}
    assert post(client, agent_id, body("t", "r2", msg), h)[0] == 200


def test_attachment_cannot_be_reused_in_another_message(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    meta = upload(client, h).json()
    first = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "a"}, part(meta)]}
    assert post(client, agent_id, body("t", "r1", first), h)[0] == 200
    second = {"id": "u2", "role": "user", "content": [{"type": "text", "text": "b"}, part(meta)]}
    status, detail, _ = post(client, agent_id, body("t", "r2", second), h)
    assert status == 422 and detail["detail"][0]["type"] == "attachment_in_use"


# --------------------------------------------------------------------------- feedback


def _answered_thread(client, h, worker):
    agent_id = make_agent(client, h)
    status, _, frames = post(client, agent_id, body("t", "r1", user("u1", "hi")), h)
    assert status == 200
    conversation = frames[1].data["value"]["conversation_id"]
    return agent_id, conversation


def test_feedback_crud_and_thread_state(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id, conv = _answered_thread(client, h, worker)
    url = f"/api/v1/conversations/{conv}/messages/r1-m0/feedback"
    r = client.put(url, json={"rating": "up"}, headers=h)
    assert r.status_code == 200 and r.json()["rating"] == "up" and r.json()["created_by"] == "alice"
    r = client.put(url, json={"rating": "down", "comment": " too long "}, headers=h)
    assert r.json()["rating"] == "down" and r.json()["comment"] == "too long"
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=h).json()
    assert thread["feedback"]["r1-m0"]["rating"] == "down"
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=h).json()["feedback"]["r1-m0"]["comment"] == "too long"
    # Another user of the tenant does not see it as their own.
    bob = get_auth_header(tenant, "bob")
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=bob).json()["feedback"] == {}
    assert client.delete(url, headers=h).status_code == 204
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=h).json()["feedback"] == {}


def test_feedback_validation_and_tenant_scope(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    _, conv = _answered_thread(client, h, worker)
    url = f"/api/v1/conversations/{conv}/messages/r1-m0/feedback"
    assert client.put(url, json={"rating": "meh"}, headers=h).status_code == 422
    assert client.put(url, json={"rating": "up", "comment": "x" * 2001}, headers=h).status_code == 422
    assert client.put(f"/api/v1/conversations/{conv}/messages/nope-m0/feedback", json={"rating": "up"}, headers=h).status_code == 404
    assert client.put(f"/api/v1/conversations/{conv}/messages/garbage/feedback", json={"rating": "up"}, headers=h).status_code == 404
    other = get_auth_header(f"t{uuid.uuid4().hex[:8]}", "bob")
    assert client.put(url, json={"rating": "up"}, headers=other).status_code == 404
    assert client.delete(url, headers=other).status_code == 404
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=other).status_code == 404


def test_the_worker_finds_a_turns_attachments_through_its_message_ids(ag, tenant, worker):
    from eap_api.ag_ui.input import derive_agent_run_id
    from eap_run_store import ChatAttachmentStore

    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    meta = upload(client, h).json()
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "see"}, part(meta)]}
    assert post(client, agent_id, body("t", "r1", msg), h)[0] == 200
    native = derive_agent_run_id(tenant, agent_id, "t", "r1")
    store = ChatAttachmentStore(app.state.database_engine)
    blobs = store.for_run(tenant, native)
    assert [b.meta.id for b in blobs] == [meta["id"]] and blobs[0].data == PNG
    assert store.for_run(f"t{uuid.uuid4().hex[:8]}", native) == []


def _replay_feedback(client, agent_id, headers) -> list[dict[str, Any]]:
    with client.stream("GET", f"/api/v1/ag-ui/agents/{agent_id}/threads/t/runs/r1/stream", headers=headers) as response:
        assert response.status_code == 200
        frames = list(iter_frames(response, deadline_s=30))
    return [
        f.data["value"] for f in frames
        if f.data and f.data["type"] == "CUSTOM" and f.data["name"] == "pydanticos.message_feedback"
    ]


def test_feedback_replays_only_to_its_author_with_the_latest_state(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    bob = get_auth_header(tenant, "bob")
    agent_id, conv = _answered_thread(client, h, worker)
    url = f"/api/v1/conversations/{conv}/messages/r1-m0/feedback"
    r = client.put(url, json={"rating": "up", "comment": "alice private note"}, headers=h)
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    # No shared run event is written.
    events = client.get(f"/api/v1/runs/{run_id}/events", headers=h).json()
    items = events["items"] if isinstance(events, dict) else events
    assert [e for e in items if e["type"] == "message_feedback"] == []
    assert _replay_feedback(client, agent_id, h) == [{"message_id": "r1-m0", "rating": "up", "comment": "alice private note"}]
    # A second user of the tenant replays the run and sees none of it.
    assert _replay_feedback(client, agent_id, bob) == []
    # up -> down: the replay and the thread GET show the latest state.
    assert client.put(url, json={"rating": "down"}, headers=h).status_code == 200
    assert _replay_feedback(client, agent_id, h) == [{"message_id": "r1-m0", "rating": "down", "comment": None}]
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=h).json()
    assert thread["feedback"]["r1-m0"]["rating"] == "down"
    # delete: both agree there is nothing.
    assert client.delete(url, headers=h).status_code == 204
    assert _replay_feedback(client, agent_id, h) == []
    assert client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t", headers=h).json()["feedback"] == {}


def test_deleting_missing_feedback_is_idempotent_and_writes_nothing(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    _, conv = _answered_thread(client, h, worker)
    url = f"/api/v1/conversations/{conv}/messages/r1-m0/feedback"
    run_id = client.put(url, json={"rating": "up"}, headers=h).json()["run_id"]
    assert client.delete(url, headers=h).status_code == 204  # removes the row
    before_events = client.get(f"/api/v1/runs/{run_id}/events", headers=h).json()
    before_feedback = client.get(f"/api/v1/conversations/{conv}/feedback", headers=h).json()
    assert client.delete(url, headers=h).status_code == 204
    assert client.delete(url, headers=h).status_code == 204
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=h).json()["feedback"] == {}
    assert client.get(f"/api/v1/conversations/{conv}/feedback", headers=h).json() == before_feedback
    assert client.get(f"/api/v1/runs/{run_id}/events", headers=h).json() == before_events


def test_api_key_principal_with_a_user_id_sees_no_feedback_anywhere():
    from types import SimpleNamespace

    from eap_api.routes import ag_ui

    principal = SimpleNamespace(tenant_id="t", user_id="svc-user", is_api_key=True)
    assert ag_ui.feedback_viewer(principal) is None
    assert ag_ui.feedback_viewer(SimpleNamespace(tenant_id="t", user_id="u", is_api_key=False)) == "u"
    c = SimpleNamespace(principal=principal, tenant_id="t", engine=None, run_store=None)
    assert ag_ui._viewer_feedback(c, "r") == []
    assert ag_ui._thread_feedback(c, "conv") == {}


def test_an_api_key_principal_with_a_user_id_sees_no_feedback_over_http_or_replay(ag, tenant, worker):
    """The HTTP read and the AG-UI replay share feedback_viewer: an API key has no own feedback, even
    when its principal carries a user_id that matches an author."""
    from eap_api.auth import AgUiPrincipal, current_principal

    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_id, conv = _answered_thread(client, h, worker)
    url = f"/api/v1/conversations/{conv}/messages/r1-m0/feedback"
    run_id = client.put(url, json={"rating": "up", "comment": "alice note"}, headers=h).json()["run_id"]
    key = AgUiPrincipal(tenant_id=tenant, kind="api_key", user_id="alice", key_id="k1")
    app.dependency_overrides[current_principal] = lambda: key
    try:
        r = client.get(f"/api/v1/conversations/{conv}/feedback", headers=h)
    finally:
        del app.dependency_overrides[current_principal]
    assert r.status_code == 200 and r.json()["feedback"] == {}, r.text
    from eap_api.auth import feedback_viewer

    assert feedback_viewer(key) is None
    assert feedback_viewer(AgUiPrincipal(tenant_id=tenant, kind="dev", user_id="alice")) == "alice"
    assert run_id
