"""The store facade's hand-off extensions the repository adapter relies on: an explicit document key, adapter
metadata under the platform stamps, the ledger's source revision, listing and removal of ledger rows."""

from __future__ import annotations

import uuid

from eap_kb_sources import DocumentPolicy
from eap_run_store import KnowledgeStore
from eap_worker import kb_source_sync as sync
from worker_tests.test_kb_source_sync import env, fake, launched  # noqa: F401  (fixtures)

KEY = "gh:docs~a.md"


def facade(env):
    return sync.WorkerStoreFacade(env.tenant, env.source.id, env.kb_id, str(uuid.uuid4()), DocumentPolicy())


def entity(store):
    return store.upsert_entity("o/r", entity_type="repository", identifiers=[("repository", "o/r", True)])


def submit(store, ent, data=b"# A\n\nhello\n", **kw):
    return store.submit_document(
        ent, "file", "https://github.com/o/r/blob/abc/docs/a.md", data, mime_type="text/markdown", title="docs/a.md",
        source_revision=kw.pop("source_revision", "blob1:m1"), key=KEY, **kw,
    )


def test_an_explicit_key_and_metadata_reach_the_knowledge_base_document(env, launched):
    store = facade(env)
    ent = entity(store)
    assert store.document_state(ent, "file", "u", key=KEY) is None
    assert submit(store, ent, metadata={"sdk_family": "mcu", "device": "AM263x", "path": "docs/a.md", "source": "forged"}) == "queued"
    doc = env.kb.get_document(env.tenant, env.kb_id, KEY)
    assert doc["metadata"]["sdk_family"] == "mcu" and doc["metadata"]["path"] == "docs/a.md"
    assert doc["metadata"]["source"] == "kb_source" and doc["metadata"]["source_id"] == env.source.id  # stamps win
    assert doc["metadata"]["relationship"] == "file" and doc["metadata"]["external_id"] == "o/r"
    state = store.document_state(ent, "file", "u", key=KEY)
    assert state is not None and state.source_revision == "blob1:m1" and state.status == "queued"
    assert len(launched) == 1
    # same bytes and metadata: no new version
    assert submit(store, ent, metadata={"sdk_family": "mcu", "device": "AM263x", "path": "docs/a.md"}) == "unchanged"
    # other metadata: a new version
    assert submit(store, ent, metadata={"sdk_family": "other", "path": "docs/a.md"}, source_revision="blob1:m2") == "queued"
    assert store.document_state(ent, "file", "u", key=KEY).source_revision == "blob1:m2"


def test_list_and_remove_keep_or_delete_the_knowledge_base_document(env, launched):
    store = facade(env)
    ent = entity(store)
    submit(store, ent)
    rows = store.list_documents(ent, "file")
    assert [(r.key, r.status, r.source_revision) for r in rows] == [(KEY, "queued", "blob1:m1")]
    assert store.list_documents(ent, "other") == []

    store.remove_document(ent, KEY, delete=False)
    assert [r.status for r in store.list_documents(ent, "file")] == ["removed"]
    assert env.kb.get_document(env.tenant, env.kb_id, KEY) is not None  # still in the knowledge base
    assert store.document_state(ent, "file", "u", key=KEY) is None  # a removed row is not "unchanged"
    assert submit(store, ent) == "unchanged"  # the file came back: the ledger row is queued again
    assert [r.status for r in store.list_documents(ent, "file")] == ["queued"]

    store.remove_document(ent, KEY, delete=True)
    assert env.kb.get_document(env.tenant, env.kb_id, KEY) is None
    assert store.list_documents(ent, "file") == []  # the ledger row followed the document
    store.remove_document(ent, KEY, delete=True)  # idempotent
