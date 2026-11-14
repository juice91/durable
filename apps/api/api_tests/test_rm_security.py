"""RM.20: security of the RAG and memory API surface, modelled on test_p0_security.py.

- Body caps: every RM write route keeps the default 256 KiB cap (413, stored nothing), and the
  document upload its own EAP_KB_MAX_DOCUMENT_BYTES cap, inclusive at the boundary.
- The request guard on every RM write route: non-UTF-8 and raw NUL bodies 415, nesting over 32
  levels 422, escaped NUL / lone surrogates a compact 422 that never echoes the value; NUL in path
  and query parameters of the RM reads a compact 422.
- Document metadata caps (keys, key charset and length, value types, string length).
- Filter injection: a metadata filter (search body and spec `KnowledgeBaseRef.filter`) only ever
  reaches SQL as bound JSON: keys outside `^[A-Za-z0-9_.-]{1,64}$` are 422, SQL-looking values are
  stored and matched literally (checked through KnowledgeStore.search on embedded chunks), and no
  other row is affected.
- resource_id charset on conversation create and on every route with `{resource_id}` in the path:
  never a 5xx, nothing created or deleted; identity cannot come from the turn body or the request
  context.
- The forget audit record carries a short hash of the resource id, never the id (salted HMAC
  when EAP_AUDIT_HASH_SALT is set), on success, 404 and 409 alike.

Worker-side checks (a model-supplied `knowledge_base` outside the enum, prompt-injected chunk text
and the tenant) are in apps/worker/worker_tests; Studio-side checks are deferred to RM.16–RM.19.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from urllib.parse import quote

import pytest
from sqlalchemy import text

from eap_api.routes.memory import AUDIT_HASH_SALT_ENV, resource_audit_hash
from eap_run_store import KnowledgeStore
from eap_schemas.knowledge import validate_filter

from .rm_world import API, build_tenant, cleanup_rm, ingest_ready, snapshot, unit_vector
from .run_helpers import requires_pg
from .test_conversations import turn
from .test_input_hardening_acceptance import _nested
from .test_knowledge_api import _knowledge_env  # noqa: F401 - autouse fixture
from .test_knowledge_api_acceptance import _raw_body
from .test_runs import SPEC, _no_hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401

pytestmark = requires_pg

BODY_CAP = 256 * 1024
TOO_LARGE = f"request body too large (max {BODY_CAP} bytes)"
NOT_UTF8 = "request body must be UTF-8 encoded JSON (no NUL bytes)"
DEPTH = "request body is nested more than 32 levels deep"


@pytest.fixture
def seeded(pg, tenant_of, fake_dbos):
    """(world tenant, other tenant): both seeded with rm_world's data; cleaned afterwards."""
    client, app = pg
    t = build_tenant(client, app, *tenant_of())
    o = build_tenant(client, app, *tenant_of())
    yield t, o
    cleanup_rm(app.state.database_engine, [t.name, o.name])


def _write_routes(t) -> list[tuple[str, str]]:
    kb = f"{API}/knowledge-bases/{t.kb['id']}"
    return [
        ("POST", f"{API}/knowledge-bases"),
        ("PATCH", kb),
        ("PUT", f"{kb}/documents/guide"),
        ("POST", f"{kb}/documents/guide/reingest"),
        ("POST", f"{kb}/reindex"),
        ("POST", f"{kb}/search?wait=0"),
        ("PUT", f"{API}/conversations/{t.c1}/memory/working"),
        ("PUT", f"{API}/agents/{t.res_agent}/memory/resources/r1/working"),
        ("POST", f"{API}/agents/{t.res_agent}/conversations"),
    ]


def _state(app, tenants):
    return snapshot(app.state.database_engine, tenants)


# ----------------------------------------------------------------------------- body caps


def test_oversize_bodies_on_rm_write_routes_are_413_and_store_nothing(pg, seeded, fake_dbos):
    client, app = pg
    t, o = seeded
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    huge = json.dumps({"name": "n", "embedding_model": "test:hash", "content": "x" * BODY_CAP,
                       "query": "q", "description": "d"}).encode()
    hdrs = {**t.headers, "content-type": "application/json"}
    for method, url in _write_routes(t):
        if url.endswith("/documents/guide"):
            continue  # the upload route has its own cap: next test
        r = client.request(method, url, content=huge, headers=hdrs)
        assert (r.status_code, r.json()) == (413, {"detail": TOO_LARGE}), (method, url, r.status_code)
        anon = client.request(method, url, content=huge, headers={"content-type": "application/json"})
        assert anon.status_code == 413, (method, url)  # size is judged before authentication
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls


