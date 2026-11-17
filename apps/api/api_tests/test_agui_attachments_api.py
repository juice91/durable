"""Attachments for API keys: the AG-UI twins of POST/GET /attachments."""

from __future__ import annotations

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
    # Linked to a message of a conversation of key_a's agent: visible to key_a, still not to key_b.
    msg = {"id": "u1", "role": "user", "content": [{"type": "text", "text": "x"}, ref(dev)]}
    assert post(client, agent_a, body("t1", "r1", msg), kh(key_a))[0] == 200
    assert client.get(f"{UP}/{dev['id']}", headers=kh(key_a)).status_code == 200
    assert client.get(f"{UP}/{dev['id']}", headers=kh(key_b)).status_code == 404
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
