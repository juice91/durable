"""RM.20 privacy: every RAG and memory deletion route leaves zero rows of its target, one query per
table, and leaves every other row (a sibling conversation, the sibling resource, another agent's
memory of the same resource, the other tenant's identical data) exactly as it was.

The world (rm_world.build_tenant, two tenants of the same shape) puts marker text in every place a
turn's content can live: the run's input, output and `runs.messages`, a `retrieve` tool_result event
and its side effect (the retrieved chunk text, decision 2026-11-14), observations, message
embeddings and working memory. After `DELETE /conversations/{id}` (directly or through
forget-resource with `include_conversations=true`) none of that conversation's markers is left in
any table, while its runs and usage rows (the audit and cost record) stay.

Routes: DELETE /conversations/{id}; DELETE /agents/{id}/memory/resources/{rid} and DELETE
/memory/resources/{rid}, each with and without include_conversations; DELETE observations (all and
one); DELETE working memory (conversation and resource); DELETE a knowledge base document; DELETE a
knowledge base (force).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from .rm_world import API, RM_TABLES, RmTenant, build_tenant, cleanup_rm, snapshot
from .run_helpers import requires_pg
from .test_knowledge_api import _knowledge_env  # noqa: F401 - autouse fixture
from .test_runs import _no_hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401

pytestmark = requires_pg


class W:
    def __init__(self, client, app, t: RmTenant, o: RmTenant) -> None:
        self.client, self.app, self.t, self.o = client, app, t, o
        self.engine = app.state.database_engine
        self.before = self.snap()
        # Not vacuous: every marker the deletion checks look for is stored before the deletion.
        flat = "\n".join(r for rows in self.before[t.name].values() for r in rows)
        for key in ("c1", "c2", "c3"):
            for what in ("input", "output", "retrieved", "recall", "obs"):
                assert t.marker(f"{what}-{key}") in flat, (what, key)
        for what in ("note-c1", "note-r1", "note-r2", "chunk", "faq"):
            assert t.marker(what) in flat, what

    def snap(self) -> dict[str, dict[str, list[str]]]:
        return snapshot(self.engine, [self.t.name, self.o.name])

    def n(self, sql: str, **params: Any) -> int:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), {"t": self.t.name, **params}).scalar_one()

    def delete(self, url: str, status: int = 200):
        r = self.client.delete(url, headers=self.t.headers)
        assert r.status_code == status, (url, r.status_code, r.text[:300])
        return r

    def assert_other_tenant_untouched(self) -> None:
        assert self.snap()[self.o.name] == self.before[self.o.name]

    def assert_rows_unchanged_except(self, *needles: str) -> None:
        """Every row of the tenant whose text mentions none of `needles` is unchanged, and no new
        row appeared that mentions none of them."""
        after = self.snap()[self.t.name]
        for table in RM_TABLES:
            keep_before = [r for r in self.before[self.t.name][table] if not any(n in r for n in needles)]
            keep_after = [r for r in after[table] if not any(n in r for n in needles)]
            assert keep_after == keep_before, f"{table}: a row outside the deletion's target changed"

    def assert_conversation_gone(self, key: str) -> None:
        """`key` is c1, c2 or c3: zero rows per table, the runs kept and scrubbed, no marker left."""
        t = self.t
        cid = getattr(t, key)
        run_id = t.runs[cid]
        assert self.n("SELECT count(*) FROM conversations WHERE tenant_id = :t AND id = :c", c=cid) == 0
        for table in ("working_memories", "memory_observations", "message_embeddings"):
            assert self.n(f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND conversation_id = :c", c=cid) == 0, table
        assert self.n("SELECT count(*) FROM side_effects WHERE tenant_id = :t AND key LIKE :k", k=f"{run_id}:%") == 0
        assert self.n(
            "SELECT count(*) FROM runs WHERE tenant_id = :t AND id = :r AND input IS NULL AND output IS NULL"
            " AND messages IS NULL AND request_context IS NULL AND conversation_id IS NULL AND turn IS NULL",
            r=run_id,
        ) == 1, "the run (audit and cost record) is kept with its content nulled"
        # Usage rows stay: the planted retrieve embedding row and nothing else lost.
        assert self.n("SELECT count(*) FROM run_model_requests WHERE tenant_id = :t AND run_id = :r", r=run_id) >= 1
        for marker in (f"input-{key}", f"output-{key}", f"retrieved-{key}", f"recall-{key}", f"obs-{key}"):
            needle = t.marker(marker)
            for table in RM_TABLES:
                hits = self.n(f"SELECT count(*) FROM {table} x WHERE tenant_id = :t AND x::text LIKE :p",
                              p=f"%{needle}%")
                assert hits == 0, f"{marker} still in {table}"
        if key == "c1":
            assert self.n("SELECT count(*) FROM working_memories x WHERE tenant_id = :t AND x::text LIKE :p",
                          p=f"%{t.marker('note-c1')}%") == 0


@pytest.fixture
def w(pg, tenant_of, fake_dbos):
    client, app = pg
    t = build_tenant(client, app, *tenant_of())
    o = build_tenant(client, app, *tenant_of())
    world = W(client, app, t, o)
    yield world
    cleanup_rm(app.state.database_engine, [t.name, o.name])


# ----------------------------------------------------------------------------- conversations


def test_delete_conversation_leaves_zero_rows_and_scrubs_the_retrieved_text(w):
    t = w.t
    w.delete(f"{API}/conversations/{t.c1}", status=204)
    w.assert_conversation_gone("c1")
    w.assert_rows_unchanged_except(t.c1, t.runs[t.c1])
    w.assert_other_tenant_untouched()
    # Idempotent: a second delete is a 404 and changes nothing.
    after = w.snap()
    w.delete(f"{API}/conversations/{t.c1}", status=404)
    assert w.snap() == after


def test_delete_conversation_drops_numeric_tool_args_and_text_keyed_results(w):
    """Final review BLOCKING 1: numbers are personal data too. A tool call with numeric arguments and
    a result whose keys are text leave nothing behind; structural numbers stay; the trace still loads."""
    t = w.t
    run_id = t.runs[t.c1]
    planted = [
        ("tool:n1:call", "tool_call",
         {"tool_name": "lookup", "tool_call_id": "n1", "args": {"phone": 5551234567, "card_last4": 4242}}),
        ("tool:n1:result", "tool_result",
         {"tool_name": "lookup", "tool_call_id": "n1", "content": {"Ada, ada@example.com": 1}}),
        ("tool:n2:result", "tool_result",
         {"tool_name": "lookup", "tool_call_id": "n2", "content": 5551234567, "phone": 5551234567}),
    ]
    with w.engine.begin() as conn:
        for key, type_, data in planted:
            seq = conn.execute(text("SELECT COALESCE(MAX(seq), -1) + 1 FROM execution_events WHERE run_id = :r"),
                               {"r": run_id}).scalar_one()
            conn.execute(text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), now())"
            ), {"t": t.name, "r": run_id, "s": seq, "k": key, "ty": type_, "d": json.dumps(data)})

    w.delete(f"{API}/conversations/{t.c1}", status=204)
    w.assert_conversation_gone("c1")
    for leak in ("5551234567", "4242", "ada@example.com", "card_last4"):
        assert w.n("SELECT count(*) FROM execution_events x WHERE tenant_id = :t AND run_id = :r AND x.data::text LIKE :p",
                   r=run_id, p=f"%{leak}%") == 0, leak
    with w.engine.connect() as conn:
        rows = conn.execute(text("SELECT seq, data FROM execution_events WHERE tenant_id = :t AND run_id = :r"
                                 " ORDER BY seq"), {"t": t.name, "r": run_id}).all()
    assert [r.seq for r in rows] == list(range(len(rows)))
    assert all(r.data.get("redacted") is True for r in rows)
    r = w.client.get(f"{API}/runs/{run_id}/trace", headers=t.headers)
    assert r.status_code == 200, r.text[:300]
    assert "5551234567" not in r.text and "ada@example.com" not in r.text
    w.assert_other_tenant_untouched()


# ----------------------------------------------------------------------------- forget per agent


def test_forget_resource_for_one_agent_keeps_conversations_by_default(w):
    t = w.t
    r = w.delete(f"{API}/agents/{t.res_agent}/memory/resources/r1")
    assert r.json()["working_memories_deleted"] == 1 and r.json()["message_embeddings_deleted"] == 2
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND agent_id = :a AND scope = 'resource'"
               " AND scope_key = 'r1'", a=t.res_agent) == 0
    assert w.n("SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND agent_id = :a AND resource_id = 'r1'",
               a=t.res_agent) == 0
    # Kept: c2's transcript (and its retrieved text), the other agent's memory of r1, resource r2.
    assert w.n("SELECT count(*) FROM runs x WHERE tenant_id = :t AND x::text LIKE :p",
               p=f"%{t.marker('retrieved-c2')}%") == 1
    assert w.n("SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND agent_id = :a AND resource_id = 'r1'",
               a=t.conv_agent) == 2
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope_key = 'r2'") == 1
    assert w.n("SELECT count(*) FROM memory_forget_watermarks WHERE tenant_id = :t AND conversation_id = :c",
               c=t.c2) == 1
    w.assert_rows_unchanged_except(t.marker("note-r1"), t.c2)
    w.assert_other_tenant_untouched()


def test_forget_resource_for_one_agent_with_conversations(w):
    t = w.t
    r = w.delete(f"{API}/agents/{t.res_agent}/memory/resources/r1?include_conversations=true")
    assert r.json()["conversations_deleted"] == [t.c2]
    w.assert_conversation_gone("c2")
    assert w.n("SELECT count(*) FROM working_memories x WHERE tenant_id = :t AND x::text LIKE :p",
               p=f"%{t.marker('note-r1')}%") == 0
    # The other agent's conversation of the same resource is untouched.
    assert w.n("SELECT count(*) FROM conversations WHERE tenant_id = :t AND id = :c", c=t.c1) == 1
    w.assert_rows_unchanged_except(t.marker("note-r1"), t.c2, t.runs[t.c2])
    w.assert_other_tenant_untouched()


# ----------------------------------------------------------------------------- forget tenant-wide


def test_forget_resource_tenant_wide_keeps_conversations_by_default(w):
    t = w.t
    r = w.delete(f"{API}/memory/resources/r1")
    body = r.json()
    assert (body["working_memories_deleted"], body["message_embeddings_deleted"], body["conversations_deleted"]) == (
        1, 4, [])
    assert w.n("SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = 'r1'") == 0
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource'"
               " AND scope_key = 'r1'") == 0
    # Conversation-scoped data of r1's conversations stays without include_conversations.
    assert w.n("SELECT count(*) FROM conversations WHERE tenant_id = :t AND resource_id = 'r1'") == 2
    assert w.n("SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND conversation_id IN (:a, :b)",
               a=t.c1, b=t.c2) == 2
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND conversation_id = :c", c=t.c1) == 1
    assert w.n("SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = 'r2'") == 2
    # Kept: c2's transcript, with its retrieved text.
    assert w.n("SELECT count(*) FROM runs x WHERE tenant_id = :t AND x::text LIKE :p",
               p=f"%{t.marker('retrieved-c2')}%") == 1
    w.assert_rows_unchanged_except(t.marker("note-r1"), t.c1, t.c2)
    w.assert_other_tenant_untouched()


def test_forget_resource_tenant_wide_with_conversations(w):
    t = w.t
    r = w.delete(f"{API}/memory/resources/r1?include_conversations=true")
    assert sorted(r.json()["conversations_deleted"]) == sorted([t.c1, t.c2])
    for key in ("c1", "c2"):
        w.assert_conversation_gone(key)
    for table in ("working_memories", "message_embeddings"):
        n = w.n(f"SELECT count(*) FROM {table} x WHERE tenant_id = :t AND x::text LIKE '%r1%'"
                " AND (x::text LIKE :a OR x::text LIKE :b)", a=f"%{t.c1}%", b=f"%{t.c2}%")
        assert n == 0, table
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource' AND scope_key = 'r1'") == 0
    # Resource r2 (conversation c3, its note, embeddings and observation) is untouched.
    assert w.n("SELECT count(*) FROM conversations WHERE tenant_id = :t AND id = :c", c=t.c3) == 1
    w.assert_rows_unchanged_except(t.marker("note-r1"), t.c1, t.c2, t.runs[t.c1], t.runs[t.c2])
    w.assert_other_tenant_untouched()


# ----------------------------------------------------------------------------- observations and notes


def test_delete_all_observations_of_a_conversation(w):
    t = w.t
    w.delete(f"{API}/conversations/{t.c1}/memory/observations")
    assert w.n("SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c", c=t.c1) == 0
    w.assert_rows_unchanged_except(t.marker("obs-c1"))
    w.assert_other_tenant_untouched()


def test_delete_one_observation(w):
    t = w.t
    w.delete(f"{API}/conversations/{t.c3}/memory/observations/{t.obs[t.c3]}", status=204)
    assert w.n("SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND id = :o", o=t.obs[t.c3]) == 0
    w.assert_rows_unchanged_except(t.marker("obs-c3"))
    w.assert_other_tenant_untouched()


def test_delete_conversation_working_memory(w):
    t = w.t
    w.delete(f"{API}/conversations/{t.c1}/memory/working", status=204)
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND conversation_id = :c", c=t.c1) == 0
    w.assert_rows_unchanged_except(t.marker("note-c1"))
    w.assert_other_tenant_untouched()


def test_delete_resource_working_memory(w):
    t = w.t
    w.delete(f"{API}/agents/{t.res_agent}/memory/resources/r2/working", status=204)
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource'"
               " AND scope_key = 'r2'") == 0
    assert w.n("SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope = 'resource'"
               " AND scope_key = 'r1'") == 1
    w.assert_rows_unchanged_except(t.marker("note-r2"))
    w.assert_other_tenant_untouched()


# ----------------------------------------------------------------------------- knowledge


def test_delete_document_leaves_zero_rows(w):
    t = w.t
    doc_id = t.ready_doc["id"]
    w.delete(f"{API}/knowledge-bases/{t.kb['id']}/documents/guide", status=204)
    assert w.n("SELECT count(*) FROM kb_documents WHERE tenant_id = :t AND id = :d", d=doc_id) == 0
    assert w.n("SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND document_id = :d", d=doc_id) == 0
    assert w.n("SELECT count(*) FROM kb_chunks x WHERE tenant_id = :t AND x::text LIKE :p",
               p=f"%{t.marker('chunk')}%") == 0
    assert w.n("SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND document_id = :d", d=t.other_doc["id"]) == 1
    # kb_documents' updated_at of the knowledge base may move; rows of other documents may not.
    w.assert_rows_unchanged_except(doc_id, t.kb["id"])
    w.assert_other_tenant_untouched()


def test_delete_knowledge_base_leaves_zero_rows(w):
    t = w.t
    kb_id = t.kb["id"]
    # Referenced by the latest version of kb_agent: 409 without force, nothing deleted.
    w.delete(f"{API}/knowledge-bases/{kb_id}", status=409)
    assert w.snap() == w.before
    w.delete(f"{API}/knowledge-bases/{kb_id}?force=true", status=204)
    for table, col in (("knowledge_bases", "id"), ("kb_documents", "kb_id"), ("kb_chunks", "kb_id")):
        assert w.n(f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND {col} = :k", k=kb_id) == 0, table
    for marker in ("chunk", "faq", "upload"):
        assert w.n("SELECT count(*) FROM kb_chunks x WHERE tenant_id = :t AND x::text LIKE :p",
                   p=f"%{t.marker(marker)}%") == 0
        assert w.n("SELECT count(*) FROM kb_documents x WHERE tenant_id = :t AND x::text LIKE :p",
                   p=f"%{t.marker(marker)}%") == 0
    # Memory, conversations and agents are untouched (the kb_ingest run row may be detached or ended).
    w.assert_rows_unchanged_except(kb_id, t.pending_run)
    w.assert_other_tenant_untouched()
