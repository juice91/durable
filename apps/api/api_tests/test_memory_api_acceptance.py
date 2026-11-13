"""RM.9 acceptance: cross-tenant and cross-agent isolation on every memory route, malformed ids and
no-echo error bodies, If-Match semantics (including a concurrent pair), schema/template modes and
caps, resource_id immutability, DELETE conversation and forget guarantees, observation deletes.
Complements test_memory_api.py (the implementer's tests); fixtures and helpers are reused from it."""

from __future__ import annotations

import json
import threading
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store.memory import TurnText

from .run_helpers import requires_pg
from .test_conversations import create_conversation, finish, new_conversation, turn
from .test_memory_api import (  # noqa: F401 - fixtures and helpers
    API, SCHEMA, SECRET, completed_turn, conv_wm, count, make_agent, mem_tenant, memory_store, res_wm, vector,
)
from .test_runs import FakeDBOSClient, _no_hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401

pytestmark = requires_pg

TABLES = (
    "conversations", "runs", "execution_events", "side_effects", "run_model_requests", "working_memories",
    "memory_observations", "message_embeddings", "memory_forget_watermarks",
)


def snapshot(app, tenant: str) -> dict[str, list[str]]:
    """Every row of the tenant in each table, as text (embeddings and content included)."""
    out: dict[str, list[str]] = {}
    with app.state.database_engine.connect() as conn:
        for table in TABLES:
            rows = conn.execute(text(f"SELECT t::text FROM {table} t WHERE tenant_id = :t ORDER BY 1"), {"t": tenant})
            out[table] = list(rows.scalars())
    return out


def plant(app, tenant: str, run_id: str, secret: str = SECRET) -> None:
    """Content, an event, a tool-result side effect and a usage row for a run, all carrying `secret`."""
    with app.state.database_engine.begin() as conn:
        p = {"t": tenant, "r": run_id, "s": secret}
        conn.execute(text(
            "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
            " VALUES (:t, :r, 900, 'planted:1', 'tool_result', CAST(:d AS jsonb), now())"
        ), {**p, "d": json.dumps({"result": secret, "text": secret})})
        conn.execute(text(
            "INSERT INTO side_effects (tenant_id, key, state, result, created_at) VALUES (:t, :k, 'completed', CAST(:d AS jsonb), now())"
        ), {"t": tenant, "k": f"{run_id}:tool:0", "d": json.dumps({"out": secret})})
        conn.execute(text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
            " VALUES (:t, :r, 'planted:0', 'test:x', 7, 3, 0, 0, true, now())"
        ), p)
        conn.execute(text("UPDATE runs SET messages = CAST(:d AS jsonb) WHERE id = :r AND tenant_id = :t"),
                     {**p, "d": json.dumps([{"content": secret}])})


class World:
    """Tenant A's data: a conversation-scoped agent (a1) and a resource-scoped schema agent (a2)."""

    def __init__(self, client, app, pg_, tenant: str, h: dict[str, str]):
        self.tenant, self.h = tenant, h
        self.a1 = make_agent(client, h, {"working": {"scope": "conversation"}})
        self.a2 = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        self.c1 = create_conversation(client, h, self.a1, resource_id="r1").json()["id"]
        self.c2 = create_conversation(client, h, self.a2, resource_id="r1").json()["id"]
        self.run1 = completed_turn(client, app, h, self.c1, input=SECRET)
        completed_turn(client, app, h, self.c1)
        self.run2 = completed_turn(client, app, h, self.c2)
        store = memory_store(pg_)
        assert client.put(conv_wm(self.c1), json={"content": SECRET}, headers=h).status_code == 200
        assert client.put(res_wm(self.a2, "r1"), json={"content": {"name": SECRET}}, headers=h).status_code == 200
        self.o1 = store.insert_observation(tenant, self.a1, self.c1, covers_from_turn=1, covers_to_turn=1,
                                           content=SECRET).observation.id
        self.o2 = store.insert_observation(tenant, self.a1, self.c1, covers_from_turn=2, covers_to_turn=2,
                                           content="two").observation.id
        for aid, cid, run in ((self.a1, self.c1, self.run1), (self.a2, self.c2, self.run2)):
            store.index_turn(tenant, aid, cid, run, [TurnText("user", SECRET, vector(5))], embedding_model="test:hash")
        plant(app, tenant, self.run1)


