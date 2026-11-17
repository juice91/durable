"""Attachments for API keys: the AG-UI twins of POST/GET /attachments."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from .conftest import get_auth_header
from .run_helpers import requires_pg
from .test_agui6_agent_endpoints import (  # noqa: F401 - fixtures and helpers
    _key,
    ag,
    agui_settings,
    body,
    post,
    tenant,
    user,
    worker,
)
from .test_chat_attachments_api import PNG, make_agent, upload

pytestmark = requires_pg

UP = "/api/v1/ag-ui/attachments"


def kh(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def key_upload(client, key, name="a.png", data=PNG, content_type="image/png"):
    return client.post(UP, files={"file": (name, data, content_type)}, headers=kh(key))


def ref(meta, kind="image"):
    return {
        "type": kind,
        "source": {"type": "url", "value": f"{UP}/{meta['id']}", "mimeType": meta["media_type"]},
    }


@pytest.fixture(autouse=True)
def _clean(ag):
    yield
    with ag[1].state.database_engine.begin() as conn:
        for t in ag[1].state.test_tenants:
            conn.execute(text("DELETE FROM chat_attachments WHERE tenant_id = :t"), {"t": t})


@pytest.fixture
def agui_client_with_key(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    key_id, key = _key(client, tenant, agent_ids=[agent_id])
    return client, agent_id, key_id, key


def test_api_key_can_upload_reference_and_fetch(agui_client_with_key, worker):
    client, agent_id, key_id, key = agui_client_with_key
    r = key_upload(client, key)
    assert r.status_code == 201, r.text
    meta = r.json()
    assert set(meta) == {"id", "name", "media_type", "size"}
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "look"}, ref(meta)]}
    status, detail, _ = post(client, agent_id, body("t1", "r1", msg), kh(key))
    assert status == 200, detail
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t1", headers=kh(key)).json()
    image = thread["messages"][0]["content"][1]
    assert image["source"]["value"] == f"{UP}/{meta['id']}"
    got = client.get(image["source"]["value"], headers=kh(key))
    assert got.status_code == 200 and got.content == PNG
    assert got.headers["x-content-type-options"] == "nosniff"
    assert got.headers["content-disposition"].startswith("inline;")


def test_created_by_is_key_id_and_dev_uses_the_user(agui_client_with_key, ag, tenant):
    client, _agent, key_id, key = agui_client_with_key
    key_meta = key_upload(client, key).json()
    dev_meta = client.post(
        UP, files={"file": ("a.png", PNG, "image/png")}, headers=get_auth_header(tenant, "alice")
    ).json()
    with ag[1].state.database_engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id::text, created_by FROM chat_attachments WHERE tenant_id = :t"), {"t": tenant}).all())
    assert rows[key_meta["id"]] == f"key:{key_id}"
    assert rows[dev_meta["id"]] == "alice"


def test_key_cannot_fetch_an_attachment_it_did_not_create_unless_linked_to_its_agent(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    _, key_a = _key(client, tenant, agent_ids=[agent_a])
    _, key_b = _key(client, tenant, agent_ids=[agent_b])
    mine = key_upload(client, key_a).json()
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_a)).status_code == 200
    # Another key of the same tenant, and the dev upload route's own rows, are 404.
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_b)).status_code == 404
    dev = upload(client, h).json()
    assert client.get(f"{UP}/{dev['id']}", headers=kh(key_a)).status_code == 404
    # A key cannot link the dev user's upload into its own message (and so cannot read it that way).
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, ref(dev)]}
    status, detail, _ = post(client, agent_a, body("t1", "r1", msg), kh(key_a))
    assert status == 422 and "attachment_not_found" in json.dumps(detail)
    assert client.get(f"{UP}/{dev['id']}", headers=kh(key_a)).status_code == 404
    # Linked to a message of a run of key_a's agent, its own upload stays visible to key_a only.
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, ref(mine)]}
    assert post(client, agent_a, body("t1", "r1", msg), kh(key_a))[0] == 200
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_a)).status_code == 200
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_b)).status_code == 404
    # Another tenant's key never sees it.
    other = f"t{uuid.uuid4().hex[:10]}"
    ag[1].state.test_tenants.append(other)
    _, key_o = _key(client, other)
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_o)).status_code == 404
    assert client.get(f"{UP}/not-a-uuid", headers=kh(key_a)).status_code == 404


def test_api_key_without_run_scope_is_403(ag, tenant):
    client, app = ag
    key_id, key = _key(client, tenant)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET scopes = '[]'::jsonb WHERE id = :i"), {"i": key_id})
    assert key_upload(client, key).status_code == 403
    assert client.get(f"{UP}/{uuid.uuid4()}", headers=kh(key)).status_code == 403


def test_missing_or_bad_credentials_are_401(ag):
    client, _ = ag
    assert client.post(UP, files={"file": ("a.png", PNG, "image/png")}).status_code == 401
    assert client.get(f"{UP}/{uuid.uuid4()}").status_code == 401


def test_api_key_upload_keeps_size_type_and_sniff_rules(agui_client_with_key, monkeypatch):
    client, _agent, _kid, key = agui_client_with_key
    monkeypatch.setenv("EAP_CHAT_ATTACHMENT_MAX_BYTES", "100")
    assert key_upload(client, key, "big.txt", b"x" * 101, "text/plain").status_code == 413
    assert key_upload(client, key, "x.exe", b"MZ", "application/x-msdownload").status_code == 415
    assert key_upload(client, key, "fake.png", b"not a png", "image/png").status_code == 415
    assert key_upload(client, key, "e.txt", b"", "text/plain").status_code == 422
    assert key_upload(client, key, "ok.txt", b"x" * 100, "text/plain").status_code == 201


def test_a_download_of_a_missing_blob_is_410(agui_client_with_key, ag):
    import asyncio

    client, _agent, _kid, key = agui_client_with_key
    meta = key_upload(client, key).json()
    with ag[1].state.database_engine.connect() as conn:
        k = conn.execute(text("SELECT storage_key FROM chat_attachments WHERE id = :i"), {"i": meta["id"]}).scalar_one()
    asyncio.run(ag[1].state.blob_store.delete(k))
    got = client.get(f"{UP}/{meta['id']}", headers=kh(key))
    assert got.status_code == 410 and got.json()["detail"]["code"] == "attachment_gone"


def test_dev_routes_unchanged(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    meta = upload(client, h).json()
    assert set(meta) == {"id", "name", "media_type", "size"}
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, {
        "type": "image", "source": {"type": "url", "value": f"/api/v1/attachments/{meta['id']}", "mimeType": "image/png"}}]}
    assert post(client, agent_id, body("t1", "r1", msg), h)[0] == 200
    thread = client.get(f"/api/v1/ag-ui/agents/{agent_id}/threads/t1", headers=h).json()
    assert thread["messages"][0]["content"][1]["source"]["value"] == f"/api/v1/attachments/{meta['id']}"


def test_a_key_upload_over_the_json_body_cap_still_works(agui_client_with_key):
    # The AG-UI route class caps JSON bodies at 2 MiB; an upload is multipart and has its own limit.
    client, _agent, _kid, key = agui_client_with_key
    data = b"a" * (3 * 1024 * 1024)
    r = key_upload(client, key, "big.txt", data, "text/plain")
    assert r.status_code == 201, r.text
    assert client.get(f"{UP}/{r.json()['id']}", headers=kh(key)).content == data


def test_key_deletes_its_own_unsent_upload(agui_client_with_key):
    client, _agent, _key_id, key = agui_client_with_key
    meta = key_upload(client, key).json()
    assert client.delete(f"{UP}/{meta['id']}", headers=kh(key)).status_code == 204
    assert client.get(f"{UP}/{meta['id']}", headers=kh(key)).status_code == 404
    assert client.delete(f"{UP}/{meta['id']}", headers=kh(key)).status_code == 404


def test_key_delete_refuses_a_linked_attachment(agui_client_with_key, worker):
    client, agent_id, _key_id, key = agui_client_with_key
    meta = key_upload(client, key).json()
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, ref(meta)]}
    assert post(client, agent_id, body("t-d", "r-d", msg), kh(key))[0] == 200
    got = client.delete(f"{UP}/{meta['id']}", headers=kh(key))
    assert got.status_code == 409 and got.json()["detail"]["code"] == "attachment_in_use"
    assert client.get(f"{UP}/{meta['id']}", headers=kh(key)).status_code == 200


def test_key_cannot_delete_what_another_key_or_the_dev_user_uploaded(ag, tenant):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    _, key_a = _key(client, tenant, agent_ids=[agent_a])
    _, key_b = _key(client, tenant, agent_ids=[agent_b])
    mine = key_upload(client, key_a).json()
    dev = upload(client, h).json()
    assert client.delete(f"{UP}/{mine['id']}", headers=kh(key_b)).status_code == 404
    assert client.delete(f"{UP}/{dev['id']}", headers=kh(key_a)).status_code == 404
    other = f"t{uuid.uuid4().hex[:10]}"
    ag[1].state.test_tenants.append(other)
    _, key_o = _key(client, other)
    assert client.delete(f"{UP}/{mine['id']}", headers=kh(key_o)).status_code == 404
    assert client.get(f"{UP}/{mine['id']}", headers=kh(key_a)).status_code == 200  # still there


def test_delete_needs_the_scope_and_credentials(ag, tenant):
    client, app = ag
    assert client.delete(f"{UP}/{uuid.uuid4()}").status_code == 401
    key_id, key = _key(client, tenant)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET scopes = '[]'::jsonb WHERE id = :i"), {"i": key_id})
    assert client.delete(f"{UP}/{uuid.uuid4()}", headers=kh(key)).status_code == 403


def test_a_reused_message_id_does_not_open_another_agents_attachment(ag, tenant, worker):
    """Message ids are client-chosen: agent B's run may carry the same id as agent A's message. B's key
    must still not read what A's run linked under it."""
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    _, key_a = _key(client, tenant, agent_ids=[agent_a])
    _, key_b = _key(client, tenant, agent_ids=[agent_b])
    secret = key_upload(client, key_a).json()
    msg_a = {"id": "same-id", "role": "user", "content": [{"type": "text", "text": "x"}, ref(secret)]}
    assert post(client, agent_a, body("ta", "ra", msg_a), kh(key_a))[0] == 200
    # Agent B has a run whose input message id is the same bare id.
    assert post(client, agent_b, body("tb", "rb", user("same-id", "hello")), kh(key_b))[0] == 200
    assert client.get(f"{UP}/{secret['id']}", headers=kh(key_a)).status_code == 200
    assert client.get(f"{UP}/{secret['id']}", headers=kh(key_b)).status_code == 404


