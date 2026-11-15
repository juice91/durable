"""Helpers for kb_ingest (RM.10) tests: knowledge bases, documents, the API's hand-off, and queries."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from eap_run_store import KnowledgeStore
from eap_schemas.knowledge import kb_ingest_partition_key


def markdown_doc(sections: int, *, tag: str = "alpha") -> str:
    """A Markdown document of `sections` `##` sections, each long enough (> min_tokens 40) to stay
    its own chunk, with distinct words so every embed text (and hash) differs."""
    parts = [f"# Handbook {tag}\n"]
    for i in range(sections):
        words = " ".join(f"w{tag}{i}x{j}" for j in range(40))
        parts.append(f"## Section {i}\n\nSection {i} of the {tag} handbook explains {words}.\n")
    return "\n".join(parts)


def create_kb(store: KnowledgeStore, tenant_id: str, *, model: str = "test:hash", dimensions: int = 1536) -> str:
    kb = store.create_knowledge_base(
        tenant_id, name=f"kb-{uuid.uuid4().hex[:8]}", embedding_model=model, dimensions=dimensions
    )
    return kb["id"]


def upload(store: KnowledgeStore, tenant_id: str, kb_id: str, content: str, *, key: str = "handbook",
           metadata: dict[str, Any] | None = None) -> tuple[str, int, uuid.UUID]:
    """put_document as the API does it: returns (document_id, version, run_id for that version)."""
    run_id = uuid.uuid4()
    result = store.put_document(
        tenant_id, kb_id, document_key=key, title="Handbook", content=content, mime_type="text/markdown",
        metadata=metadata or {"team": "support"}, run_id=str(run_id),
    )
    return result.document["id"], result.document["latest_version"], run_id


def ingest_input(document_id: str, version: int) -> str:
    return json.dumps({"doc_version": version, "document_id": document_id}, sort_keys=True, separators=(",", ":"))


def reindex_input(generation: int) -> str:
    return json.dumps({"reindex": generation}, sort_keys=True, separators=(",", ":"))


def enqueue_kb_ingest(client, run_store, *, tenant_id: str, kb_id: str, input_json: str, app_version: str,
                      run_id: uuid.UUID | None = None, legacy_queue: bool = False) -> uuid.UUID:
    """The API's hand-off (apps/api launcher.launch_kb_ingest): insert the run row, then enqueue on
    the partitioned `eap-kb-ingest-fair` keyed by tenant and knowledge base. `legacy_queue` enqueues
    as the API did before the partitioning: on the unpartitioned `eap-kb-ingest`, with no key."""
    run_id = run_id or uuid.uuid4()
    run_store.create_kb_ingest_run(tenant_id, kb_id, input_json, run_id)
    options: dict[str, Any] = {
        "queue_name": "eap-kb-ingest" if legacy_queue else "eap-kb-ingest-fair",
        "workflow_name": "eap.kb_ingest",
        "workflow_id": str(run_id),
        "app_version": app_version,
    }
    if not legacy_queue:
        options["queue_partition_key"] = kb_ingest_partition_key(tenant_id, str(kb_id))
    client.enqueue(
        options,
        str(run_id),
        tenant_id,
        str(kb_id),
        input_json,
    )
    return run_id


def usage_rows(engine, run_id) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT request_key, operation, input_tokens, cost_usd FROM run_model_requests"
                    " WHERE run_id = :r ORDER BY request_key"
                ),
                {"r": str(run_id)},
            ).mappings()
        ]


def chunk_stats(engine, tenant_id: str, document_id: str) -> list[dict[str, Any]]:
    """Per (doc_version, generation): rows, embedded rows, distinct ordinals, dimensions."""
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT doc_version, generation, count(*) AS n, count(embedding) AS embedded,"
                    " count(DISTINCT ordinal) AS ordinals, max(dimensions) AS dims FROM kb_chunks"
                    " WHERE tenant_id = :t AND document_id = :d GROUP BY doc_version, generation"
                    " ORDER BY doc_version, generation"
                ),
                {"t": tenant_id, "d": document_id},
            ).mappings()
        ]


def cleanup_knowledge(engine, tenant_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant_id})


def assert_contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events))), [e[:2] for e in events]
    keys = [e[1] for e in events]
    assert len(keys) == len(set(keys)), keys
