"""RM.20: cross-tenant isolation over every API route added or changed in the RAG and memory
milestone, modelled on test_p0_cross_tenant.py.

Two tenants with interleaved data of the same shape (rm_world.build_tenant: a knowledge base with two
embedded, active documents and a pending API upload, an agent attaching it, conversation- and
resource-scoped working memory agents, three conversations over two resources with a completed
turn each, a planted retrieve result, observations and message embeddings). Tenant B then calls
every RM route with tenant A's ids. Each call answers 404 (or an empty / B-only result) without
echoing A's content, and changes nothing: every RM table (plus runs, events, side effects, usage,
conversations, agents) is snapshotted for both tenants before and after, and nothing is enqueued.

Routes: knowledge.py (embedding-models, list/create/get/patch/delete knowledge base, documents
list/get/PUT/DELETE, reingest, reindex POST/DELETE, search), memory.py (conversation and resource
working memory GET/PUT/DELETE, the agent's note list, observations list/delete one/delete all,
forget per agent and tenant-wide), conversations.py (create with resource_id, read, DELETE), and
publish / draft of a spec naming the other tenant's knowledge base.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from .rm_world import API, RmTenant, build_tenant, cleanup_rm, snapshot
from .run_helpers import requires_pg
from .test_knowledge_api import _knowledge_env  # noqa: F401 - autouse fixture
from .test_runs import SPEC, _no_hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401

pytestmark = requires_pg


class World:
    def __init__(self, pg_, tenant_of_, fake) -> None:
        self.client, self.app = pg_
        self.engine = self.app.state.database_engine
        self.fake = fake
        names = [tenant_of_() for _ in range(2)]
        # Interleaved: A then B, so ids and timestamps of the two tenants alternate within each table.
        self.a, self.b = (build_tenant(self.client, self.app, n, h) for n, h in names)
        self.tenants = [self.a.name, self.b.name]

    def snapshot(self) -> dict[str, dict[str, list[str]]]:
        return snapshot(self.engine, self.tenants)

    def call(self, method: str, url: str, body: Any = None, t: RmTenant | None = None):
        kw: dict[str, Any] = {"headers": (t or self.b).headers}
        if body is not None:
            kw["json"] = body
        return self.client.request(method, url, **kw)


@pytest.fixture
def world(pg, tenant_of, fake_dbos):
    w = World(pg, tenant_of, fake_dbos)
    yield w
    cleanup_rm(w.engine, w.tenants)


@pytest.fixture
def unchanged(world):
    """After the test body: no row of either tenant changed and nothing was enqueued."""
    before = world.snapshot()
    calls = len(world.fake.calls)
    yield world
    after = world.snapshot()
    for tenant in before:
        for table in before[tenant]:
            assert after[tenant][table] == before[tenant][table], f"{table} of {tenant} changed"
    assert len(world.fake.calls) == calls, "a run was enqueued"


def _assert_no_leak(w: World, r, method: str, url: str) -> None:
    body = r.text
    a = w.a
    turn_text = [f"{kind}-{c}" for kind in ("input", "output") for c in ("c1", "c2", "c3")]
    for marker in ("chunk", "faq", "upload", "note-c1", "note-r1", "obs-c1", "recall-c1", "retrieved-c1", *turn_text):
        assert a.marker(marker) not in body, (method, url, marker)
    for ident in (a.c1, a.c2, a.c3, a.conv_agent, a.res_agent, a.kb["id"], a.ready_doc["id"]):
        assert ident not in body, (method, url, "echoed an id of A")


def _a_routes(a: RmTenant) -> list[tuple[str, str, Any]]:
    """Every RM route that names one of A's objects, with a body B could plausibly send."""
    kb = f"{API}/knowledge-bases/{a.kb['id']}"
    doc = f"{kb}/documents/guide"
    obs = f"{API}/conversations/{a.c1}/memory/observations"
    res = f"{API}/agents/{a.res_agent}/memory/resources"
    return [
        # knowledge.py
        ("GET", kb, None),
        ("PATCH", kb, {"description": "stolen", "name": "renamed"}),
        ("GET", f"{kb}/documents", None),
        ("GET", f"{kb}/documents?status=ready", None),
        ("GET", f"{doc}?include_content=true", None),
        ("GET", f"{kb}/documents/upload.md", None),
        ("PUT", doc, {"content": "replaced", "mime_type": "text/plain"}),
        ("PUT", f"{kb}/documents/new-key", {"content": "planted", "mime_type": "text/plain"}),
        ("PUT", doc, {"content": "replaced", "mime_type": "text/plain", "run_id": a.pending_run}),
        ("POST", f"{doc}/reingest", None),
        ("POST", f"{doc}/reingest", {"run_id": str(uuid.uuid4())}),
        ("POST", f"{kb}/reindex", {"embedding_model": "test:hash768"}),
        ("POST", f"{kb}/reindex", {"embedding_model": "test:hash768", "dry_run": True}),
        ("DELETE", f"{kb}/reindex", None),
        ("POST", f"{kb}/search?wait=0", {"query": "printer"}),
        ("POST", f"{kb}/search?wait=0", {"query": "printer", "run_id": a.pending_run}),
        ("DELETE", doc, None),
        ("DELETE", kb, None),
        ("DELETE", f"{kb}?force=true", None),
        # memory.py: conversation notes
        ("GET", f"{API}/conversations/{a.c1}/memory/working", None),
        ("PUT", f"{API}/conversations/{a.c1}/memory/working", {"content": "overwritten"}),
        ("DELETE", f"{API}/conversations/{a.c1}/memory/working", None),
        # memory.py: resource notes and the agent's list
        ("GET", f"{API}/agents/{a.res_agent}/memory/working", None),
        ("GET", f"{res}/r1/working", None),
        ("PUT", f"{res}/r1/working", {"content": {"name": "overwritten"}}),
        ("DELETE", f"{res}/r1/working", None),
        # memory.py: observations
        ("GET", obs, None),
        ("GET", f"{obs}?include_superseded=true", None),
        ("DELETE", f"{obs}/{a.obs[a.c1]}", None),
        ("DELETE", obs, None),
        # memory.py: forget per agent (A's agent id)
        ("DELETE", f"{res}/r1", None),
        ("DELETE", f"{res}/r1?include_conversations=true", None),
        ("DELETE", f"{API}/agents/{a.conv_agent}/memory/resources/r1?include_conversations=true", None),
        # conversations.py
        ("GET", f"{API}/conversations/{a.c1}", None),
        ("DELETE", f"{API}/conversations/{a.c1}", None),
        ("DELETE", f"{API}/conversations/{a.c2}", None),
        ("POST", f"{API}/agents/{a.res_agent}/conversations", {"resource_id": "r1"}),
        ("POST", f"{API}/conversations/{a.c2}/runs", {"run_id": str(uuid.uuid4()), "input": "hi"}),
        # A's runs, kb_ingest included
        ("GET", f"{API}/runs/{a.pending_run}", None),
        ("GET", f"{API}/runs/{a.runs[a.c1]}", None),
        ("GET", f"{API}/runs/{a.runs[a.c1]}/events", None),
    ]