def test_a_key_cannot_link_another_principals_attachment(ag, tenant, worker):
    client, _ = ag
    h = get_auth_header(tenant, "alice")
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    _, key_a = _key(client, tenant, agent_ids=[agent_a])
    _, key_b = _key(client, tenant, agent_ids=[agent_b])
    theirs = key_upload(client, key_a).json()  # unsent, another key's
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, ref(theirs)]}
    status, detail, _ = post(client, agent_b, body("tb", "rb", msg), kh(key_b))
    assert status == 422 and "attachment_not_found" in json.dumps(detail)
    assert client.get(f"{UP}/{theirs['id']}", headers=kh(key_b)).status_code == 404
    # Still unsent and still its owner's: key_a can use it.
    msg_a = {"id": "ua", "role": "user", "content": [{"type": "text", "text": "x"}, ref(theirs)]}
    assert post(client, agent_a, body("ta", "ra", msg_a), kh(key_a))[0] == 200


# --------------------------------------------------------------------------- security of the two routes
# (the per-route sweep in test_agui15_security.py lists the agent and workflow routes only)


def _last_used(app, key_id):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT last_used_at FROM api_keys WHERE id = :i"), {"i": key_id}).scalar()


def _rows(app, tenant):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM chat_attachments WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def test_a_disallowed_origin_is_403_on_both_routes_and_touches_nothing(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    key_id, key = _key(client, tenant, allowed_origins=["https://ok.test"])
    own = client.post(UP, files={"file": ("a.png", PNG, "image/png")}, headers=h).json()  # a dev upload
    evil = kh(key) | {"Origin": "https://evil.test"}
    before = _rows(app, tenant)
    up = client.post(UP, files={"file": ("a.png", PNG, "image/png")}, headers=evil)
    got = client.get(f"{UP}/{own['id']}", headers=evil)
    assert (up.status_code, got.status_code) == (403, 403)
    assert _rows(app, tenant) == before and _last_used(app, key_id) is None
    for refusal in (up, got):  # no key, no storage key, no attachment detail in a refusal
        assert key not in refusal.text and "attachments/" not in refusal.text and own["id"] not in refusal.text
    ok = kh(key) | {"Origin": "https://ok.test"}
    assert client.post(UP, files={"file": ("a.png", PNG, "image/png")}, headers=ok).status_code == 201
    assert _last_used(app, key_id) is not None


def test_a_revoked_key_cannot_upload_or_fetch(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    key_id, key = _key(client, tenant)
    meta = key_upload(client, key).json()
    assert client.delete(f"/api/v1/api-keys/{key_id}", headers=h).status_code == 204
    before = _rows(app, tenant)
    up = key_upload(client, key)
    got = client.get(f"{UP}/{meta['id']}", headers=kh(key))
    gone = client.delete(f"{UP}/{meta['id']}", headers=kh(key))
    assert (up.status_code, got.status_code, gone.status_code) == (401, 401, 401)
    assert up.json() == got.json() == {"detail": "Invalid API key"}
    assert _rows(app, tenant) == before
    for refusal in (up, got, gone):
        assert key not in refusal.text and meta["id"] not in refusal.text


def test_a_foreign_credential_gets_the_same_404_as_an_unknown_id(ag, tenant):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    _, key_a = _key(client, tenant, agent_ids=[agent_a])
    _, key_b = _key(client, tenant, agent_ids=[agent_b])
    other = f"t{uuid.uuid4().hex[:10]}"
    app.state.test_tenants.append(other)
    _, key_o = _key(client, other)
    meta = key_upload(client, key_a).json()
    unknown = client.get(f"{UP}/{uuid.uuid4()}", headers=kh(key_a))
    assert unknown.status_code == 404
    for foreign in (key_b, key_o, None):  # another key, another tenant's key, the dev bearer of another tenant
        headers = kh(foreign) if foreign else get_auth_header(other, "bob")
        got = client.get(f"{UP}/{meta['id']}", headers=headers)
        assert (got.status_code, got.json()) == (404, unknown.json()), foreign
        gone = client.delete(f"{UP}/{meta['id']}", headers=headers)
        assert gone.status_code == 404 and gone.json() == unknown.json()
    assert client.get(f"{UP}/{meta['id']}", headers=kh(key_a)).status_code == 200
