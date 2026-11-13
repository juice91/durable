"""Memory and conversation API (RAG and memory milestone, RM.9): resource_id on conversations,
DELETE /conversations/{id}, working memory per conversation and per resource with If-Match,
observations, forget-resource per agent and tenant-wide. Same Postgres app and fake DBOS client as
test_runs.py; memory rows are seeded through MemoryStore."""

from __future__ import annotations

import hashlib
import hmac
import math
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_run_store import MemoryStore
from eap_run_store.memory import TurnText

from eap_api.routes.memory import resource_audit_hash

from .run_helpers import requires_pg
from .test_conversations import create_conversation, finish, new_conversation, turn
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    FakeDBOSClient,
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

API = "/api/v1"
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "prefs": {"type": "object", "properties": {"tone": {"enum": ["formal", "casual"]}}},
    },
}
SECRET = "s3cret-value-xyz"


@pytest.fixture
def mem_tenant(pg, tenant_of):
    """tenant_of, plus a teardown that removes the tenant's memory rows before the shared cleanup
    (resource notes have no FK to agents; observations reference runs without a cascade)."""
    made: list[str] = []

    def make() -> tuple[str, dict[str, str]]:
        t, h = tenant_of()
        made.append(t)
        return t, h

    yield make
    with pg[1].state.database_engine.begin() as conn:
        for t in made:
            for table in ("message_embeddings", "memory_observations", "working_memories", "memory_forget_watermarks"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})


def memory_store(pg) -> MemoryStore:
    return MemoryStore(pg[1].state.database_engine)


def make_agent(client: TestClient, h: dict[str, str], memory: dict[str, Any] | None = None, publish: bool = True) -> str:
    spec = {**SPEC, **({"memory": memory} if memory is not None else {})}
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    if publish:
        assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


def conv_wm(cid: str) -> str:
    return f"{API}/conversations/{cid}/memory/working"


def res_wm(aid: str, rid: str) -> str:
    return f"{API}/agents/{aid}/memory/resources/{rid}/working"


def completed_turn(client, app, h, cid: str, input: str = "hello") -> str:
    run_id = str(uuid.uuid4())
    r = turn(client, h, cid, run_id=run_id, input=input)
    assert r.status_code == 201, r.text
    finish(app, run_id)
    return run_id


def vector(seed: int, dims: int = 1536) -> list[float]:
    v = [math.sin(seed + i) for i in range(dims)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def count(app, sql: str, **params: Any) -> int:
    with app.state.database_engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


# ------------------------------------------------------------------------------- resource_id


class TestResourceId:
    def test_defaults_to_user_and_is_returned(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h)
        r = create_conversation(client, h, aid)
        assert r.status_code == 201 and r.json()["resource_id"] == "alice"
        cid = r.json()["id"]
        assert client.get(f"{API}/conversations/{cid}", headers=h).json()["resource_id"] == "alice"
        assert client.get(f"{API}/conversations", headers=h).json()["items"][0]["resource_id"] == "alice"

    def test_explicit_retry_and_mismatch(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h)
        cid = str(uuid.uuid4())
        r = create_conversation(client, h, aid, conversation_id=cid, resource_id="end-user:42")
        assert r.status_code == 201 and r.json()["resource_id"] == "end-user:42"
        again = create_conversation(client, h, aid, conversation_id=cid, resource_id="end-user:42")
        assert again.status_code == 200
        other = create_conversation(client, h, aid, conversation_id=cid, resource_id="end-user:43")
        assert other.status_code == 409 and "different resource_id" in other.json()["detail"]
        # Omitted = the caller's user id, which differs from the stored resource.
        assert create_conversation(client, h, aid, conversation_id=cid).status_code == 409

    @pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "slash/no"])
    def test_invalid_resource_id_is_422_without_echo(self, pg, mem_tenant, bad):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h)
        r = create_conversation(client, h, aid, resource_id=bad)
        assert r.status_code == 422
        if bad:
            assert bad not in r.text


