"""Follow-up 4: GET .../memory/working?missing=null answers 200 `null` (ETag "0") for an absent
note, so a client probing on every chat load gets no failed request; the default stays 404."""

from __future__ import annotations

import uuid

from .run_helpers import requires_pg
from .test_conversations import new_conversation
from .test_memory_api import API, conv_wm, make_agent, mem_tenant, res_wm  # noqa: F401 - fixture
from .test_runs import FakeDBOSClient, _no_hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

pytestmark = requires_pg


def test_conversation_note_missing_null(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    cid = new_conversation(client, h, make_agent(client, h, {"working": {"scope": "conversation"}}))
    # Default: 404, as before.
    assert client.get(conv_wm(cid), headers=h).status_code == 404
    assert client.get(conv_wm(cid), params={"missing": "404"}, headers=h).status_code == 404
    r = client.get(conv_wm(cid), params={"missing": "null"}, headers=h)
    assert (r.status_code, r.json(), r.headers["etag"]) == (200, None, '"0"')
    # PUT with If-Match: 0 (the ETag above) creates it; then the note itself is returned.
    assert client.put(conv_wm(cid), json={"content": "hi"}, headers={**h, "If-Match": r.headers["etag"]}).status_code == 200
    r = client.get(conv_wm(cid), params={"missing": "null"}, headers=h)
    assert r.status_code == 200 and r.json()["content"] == "hi" and r.headers["etag"] == '"1"'


def test_resource_note_missing_null(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    aid = make_agent(client, h, {"working": {}})
    assert client.get(res_wm(aid, "alice"), headers=h).status_code == 404
    r = client.get(res_wm(aid, "alice"), params={"missing": "null"}, headers=h)
    assert (r.status_code, r.json(), r.headers["etag"]) == (200, None, '"0"')
    assert client.put(res_wm(aid, "alice"), json={"content": "x"}, headers=h).status_code == 200
    assert client.get(res_wm(aid, "alice"), params={"missing": "null"}, headers=h).json()["content"] == "x"


def test_missing_null_never_hides_an_unknown_or_foreign_owner(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    _, other = mem_tenant()
    aid = make_agent(client, h, {"working": {"scope": "conversation"}})
    cid = new_conversation(client, h, aid)
    q = {"missing": "null"}
    assert client.get(conv_wm(cid), params=q, headers=other).status_code == 404
    assert client.get(conv_wm(str(uuid.uuid4())), params=q, headers=h).status_code == 404
    assert client.get(conv_wm("not-a-uuid"), params=q, headers=h).status_code == 404
    assert client.get(res_wm(aid, "alice"), params=q, headers=other).status_code == 404
    assert client.get(res_wm(str(uuid.uuid4()), "alice"), params=q, headers=h).status_code == 404


def test_missing_rejects_other_values(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    aid = make_agent(client, h, {"working": {}})
    assert client.get(res_wm(aid, "alice"), params={"missing": "empty"}, headers=h).status_code == 422


def test_the_agent_list_route_is_already_200_when_empty(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    aid = make_agent(client, h, {"working": {}})
    r = client.get(f"{API}/agents/{aid}/memory/working", headers=h)
    assert (r.status_code, r.json()) == (200, {"items": []})