def test_document_upload_cap_is_inclusive_and_an_oversize_upload_stores_nothing(pg, seeded, fake_dbos, monkeypatch):
    client, app = pg
    t, o = seeded
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "4000")
    url = f"{API}/knowledge-bases/{t.kb['id']}/documents/cap.txt"
    hdrs = {**t.headers, "content-type": "application/json"}
    before = _state(app, [t.name, o.name])
    for size in (4001, 40_000, 6 * 1024 * 1024):
        r = client.put(url, content=_raw_body(size), headers=hdrs)
        assert r.status_code == 413 and "max 4000 bytes" in r.json()["detail"], (size, r.status_code)
    assert _state(app, [t.name, o.name]) == before
    assert client.put(url, content=_raw_body(4000), headers=hdrs).status_code == 202


# ----------------------------------------------------------------------------- request guard


def test_bad_encodings_nul_and_depth_on_rm_write_routes(pg, seeded, fake_dbos):
    client, app = pg
    t, o = seeded
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    authed = {**t.headers, "content-type": "application/json"}
    payloads = {
        "utf-16": json.dumps({"content": "x"}).encode("utf-16"),
        "latin-1": '{"content": "café"}'.encode("latin-1"),
        "raw NUL": b'{"content": "a\x00b"}',
        "overlong": b'{"content": "\xc0\xaf"}',
        "encoded surrogate": b'{"content": "\xed\xa0\x80"}',
    }
    for method, url in _write_routes(t):
        for label, payload in payloads.items():
            r = client.request(method, url, content=payload, headers=authed)
            assert (r.status_code, r.json()) == (415, {"detail": NOT_UTF8}), (method, url, label)
        deep = client.request(method, url, content=_nested(33), headers=authed)
        assert (deep.status_code, deep.json()) == (422, {"detail": DEPTH}), (method, url)
        for poison in ("a\x00b", "c\ud800d"):
            body = {"content": poison, "name": poison, "query": poison, "title": poison, "resource_id": poison,
                    "embedding_model": poison, "mime_type": "text/plain", "metadata": {"k": poison},
                    "filter": {"k": poison}, "description": poison}
            r = client.request(method, url, content=json.dumps(body).encode(), headers=authed)
            assert r.status_code == 422, (method, url, poison, r.status_code)
            assert all(set(i) == {"loc", "msg", "type"} for i in r.json()["detail"]), (method, url)
            assert poison not in r.text
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls


def test_nul_in_rm_path_and_query_parameters_is_a_compact_422_and_never_5xx(pg, seeded):
    client, app = pg
    t, o = seeded
    before = _state(app, [t.name, o.name])
    kb = f"{API}/knowledge-bases/{t.kb['id']}"
    urls = [
        ("GET", f"{API}/knowledge-bases/a%00b"),
        ("GET", f"{kb}/documents/gu%00ide"),
        ("DELETE", f"{kb}/documents/gu%00ide"),
        ("GET", f"{kb}/documents?status=re%00ady"),
        ("GET", f"{API}/conversations/{t.c1}/memory/observations?include_superseded=%00"),
        ("GET", f"{API}/agents/{t.res_agent}/memory/resources/r%001/working"),
        ("DELETE", f"{API}/agents/{t.res_agent}/memory/resources/r%001"),
        ("DELETE", f"{API}/memory/resources/r%001?include_conversations=true"),
        ("DELETE", f"{API}/memory/resources/r1?include_conversations=tr%00ue"),
    ]
    for method, url in urls:
        r = client.request(method, url, headers=t.headers)
        assert r.status_code == 422, (method, url, r.status_code, r.text[:200])
        detail = r.json()["detail"]
        assert all(set(i) == {"loc", "msg", "type"} and i["type"] == "string_unstorable" for i in detail), detail
        for echoed in ("gu\x00ide", "gu\\u0000ide", "re\\u0000ady", "r\\u00001", "a\\u0000b", "tr\\u0000ue"):
            assert echoed not in r.text, (url, echoed)
        assert client.request(method, url).status_code == 401, (method, url)  # auth first
    assert _state(app, [t.name, o.name]) == before


# ----------------------------------------------------------------------------- metadata