def test_every_rm_route_with_the_other_tenants_ids_is_404_and_changes_nothing(unchanged):
    w = unchanged
    for method, url, body in _a_routes(w.a):
        r = w.call(method, url, body)
        assert r.status_code == 404, (method, url, r.status_code, r.text[:300])
        _assert_no_leak(w, r, method, url)


def test_lists_show_only_the_callers_own_rows(unchanged):
    w = unchanged
    a, b = w.a, w.b
    kbs = w.call("GET", f"{API}/knowledge-bases").json()["items"]
    assert [k["id"] for k in kbs] == [b.kb["id"]]
    docs = w.call("GET", f"{API}/knowledge-bases/{b.kb['id']}/documents").json()["items"]
    assert sorted(d["document_key"] for d in docs) == ["faq", "guide", "upload.md"]
    assert {d["id"] for d in docs}.isdisjoint({a.ready_doc["id"], a.other_doc["id"]})
    notes = w.call("GET", f"{API}/agents/{b.res_agent}/memory/working").json()
    assert a.marker("note-r1") not in str(notes) and b.marker("note-r1") in str(notes)
    convs = w.call("GET", f"{API}/conversations").json()
    ids = {c["id"] for c in convs["items"]}
    assert ids == {b.c1, b.c2, b.c3}
    for cid in (b.c1, b.c2, b.c3):
        body = w.call("GET", f"{API}/conversations/{cid}").json()
        assert body["resource_id"] in ("r1", "r2")
        assert a.marker("input-c1") not in str(body)
    runs = w.call("GET", f"{API}/runs?kind=kb_ingest").json()["items"]
    assert [r.get("run_id", r.get("id")) for r in runs] == [b.pending_run]
    obs = w.call("GET", f"{API}/conversations/{b.c1}/memory/observations").json()
    assert [o["id"] for o in obs["items"]] == [b.obs[b.c1]]
    # The embedding models are platform-wide: both tenants see the same list.
    assert w.call("GET", f"{API}/embedding-models").json() == w.call("GET", f"{API}/embedding-models", t=a).json()


