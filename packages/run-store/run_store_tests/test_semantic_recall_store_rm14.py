"""MemoryStore additions of RM.14: `recall_rows_exist` (the EXISTS check that lets recall skip the
query embedding for a brand-new user) and `recall_neighbours_many` (one query for every hit turn),
both scoped exactly like search_recall / recall_neighbours."""

from __future__ import annotations

import os
import uuid

import pytest

from eap_db.testing import SCHEMA_ENV
from eap_run_store import ConversationNotFound, MemoryStore, RunStore

from .test_memory_store import cleanup_tenant, conversation
from .test_semantic_recall_store import MODEL, MODEL_768, index

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"rec14_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    for t in (tenant, tenant + "_b"):
        cleanup_tenant(engine, t)


# ------------------------------------------------------------------------------ recall_rows_exist


def test_rows_exist_follows_the_recall_scope(store, runs, engine, tenant, agent):
    ana = conversation(runs, tenant, agent, resource="ana", turns=1)
    current = conversation(runs, tenant, agent, resource="ana")
    bob = conversation(runs, tenant, agent, resource="bob")
    lone = conversation(runs, tenant, agent, resource=None, turns=1)

    def exists(conv, **kw):
        return store.recall_rows_exist(tenant, agent, conv, embedding_model=kw.pop("model", MODEL), **kw)

    assert not exists(current)  # nothing indexed yet
    index(store, tenant, agent, ana, 1)
    assert exists(current) and exists(ana)  # resource scope: a sibling conversation's rows count
    assert not exists(bob)  # another resource
    assert not exists(current, scope="conversation")  # conversation scope: only its own rows
    assert exists(ana, scope="conversation")
    assert not exists(current, model=MODEL_768)  # rows under another embedding model do not count
    assert not exists(lone)  # no resource: falls back to its own (unindexed) rows
    index(store, tenant, agent, lone, 1)
    assert exists(lone)  # its own rows now
    other_agent = str(uuid.uuid4())
    sibling = conversation(runs, tenant, other_agent, resource="ana")
    assert not store.recall_rows_exist(tenant, other_agent, sibling, embedding_model=MODEL)  # another agent


def test_rows_exist_rejects_foreign_conversations_and_bad_arguments(store, runs, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    index(store, tenant, agent, conv, 1)
    for t, a, c in ((tenant + "_b", agent, conv), (tenant, str(uuid.uuid4()), conv), (tenant, agent, str(uuid.uuid4()))):
        with pytest.raises(ConversationNotFound):
            store.recall_rows_exist(t, a, c, embedding_model=MODEL)
    with pytest.raises(ValueError):
        store.recall_rows_exist(tenant, agent, conv, embedding_model=MODEL, scope="tenant")


# ------------------------------------------------------------------------- recall_neighbours_many


def _turns(rows):
    return [(m.turn, m.role) for m in rows]


def test_neighbours_many_equals_recall_neighbours_per_hit(store, runs, tenant, agent):
    a = conversation(runs, tenant, agent, resource="ana", turns=6)
    b = conversation(runs, tenant, agent, resource="ana", turns=2)
    for n in range(1, 7):
        index(store, tenant, agent, a, n)
    for n in (1, 2):
        index(store, tenant, agent, b, n)
    current = conversation(runs, tenant, agent, resource="ana")
    hits = [(a, 2), (a, 3), (b, 1), (a, 6)]
    got = store.recall_neighbours_many(tenant, agent, hits, 1, current_conversation_id=current)
    assert set(got) == set(hits)
    for conv, turn in hits:
        single = store.recall_neighbours(tenant, agent, conv, turn, 1, current_conversation_id=current)
        assert _turns(got[(conv, turn)]) == _turns(single) and [m.id for m in got[(conv, turn)]] == [m.id for m in single]
    assert _turns(got[(a, 6)]) == [(5, "user"), (5, "assistant"), (6, "user"), (6, "assistant")]
    assert store.recall_neighbours_many(tenant, agent, [], 1, current_conversation_id=current) == {}
    with pytest.raises(ValueError):
        store.recall_neighbours_many(tenant, agent, hits, -1, current_conversation_id=current)


def test_neighbours_many_excludes_hits_of_another_tenant_agent_or_resource(store, runs, engine, tenant, agent):
    ana = conversation(runs, tenant, agent, resource="ana", turns=1)
    bob = conversation(runs, tenant, agent, resource="bob", turns=1)
    other_agent = str(uuid.uuid4())
    other_agents_conv = conversation(runs, tenant, other_agent, resource="ana", turns=1)
    foreign = conversation(RunStore(engine), tenant + "_b", agent, resource="ana", turns=1)
    index(store, tenant, agent, ana, 1)
    index(store, tenant, agent, bob, 1)
    index(store, tenant, other_agent, other_agents_conv, 1)
    index(store, tenant + "_b", agent, foreign, 1)
    current = conversation(runs, tenant, agent, resource="ana")
    hits = [(ana, 1), (bob, 1), (other_agents_conv, 1), (foreign, 1)]
    got = store.recall_neighbours_many(tenant, agent, hits, 1, current_conversation_id=current)
    assert _turns(got[(ana, 1)]) == [(1, "user"), (1, "assistant")]
    # Hit ids from another resource, another agent or another tenant (never from search_recall, but
    # passed anyway) return nothing.
    assert got[(bob, 1)] == [] and got[(other_agents_conv, 1)] == [] and got[(foreign, 1)] == []
    # A current conversation that is not this tenant's and agent's sees nothing at all.
    assert store.recall_neighbours_many(tenant + "_b", agent, hits, 1, current_conversation_id=current) == {}
    assert store.recall_neighbours_many(tenant, other_agent, hits, 1, current_conversation_id=current) == {}
    assert store.recall_neighbours_many(tenant, agent, hits, 1, current_conversation_id=str(uuid.uuid4())) == {}


def test_neighbours_many_exclude_turns_apply_only_to_the_current_conversation(store, runs, tenant, agent):
    current = conversation(runs, tenant, agent, resource="ana", turns=3)
    sibling = conversation(runs, tenant, agent, resource="ana", turns=3)
    for conv in (current, sibling):
        for n in (1, 2, 3):
            index(store, tenant, agent, conv, n)
    got = store.recall_neighbours_many(
        tenant, agent, [(current, 2), (sibling, 2)], 1, current_conversation_id=current, exclude_turns=[2, 3],
    )
    assert {m.turn for m in got[(current, 2)]} == {1}  # turns 2 and 3 of the current conversation dropped
    assert {m.turn for m in got[(sibling, 2)]} == {1, 2, 3}  # the same turn numbers elsewhere stay