BAD_METADATA = {
    "17 keys": {f"k{i}": i for i in range(17)},
    "nested object": {"team": {"name": "support"}},
    "list value": {"team": ["a", "b"]},
    "null value": {"team": None},
    "257-char string": {"team": "s" * 257},
    "65-char key": {"k" * 65: 1},
    "key with a space": {"a b": 1},
    "SQL in a key": {"team'); DROP TABLE kb_chunks; --": 1},
    "operator key": {"metadata @> '{}'": 1},
    "json path key": {"$.team": 1},
    "arrow key": {"team->>0": 1},
    "empty key": {"": 1},
    "not an object": ["team", "support"],
    "huge integer": {"n": 2**64},
}


@pytest.mark.parametrize("label", list(BAD_METADATA))
def test_bad_document_metadata_is_422_and_stores_nothing(pg, seeded, fake_dbos, label):
    client, app = pg
    t, o = seeded
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    r = client.put(f"{API}/knowledge-bases/{t.kb['id']}/documents/meta.md",
                   json={"content": "# M\n\nbody", "mime_type": "text/markdown", "metadata": BAD_METADATA[label]},
                   headers=t.headers)
    assert r.status_code == 422, (label, r.status_code, r.text[:300])
    assert "DROP TABLE" not in r.text and "s" * 257 not in r.text
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls


def test_metadata_at_every_limit_is_accepted(pg, seeded):
    client, _ = pg
    t, _ = seeded
    meta = {f"k{i}": i for i in range(15)}
    meta["k" * 64] = "s" * 256
    r = client.put(f"{API}/knowledge-bases/{t.kb['id']}/documents/meta.md",
                   json={"content": "# M\n\nbody", "mime_type": "text/markdown", "metadata": meta}, headers=t.headers)
    assert r.status_code == 202, r.text
    assert r.json()["document"]["metadata"] == meta


# ----------------------------------------------------------------------------- filter injection

INJECTED_VALUES = [
    "x'); DROP TABLE kb_chunks; --",
    "support' OR '1'='1",
    '{"team": "support"}',
    "%",
    "\\\"}, \"team\": \"sales",
]
BAD_FILTER_KEYS = ["team'; DROP TABLE kb_chunks; --", "a b", "metadata @> '{}'", "$.team", "team->>0", "\"t\"", ""]


def test_bad_filter_keys_are_422_on_search_and_in_a_spec(pg, seeded, fake_dbos):
    client, app = pg
    t, o = seeded
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    for key in BAD_FILTER_KEYS:
        r = client.post(f"{API}/knowledge-bases/{t.kb['id']}/search?wait=0",
                        json={"query": "printer", "filter": {key: "support"}}, headers=t.headers)
        assert r.status_code == 422, (key, r.status_code, r.text[:300])
        assert "DROP TABLE" not in r.text
        spec = {**SPEC, "knowledge_bases": [{"id": t.kb["id"], "filter": {key: "support"}}]}
        r = client.post(f"{API}/agents", json={"spec": spec}, headers=t.headers)
        assert r.status_code == 422, (key, r.status_code, r.text[:300])
    for bad in ({"team": {"$ne": "x"}}, {"team": []}, {"team": [["a"]]}, {"team": None}, "team = 'x'"):
        r = client.post(f"{API}/knowledge-bases/{t.kb['id']}/search?wait=0",
                        json={"query": "printer", "filter": bad}, headers=t.headers)
        assert r.status_code == 422, (bad, r.status_code)
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls


def test_sql_looking_filter_values_are_stored_literally(pg, seeded, fake_dbos):
    """Accepted values travel as JSON data: the search run's input and the published spec hold
    them byte for byte, and the search run's tenant is the caller's."""
    client, app = pg
    t, _ = seeded
    for value in INJECTED_VALUES:
        r = client.post(f"{API}/knowledge-bases/{t.kb['id']}/search?wait=0",
                        json={"query": "printer", "filter": {"team": [value, "support"]}}, headers=t.headers)
        assert r.status_code == 202, (value, r.status_code, r.text[:300])
        with app.state.database_engine.connect() as conn:
            row = conn.execute(text("SELECT tenant_id, input FROM runs WHERE id = :r"),
                               {"r": r.json()["run_id"]}).one()
        assert row.tenant_id == t.name
        assert json.loads(row.input)["filter"] == {"team": [value, "support"]}
        spec = {**SPEC, "knowledge_bases": [{"id": t.kb["id"], "filter": {"team": value}}]}
        r = client.post(f"{API}/agents", json={"spec": spec}, headers=t.headers)
        assert r.status_code == 201, r.text
        p = client.post(f"{API}/agents/{r.json()['id']}/versions", headers=t.headers)
        assert p.status_code == 201, p.text
        assert p.json()["spec"]["knowledge_bases"][0]["filter"] == {"team": value}


