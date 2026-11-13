"""MemoryStore semantic recall (RM.8): idempotent index_turn, search_recall scoping (tenant, agent,
resource, conversation, embedding model), excluded turns, neighbours, deletion by conversation,
resource and age, and the HNSW index of each dimension."""

from __future__ import annotations

import math
import os
import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import (
    ConversationNotFound,
    MemoryEvent,
    MemoryStore,
    RunNotFound,
    RunStore,
    TurnText,
)

from .test_knowledge_store import vec
from .test_memory_store import add_turn, cleanup_tenant, conversation, count, run_of

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

MODEL = "test:hash"
MODEL_768 = "test:hash768"
MODEL_1024 = "test:hash1024"


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"rec_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    for t in (tenant, tenant + "_b"):
        cleanup_tenant(engine, t)


def index(store, tenant, agent, conv, n, *, hot=None, dims=1536, model=MODEL, user=None, answer=None, engine=None):
    """Index turn `n` of `conv` (both roles; the user text on axis `hot`, the answer on hot + 1)."""
    run_id = run_of(engine or store.engine, tenant, conv, n)
    h = n if hot is None else hot
    return store.index_turn(
        tenant, agent, conv, run_id,
        [TurnText("user", user or f"user says {n}", vec(dims, h)),
         TurnText("assistant", answer or f"answer {n}", vec(dims, h + 1000))],
        embedding_model=model,
    )


def recall(store, tenant, agent, conv, hot, *, dims=1536, model=MODEL, **kw):
    kw.setdefault("top_k", 10)
    return store.search_recall(tenant, agent, conv, vec(dims, hot), embedding_model=model, **kw)


# ---------------------------------------------------------------------------------------- index


def test_index_turn_is_idempotent_by_run_and_role(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    first = index(store, tenant, agent, conv, 1)
    assert (first.turn, first.resource_id, first.inserted, first.existing) == (1, "u1", ("user", "assistant"), ())
    again = index(store, tenant, agent, conv, 1, user="different text")
    assert (again.inserted, again.existing) == ((), ("user", "assistant"))
    rows = count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant)
    assert rows == 2
    texts = {h.text for h in recall(store, tenant, agent, conv, 1)}
    assert "user says 1" in texts and "different text" not in texts  # the first write stands


def test_index_turn_takes_turn_and_resource_from_the_database(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=3)
    result = index(store, tenant, agent, conv, 3)
    assert (result.turn, result.resource_id) == (3, "ana")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT turn, resource_id, dimensions, embedding_model FROM message_embeddings"
                 " WHERE tenant_id = :t AND role = 'user'"), {"t": tenant}
        ).one()
    assert tuple(row) == (3, "ana", 1536, MODEL)