# ---------------------------------------------------------------------- DELETE /conversations


class TestDeleteConversation:
    def test_deletes_memory_keeps_scrubbed_runs(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        cid = new_conversation(client, h, aid)
        run1 = completed_turn(client, app, h, cid, input=SECRET)
        run2 = completed_turn(client, app, h, cid)
        store = memory_store(pg)
        assert client.put(conv_wm(cid), json={"content": "note"}, headers=h).status_code == 200
        store.update_working_memory(tenant, aid, scope="resource", scope_key="alice", content="keep me")
        store.insert_observation(tenant, aid, cid, covers_from_turn=1, covers_to_turn=1, content="obs", run_id=run1)
        store.index_turn(
            tenant, aid, cid, run2,
            [TurnText("user", "hello", vector(1)), TurnText("assistant", "ok", vector(2))],
            embedding_model="test:hash",
        )

        r = client.delete(f"{API}/conversations/{cid}", headers=h)
        assert r.status_code == 204, r.text
        for table in ("memory_observations", "message_embeddings"):
            assert count(app, f"SELECT count(*) FROM {table} WHERE tenant_id = :t", t=tenant) == 0
        assert count(
            app, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'conversation'", t=tenant
        ) == 0
        assert count(app, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource'", t=tenant) == 1
        assert count(
            app,
            "SELECT count(*) FROM runs WHERE tenant_id = :t AND id = ANY(:ids) AND input IS NULL AND output IS NULL"
            " AND conversation_id IS NULL",
            t=tenant, ids=[run1, run2],
        ) == 2
        assert count(
            app, "SELECT count(*) FROM execution_events WHERE run_id = :r AND data::text LIKE :s", r=run1, s=f"%{SECRET}%"
        ) == 0
        assert client.get(f"{API}/conversations/{cid}", headers=h).status_code == 404
        # Repeated: nothing left to delete.
        assert client.delete(f"{API}/conversations/{cid}", headers=h).status_code == 404

    def test_cross_tenant_malformed_unknown_404(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        _, other = mem_tenant()
        cid = new_conversation(client, h, make_agent(client, h))
        assert client.delete(f"{API}/conversations/{cid}", headers=other).status_code == 404
        assert client.delete(f"{API}/conversations/not-a-uuid", headers=h).status_code == 404
        assert client.delete(f"{API}/conversations/{uuid.uuid4()}", headers=h).status_code == 404
        assert client.get(f"{API}/conversations/{cid}", headers=h).status_code == 200

    def test_busy_turn_is_409_with_retry_after(self, pg, mem_tenant):
        client, app = pg
        _, h = mem_tenant()
        cid = new_conversation(client, h, make_agent(client, h))
        run_id = str(uuid.uuid4())
        assert turn(client, h, cid, run_id=run_id).status_code == 201
        r = client.delete(f"{API}/conversations/{cid}", headers=h)
        assert r.status_code == 409 and r.headers["Retry-After"] and "Retry" in r.json()["detail"]
        finish(app, run_id)
        assert client.delete(f"{API}/conversations/{cid}", headers=h).status_code == 204


# --------------------------------------------------------------------------- working memory


class TestConversationWorkingMemory:
    def test_put_get_if_match_delete(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation", "template": "# Notes\n-"}})
        cid = new_conversation(client, h, aid)
        assert client.get(conv_wm(cid), headers=h).status_code == 404

        r = client.put(conv_wm(cid), json={"content": "# Notes\n- likes tea"}, headers={**h, "If-Match": "0"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"] == 1 and body["format"] == "markdown" and body["scope"] == "conversation"
        assert body["scope_key"] == cid and body["agent_id"] == aid and r.headers["ETag"] == '"1"'

        got = client.get(conv_wm(cid), headers=h)
        assert got.json()["content"] == "# Notes\n- likes tea" and got.headers["ETag"] == '"1"'

        stale = client.put(conv_wm(cid), json={"content": "x"}, headers={**h, "If-Match": "0"})
        assert stale.status_code == 409 and "version 1" in stale.json()["detail"]
        ok = client.put(conv_wm(cid), json={"content": "y"}, headers={**h, "If-Match": 'W/"1"'})
        assert ok.status_code == 200 and ok.json()["version"] == 2
        assert client.put(conv_wm(cid), json={"content": "z"}, headers={**h, "If-Match": "abc"}).status_code == 422

        assert client.delete(conv_wm(cid), headers={**h, "If-Match": "1"}).status_code == 409
        assert client.delete(conv_wm(cid), headers={**h, "If-Match": "2"}).status_code == 204
        assert client.delete(conv_wm(cid), headers=h).status_code == 204  # idempotent
        assert client.get(conv_wm(cid), headers=h).status_code == 404

    def test_config_rules(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        # No memory config at all; and an unpublished agent.
        for aid in (make_agent(client, h), make_agent(client, h, {"working": {"scope": "conversation"}}, publish=False)):
            cid = new_conversation(client, h, aid)
            r = client.put(conv_wm(cid), json={"content": "x"}, headers=h)
            assert r.status_code == 422 and "memory.working" in r.json()["detail"]
        # Resource-scoped agent: the conversation route refuses and names the right one.
        aid = make_agent(client, h, {"working": {}})
        cid = new_conversation(client, h, aid)
        r = client.put(conv_wm(cid), json={"content": "x"}, headers=h)
        assert r.status_code == 422 and "resource-scoped" in r.json()["detail"]
        # Template mode wants a string.
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        cid = new_conversation(client, h, aid)
        r = client.put(conv_wm(cid), json={"content": {"a": 1}}, headers=h)
        assert r.status_code == 422 and "string" in r.json()["detail"]
        too_big = client.put(conv_wm(cid), json={"content": "x" * (17 * 1024)}, headers=h)
        assert too_big.status_code == 422 and "x" * 50 not in too_big.text

    def test_cross_tenant_404(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        _, other = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        cid = new_conversation(client, h, aid)
        assert client.put(conv_wm(cid), json={"content": SECRET}, headers=h).status_code == 200
        for method in ("get", "delete"):
            r = getattr(client, method)(conv_wm(cid), headers=other)
            assert r.status_code == 404 and SECRET not in r.text
        assert client.put(conv_wm(cid), json={"content": "x"}, headers=other).status_code == 404
        assert client.get(conv_wm("nope"), headers=h).status_code == 404
        assert client.get(conv_wm(cid), headers=h).json()["content"] == SECRET


class TestWriteReadBackAndConditionalDelete:
    def test_put_returns_its_own_row_and_etag(self, pg, mem_tenant, monkeypatch):
        """The PUT response is the row the write stored, not a re-read (a later writer's version, or
        a 404 after a concurrent delete)."""
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {}})
        assert client.put(res_wm(aid, "u1"), json={"content": "v1"}, headers=h).status_code == 200
        # Simulate a concurrent delete committing right after the write: a re-read would 404.
        monkeypatch.setattr(MemoryStore, "get_working_memory", lambda *a, **k: None)
        r = client.put(res_wm(aid, "u1"), json={"content": "v2"}, headers={**h, "If-Match": "1"})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "v2" and r.json()["version"] == 2 and r.headers["ETag"] == '"2"'

    def test_conditional_delete_of_missing_note_is_204(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {}})
        assert client.put(res_wm(aid, "u1"), json={"content": "x"}, headers=h).status_code == 200
        assert client.delete(res_wm(aid, "u1"), headers={**h, "If-Match": '"1"'}).status_code == 204
        # A retry of the same conditional delete: already gone, so 204, not a version conflict.
        assert client.delete(res_wm(aid, "u1"), headers={**h, "If-Match": '"1"'}).status_code == 204
        assert client.delete(res_wm(aid, "u1"), headers={**h, "If-Match": "abc"}).status_code == 422
        # PUT If-Match N on a missing note is still a conflict, with the plain wording.
        r = client.put(res_wm(aid, "u1"), json={"content": "y"}, headers={**h, "If-Match": "1"})
        assert r.status_code == 409 and "does not exist (version 0)" in r.json()["detail"]


class TestResourceWorkingMemory:
    def test_schema_mode_put_get_list(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": SCHEMA}})
        r = client.put(res_wm(aid, "end-user:1"), json={"content": {"name": "Ann", "prefs": {"tone": "casual"}}}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["format"] == "json" and r.json()["scope"] == "resource" and r.json()["scope_key"] == "end-user:1"
        assert client.get(res_wm(aid, "end-user:1"), headers=h).json()["content"]["name"] == "Ann"
        listed = client.get(f"{API}/agents/{aid}/memory/working?scope=resource", headers=h).json()["items"]
        assert [n["scope_key"] for n in listed] == ["end-user:1"]
        assert client.get(f"{API}/agents/{aid}/memory/working?scope=conversation", headers=h).json()["items"] == []

    def test_invalid_content_never_echoes(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"json_schema": {**SCHEMA, "additionalProperties": False}}})
        for content in ({"age": SECRET}, {"model_key_" + SECRET: 1}, SECRET):
            r = client.put(res_wm(aid, "u1"), json={"content": content}, headers=h)
            assert r.status_code == 422 and SECRET not in r.text, r.text
        assert client.get(res_wm(aid, "u1"), headers=h).status_code == 404

    def test_scope_mismatch_and_bad_ids(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        aid = make_agent(client, h, {"working": {"scope": "conversation"}})
        r = client.put(res_wm(aid, "u1"), json={"content": "x"}, headers=h)
        assert r.status_code == 422 and "conversation-scoped" in r.json()["detail"]
        assert client.get(res_wm(aid, "bad id"), headers=h).status_code == 404
        assert client.put(res_wm(aid, "bad%20id"), json={"content": "x"}, headers=h).status_code == 404
        assert client.get(res_wm("not-a-uuid", "u1"), headers=h).status_code == 404
        assert client.get(res_wm(str(uuid.uuid4()), "u1"), headers=h).status_code == 404

    def test_cross_tenant_and_cross_agent(self, pg, mem_tenant):
        client, _ = pg
        _, h = mem_tenant()
        _, other = mem_tenant()
        a1 = make_agent(client, h, {"working": {}})
        a2 = make_agent(client, h, {"working": {}})
        assert client.put(res_wm(a1, "u1"), json={"content": SECRET}, headers=h).status_code == 200
        assert client.get(res_wm(a2, "u1"), headers=h).status_code == 404  # notes are per agent
        for r in (
            client.get(res_wm(a1, "u1"), headers=other),
            client.put(res_wm(a1, "u1"), json={"content": "x"}, headers=other),
            client.delete(res_wm(a1, "u1"), headers=other),
            client.get(f"{API}/agents/{a1}/memory/working", headers=other),
        ):
            assert r.status_code == 404 and r.json()["detail"] == "Agent not found" and SECRET not in r.text
        assert client.get(res_wm(a1, "u1"), headers=h).json()["content"] == SECRET


# ------------------------------------------------------------------------------- observations


class TestObservations:
    def seed(self, pg, client, h, tenant: str) -> tuple[str, str, list[str]]:
        aid = make_agent(client, h)
        cid = new_conversation(client, h, aid)
        for _ in range(3):
            completed_turn(client, pg[1], h, cid)
        store = memory_store(pg)
        ids = [
            store.insert_observation(tenant, aid, cid, covers_from_turn=1, covers_to_turn=1, content="first").observation.id,
            store.insert_observation(tenant, aid, cid, covers_from_turn=2, covers_to_turn=3, content="second").observation.id,
        ]
        return aid, cid, ids

    def test_list_and_delete_newest_only(self, pg, mem_tenant):
        client, _ = pg
        tenant, h = mem_tenant()
        _, cid, (older, newer) = self.seed(pg, client, h, tenant)
        url = f"{API}/conversations/{cid}/memory/observations"
        listed = client.get(url, headers=h).json()
        assert [o["id"] for o in listed["items"]] == [older, newer] and listed["covered_to_turn"] == 3

        r = client.delete(f"{url}/{older}", headers=h)
        assert r.status_code == 409 and "newest" in r.json()["detail"]
        assert client.delete(f"{url}/{newer}", headers=h).status_code == 204
        assert client.delete(f"{url}/{newer}", headers=h).status_code == 404
        assert client.get(url, headers=h).json()["covered_to_turn"] == 1
        assert client.delete(f"{url}/{older}", headers=h).status_code == 204
        assert client.get(url, headers=h).json() == {"items": [], "covered_to_turn": 0}

    def test_delete_all_and_isolation(self, pg, mem_tenant):
        client, _ = pg
        tenant, h = mem_tenant()
        _, other = mem_tenant()
        _, cid, (_, newer) = self.seed(pg, client, h, tenant)
        other_cid = new_conversation(client, h, make_agent(client, h))
        url = f"{API}/conversations/{cid}/memory/observations"
        # Another conversation's path, another tenant, malformed ids: 404, nothing deleted.
        assert client.delete(f"{API}/conversations/{other_cid}/memory/observations/{newer}", headers=h).status_code == 404
        assert client.delete(f"{url}/{newer}", headers=other).status_code == 404
        assert client.get(url, headers=other).status_code == 404
        assert client.delete(url, headers=other).status_code == 404
        assert client.delete(f"{url}/not-a-uuid", headers=h).status_code == 404
        assert len(client.get(url, headers=h).json()["items"]) == 2
        assert client.delete(url, headers=h).json() == {"deleted": 2}
        assert client.delete(url, headers=h).json() == {"deleted": 0}


# ---------------------------------------------------------------------------- forget resource


class TestForgetResource:
    def seed(self, pg, client, h, tenant: str, aid: str, rid: str) -> tuple[str, str]:
        r = create_conversation(client, h, aid, resource_id=rid)
        cid = r.json()["id"]
        run_id = completed_turn(client, pg[1], h, cid)
        store = memory_store(pg)
        store.update_working_memory(tenant, aid, scope="resource", scope_key=rid, content="note")
        store.index_turn(
            tenant, aid, cid, run_id, [TurnText("user", "hi", vector(3))], embedding_model="test:hash"
        )
        return cid, run_id

    def resource_rows(self, app, tenant: str, rid: str) -> int:
        return count(
            app,
            "SELECT (SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource' AND scope_key = :k)"
            " + (SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = :k)",
            t=tenant, k=rid,
        )

    def test_per_agent_then_tenant_wide(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        a1, a2 = make_agent(client, h), make_agent(client, h)
        self.seed(pg, client, h, tenant, a1, "u1")
        self.seed(pg, client, h, tenant, a2, "u1")
        r = client.delete(f"{API}/agents/{a1}/memory/resources/u1", headers=h)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "resource_id": "u1", "agent_id": a1, "working_memories_deleted": 1, "message_embeddings_deleted": 1,
            "conversations_deleted": [], "busy_conversations": [],
        }
        assert self.resource_rows(app, tenant, "u1") == 2  # a2's remain
        r = client.delete(f"{API}/memory/resources/u1?include_conversations=true", headers=h)
        assert r.status_code == 200 and r.json()["agent_id"] is None and len(r.json()["conversations_deleted"]) == 2
        assert self.resource_rows(app, tenant, "u1") == 0
        assert count(app, "SELECT count(*) FROM conversations WHERE tenant_id = :t", t=tenant) == 0
        again = client.delete(f"{API}/memory/resources/u1?include_conversations=true", headers=h).json()
        assert again["working_memories_deleted"] == 0 and again["conversations_deleted"] == []

    def test_forget_is_audit_logged_without_content(self, pg, mem_tenant, caplog):
        client, _ = pg
        tenant, h = mem_tenant()
        aid = make_agent(client, h)
        self.seed(pg, client, h, tenant, aid, "u3")
        with caplog.at_level("INFO", logger="eap_api"):
            assert client.delete(f"{API}/agents/{aid}/memory/resources/u3", headers=h).status_code == 200
            assert client.delete(f"{API}/memory/resources/u3?include_conversations=true", headers=h).status_code == 200
        records = [r for r in caplog.records if getattr(r, "eap.audit", None) == "memory_forget"]
        assert [getattr(r, "eap.agent_id") for r in records] == [aid, "*"]
        first = records[0]
        assert getattr(first, "eap.tenant_id") == tenant and getattr(first, "eap.user_id") == "alice"
        assert getattr(first, "eap.resource_hash") == resource_audit_hash("u3") and getattr(first, "eap.outcome") == "done"
        assert not hasattr(first, "eap.resource_id")
        assert all("u3" not in r.getMessage() for r in records)
        assert getattr(first, "eap.working_memories") == 1 and getattr(first, "eap.message_embeddings") == 1
        assert getattr(records[1], "eap.conversations") == 1
        assert all("note" not in r.getMessage() for r in records)

    def test_forget_audit_logs_a_salted_hash_not_the_email(self, pg, mem_tenant, caplog, monkeypatch):
        client, _ = pg
        _, h = mem_tenant()
        email = "Jane.Doe@example.com"
        monkeypatch.delenv("EAP_AUDIT_HASH_SALT", raising=False)
        plain = resource_audit_hash(email)
        assert plain == hashlib.sha256(email.encode()).hexdigest()[:16]
        monkeypatch.setenv("EAP_AUDIT_HASH_SALT", "deployment-salt")
        salted = resource_audit_hash(email)
        assert salted == hmac.new(b"deployment-salt", email.encode(), hashlib.sha256).hexdigest()[:16]
        assert salted != plain and resource_audit_hash(email) == salted  # stable
        with caplog.at_level("INFO", logger="eap_api"):
            assert client.delete(f"{API}/memory/resources/{email}", headers=h).status_code == 200
        (record,) = [r for r in caplog.records if getattr(r, "eap.audit", None) == "memory_forget"]
        assert getattr(record, "eap.resource_hash") == salted
        assert email not in record.getMessage() and email.lower() not in record.getMessage().lower()
        assert all(email not in str(v) for v in vars(record).values())

    def test_busy_is_409_and_isolation(self, pg, mem_tenant):
        client, app = pg
        tenant, h = mem_tenant()
        _, other = mem_tenant()
        aid = make_agent(client, h)
        cid, _ = self.seed(pg, client, h, tenant, aid, "u2")
        # Another tenant: the agent is not theirs; tenant-wide forget only touches their own tenant.
        assert client.delete(f"{API}/agents/{aid}/memory/resources/u2", headers=other).status_code == 404
        assert client.delete(f"{API}/memory/resources/u2", headers=other).json()["working_memories_deleted"] == 0
        assert self.resource_rows(app, tenant, "u2") == 2
        assert client.delete(f"{API}/memory/resources/bad%20id", headers=h).status_code == 404

        run_id = str(uuid.uuid4())
        assert turn(client, h, cid, run_id=run_id).status_code == 201
        r = client.delete(f"{API}/memory/resources/u2", headers=h)
        assert r.status_code == 409 and r.headers["Retry-After"] and r.json()["busy_conversations"] == [cid]
        assert self.resource_rows(app, tenant, "u2") == 2  # all-or-nothing
        finish(app, run_id)
        assert client.delete(f"{API}/agents/{aid}/memory/resources/u2", headers=h).status_code == 200
        assert self.resource_rows(app, tenant, "u2") == 0