def test_filter_values_reach_the_retrieval_sql_as_bound_json(pg, seeded):
    """KnowledgeStore.search (the SQL `retrieve` and the search tester run) with SQL-looking filter
    values: only a document whose metadata holds exactly that value matches; nothing else in the
    tenant (or the other tenant) is returned or changed."""
    client, app = pg
    t, o = seeded
    engine = app.state.database_engine
    store = KnowledgeStore(engine)
    evil = INJECTED_VALUES[0]
    planted = ingest_ready(store, t.name, t.kb, "evil", ["evil body"], metadata={"team": evil}, seed=40)
    before = _state(app, [t.name, o.name])

    def hits(flt):
        return [h.document_id for h in store.search(t.name, [t.kb["id"]], unit_vector(40), embedding_model="test:hash",
                                                    top_k=20, metadata_filter=validate_filter(flt))]

    assert set(hits({"team": evil})) == {planted["id"]}
    assert hits({"team": "support' OR '1'='1"}) == []
    assert hits({"team": '{"team": "support"}'}) == []
    assert hits({"team": "%"}) == []
    assert set(hits({"team": [evil, "support"]})) == {planted["id"], t.ready_doc["id"]}
    assert set(hits({"team": "support"})) == {t.ready_doc["id"]}
    # The other tenant's identical documents never appear, whatever the filter.
    assert o.ready_doc["id"] not in hits({"team": ["support", "sales", evil]})
    assert _state(app, [t.name, o.name]) == before


# ----------------------------------------------------------------------------- resource_id

BAD_RESOURCE_IDS = {
    "space": "a b",
    "slash": "a/b",
    "backslash": "a\\b",
    "quote": "a'b",
    "semicolon": "a;b",
    "percent": "a%b",
    "hyphen lookalike U+2010": "user‐1",
    "cyrillic a": "аlice",
    "fullwidth": "ａlice",
    "zero width": "ali​ce",
    "accented": "adá",
    "129 chars": "r" * 129,
    "dot": ".",
    "dot dot": "..",
    "newline": "a\nb",
    "tab": "a\tb",
}


@pytest.mark.parametrize("label", list(BAD_RESOURCE_IDS))
def test_bad_resource_ids_never_5xx_and_change_nothing(pg, seeded, fake_dbos, label):
    client, app = pg
    t, o = seeded
    rid = BAD_RESOURCE_IDS[label]
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    r = client.post(f"{API}/agents/{t.res_agent}/conversations", json={"resource_id": rid}, headers=t.headers)
    assert r.status_code == 422, (label, r.status_code, r.text[:300])
    # Dot segments would be normalised away by any HTTP client before they reach the server, so
    # they are sent percent-encoded: the server must still refuse them as resource ids.
    enc = quote(rid, safe="").replace(".", "%2E") if rid.strip(".") == "" else quote(rid, safe="")
    res = f"{API}/agents/{t.res_agent}/memory/resources/{enc}"
    for method, url, body in (
        ("GET", f"{res}/working", None),
        ("PUT", f"{res}/working", {"content": {"name": "x"}}),
        ("DELETE", f"{res}/working", None),
        ("DELETE", res, None),
        ("DELETE", f"{res}?include_conversations=true", None),
        ("DELETE", f"{API}/memory/resources/{enc}", None),
        ("DELETE", f"{API}/memory/resources/{enc}?include_conversations=true", None),
    ):
        r = client.request(method, url, headers=t.headers, **({"json": body} if body is not None else {}))
        assert r.status_code in (404, 405, 422), (label, method, url, r.status_code, r.text[:300])
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls


def test_resource_id_at_128_characters_and_every_allowed_character_works(pg, seeded):
    client, _ = pg
    t, _ = seeded
    for rid in ("r" * 128, "Ab9_.:@-z", "user@example.com", "tenant:user-1"):
        r = client.post(f"{API}/agents/{t.res_agent}/conversations", json={"resource_id": rid}, headers=t.headers)
        assert r.status_code == 201, (rid, r.text)
        assert r.json()["resource_id"] == rid
        r = client.put(f"{API}/agents/{t.res_agent}/memory/resources/{quote(rid, safe='')}/working",
                       json={"content": {"name": "n"}}, headers=t.headers)
        assert r.status_code == 200, (rid, r.text)