def test_index_turn_with_event_appends_it_once(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    items = [TurnText("user", "hello", vec(1536, 1))]
    event = MemoryEvent(run_id=run_id, event_key="embed:index:1", type="embedding")
    for _ in range(2):
        store.index_turn(tenant, agent, conv, run_id, items, embedding_model=MODEL, event=event)
    with engine.connect() as conn:
        data = conn.execute(
            text("SELECT data FROM execution_events WHERE tenant_id = :t AND run_id = :r"), {"t": tenant, "r": run_id}
        ).scalars().all()
    assert data == [{"model_id": MODEL, "dimensions": 1536, "turn": 1, "inserted": 1, "existing": 0}]


def test_index_turn_checks_conversation_and_run(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    other = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    items = [TurnText("user", "x", vec(1536, 1))]
    with pytest.raises(ConversationNotFound):  # another agent's conversation
        store.index_turn(tenant, str(uuid.uuid4()), conv, run_id, items, embedding_model=MODEL)
    with pytest.raises(ConversationNotFound):  # another tenant
        store.index_turn(tenant + "_b", agent, conv, run_id, items, embedding_model=MODEL)
    with pytest.raises(RunNotFound):  # a run of another conversation
        store.index_turn(tenant, agent, other, run_id, items, embedding_model=MODEL)
    with pytest.raises(RunNotFound):  # an unknown run
        store.index_turn(tenant, agent, conv, str(uuid.uuid4()), items, embedding_model=MODEL)
    with pytest.raises(RunNotFound):  # the event names a run of another conversation
        store.index_turn(tenant, agent, conv, run_id, items, embedding_model=MODEL,
                         event=MemoryEvent(run_id=run_of(engine, tenant, other, 1), event_key="e"))
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 0


@pytest.mark.parametrize(
    "items",
    [
        [],
        [TurnText("user", "a", vec(1536, 1)), TurnText("user", "b", vec(1536, 2))],
        [TurnText("system", "a", vec(1536, 1))],
        [TurnText("user", "  ", vec(1536, 1))],
        [TurnText("user", "a", vec(512, 1))],
        [TurnText("user", "a", vec(1536, 1)), TurnText("assistant", "b", vec(768, 1))],
        [TurnText("user", "a", [float("nan")] * 768)],
    ],
)
def test_index_turn_rejects_bad_items(store, runs, engine, tenant, agent, items):
    conv = conversation(runs, tenant, agent, turns=1)
    with pytest.raises(ValueError):
        store.index_turn(tenant, agent, conv, run_of(engine, tenant, conv, 1), items, embedding_model=MODEL)


# --------------------------------------------------------------------------------------- search


def test_search_ranks_by_cosine_and_applies_min_score(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=3)
    for n in (1, 2, 3):
        index(store, tenant, agent, conv, n)
    current = conversation(runs, tenant, agent)
    hits = recall(store, tenant, agent, current, 2, top_k=3)
    assert (hits[0].turn, hits[0].role, hits[0].text) == (2, "user", "user says 2")
    assert hits[0].score == pytest.approx(1.0) and hits[0].distance == pytest.approx(0.0, abs=1e-6)
    assert [h.distance for h in hits] == sorted(h.distance for h in hits) and len(hits) == 3
    assert [(h.turn, h.role) for h in recall(store, tenant, agent, current, 2, min_score=0.9)] == [(2, "user")]


def test_resource_scope_isolation(store, runs, engine, tenant, agent):
    """Another resource of the same tenant and agent, another agent and another tenant never recall."""
    mine = conversation(runs, tenant, agent, resource="ana", turns=1)
    index(store, tenant, agent, mine, 1, hot=7)
    other_resource = conversation(runs, tenant, agent, resource="bob", turns=1)
    index(store, tenant, agent, other_resource, 1, hot=7)
    other_agent = str(uuid.uuid4())
    oa = conversation(runs, tenant, other_agent, resource="ana", turns=1)
    index(store, tenant, other_agent, oa, 1, hot=7)
    ot = conversation(runs, tenant + "_b", agent, resource="ana", turns=1)
    index(store, tenant + "_b", agent, ot, 1, hot=7)

    current = conversation(runs, tenant, agent, resource="ana")
    assert {h.conversation_id for h in recall(store, tenant, agent, current, 7)} == {mine}
    bob_now = conversation(runs, tenant, agent, resource="bob")
    assert {h.conversation_id for h in recall(store, tenant, agent, bob_now, 7)} == {other_resource}
    # another agent's or tenant's view of "ana"
    assert {h.conversation_id for h in recall(store, tenant, other_agent, conversation(runs, tenant, other_agent,
                                                                                         resource="ana"), 7)} == {oa}
    # the current conversation must belong to the caller's tenant and agent
    with pytest.raises(ConversationNotFound):
        recall(store, tenant, other_agent, current, 7)
    with pytest.raises(ConversationNotFound):
        recall(store, tenant + "_b", agent, current, 7)


def test_conversation_scope_and_resourceless_fallback(store, runs, engine, tenant, agent):
    a = conversation(runs, tenant, agent, resource="ana", turns=2)
    b = conversation(runs, tenant, agent, resource="ana", turns=1)
    for n in (1, 2):
        index(store, tenant, agent, a, n, hot=5)
    index(store, tenant, agent, b, 1, hot=5)
    assert {h.conversation_id for h in recall(store, tenant, agent, a, 5, scope="conversation")} == {a}
    assert {h.conversation_id for h in recall(store, tenant, agent, a, 5)} == {a, b}
    # A conversation without a resource id recalls only its own rows, even in resource scope.
    lone = conversation(runs, tenant, agent, resource=None, turns=1)
    index(store, tenant, agent, lone, 1, hot=5)
    assert {h.conversation_id for h in recall(store, tenant, agent, lone, 5)} == {lone}
    with pytest.raises(ValueError):
        recall(store, tenant, agent, a, 5, scope="tenant")


def test_rows_under_another_embedding_model_are_ignored(store, runs, engine, tenant, agent):
    old = conversation(runs, tenant, agent, turns=2)
    index(store, tenant, agent, old, 1, hot=3, model="test:old-model")
    index(store, tenant, agent, old, 2, hot=3, model=MODEL)
    current = conversation(runs, tenant, agent)
    assert {h.turn for h in recall(store, tenant, agent, current, 3)} == {2}
    assert {h.turn for h in recall(store, tenant, agent, current, 3, model="test:old-model")} == {1}
    # same model name, other dimensions: the dimension predicate keeps them apart
    assert recall(store, tenant, agent, current, 3, dims=768) == []


def test_excluded_turns_never_return(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=4)
    other = conversation(runs, tenant, agent, resource="ana", turns=2)
    for n in range(1, 5):
        index(store, tenant, agent, conv, n, hot=9)
    for n in (1, 2):
        index(store, tenant, agent, other, n, hot=9)
    hits = recall(store, tenant, agent, conv, 9, top_k=50, exclude_turns=[2, 3, 4])
    # only this conversation's turns 2..4 are excluded; the other conversation's turn 2 still returns
    assert {(h.conversation_id, h.turn) for h in hits} == {(conv, 1), (other, 1), (other, 2)}
    assert {(h.conversation_id, h.turn) for h in recall(store, tenant, agent, conv, 9, top_k=50,
                                                         scope="conversation", exclude_turns=[1])} == {
        (conv, 2), (conv, 3), (conv, 4)}


@pytest.mark.parametrize("dims,model", [(768, MODEL_768), (1024, MODEL_1024), (1536, MODEL)])
def test_search_uses_the_hnsw_index_of_its_dimension(store, runs, engine, tenant, agent, dims, model):
    conv = conversation(runs, tenant, agent, turns=2)
    for n in (1, 2):
        index(store, tenant, agent, conv, n, dims=dims, model=model)
    hits = recall(store, tenant, agent, conv, 1, dims=dims, model=model, top_k=2)
    assert hits[0].turn == 1 and hits[0].role == "user"
    for kw in ({}, {"scope": "conversation", "exclude_turns": [2], "min_score": 0.1}):
        plan = store.explain_search_recall(tenant, agent, conv, vec(dims, 1), embedding_model=model, top_k=4, **kw)
        assert f"ix_message_embeddings_embedding_{dims}" in plan, plan


def test_search_validates_arguments(store, runs, tenant, agent):
    conv = conversation(runs, tenant, agent)
    for kw in ({"top_k": 0}, {"top_k": 51}, {"min_score": 1.5}):
        with pytest.raises(ValueError):
            recall(store, tenant, agent, conv, 1, **kw)
    with pytest.raises(ValueError):
        recall(store, tenant, agent, conv, 1, dims=512)
    with pytest.raises(ValueError):
        recall(store, tenant, agent, conv, 1, model="no-provider")


# ------------------------------------------------------------------------------------ neighbours


def test_neighbours_returns_the_range_in_order(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=5)
    for n in range(1, 6):
        index(store, tenant, agent, conv, n)
    got = store.recall_neighbours(tenant, agent, conv, 3, 1, current_conversation_id=conv)
    assert [(m.turn, m.role) for m in got] == [
        (2, "user"), (2, "assistant"), (3, "user"), (3, "assistant"), (4, "user"), (4, "assistant")]
    assert [m.turn for m in store.recall_neighbours(tenant, agent, conv, 1, 0, current_conversation_id=conv)] == [1, 1]
    assert {m.turn for m in store.recall_neighbours(tenant, agent, conv, 5, 3, current_conversation_id=conv, exclude_turns=[4, 5])} == {2, 3}
    # tenant and agent scoped
    assert store.recall_neighbours(tenant, str(uuid.uuid4()), conv, 3, 1, current_conversation_id=conv) == []
    assert store.recall_neighbours(tenant + "_b", agent, conv, 3, 1, current_conversation_id=conv) == []
    with pytest.raises(ValueError):
        store.recall_neighbours(tenant, agent, conv, 3, -1, current_conversation_id=conv)


def test_neighbours_return_stored_text_not_raw_input(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    index(store, tenant, agent, conv, 1, user="my email is [EMAIL]")
    (user, _) = store.recall_neighbours(tenant, agent, conv, 1, 0, current_conversation_id=conv)
    assert user.text == "my email is [EMAIL]"  # the runs row says "in 1"


# -------------------------------------------------------------------------------------- deletes


def test_delete_conversation_removes_its_embeddings(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=2)
    keep = conversation(runs, tenant, agent, resource="ana", turns=1)
    for n in (1, 2):
        index(store, tenant, agent, conv, n, hot=4)
    index(store, tenant, agent, keep, 1, hot=4)
    assert store.delete_conversation(tenant, conv) is True
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c",
                 t=tenant, c=conv) == 0
    now = conversation(runs, tenant, agent, resource="ana")
    assert {h.conversation_id for h in recall(store, tenant, agent, now, 4)} == {keep}


def test_the_conversation_fk_cascades(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    index(store, tenant, agent, conv, 1)
    with engine.begin() as conn:  # runs keep no FK to a deleted conversation (delete_conversation detaches them)
        conn.execute(text("UPDATE runs SET conversation_id = NULL, turn = NULL WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t AND id = :c"), {"t": tenant, "c": conv})
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 0


def test_index_after_delete_conversation_finds_no_conversation(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    store.delete_conversation(tenant, conv)
    with pytest.raises(ConversationNotFound):
        store.index_turn(tenant, agent, conv, run_id, [TurnText("user", "x", vec(1536, 1))], embedding_model=MODEL)


@pytest.mark.parametrize("per_agent", [True, False])
def test_forget_resource_removes_its_embeddings(store, runs, engine, tenant, agent, per_agent):
    other_agent = str(uuid.uuid4())
    a = conversation(runs, tenant, agent, resource="ana", turns=1)
    b = conversation(runs, tenant, other_agent, resource="ana", turns=1)
    bob = conversation(runs, tenant, agent, resource="bob", turns=1)
    lone = conversation(runs, tenant, agent, resource=None, turns=1)
    ot = conversation(runs, tenant + "_b", agent, resource="ana", turns=1)
    for t, ag, c in ((tenant, agent, a), (tenant, other_agent, b), (tenant, agent, bob), (tenant, agent, lone),
                     (tenant + "_b", agent, ot)):
        index(store, t, ag, c, 1)
    result = store.forget_resource(tenant, "ana", agent_id=agent if per_agent else None)
    assert result.message_embeddings == (2 if per_agent else 4)
    left = count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = 'ana'",
                 t=tenant)
    assert left == (2 if per_agent else 0)
    # other resources, resourceless conversations and other tenants are untouched
    for c, t in ((bob, tenant), (lone, tenant), (ot, tenant + "_b")):
        assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c",
                     t=t, c=c) == 2
    # the conversations themselves stay (include_conversations is off), idempotent
    assert runs.get_conversation(tenant, a) is not None
    assert store.forget_resource(tenant, "ana", agent_id=agent if per_agent else None).message_embeddings == 0


def test_forget_resource_busy_deletes_no_embeddings(store, runs, engine, tenant, agent):
    from eap_run_store import ResourceBusy

    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    index(store, tenant, agent, conv, 1)
    add_turn(engine, tenant, agent, conv, 2, status="running")
    with pytest.raises(ResourceBusy):
        store.forget_resource(tenant, "ana")
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 2


def test_retention_deletes_by_age_in_batches(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=4)
    for n in range(1, 5):
        index(store, tenant, agent, conv, n)
    other = conversation(runs, tenant + "_b", agent, turns=1)
    index(store, tenant + "_b", agent, other, 1)
    with engine.begin() as conn:  # back-date turns 1..3 (6 rows) and the other tenant's rows
        conn.execute(text("UPDATE message_embeddings SET created_at = now() - interval '3 days'"
                          " WHERE (tenant_id = :t AND turn <= 3) OR tenant_id = :b"), {"t": tenant, "b": tenant + "_b"})
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    tenants = store.message_embedding_tenants_before(cutoff)
    assert {tenant, tenant + "_b"} <= set(tenants)
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=4) == 4
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=4) == 2
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=4) == 0
    assert {m.turn for m in store.recall_neighbours(tenant, agent, conv, 4, 3, current_conversation_id=conv)} == {4}
    # another tenant's old rows wait for their own batch
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant + "_b") == 2
    with pytest.raises(ValueError):
        store.delete_message_embeddings_before(tenant, datetime.now())