def test_publish_and_draft_with_the_other_tenants_knowledge_base_are_refused(world):
    """A spec attaching A's knowledge base (alone or next to B's own): B's publish is 422 naming
    only the id B sent, a draft test run is refused, nothing is enqueued, no version or run is
    created for B, and A's rows are untouched."""
    from sqlalchemy import text

    w = world
    a, b = w.a, w.b
    before = w.snapshot()
    calls = len(w.fake.calls)
    for refs in ([{"id": a.kb["id"]}], [{"id": b.kb["id"]}, {"id": a.kb["id"]}]):
        spec = {**SPEC, "knowledge_bases": refs}
        r = w.call("POST", f"{API}/agents", {"spec": spec})
        assert r.status_code == 201, r.text  # a draft may hold anything; publishing is the check
        agent_id = r.json()["id"]
        p = w.call("POST", f"{API}/agents/{agent_id}/versions")
        assert p.status_code == 422, (p.status_code, p.text[:300])
        assert p.json()["detail"] == f"knowledge base '{a.kb['id']}' does not exist in this tenant"
        _assert_no_leak_except_id(w, p)
        d = w.call("POST", f"{API}/agents/{agent_id}/runs", {"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"})
        assert d.status_code == 422, (d.status_code, d.text[:300])
        with w.engine.connect() as conn:
            n_versions = conn.execute(text("SELECT count(*) FROM agent_versions WHERE agent_id = :a"),
                                      {"a": agent_id}).scalar_one()
            n_runs = conn.execute(text("SELECT count(*) FROM runs WHERE agent_id = :a"), {"a": agent_id}).scalar_one()
        assert (n_versions, n_runs) == (0, 0)
    after = w.snapshot()
    assert after[a.name] == before[a.name]
    assert after[b.name]["runs"] == before[b.name]["runs"]
    assert len(w.fake.calls) == calls


def _assert_no_leak_except_id(w: World, r) -> None:
    for marker in ("chunk", "faq", "upload"):
        assert w.a.marker(marker) not in r.text
    assert "Product docs" not in r.text  # A's knowledge base name


def test_a_new_knowledge_base_with_the_other_tenants_name_touches_only_the_callers_tenant(world):
    w = world
    before_a = w.snapshot()[w.a.name]
    r = w.call("POST", f"{API}/knowledge-bases", {"name": "Product docs 2", "embedding_model": "test:hash"})
    assert r.status_code == 201, r.text
    dup = w.call("POST", f"{API}/knowledge-bases", {"name": "Product docs", "embedding_model": "test:hash"})
    assert dup.status_code == 409  # B's own knowledge base of that name, not A's
    assert w.snapshot()[w.a.name] == before_a


def test_tenant_wide_forget_by_b_removes_only_bs_rows(world):
    """DELETE /memory/resources/{rid} names no object of another tenant, so it is not a 404: B
    forgetting `r1` removes B's resource memory and never touches A's identical `r1`."""
    w = world
    before = w.snapshot()
    r = w.call("DELETE", f"{API}/memory/resources/r1?include_conversations=true")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["working_memories_deleted"] == 1 and body["message_embeddings_deleted"] == 4
    assert sorted(body["conversations_deleted"]) == sorted([w.b.c1, w.b.c2])
    after = w.snapshot()
    assert after[w.a.name] == before[w.a.name]
    assert after[w.b.name]["working_memories"] != before[w.b.name]["working_memories"]
    _assert_no_leak(w, r, "DELETE", "/memory/resources/r1")


def test_b_cannot_reuse_as_ids_for_new_objects(unchanged):
    """Client-chosen ids that belong to A (conversation_id on create, run_id on a turn, upload,
    reingest or search) never reach A's rows: 409 or 404, and nothing is written for either."""
    w = unchanged
    a, b = w.a, w.b
    r = w.call("POST", f"{API}/agents/{b.conv_agent}/conversations", {"conversation_id": a.c1, "resource_id": "r1"})
    assert r.status_code in (404, 409), (r.status_code, r.text[:300])
    for run_id in (a.pending_run, a.runs[a.c1]):
        r = w.call("POST", f"{API}/conversations/{b.c3}/runs", {"run_id": run_id, "input": "hi"})
        assert r.status_code in (404, 409), (r.status_code, r.text[:300])
        r = w.call("PUT", f"{API}/knowledge-bases/{b.kb['id']}/documents/guide",
                   {"content": "new", "mime_type": "text/plain", "run_id": run_id})
        assert r.status_code in (404, 409), (r.status_code, r.text[:300])
        r = w.call("POST", f"{API}/knowledge-bases/{b.kb['id']}/documents/guide/reingest", {"run_id": run_id})
        assert r.status_code in (200, 404, 409), (r.status_code, r.text[:300])
        assert r.status_code != 200 or r.json()["run_id"] is None
        r = w.call("POST", f"{API}/knowledge-bases/{b.kb['id']}/search?wait=0", {"query": "q", "run_id": run_id})
        assert r.status_code in (404, 409), (r.status_code, r.text[:300])