def test_identity_never_comes_from_the_turn_body_or_the_request_context(pg, seeded, fake_dbos):
    """A turn cannot move a conversation to another resource: `resource_id` in the turn body or the
    request context is refused, and the conversation's resource is unchanged."""
    client, app = pg
    t, o = seeded
    before, calls = _state(app, [t.name, o.name]), len(fake_dbos.calls)
    for extra in ({"resource_id": "r2"}, {"request_context": {"resource_id": "r2"}},
                  {"request_context": {"user_id": "mallory"}}, {"tenant_id": o.name}):
        r = turn(client, t.headers, t.c3, run_id=str(uuid.uuid4()), input="hi", **extra)
        assert r.status_code == 422, (extra, r.status_code, r.text[:300])
    assert _state(app, [t.name, o.name]) == before and len(fake_dbos.calls) == calls
    # The resource id is fixed at creation: a create with the same conversation id and another
    # resource is a 409.
    r = client.post(f"{API}/agents/{t.res_agent}/conversations", json={"conversation_id": t.c3, "resource_id": "r1"},
                    headers=t.headers)
    assert r.status_code == 409, r.text
    assert client.get(f"{API}/conversations/{t.c3}", headers=t.headers).json()["resource_id"] == "r2"


# ----------------------------------------------------------------------------- forget audit

AUDIT_RID = "ada.lovelace@example.com"


def _all_log_text(caplog) -> str:
    parts = []
    # The test client's own request log (httpx2, client side) prints the URL it sent; every
    # server-side record (eap_api, the audit, uvicorn, sqlalchemy) is checked.
    for rec in (r for r in caplog.records if not r.name.startswith("httpx")):
        parts.append(rec.getMessage())
        parts.append(json.dumps({k: str(v) for k, v in rec.__dict__.items()}, sort_keys=True))
    return "\n".join(parts)


@pytest.mark.parametrize("salt", [None, "pepper-1"])
def test_forget_audit_records_carry_the_resource_hash_never_the_id(pg, seeded, fake_dbos, caplog, monkeypatch, salt):
    client, app = pg
    t, _ = seeded
    if salt is None:
        monkeypatch.delenv(AUDIT_HASH_SALT_ENV, raising=False)
        expected = hashlib.sha256(AUDIT_RID.encode()).hexdigest()[:16]
    else:
        monkeypatch.setenv(AUDIT_HASH_SALT_ENV, salt)
        expected = hmac.new(salt.encode(), AUDIT_RID.encode(), hashlib.sha256).hexdigest()[:16]
    assert resource_audit_hash(AUDIT_RID) == expected
    r = client.post(f"{API}/agents/{t.res_agent}/conversations", json={"resource_id": AUDIT_RID}, headers=t.headers)
    assert r.status_code == 201, r.text
    busy_conv = r.json()["id"]
    caplog.set_level(logging.DEBUG)
    # 409: a queued turn of the resource.
    assert turn(client, t.headers, busy_conv, run_id=str(uuid.uuid4()), input="hi").status_code == 201
    for url in (f"{API}/agents/{t.res_agent}/memory/resources/{AUDIT_RID}", f"{API}/memory/resources/{AUDIT_RID}"):
        r = client.delete(url, headers=t.headers)
        assert r.status_code == 409, (url, r.status_code, r.text[:300])
        assert AUDIT_RID not in r.text
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE tenant_id = :t AND conversation_id = :c"),
                     {"t": t.name, "c": busy_conv})
    # 404: an unknown agent; 200: both forget routes, with and without conversations.
    assert client.delete(f"{API}/agents/{uuid.uuid4()}/memory/resources/{AUDIT_RID}", headers=t.headers).status_code == 404
    for url in (f"{API}/agents/{t.res_agent}/memory/resources/{AUDIT_RID}",
                f"{API}/memory/resources/{AUDIT_RID}?include_conversations=true"):
        r = client.delete(url, headers=t.headers)
        assert r.status_code == 200, (url, r.text)
    logged = _all_log_text(caplog)
    assert AUDIT_RID not in logged and "ada.lovelace" not in logged
    audits = [rec for rec in caplog.records if getattr(rec, "eap.audit", None) == "memory_forget"]
    assert len(audits) >= 4, [rec.getMessage() for rec in caplog.records]
    for rec in audits:
        assert getattr(rec, "eap.resource_hash") == expected
        assert expected in rec.getMessage()
        assert getattr(rec, "eap.tenant_id") == t.name
    outcomes = [getattr(rec, "eap.outcome") for rec in audits]
    assert outcomes.count("busy") == 2 and outcomes.count("done") == 2, outcomes
