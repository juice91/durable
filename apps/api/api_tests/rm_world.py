"""RM.20: one tenant's worth of RAG and memory data, the same shape for every tenant, for the
milestone-wide cross-tenant, security and deletion suites (test_rm_cross_tenant.py,
test_rm_security.py, test_rm_deletion_privacy.py).

Seeded through the real API where a route exists (agents, conversations, turns, notes, document
uploads) and through the run-store stores otherwise (embedded chunks, observations, message
embeddings), plus a planted `retrieve` tool_result event, tool-result side effect and
`runs.messages` carrying the retrieved chunk text, as the worker would write them.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from eap_run_store import ChunkInput, KnowledgeStore, MemoryStore
from eap_run_store.memory import TurnText

from .test_conversations import finish, turn
from .test_runs import SPEC

API = "/api/v1"
NOTE_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "city": {"type": "string"}}}

# Every table an RM route can read or change, plus the run tables it can scrub. Snapshots are of
# whole rows as text (embeddings, content and JSON included), per tenant.
RM_TABLES = (
    "knowledge_bases", "kb_documents", "kb_chunks",
    "conversations", "runs", "execution_events", "side_effects", "run_model_requests",
    "working_memories", "memory_observations", "message_embeddings", "memory_forget_watermarks",
    "agents", "agent_versions",
)


def secret(tenant: str, what: str) -> str:
    """A per-tenant, per-kind marker: finding it anywhere it should not be is a leak."""
    return f"zq-{what}-{tenant}-marker"


def unit_vector(seed: int, dims: int = 1536) -> list[float]:
    v = [math.sin(seed * 7 + i) for i in range(dims)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def chunk_input(ordinal: int, body: str, title: str) -> ChunkInput:
    embed = f"path: Doc > {title}\n\ntitle: {title}\n\n{body}"
    return ChunkInput(
        ordinal=ordinal, title=title, text=body, embed_text=embed,
        embed_text_hash=hashlib.sha256(embed.encode()).hexdigest(),
        approx_tokens=max(1, math.ceil(len(embed) / 4)), heading_path=("Doc", title),
        anchor=f"https://example.test/doc#{title.lower()}",
    )


def ingest_ready(store: KnowledgeStore, tenant: str, kb: dict[str, Any], key: str, bodies: list[str],
                 metadata: dict[str, Any] | None = None, seed: int = 0) -> dict[str, Any]:
    """Upload, chunk, embed and finalise one document version straight through the store (what a
    completed eap.kb_ingest leaves behind). Returns the document row."""
    put = store.put_document(
        tenant, kb["id"], document_key=key, title=key, content="\n\n".join(bodies), mime_type="text/markdown",
        source_url="https://example.test/doc", metadata=metadata,
    )
    doc, version = put.document, put.document["latest_version"]
    gen = store.get_knowledge_base(tenant, kb["id"])["active_generation"]
    store.upsert_chunks(tenant, doc["id"], version, gen,
                        [chunk_input(i, b, f"S{i}") for i, b in enumerate(bodies)], metadata=metadata or {})
    batch = store.next_unembedded_batch(tenant, kb["id"], gen, document_id=doc["id"], doc_version=version)
    store.write_embeddings(tenant, kb["id"], gen, {c.id: unit_vector(seed + c.ordinal) for c in batch},
                           embedding_model=kb["embedding_model"])
    assert store.finalize_document(tenant, doc["id"], version, gen,
                                   embedding_model=kb["embedding_model"]).status == "switched"
    return store.get_document_by_id(tenant, doc["id"])


def plant_retrieval(engine, tenant: str, run_id: str, chunk_text: str) -> None:
    """What a turn that called `retrieve` leaves in the run records: a tool_result event with the
    sources and the section text the model saw, the tool-result side effect, `runs.messages`."""
    with engine.begin() as conn:
        seq = conn.execute(text("SELECT COALESCE(MAX(seq), -1) + 1 FROM execution_events WHERE run_id = :r"),
                           {"r": run_id}).scalar_one()
        data = {"tool_call_id": "call_r", "tool_name": "retrieve", "result": f"[1] Doc — kb:guide\n{chunk_text}",
                "sources": [{"n": 1, "title": "Doc", "url": "kb:guide", "score": 0.9}]}
        conn.execute(text(
            "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
            " VALUES (:t, :r, :s, 'tool:call_r:result', 'tool_result', CAST(:d AS jsonb), now())"
        ), {"t": tenant, "r": run_id, "s": seq, "d": json.dumps(data)})
        conn.execute(text(
            "INSERT INTO side_effects (tenant_id, key, state, result, created_at)"
            " VALUES (:t, :k, 'completed', CAST(:d AS jsonb), now())"
        ), {"t": tenant, "k": f"{run_id}:call_r", "d": json.dumps({"text": chunk_text})})
        conn.execute(text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, operation, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
            " VALUES (:t, :r, 'embed:retrieve:call_r', 'test:hash', 'embedding', 5, 0, 0, 0, true, now())"
        ), {"t": tenant, "r": run_id})
        conn.execute(text("UPDATE runs SET messages = CAST(:m AS jsonb) WHERE tenant_id = :t AND id = :r"),
                     {"t": tenant, "r": run_id, "m": json.dumps([{"parts": [{"content": chunk_text}]}])})


@dataclass
class RmTenant:
    name: str
    headers: dict[str, str]
    kb: dict[str, Any] = field(default_factory=dict)
    ready_doc: dict[str, Any] = field(default_factory=dict)  # key "guide", embedded chunks, active
    other_doc: dict[str, Any] = field(default_factory=dict)  # key "faq", embedded chunks, active
    pending_run: str = ""  # the kb_ingest run of the API upload of "upload.md"
    kb_agent: str = ""  # an agent whose published spec attaches `kb`
    conv_agent: str = ""  # memory.working scope conversation
    res_agent: str = ""  # memory.working scope resource, JSON schema
    c1: str = ""  # conv_agent, resource r1
    c2: str = ""  # res_agent, resource r1
    c3: str = ""  # res_agent, resource r2 (the sibling resource)
    runs: dict[str, str] = field(default_factory=dict)  # conversation id -> its completed turn's run id
    obs: dict[str, str] = field(default_factory=dict)  # conversation id -> observation id

    def marker(self, what: str) -> str:
        return secret(self.name, what)


def build_tenant(client, app, name: str, headers: dict[str, str]) -> RmTenant:
    """Seed one tenant (see the module docstring). Deterministic shape; content carries markers."""
    engine = app.state.database_engine
    t = RmTenant(name, headers)
    kstore, mstore = KnowledgeStore(engine), MemoryStore(engine)

    r = client.post(f"{API}/knowledge-bases", json={"name": "Product docs", "embedding_model": "test:hash"},
                    headers=headers)
    assert r.status_code == 201, r.text
    t.kb = r.json()
    t.ready_doc = ingest_ready(kstore, name, t.kb, "guide", [t.marker("chunk") + " printer floor 3", "reboot it"],
                               metadata={"team": "support"}, seed=1)
    t.other_doc = ingest_ready(kstore, name, t.kb, "faq", ["faq body " + t.marker("faq")],
                               metadata={"team": "sales"}, seed=5)
    r = client.put(f"{API}/knowledge-bases/{t.kb['id']}/documents/upload.md",
                   json={"content": "# Up\n\n" + t.marker("upload"), "mime_type": "text/markdown"}, headers=headers)
    assert r.status_code == 202, r.text
    t.pending_run = r.json()["run_id"]

    def agent(spec_extra: dict[str, Any]) -> str:
        r = client.post(f"{API}/agents", json={"spec": {**SPEC, **spec_extra}}, headers=headers)
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        r = client.post(f"{API}/agents/{aid}/versions", headers=headers)
        assert r.status_code == 201, r.text
        return aid

    t.kb_agent = agent({"knowledge_bases": [{"id": t.kb["id"], "filter": {"team": "support"}}]})
    t.conv_agent = agent({"memory": {"working": {"scope": "conversation"}}})
    t.res_agent = agent({"memory": {"working": {"json_schema": NOTE_SCHEMA}}})

    for attr, aid, rid in (("c1", t.conv_agent, "r1"), ("c2", t.res_agent, "r1"), ("c3", t.res_agent, "r2")):
        r = client.post(f"{API}/agents/{aid}/conversations", json={"resource_id": rid}, headers=headers)
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        setattr(t, attr, cid)
        run_id = str(uuid.uuid4())
        r = turn(client, headers, cid, run_id=run_id, input=t.marker(f"input-{attr}"))
        assert r.status_code == 201, r.text
        finish(app, run_id, output=t.marker(f"output-{attr}"))
        t.runs[cid] = run_id
        plant_retrieval(engine, name, run_id, t.marker(f"retrieved-{attr}"))
        mstore.index_turn(name, aid, cid, run_id,
                          [TurnText("user", t.marker(f"recall-{attr}"), unit_vector(11)),
                           TurnText("assistant", "ok", unit_vector(12))],
                          embedding_model="test:hash")
        t.obs[cid] = mstore.insert_observation(name, aid, cid, covers_from_turn=1, covers_to_turn=1,
                                               content=t.marker(f"obs-{attr}"), run_id=run_id).observation.id

    r = client.put(f"{API}/conversations/{t.c1}/memory/working", json={"content": t.marker("note-c1")}, headers=headers)
    assert r.status_code == 200, r.text
    for rid in ("r1", "r2"):
        r = client.put(f"{API}/agents/{t.res_agent}/memory/resources/{rid}/working",
                       json={"content": {"name": t.marker(f"note-{rid}")}}, headers=headers)
        assert r.status_code == 200, r.text
    return t


def snapshot(engine, tenants: list[str]) -> dict[str, dict[str, list[str]]]:
    """Every row of each tenant in every RM_TABLES table, as text, in a stable order."""
    out: dict[str, dict[str, list[str]]] = {}
    with engine.connect() as conn:
        for tenant in tenants:
            out[tenant] = {
                table: list(conn.execute(text(f"SELECT x::text FROM {table} x WHERE tenant_id = :t ORDER BY 1"),
                                         {"t": tenant}).scalars())
                for table in RM_TABLES
            }
    return out


def cleanup_rm(engine, tenants: list[str]) -> None:
    """The RM rows the shared cleanup_tenant does not remove (FK order: memory rows reference runs
    and conversations; knowledge bases are referenced by kb_ingest runs)."""
    with engine.begin() as conn:
        for tenant in tenants:
            for table in ("message_embeddings", "memory_observations", "working_memories",
                          "memory_forget_watermarks"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
            run_ids = [r[0] for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": tenant})]
            for run_id in run_ids:
                conn.execute(text("DELETE FROM execution_events WHERE run_id = :r"), {"r": run_id})
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
