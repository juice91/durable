"""The shared kb_ingest hand-off (`eap_run_store.kb_ingest_handoff`), used by the API upload route and
the worker's source sync. The route's own behaviour is covered by apps/api/api_tests/test_knowledge_api*."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore, RunStore
from eap_run_store import kb_ingest_handoff as h


class Launcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, **kw) -> None:
        if self.fail:
            raise RuntimeError("queue down: postgresql://u:secret@h/db")
        self.calls.append(kw)


@pytest.fixture
def kb(engine):
    tenant = f"t_{uuid.uuid4().hex[:8]}"
    store = KnowledgeStore(engine)
    kb_id = store.create_knowledge_base(tenant, name="kb", embedding_model="openai:m", dimensions=768)["id"]
    yield tenant, kb_id, store, RunStore(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})


def ingest(kb, launch, content="# Doc\n\nhello world " * 10, **kw):
    tenant, kb_id, store, runs = kb
    return h.ingest_document(
        store, runs, launch, tenant, kb_id, document_key="doc", title="Doc", content=content,
        mime_type="text/markdown", **kw,
    )


def test_a_new_document_is_stored_recorded_and_enqueued_once(kb):
    launch = Launcher()
    out = ingest(kb, launch)
    assert (out.unchanged, out.created) == (False, True) and out.document["latest_version"] == 1
    assert out.run["kind"] == "kb_ingest" and out.run["status"] == "queued" and len(launch.calls) == 1
    assert launch.calls[0]["input_json"] == h.ingest_input(out.document["id"], 1)
    same = ingest(kb, launch)  # same content: nothing written, no run
    assert (same.unchanged, same.run) == (True, {"run_id": None}) and len(launch.calls) == 1
    changed = ingest(kb, launch, content="# Doc\n\nsomething else entirely " * 10)
    assert (changed.unchanged, changed.document["latest_version"]) == (False, 2) and len(launch.calls) == 2


def test_a_retry_with_the_same_run_id_hands_the_recorded_run_off_again(kb):
    launch, run_id = Launcher(), uuid.uuid4()
    first = ingest(kb, launch, run_id=run_id)
    retry = ingest(kb, launch, run_id=run_id)
    assert retry.run["run_id"] == first.run["run_id"] == str(run_id) and len(launch.calls) == 2  # DBOS dedups by id
    with pytest.raises(h.RunIdInUse):
        ingest(kb, launch, content="different content " * 10, run_id=run_id)


def test_a_failed_enqueue_marks_the_run_failed_and_a_retry_requeues_it(kb):
    launch = Launcher()
    launch.fail = True
    run_id = uuid.uuid4()
    with pytest.raises(h.HandOffFailed) as info:
        ingest(kb, launch, run_id=run_id)
    assert info.value.run_id == str(run_id) and "secret" not in str(info.value)
    tenant, _, _, runs = kb
    assert runs.get_run(tenant, run_id)["status"] == "failed"
    launch.fail = False
    retry = ingest(kb, launch, run_id=run_id)
    assert retry.run["status"] == "queued" and len(launch.calls) == 1


def test_a_run_id_of_another_tenant_is_refused_before_anything_is_written(kb, engine):
    tenant, kb_id, store, runs = kb
    other = f"t_{uuid.uuid4().hex[:8]}"
    other_kb = store.create_knowledge_base(other, name="kb", embedding_model="openai:m", dimensions=768)["id"]
    try:
        run_id = uuid.uuid4()
        h.ingest_document(store, runs, Launcher(), other, other_kb, document_key="d", title="d",
                          content="text " * 20, mime_type="text/markdown", run_id=run_id)
        with pytest.raises(h.RunIdInUse):
            ingest(kb, Launcher(), run_id=run_id)
        assert store.get_document(tenant, kb_id, "doc") is None
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": other})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": other})


def test_input_helpers_are_canonical_and_ignore_the_embed_split_flag():
    assert h.ingest_input("d1", 2) == '{"doc_version":2,"document_id":"d1","embed_split":true}'
    assert h.same_ingest_input('{"doc_version":2,"document_id":"d1"}', h.ingest_input("d1", 2))
    assert not h.same_ingest_input('{"doc_version":1,"document_id":"d1"}', h.ingest_input("d1", 2))
    assert not h.same_ingest_input(None, h.reindex_input(3))
