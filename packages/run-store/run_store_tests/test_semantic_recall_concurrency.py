"""MemoryStore semantic recall (RM.8) under forced interleavings: index_turn takes the conversation
FOR KEY SHARE before its embedding rows (lock order conversation → working memory → observations →
message embeddings → run → ledger), so it serialises with delete_conversation and forget_resource
without deadlocks; two indexers of one turn write once; retention and recall never wait on locks
they do not need."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import ConversationNotFound, MemoryStore, RunStore, TurnText

from .test_knowledge_store import vec
from .test_knowledge_store_concurrency import interleave
from .test_memory_store import cleanup_tenant, conversation, count, run_of

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

MODEL = "test:hash"
INDEX_LOCK = ("FROM conversations", "FOR KEY SHARE")
INDEX_INSERT = ("INSERT INTO message_embeddings",)
DELETE_LOCK = ("FROM conversations", "FOR UPDATE")


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"recc_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    cleanup_tenant(engine, tenant)


def _index(store, engine, tenant, agent, conv, n=1, text_="hello"):
    run_id = run_of(engine, tenant, conv, n)
    return lambda: store.index_turn(
        tenant, agent, conv, run_id,
        [TurnText("user", text_, vec(1536, n)), TurnText("assistant", "answer", vec(1536, n + 1))],
        embedding_model=MODEL,
    )


def _rows(engine, tenant) -> int:
    return count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant)


@pytest.mark.parametrize("needles", [INDEX_LOCK, INDEX_INSERT])
def test_delete_conversation_waits_for_index_turn(store, runs, engine, tenant, agent, needles):
    conv = conversation(runs, tenant, agent, turns=1)
    idx = ("index", _index(store, engine, tenant, agent, conv))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    indexed, deleted = interleave(engine, idx, needles, delete, blocks=True)
    assert indexed.inserted == ("user", "assistant") and deleted is True
    assert _rows(engine, tenant) == 0


def test_index_turn_waits_for_delete_conversation_then_finds_nothing(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    idx = ("index", _index(store, engine, tenant, agent, conv))
    with pytest.raises(ConversationNotFound):
        interleave(engine, delete, DELETE_LOCK, idx, blocks=True)
    assert _rows(engine, tenant) == 0


@pytest.mark.parametrize("include_conversations", [False, True])
def test_forget_resource_waits_for_index_turn_and_deletes_its_rows(
    store, runs, engine, tenant, agent, include_conversations
):
    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    idx = ("index", _index(store, engine, tenant, agent, conv))
    forget = ("forget", lambda: store.forget_resource(tenant, "ana", include_conversations=include_conversations))
    indexed, result = interleave(engine, idx, INDEX_INSERT, forget, blocks=True)
    assert indexed.inserted == ("user", "assistant") and result.message_embeddings == 2
    assert _rows(engine, tenant) == 0


def test_index_turn_waits_for_forget_resource_then_skips(store, runs, engine, tenant, agent):
    """The indexer of a completed turn waits for forget's conversation lock, then finds the forget
    watermark (its turn is at or below it) and writes nothing (a late post-turn step cannot undo the
    forget). With include_conversations the conversation is gone, or the rows go with its delete."""
    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    forget = ("forget", lambda: store.forget_resource(tenant, "ana"))
    idx = ("index", _index(store, engine, tenant, agent, conv))
    result, indexed = interleave(engine, forget, ("FROM conversations", "FOR UPDATE"), idx, blocks=True)
    assert result.message_embeddings == 0 and indexed.skipped is True and indexed.inserted == ()
    assert _rows(engine, tenant) == 0

    conv2 = conversation(runs, tenant, agent, resource="bob", turns=1)
    forget2 = ("forget", lambda: store.forget_resource(tenant, "bob", include_conversations=True))
    idx2 = ("index", _index(store, engine, tenant, agent, conv2))
    try:
        _, indexed2 = interleave(engine, forget2, ("FROM conversations", "FOR UPDATE"), idx2, blocks=True)
        assert indexed2.skipped is True
    except ConversationNotFound:
        pass  # landed after the conversation delete
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c",
                 t=tenant, c=conv2) == 0


def test_two_indexers_of_one_turn_write_once(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    a = ("index_a", _index(store, engine, tenant, agent, conv, text_="from a"))
    b = ("index_b", _index(store, engine, tenant, agent, conv, text_="from b"))
    # a has inserted its rows (uncommitted); b's insert of the same (run_id, role) waits, then skips.
    first, second = interleave(engine, a, INDEX_INSERT, b, blocks=True)
    assert first.inserted == ("user", "assistant") and second.existing == ("user", "assistant")
    assert second.inserted == ()
    assert _rows(engine, tenant) == 2
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT text FROM message_embeddings WHERE tenant_id = :t AND role = 'user'"), {"t": tenant}
        ).scalar_one() == "from a"


def test_recall_does_not_wait_for_a_paused_delete(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    _index(store, engine, tenant, agent, conv)()
    current = conversation(runs, tenant, agent, resource="ana")
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    search = ("search", lambda: store.search_recall(tenant, agent, current, vec(1536, 1), embedding_model=MODEL))
    # delete has removed the rows but not committed: recall reads the committed state without waiting
    deleted, hits = interleave(engine, delete, ("DELETE FROM message_embeddings",), search, blocks=False)
    assert deleted is True and {h.conversation_id for h in hits} == {conv}
    assert store.search_recall(tenant, agent, current, vec(1536, 1), embedding_model=MODEL) == []


def test_retention_and_delete_conversation_serialise_on_rows_only(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    _index(store, engine, tenant, agent, conv)()
    with engine.begin() as conn:
        conn.execute(text("UPDATE message_embeddings SET created_at = now() - interval '3 days' WHERE tenant_id = :t"),
                     {"t": tenant})
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    sweep = ("sweep", lambda: store.delete_message_embeddings_before(tenant, cutoff))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    # the sweep holds the rows it deleted; delete_conversation waits on those rows (not on a lock the
    # sweep could wait for in turn), then finds them gone
    swept, deleted = interleave(engine, sweep, ("DELETE FROM message_embeddings",), delete, blocks=True)
    assert swept == 2 and deleted is True and _rows(engine, tenant) == 0


@pytest.mark.parametrize("holder", ["delete", "forget"])
def test_retention_skips_rows_a_delete_holds(store, runs, engine, tenant, agent, holder):
    """The other order: delete_conversation (or forget_resource) has deleted rows and not committed;
    the sweep skips them (SKIP LOCKED) instead of waiting, so the two can never deadlock."""
    conv = conversation(runs, tenant, agent, resource="ana", turns=2)
    _index(store, engine, tenant, agent, conv, 1)()
    _index(store, engine, tenant, agent, conv, 2)()
    with engine.begin() as conn:
        conn.execute(text("UPDATE message_embeddings SET created_at = now() - interval '3 days' WHERE tenant_id = :t"),
                     {"t": tenant})
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    if holder == "delete":
        first = ("delete", lambda: store.delete_conversation(tenant, conv))
    else:
        first = ("forget", lambda: store.forget_resource(tenant, "ana"))
    sweep = ("sweep", lambda: store.delete_message_embeddings_before(tenant, cutoff))
    _, swept = interleave(engine, first, ("DELETE FROM message_embeddings",), sweep, blocks=False)
    assert swept == 0 and _rows(engine, tenant) == 0