def test_deleting_a_run_cascades_to_its_embeddings(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    index(store, tenant, agent, conv, 1)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 0


def test_neighbours_are_scoped_to_the_current_conversations_resource(store, runs, engine, tenant, agent):
    ana = conversation(runs, tenant, agent, resource="ana", turns=2)
    bob = conversation(runs, tenant, agent, resource="bob", turns=2)
    lone = conversation(runs, tenant, agent, resource=None, turns=2)
    for c in (ana, bob, lone):
        for n in (1, 2):
            index(store, tenant, agent, c, n)
    current = conversation(runs, tenant, agent, resource="ana")
    assert {m.turn for m in store.recall_neighbours(tenant, agent, ana, 1, 1, current_conversation_id=current)} == {1, 2}
    assert store.recall_neighbours(tenant, agent, bob, 1, 1, current_conversation_id=current) == []
    assert store.recall_neighbours(tenant, agent, lone, 1, 1, current_conversation_id=current) == []
    # a current conversation without a resource sees only its own rows
    assert {m.turn for m in store.recall_neighbours(tenant, agent, lone, 1, 1, current_conversation_id=lone)} == {1, 2}
    assert store.recall_neighbours(tenant, agent, ana, 1, 1, current_conversation_id=lone) == []
    # an unknown, another agent's or another tenant's current conversation sees nothing
    assert store.recall_neighbours(tenant, agent, ana, 1, 1, current_conversation_id=str(uuid.uuid4())) == []
    other_agent = conversation(runs, tenant, str(uuid.uuid4()), resource="ana")
    assert store.recall_neighbours(tenant, agent, ana, 1, 1, current_conversation_id=other_agent) == []


# ------------------------------------------------------------------------------- forget watermarks


def _watermark(engine, tenant, conv):
    with engine.connect() as conn:
        return conn.execute(text("SELECT forgotten_through_turn FROM memory_forget_watermarks"
                                 " WHERE tenant_id = :t AND conversation_id = :c"), {"t": tenant, "c": conv}).scalar()


def test_index_after_forget_skips_turns_that_existed_before_it(store, runs, engine, tenant, agent):
    """Crash replay: turns completed, the resource was forgotten, then the (recovered) post-turn
    index step runs: it writes nothing. A turn started after the forget indexes; a replay of an old
    turn still skips. Turn numbers only, no clocks."""
    conv = conversation(runs, tenant, agent, resource="ana", turns=2)
    run2 = run_of(engine, tenant, conv, 2)
    store.forget_resource(tenant, "ana")
    assert _watermark(engine, tenant, conv) == 2
    late = store.index_turn(tenant, agent, conv, run2, [TurnText("user", "secret", vec(1536, 1))],
                            embedding_model=MODEL, event=MemoryEvent(run_id=run2, event_key="embed:index:2"))
    assert late.skipped is True and (late.turn, late.inserted, late.existing) == (2, (), ())
    assert index(store, tenant, agent, conv, 1).skipped is True
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 0
    assert count(engine, "SELECT count(*) FROM execution_events WHERE run_id = :r", r=run2) == 0
    add_turn(engine, tenant, agent, conv, 3)  # a turn started after the forget
    fresh = index(store, tenant, agent, conv, 3)
    assert fresh.skipped is False and fresh.inserted == ("user", "assistant")
    assert index(store, tenant, agent, conv, 2).skipped is True  # every replay of the old turn skips
    # a second forget raises the watermark to the max; it never lowers it
    store.forget_resource(tenant, "ana")
    assert _watermark(engine, tenant, conv) == 3
    with engine.begin() as conn:
        conn.execute(text("UPDATE memory_forget_watermarks SET forgotten_through_turn = 9 WHERE tenant_id = :t"),
                     {"t": tenant})
    store.forget_resource(tenant, "ana")
    assert _watermark(engine, tenant, conv) == 9


def test_forget_of_an_empty_conversation_does_not_block_its_first_turn(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana")
    store.forget_resource(tenant, "ana")
    assert _watermark(engine, tenant, conv) == 0
    add_turn(engine, tenant, agent, conv, 1)
    assert index(store, tenant, agent, conv, 1).inserted == ("user", "assistant")


def test_watermarks_per_agent_or_all_agents_and_per_tenant(store, runs, engine, tenant, agent):
    other = str(uuid.uuid4())
    mine = conversation(runs, tenant, agent, resource="ana", turns=1)
    theirs = conversation(runs, tenant, other, resource="ana", turns=1)
    lone = conversation(runs, tenant, agent, resource=None, turns=1)
    bob = conversation(runs, tenant, agent, resource="bob", turns=1)
    elsewhere = conversation(runs, tenant + "_b", agent, resource="ana", turns=1)
    store.forget_resource(tenant, "ana", agent_id=other)  # one agent
    assert _watermark(engine, tenant, theirs) == 1 and _watermark(engine, tenant, mine) is None
    assert index(store, tenant, other, theirs, 1).skipped is True
    assert index(store, tenant, agent, mine, 1).skipped is False  # another agent's forget
    store.forget_resource(tenant, "ana")  # every agent
    assert _watermark(engine, tenant, mine) == 1
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 0
    assert index(store, tenant, agent, mine, 1).skipped is True
    for c in (lone, bob):  # no resource, or another resource: unaffected
        assert _watermark(engine, tenant, c) is None and index(store, tenant, agent, c, 1).skipped is False
    assert index(store, tenant + "_b", agent, elsewhere, 1).skipped is False  # another tenant
    assert _watermark(engine, tenant + "_b", elsewhere) is None


def test_watermark_cascades_with_the_conversation_and_survives_retention(store, runs, engine, tenant, agent):
    gone = conversation(runs, tenant, agent, resource="ana", turns=1)
    kept = conversation(runs, tenant, agent, resource="ana", turns=1)
    store.forget_resource(tenant, "ana")
    store.delete_message_embeddings_before(tenant, datetime.now(timezone.utc) + timedelta(days=1))
    assert _watermark(engine, tenant, kept) == 1
    assert store.delete_conversation(tenant, gone) is True
    assert _watermark(engine, tenant, gone) is None  # nothing of the resource lingers
    store.forget_resource(tenant, "ana", include_conversations=True)
    assert count(engine, "SELECT count(*) FROM memory_forget_watermarks WHERE tenant_id = :t", t=tenant) == 0


# ------------------------------------------------------------------ the HNSW path on a larger table


def test_exclusion_and_top_k_through_the_hnsw_index(tenant, agent):
    """With sequential scans (and sorts) off, so the partial HNSW index answers: filters and
    exclusions are applied during the iterative index scan and top_k is still filled.

    Runs in its own throwaway schema: thousands of rows (and the statistics an ANALYZE or
    autovacuum takes of them) would change the plans other tests of the session's schema see."""
    from alembic import command
    from sqlalchemy import create_engine, event
    from sqlalchemy.engine import make_url

    from eap_db import alembic_config
    from eap_db.testing import drop_schema, schema_url

    from .conftest import DATABASE_URL

    url = make_url(DATABASE_URL)
    base = url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)
    schema = f"eap_test_hnsw_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    with engine.begin() as conn:
        command.upgrade(alembic_config(conn), "head")
    runs = RunStore(engine)
    hnsw_engine = create_engine(schema_url(base, schema))

    @event.listens_for(hnsw_engine, "connect")
    def _no_seqscan(dbapi_conn, _record):
        # A few thousand rows still look cheaper to sort than to walk HNSW; with sorts off, the
        # ordered HNSW scan is the planner's only good path (as on a production-sized table).
        with dbapi_conn.cursor() as cur:
            for setting in ("enable_seqscan", "enable_bitmapscan", "enable_sort", "enable_incremental_sort"):
                cur.execute(f"SET {setting} = off")

    store = MemoryStore(hnsw_engine)
    dims = 768

    rng = random.Random(7)

    def near(radius: float) -> dict[int, float]:
        """The query axis plus noise of exactly `radius` on 32 random other axes: cosine distance to
        the query grows with the radius, and random directions keep the HNSW graph well connected."""
        axes = rng.sample(range(1, dims), 32)
        noise = [rng.gauss(0, 1) for _ in axes]
        scale = radius / math.sqrt(sum(x * x for x in noise))
        return {0: 1.0, **{a: x * scale for a, x in zip(axes, noise)}}

    def literal(weights: dict[int, float]) -> str:
        v = ["0"] * dims
        for axis, w in weights.items():
            v[axis] = repr(w)
        return "[" + ",".join(v) + "]"

    def bulk(conv: str, resource: str, turns: int, weights, model: str = MODEL_768) -> None:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
                " created_at, updated_at) SELECT gen_random_uuid()::text, :t, 'agent', :a, 1, 'completed', 'x', :c, n,"
                " now(), now() FROM generate_series(1, :n) n"), {"t": tenant, "a": agent, "c": conv, "n": turns})
            ids = dict(conn.execute(text("SELECT turn, id FROM runs WHERE tenant_id = :t AND conversation_id = :c"),
                                    {"t": tenant, "c": conv}).all())
            conn.execute(text(
                "INSERT INTO message_embeddings (tenant_id, agent_id, resource_id, conversation_id, turn, run_id, role,"
                " text, embedding_model, dimensions, embedding)"
                " VALUES (:t, :a, :res, :c, :turn, :r, 'user', :txt, :m, 768, CAST(:e AS public.vector))"),
                [{"t": tenant, "a": agent, "res": resource, "c": conv, "turn": n, "r": ids[n], "txt": f"{conv} {n}",
                  "m": model, "e": literal(weights(n))} for n in range(1, turns + 1)])

    current = conversation(runs, tenant, agent, resource="ana")
    other = conversation(runs, tenant, agent, resource="ana")
    old = conversation(runs, tenant, agent, resource="ana")
    bob = conversation(runs, tenant, agent, resource="bob")
    bulk(current, "ana", 300, lambda n: near(0.2 + 0.01 * n))  # farther as n grows
    bulk(other, "ana", 300, lambda n: near(5.0))  # far from the query
    # rows nearer than any of them that the filters must drop during the index scan: the same
    # resource under the previous embedding model (a large history, so HNSW beats sorting it), and
    # another resource
    bulk(old, "ana", 3000, lambda n: near(0.05), model="test:old768")
    bulk(bob, "bob", 300, lambda n: near(0.05))
    with engine.begin() as conn:
        conn.execute(text("ANALYZE message_embeddings"))
    query = [1.0] + [0.0] * (dims - 1)
    try:
        plan = store.explain_search_recall(tenant, agent, current, query, embedding_model=MODEL_768, top_k=5,
                                           exclude_turns=range(1, 51), disable_seqscan=False)
        assert "ix_message_embeddings_embedding_768" in plan, plan
        hits = store.search_recall(tenant, agent, current, query, embedding_model=MODEL_768, top_k=5,
                                   exclude_turns=range(1, 51))
        # approximate nearest neighbours: top_k is filled, only from this resource and model (the
        # nearer old-model and other-resource rows are filtered during the scan), never an excluded
        # turn, among the nearest eligible turns, sorted by exact distance
        assert len(hits) == 5 and all(h.conversation_id == current for h in hits)
        assert all(51 <= h.turn <= 80 for h in hits), [h.turn for h in hits]
        assert [h.distance for h in hits] == sorted(h.distance for h in hits)
        hits = store.search_recall(tenant, agent, current, query, embedding_model=MODEL_768, top_k=5,
                                   scope="conversation", exclude_turns=range(1, 299))
        assert [h.turn for h in hits] == [299, 300]  # only two left in scope: never padded from elsewhere
    finally:
        hnsw_engine.dispose()
        engine.dispose()
        drop_schema(base, schema)