def routes(w: World) -> list[tuple[str, str, dict[str, Any] | None]]:
    obs = f"{API}/conversations/{w.c1}/memory/observations"
    return [
        ("DELETE", f"{API}/conversations/{w.c1}", None),
        ("GET", conv_wm(w.c1), None),
        ("PUT", conv_wm(w.c1), {"content": "x"}),
        ("DELETE", conv_wm(w.c1), None),
        ("GET", f"{API}/agents/{w.a2}/memory/working", None),
        ("GET", res_wm(w.a2, "r1"), None),
        ("PUT", res_wm(w.a2, "r1"), {"content": {"name": "x"}}),
        ("DELETE", res_wm(w.a2, "r1"), None),
        ("GET", obs, None),
        ("GET", obs + "?include_superseded=true", None),
        ("DELETE", obs, None),
        ("DELETE", f"{obs}/{w.o2}", None),
        ("DELETE", f"{obs}/{w.o1}", None),
        ("DELETE", f"{API}/agents/{w.a1}/memory/resources/r1?include_conversations=true", None),
        ("DELETE", f"{API}/agents/{w.a2}/memory/resources/r1", None),
    ]


class TestIsolation:
    def test_every_route_cross_tenant(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        _, other = mem_tenant()
        w = World(client, app, pg, tenant, h)
        before = snapshot(app, tenant)
        assert any(SECRET in row for rows in before.values() for row in rows)
        for method, url, body in routes(w):
            r = client.request(method, url, json=body, headers=other)
            assert r.status_code == 404, (method, url, r.status_code, r.text)
            assert SECRET not in r.text and w.c1 not in r.text and w.a1 not in r.text
        # Tenant-wide forget has no per-tenant id to hide: it only ever sees the caller's own rows.
        r = client.delete(f"{API}/memory/resources/r1?include_conversations=true", headers=other)
        assert r.status_code == 200 and r.json()["working_memories_deleted"] == 0
        assert r.json()["message_embeddings_deleted"] == 0 and r.json()["conversations_deleted"] == []
        assert snapshot(app, tenant) == before

    def test_other_tenant_same_resource_id_is_untouched(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        tb, hb = mem_tenant()
        w = World(client, app, pg, tenant, h)
        wb = World(client, app, pg, tb, hb)
        before_a, before_b = snapshot(app, tenant), snapshot(app, tb)
        assert client.delete(f"{API}/memory/resources/r1?include_conversations=true", headers=hb).status_code == 200
        assert snapshot(app, tenant) == before_a and snapshot(app, tb) != before_b
        assert count(app, "SELECT count(*) FROM conversations WHERE tenant_id = :t", t=tb) == 0
        assert client.get(res_wm(w.a2, "r1"), headers=h).status_code == 200
        assert client.get(res_wm(wb.a2, "r1"), headers=hb).status_code == 404

    def test_cross_agent_same_tenant(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        # a1 has no resource note even though a2 has one for the same resource id.
        assert client.get(res_wm(w.a1, "r1"), headers=h).status_code == 404
        assert client.get(f"{API}/agents/{w.a1}/memory/working?scope=resource", headers=h).json()["items"] == []
        before = snapshot(app, tenant)
        r = client.put(res_wm(w.a1, "r1"), json={"content": "x"}, headers=h)
        assert r.status_code == 422 and "conversation-scoped" in r.json()["detail"]
        assert snapshot(app, tenant) == before
        # Observations are per conversation: c1's observation is not reachable through c2.
        obs2 = f"{API}/conversations/{w.c2}/memory/observations"
        assert client.get(obs2, headers=h).json() == {"items": [], "covered_to_turn": 0}
        assert client.delete(f"{obs2}/{w.o2}", headers=h).status_code == 404
        assert client.delete(obs2, headers=h).json() == {"deleted": 0}
        assert snapshot(app, tenant) == before
        # Forget for a1 alone leaves a2's note, embeddings and conversation.
        r = client.delete(f"{API}/agents/{w.a1}/memory/resources/r1?include_conversations=true", headers=h)
        assert r.status_code == 200 and r.json()["conversations_deleted"] == [w.c1]
        assert client.get(res_wm(w.a2, "r1"), headers=h).status_code == 200
        assert client.get(f"{API}/conversations/{w.c2}", headers=h).status_code == 200
        assert count(app, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND agent_id = :a",
                     t=tenant, a=w.a2) == 1

    def test_observation_route_checks_conversation_ownership(self, pg, mem_tenant):
        """The store only filters by agent; a conversation of another tenant never reaches it."""
        client, app = pg
        tenant, h = mem_tenant()
        tb, hb = mem_tenant()
        w = World(client, app, pg, tenant, h)
        agent_b = make_agent(client, hb)
        cb = new_conversation(client, hb, agent_b)
        before = snapshot(app, tenant)
        # B's own conversation with A's observation id: 404 (not found in B's conversation).
        assert client.delete(f"{API}/conversations/{cb}/memory/observations/{w.o2}", headers=hb).status_code == 404
        # A's conversation with B's credentials, even naming B's agent in other routes: 404.
        assert client.get(f"{API}/conversations/{w.c1}/memory/observations", headers=hb).status_code == 404
        assert client.get(f"{API}/agents/{agent_b}/memory/working", headers=hb).json() == {"items": []}
        assert snapshot(app, tenant) == before


BAD = "bad-" + SECRET


class TestMalformedInputs:
    def test_malformed_ids_404_without_echo(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        before = snapshot(app, tenant)
        urls = [
            ("GET", conv_wm(BAD)), ("PUT", conv_wm(BAD)), ("DELETE", conv_wm(BAD)), ("DELETE", f"{API}/conversations/{BAD}"),
            ("GET", f"{API}/conversations/{BAD}/memory/observations"),
            ("DELETE", f"{API}/conversations/{BAD}/memory/observations"),
            ("DELETE", f"{API}/conversations/{BAD}/memory/observations/{w.o1}"),
            ("DELETE", f"{API}/conversations/{w.c1}/memory/observations/{BAD}"),
            ("GET", f"{API}/agents/{BAD}/memory/working"), ("GET", res_wm(BAD, "r1")), ("PUT", res_wm(BAD, "r1")),
            ("DELETE", res_wm(BAD, "r1")), ("DELETE", f"{API}/agents/{BAD}/memory/resources/r1"),
            ("GET", res_wm(w.a2, "bad id " + SECRET)), ("PUT", res_wm(w.a2, "bad%20id%20" + SECRET)),
            ("DELETE", res_wm(w.a2, "bad%20id%20" + SECRET)),
            ("DELETE", f"{API}/agents/{w.a2}/memory/resources/bad%20id%20{SECRET}"),
            ("DELETE", f"{API}/memory/resources/bad%20id%20{SECRET}"),
            ("DELETE", f"{API}/memory/resources/{'x' * 129}{SECRET}"),
            ("GET", res_wm(w.a2, "a%2Fb" + SECRET)),
        ]
        for method, url in urls:
            body = {"content": {"name": "x"}} if method == "PUT" else None
            r = client.request(method, url, json=body, headers=h)
            assert r.status_code in (404, 422), (method, url, r.status_code)
            assert SECRET not in r.text, (method, url, r.text)
        assert snapshot(app, tenant) == before

    @pytest.mark.parametrize("path, query", [
        ("/agents/{a2}/memory/working", "scope=" + SECRET), ("/agents/{a2}/memory/working", "limit=" + SECRET),
        ("/agents/{a2}/memory/working", "limit=0"), ("/agents/{a2}/memory/working", "limit=501"),
        ("/conversations/{c1}/memory/observations", "include_superseded=" + SECRET),
        ("/memory/resources/r1", "include_conversations=" + SECRET),
    ])
    def test_bad_query_is_422_without_echo(self, pg, mem_tenant, path, query):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        url = API + path.format(a2=w.a2, c1=w.c1) + "?" + query
        r = client.get(url, headers=h) if "working" in path or "observations" in path else client.delete(url, headers=h)
        assert r.status_code == 422, r.text
        assert SECRET not in r.text, r.text

    def test_bad_bodies_422_without_echo(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        before = snapshot(app, tenant)
        url = res_wm(w.a2, "r1")
        for body in ({}, {"content": 5}, {"content": [SECRET]}, {"content": None, "extra": SECRET}):
            r = client.put(url, json=body, headers=h)
            assert r.status_code == 422 and SECRET not in r.text, (body, r.text)
        r = client.put(url, content=b'{"content": "' + SECRET.encode() + b'"', headers={**h, "Content-Type": "application/json"})
        assert r.status_code == 422 and SECRET not in r.text, r.text
        assert snapshot(app, tenant) == before

    def test_if_match_garbage_is_422_without_echo(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        before = snapshot(app, tenant)
        for bad in (SECRET, "-1", "1.5", "1,2", "*", "1" * 10, "W/", ""):
            for r in (client.put(res_wm(w.a2, "r1"), json={"content": {"name": "x"}}, headers={**h, "If-Match": bad}),
                      client.delete(res_wm(w.a2, "r1"), headers={**h, "If-Match": bad})):
                assert r.status_code == 422 and SECRET not in r.text, (bad, r.status_code, r.text)
        assert snapshot(app, tenant) == before

    def test_if_match_unbalanced_quote_is_422(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        for bad in ('"1', '1"', 'W/"1'):
            r = client.put(res_wm(aid, "r1"), json={"content": {"name": "x"}}, headers={**h, "If-Match": bad})
            assert r.status_code == 422, (bad, r.status_code)

    def test_top_level_unknown_body_key_is_422(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        r = client.put(res_wm(aid, "r1"), json={"content": {"name": "x"}, "extra": 1}, headers=h)
        assert r.status_code == 422


STRICT = {**SCHEMA, "additionalProperties": False}


def race(fns):
    """Run the callables at the same moment; return their results."""
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)

    def run(i, fn):
        barrier.wait()
        out[i] = fn()

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return out


class TestWorkingMemoryRules:
    def test_if_match_versions_on_resource_route(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        url = res_wm(aid, "u1")
        put = lambda n, im=None: client.put(url, json={"content": {"name": n}}, headers={**h, **({"If-Match": im} if im else {})})
        assert put("a", "0").status_code == 200
        r = put("b", "0")
        assert r.status_code == 409 and "version 1" in r.json()["detail"]
        assert put("b", "7").status_code == 409
        assert client.get(url, headers=h).json()["content"] == {"name": "a"}
        for n, im in (("c", "1"), ("d", '"2"'), ("e", 'W/"3"')):
            r = put(n, im)
            assert r.status_code == 200 and r.headers["ETag"] == f'"{r.json()["version"]}"', r.text
        assert client.get(url, headers=h).json()["version"] == 4
        assert put("f").json()["version"] == 5  # no If-Match: unconditional
        assert client.delete(url, headers={**h, "If-Match": "4"}).status_code == 409
        assert client.get(url, headers=h).status_code == 200
        assert client.delete(url, headers={**h, "If-Match": '"5"'}).status_code == 204
        assert client.get(url, headers=h).status_code == 404
        assert put("g", "0").json()["version"] == 1  # the note may be created again

    def test_concurrent_puts_same_if_match_one_wins(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        url = res_wm(aid, "u1")
        assert client.put(url, json={"content": {"name": "start"}}, headers=h).status_code == 200
        res = race([lambda i=i: client.put(url, json={"content": {"name": f"w{i}"}}, headers={**h, "If-Match": "1"})
                    for i in range(4)])
        assert sorted(r.status_code for r in res) == [200, 409, 409, 409]
        winner = next(r for r in res if r.status_code == 200).json()["content"]
        got = client.get(url, headers=h).json()
        assert got["version"] == 2 and got["content"] == winner

    def test_concurrent_create_only_one_wins(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        cid = new_conversation(client, h, aid)
        res = race([lambda i=i: client.put(conv_wm(cid), json={"content": f"w{i}"}, headers={**h, "If-Match": "0"})
                    for i in range(4)])
        assert sorted(r.status_code for r in res) == [200, 409, 409, 409]
        assert client.get(conv_wm(cid), headers=h).json()["version"] == 1

    def test_template_vs_schema_mode(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        schema_agent = make_agent(client, h, {"working": {"json_schema": STRICT}})
        tmpl_agent = make_agent(client, h, {"working": {"template": "# Notes\n-"}})
        r = client.put(res_wm(schema_agent, "u"), json={"content": "plain text " + SECRET}, headers=h)
        assert r.status_code == 422 and "object" in r.json()["detail"] and SECRET not in r.text
        r = client.put(res_wm(tmpl_agent, "u"), json={"content": {"name": SECRET}}, headers=h)
        assert r.status_code == 422 and "string" in r.json()["detail"] and SECRET not in r.text
        ok = client.put(res_wm(tmpl_agent, "u"), json={"content": "# Notes\n- fine"}, headers=h)
        assert ok.status_code == 200 and ok.json()["format"] == "markdown"
        assert client.get(res_wm(schema_agent, "u"), headers=h).status_code == 404

    @pytest.mark.parametrize("content", [
        {"age": "old"}, {"age": 1.5}, {"prefs": {"tone": "rude"}}, {"name": 5}, {"prefs": "x"}, {"model_key_" + SECRET: 1},
    ])
    def test_schema_violations_422_path_only(self, pg, mem_tenant, content):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": STRICT}})
        r = client.put(res_wm(aid, "u"), json={"content": {**content, "name": content.get("name", "n")}}, headers=h)
        assert r.status_code == 422 and SECRET not in r.text and "rude" not in r.text and "old" not in r.text, r.text
        assert client.get(res_wm(aid, "u"), headers=h).status_code == 404


def nested(depth: int, leaf: Any) -> Any:
    value = leaf
    for _ in range(depth):
        value = {"k": value}
    return value


class TestWorkingMemoryCaps:
    def test_size_cap_schema_mode_422_no_echo(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        r = client.put(res_wm(aid, "u"), json={"content": {"name": SECRET + "x" * (17 * 1024)}}, headers=h)
        assert r.status_code == 422 and SECRET not in r.text and "xxxxxxxx" not in r.text, r.text[:300]
        assert count(app, "SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=tenant) == 0
        edge = client.put(res_wm(aid, "u"), json={"content": {"name": "y" * 15000}}, headers=h)
        assert edge.status_code == 200  # under the 16 KiB cap

    def test_size_cap_template_mode_resource_route(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {}})
        r = client.put(res_wm(aid, "u"), json={"content": SECRET + "x" * (17 * 1024)}, headers=h)
        assert r.status_code == 422 and SECRET not in r.text and "xxxxxxxx" not in r.text
        assert client.get(res_wm(aid, "u"), headers=h).status_code == 404

    @pytest.mark.parametrize("depth", [17, 40, 500, 3000])
    def test_depth_cap_422_no_echo_no_crash(self, pg, mem_tenant, depth):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        raw = '{"content": {"name": "n", "deep": ' + '{"k": ' * depth + '"' + SECRET + '"' + "}" * depth + "}}"
        r = client.put(res_wm(aid, "u"), content=raw, headers={**h, "Content-Type": "application/json"})
        assert r.status_code == 422, (depth, r.status_code, r.text[:200])
        assert SECRET not in r.text
        assert client.get(res_wm(aid, "u"), headers=h).status_code == 404

    def test_list_limit_scope_and_order(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "resource"}})
        for k in ("a", "b", "c"):
            assert client.put(res_wm(aid, k), json={"content": f"note {k}"}, headers=h).status_code == 200
        url = f"{API}/agents/{aid}/memory/working"
        assert [n["scope_key"] for n in client.get(url + "?limit=2", headers=h).json()["items"]] == ["c", "b"]
        assert len(client.get(url + "?limit=500", headers=h).json()["items"]) == 3
        assert client.get(url + "?scope=conversation", headers=h).json()["items"] == []


class TestResourceIdRules:
    def test_default_is_caller_user_id_and_per_user(self, pg, mem_tenant):
        client, _ = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h)
        bob = {"Authorization": f"Bearer dev:{tenant}:bob_2"}
        assert create_conversation(client, bob, aid).json()["resource_id"] == "bob_2"
        assert create_conversation(client, h, aid).json()["resource_id"] == "alice"

    def test_defaulted_value_is_immutable_too(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h)
        cid = str(uuid.uuid4())
        assert create_conversation(client, h, aid, conversation_id=cid).json()["resource_id"] == "alice"
        assert create_conversation(client, h, aid, conversation_id=cid).status_code == 200
        r = create_conversation(client, h, aid, conversation_id=cid, resource_id="mallory")
        assert r.status_code == 409 and "mallory" not in r.text
        assert client.get(f"{API}/conversations/{cid}", headers=h).json()["resource_id"] == "alice"

    @pytest.mark.parametrize("bad", ["a b", "é", "x" * 129, "a/b", "a\nb", " ", "<script>", "a;b", None.__class__.__name__ + "?"])
    def test_invalid_resource_id_422_no_echo_and_nothing_created(self, pg, mem_tenant, bad):
        client, _ = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h)
        r = create_conversation(client, h, aid, resource_id=bad + SECRET)
        assert r.status_code == 422 and SECRET not in r.text and bad + SECRET not in r.text
        assert client.get(f"{API}/conversations", headers=h).json()["items"] == []

    def test_resource_id_types_and_same_id_other_agent(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        a1, a2 = make_agent(client, h), make_agent(client, h)
        for bad in (5, ["x"], {"a": SECRET}, True):
            r = create_conversation(client, h, a1, resource_id=bad)
            assert r.status_code == 422 and SECRET not in r.text
        cid = str(uuid.uuid4())
        assert create_conversation(client, h, a1, conversation_id=cid, resource_id="r").status_code == 201
        # Another agent reusing the id is the existing other-agent conflict, never a silent success.
        r = create_conversation(client, h, a2, conversation_id=cid, resource_id="r")
        assert r.status_code in (404, 409), r.text


def run_status(app, run_id: str, status: str) -> None:
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = :s WHERE id = :r"), {"s": status, "r": run_id})


def rows_with_secret(app, tenant: str) -> dict[str, int]:
    return {t: sum(SECRET in r for r in rows) for t, rows in snapshot(app, tenant).items() if t != "working_memories"}


class TestDeleteConversationGuarantees:
    @pytest.mark.parametrize("status", ["queued", "running"])
    def test_busy_turn_409_nothing_changes_then_204(self, pg, mem_tenant, status):
        client, app = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        cid = new_conversation(client, h, aid)
        completed_turn(client, app, h, cid)
        run_id = str(uuid.uuid4())
        assert turn(client, h, cid, run_id=run_id, input=SECRET).status_code == 201
        run_status(app, run_id, status)
        assert client.put(conv_wm(cid), json={"content": "note"}, headers=h).status_code == 200
        before = snapshot(app, tenant)
        r = client.delete(f"{API}/conversations/{cid}", headers=h)
        assert r.status_code == 409 and int(r.headers["Retry-After"]) > 0 and SECRET not in r.text
        assert snapshot(app, tenant) == before
        finish(app, run_id)
        assert client.delete(f"{API}/conversations/{cid}", headers=h).status_code == 204

    def test_content_gone_usage_and_runs_remain(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        run_ids = [w.run1, w.run2]
        usage_before = count(app, "SELECT count(*) FROM run_model_requests WHERE tenant_id = :t AND run_id = :r",
                             t=tenant, r=w.run1)
        events_before = count(app, "SELECT count(*) FROM execution_events WHERE tenant_id = :t AND run_id = :r",
                              t=tenant, r=w.run1)
        assert usage_before == 1 and rows_with_secret(app, tenant)["execution_events"] >= 1
        assert rows_with_secret(app, tenant)["side_effects"] == 1 and rows_with_secret(app, tenant)["runs"] >= 1
        assert count(app, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c",
                     t=tenant, c=w.c1) == 1
        assert client.delete(f"{API}/conversations/{w.c1}", headers=h).status_code == 204
        found = rows_with_secret(app, tenant)
        assert found["execution_events"] == found["side_effects"] == found["runs"] == found["memory_observations"] == 0
        assert found["message_embeddings"] == 1  # only c2's own turn text remains
        for table in ("memory_observations", "message_embeddings"):
            assert count(app, f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND conversation_id = :c",
                         t=tenant, c=w.c1) == 0
        assert count(app, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope_key = :c",
                     t=tenant, c=w.c1) == 0
        assert count(app, "SELECT count(*) FROM run_model_requests WHERE tenant_id = :t AND run_id = :r",
                     t=tenant, r=w.run1) == usage_before
        assert count(app, "SELECT count(*) FROM execution_events WHERE tenant_id = :t AND run_id = :r",
                     t=tenant, r=w.run1) == events_before  # scrubbed, not deleted
        assert count(app, "SELECT count(*) FROM runs WHERE tenant_id = :t AND id = ANY(:ids) AND status = 'completed'",
                     t=tenant, ids=run_ids) == 2
        # Other conversations are untouched.
        assert client.get(f"{API}/conversations/{w.c2}", headers=h).status_code == 200
        assert client.get(res_wm(w.a2, "r1"), headers=h).status_code == 200
        assert client.get(conv_wm(w.c1), headers=h).status_code == 404
        assert client.post(f"{API}/conversations/{w.c1}/runs", json={"run_id": str(uuid.uuid4()), "input": "x"},
                           headers=h).status_code == 404

    def test_watermark_goes_with_the_conversation(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        assert client.delete(f"{API}/agents/{w.a1}/memory/resources/r1", headers=h).status_code == 200
        wm = "SELECT count(*) FROM memory_forget_watermarks WHERE tenant_id = :t AND conversation_id = :c"
        assert count(app, wm, t=tenant, c=w.c1) == 1
        assert client.delete(f"{API}/conversations/{w.c1}", headers=h).status_code == 204
        assert count(app, wm, t=tenant, c=w.c1) == 0


MEMORY_TABLES = ("working_memories", "memory_observations", "message_embeddings")


def resource_rows(app, tenant: str, rid: str) -> dict[str, int]:
    """Rows of every memory table that belong to the resource (notes, observations through its conversations)."""
    q = {
        "working_memories": "SELECT count(*) FROM working_memories w WHERE tenant_id = :t AND (w.scope_key = :k OR"
        " w.conversation_id IN (SELECT id FROM conversations WHERE tenant_id = :t AND resource_id = :k))",
        "memory_observations": "SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND conversation_id IN"
        " (SELECT id FROM conversations WHERE tenant_id = :t AND resource_id = :k)",
        "message_embeddings": "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = :k",
    }
    return {name: count(app, sql, t=tenant, k=rid) for name, sql in q.items()}


class TestForgetGuarantees:
    def test_forget_user_leaves_no_trace_and_other_resource_stays(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        other_cid = create_conversation(client, h, w.a1, resource_id="r2").json()["id"]
        other_run = completed_turn(client, app, h, other_cid)
        memory_store(pg).index_turn(tenant, w.a1, other_cid, other_run, [TurnText("user", "keep", vector(9))],
                                    embedding_model="test:hash")
        assert client.put(conv_wm(other_cid), json={"content": "keep"}, headers=h).status_code == 200
        assert all(v > 0 for v in resource_rows(app, tenant, "r1").values())
        kept = resource_rows(app, tenant, "r2")
        r = client.delete(f"{API}/memory/resources/r1?include_conversations=true", headers=h)
        assert r.status_code == 200, r.text
        assert sorted(r.json()["conversations_deleted"]) == sorted([w.c1, w.c2])
        assert resource_rows(app, tenant, "r1") == {t: 0 for t in MEMORY_TABLES}
        assert resource_rows(app, tenant, "r2") == kept and kept["message_embeddings"] == 1
        found = rows_with_secret(app, tenant)
        assert found["execution_events"] == found["side_effects"] == found["runs"] == found["message_embeddings"] == 0
        assert count(app, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND content::text LIKE :s",
                     t=tenant, s=f"%{SECRET}%") == 0
        again = client.delete(f"{API}/memory/resources/r1?include_conversations=true", headers=h)
        assert again.status_code == 200 and again.json()["conversations_deleted"] == []
        assert again.json()["working_memories_deleted"] == again.json()["message_embeddings_deleted"] == 0

    def test_without_conversations_keeps_them_and_new_turn_works(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        store = memory_store(pg)
        r = client.delete(f"{API}/agents/{w.a1}/memory/resources/r1", headers=h)
        assert r.status_code == 200 and r.json()["conversations_deleted"] == []
        assert r.json()["message_embeddings_deleted"] == 1 and r.json()["working_memories_deleted"] == 0
        assert client.get(f"{API}/conversations/{w.c1}", headers=h).status_code == 200
        assert len(client.get(f"{API}/conversations/{w.c1}/memory/observations", headers=h).json()["items"]) == 2
        assert client.get(conv_wm(w.c1), headers=h).status_code == 200  # conversation-scoped note stays
        # A recovered step for a pre-forget turn writes nothing; a new turn indexes normally.
        late = store.index_turn(tenant, w.a1, w.c1, w.run1, [TurnText("user", "late", vector(7))], embedding_model="test:hash")
        assert late.skipped is True
        new_run = completed_turn(client, app, h, w.c1, input="after forget")
        fresh = store.index_turn(tenant, w.a1, w.c1, new_run, [TurnText("user", "fresh", vector(8))], embedding_model="test:hash")
        assert fresh.skipped is False
        assert count(app, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c",
                     t=tenant, c=w.c1) == 1
        twice = client.delete(f"{API}/agents/{w.a1}/memory/resources/r1", headers=h).json()
        assert twice["message_embeddings_deleted"] == 1  # the fresh turn, forgotten again
        assert client.delete(f"{API}/agents/{w.a1}/memory/resources/r1", headers=h).json()["message_embeddings_deleted"] == 0

    @pytest.mark.parametrize("path, include", [
        ("/memory/resources/r1", "true"), ("/memory/resources/r1", "false"),
        ("/agents/{a1}/memory/resources/r1", "true"), ("/agents/{a1}/memory/resources/r1", "false"),
    ])
    def test_busy_is_all_or_nothing(self, pg, mem_tenant, path, include):
        client, app = pg
        tenant, h = mem_tenant()
        w = World(client, app, pg, tenant, h)
        busy_run = str(uuid.uuid4())
        assert turn(client, h, w.c1, run_id=busy_run).status_code == 201  # c1 is busy, c2 is idle
        before = snapshot(app, tenant)
        r = client.delete(f"{API}{path.format(a1=w.a1)}?include_conversations={include}", headers=h)
        assert r.status_code == 409 and int(r.headers["Retry-After"]) > 0, r.text
        assert r.json()["busy_conversations"] == [w.c1] and SECRET not in r.text
        assert snapshot(app, tenant) == before
        finish(app, busy_run)
        assert client.delete(f"{API}{path.format(a1=w.a1)}?include_conversations={include}", headers=h).status_code == 200


class TestObservationDeletes:
    def seed(self, client, app, pg_, tenant, h):
        aid = make_agent(client, h)
        cid = new_conversation(client, h, aid)
        for _ in range(3):
            completed_turn(client, app, h, cid)
        store = memory_store(pg_)
        ids = [store.insert_observation(tenant, aid, cid, covers_from_turn=i, covers_to_turn=i, content=f"o{i}")
               .observation.id for i in (1, 2, 3)]
        return aid, cid, ids, store

    def test_superseded_and_reflection_rules(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        aid, cid, (o1, o2, o3), store = self.seed(client, app, pg, tenant, h)
        refl = store.reflect(tenant, aid, cid, supersede_ids=[o1, o2], content="refl").observation.id
        url = f"{API}/conversations/{cid}/memory/observations"
        active = client.get(url, headers=h).json()
        assert [o["id"] for o in active["items"]] == [refl, o3] and active["covered_to_turn"] == 3
        everything = client.get(url + "?include_superseded=true", headers=h).json()["items"]
        assert {o["id"] for o in everything} == {o1, o2, refl, o3}
        assert {o["id"] for o in everything if o["superseded_by"] == refl} == {o1, o2}
        before = snapshot(app, tenant)
        for oid in (o1, o2, refl):  # superseded rows and a reflection that is not the newest
            r = client.delete(f"{url}/{oid}", headers=h)
            assert r.status_code == 409 and "newest" in r.json()["detail"], (oid, r.text)
        assert snapshot(app, tenant) == before
        assert client.delete(f"{url}/{o3}", headers=h).status_code == 204
        assert client.get(url, headers=h).json()["covered_to_turn"] == 2
        assert client.delete(f"{url}/{refl}", headers=h).status_code == 204  # now the newest: takes its sources too
        assert client.get(url + "?include_superseded=true", headers=h).json() == {"items": [], "covered_to_turn": 0}
        assert client.delete(f"{url}/{refl}", headers=h).status_code == 404

    def test_not_newest_409_has_no_content_and_newest_204(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        _, cid, (o1, o2, o3), _ = self.seed(client, app, pg, tenant, h)
        url = f"{API}/conversations/{cid}/memory/observations"
        r = client.delete(f"{url}/{o2}", headers=h)
        assert r.status_code == 409 and "o2" not in r.text and "o1" not in r.text
        assert client.delete(f"{url}/{o3}", headers=h).status_code == 204
        assert client.delete(f"{url}/{o2}", headers=h).status_code == 204
        assert client.delete(f"{url}/{o1}", headers=h).status_code == 204
        assert client.delete(url, headers=h).json() == {"deleted": 0}

    def test_observation_ids_are_per_conversation_and_tenant(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        _, cid, (_, _, o3), _ = self.seed(client, app, pg, tenant, h)
        _, cid2, _, _ = self.seed(client, app, pg, tenant, h)
        before = snapshot(app, tenant)
        assert client.delete(f"{API}/conversations/{cid2}/memory/observations/{o3}", headers=h).status_code == 404
        assert client.delete(f"{API}/conversations/{cid2}/memory/observations/{str(o3).upper()}x", headers=h).status_code == 404
        assert snapshot(app, tenant) == before
