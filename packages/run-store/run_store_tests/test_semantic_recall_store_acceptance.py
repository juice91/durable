"""RM.8 acceptance for the semantic-recall store: isolation of every method (snapshot before and
after), model separation, exclusions, scope, cascade and privacy, idempotency, min_score/top_k and
ordering by exact distance, EXPLAIN, retention and pickling."""

from __future__ import annotations

import math
import pickle
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_run_store import (
    ConversationNotFound,
    ForgetResult,
    IndexTurnResult,
    MemoryEvent,
    MemoryStore,
    RecallHit,
    RecallMessage,
    RunNotFound,
    RunStore,
    TurnText,
)

from .test_knowledge_store import vec
from .test_memory_store import add_turn, cleanup_tenant, conversation, count, run_of

MODEL = "test:hash"
SECRET = "zz-planted-secret-4417"


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"rec_a_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    for t in (tenant, tenant + "_b"):
        cleanup_tenant(engine, t)  # the embeddings go with their runs (FK cascade)


def put(store, tenant, agent, conv, n, user_v, answer_v=None, *, model=MODEL, user="u", answer="a"):
    """Index turn `n` of `conv` (the turn's run must exist): user vector, optional answer vector."""
    items = [TurnText("user", f"{user} {n}", user_v)]
    if answer_v is not None:
        items.append(TurnText("assistant", f"{answer} {n}", answer_v))
    return store.index_turn(tenant, agent, conv, run_of(store.engine, tenant, conv, n), items, embedding_model=model)


def ang(cos: float, dims: int = 1536, other: int = 100) -> list[float]:
    """Unit vector whose cosine similarity with vec(dims, 0) is exactly `cos`."""
    v = [0.0] * dims
    v[0], v[other] = cos, math.sqrt(1 - cos * cos)
    return v


def rows(engine, tenants):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT id, tenant_id, agent_id, resource_id, conversation_id, turn, run_id, role, text, embedding_model,"
            " dimensions, embedding::text, created_at FROM message_embeddings WHERE tenant_id = ANY(:t)"
            " ORDER BY id"), {"t": list(tenants)})]


def search(store, tenant, agent, conv, v, **kw):
    kw.setdefault("top_k", 50)
    kw.setdefault("embedding_model", MODEL)
    return store.search_recall(tenant, agent, conv, v, **kw)


def world(runs, store, tenant, agent, hot=5):
    """Two conversations of resource `ana` plus look-alikes (identical vectors) in another resource,
    another agent and another tenant. Returns (mine, current, others)."""
    b, other_agent = tenant + "_b", str(uuid.uuid4())
    mine = conversation(runs, tenant, agent, resource="ana", turns=2)
    current = conversation(runs, tenant, agent, resource="ana")
    bob = conversation(runs, tenant, agent, resource="bob", turns=2)
    oa = conversation(runs, tenant, other_agent, resource="ana", turns=2)
    ot = conversation(runs, b, agent, resource="ana", turns=2)
    for t, a, c in ((tenant, agent, mine), (tenant, agent, bob), (tenant, other_agent, oa), (b, agent, ot)):
        for n in (1, 2):
            put(store, t, a, c, n, vec(1536, hot), vec(1536, hot))
    return mine, current, {"bob": bob, "agent": (other_agent, oa), "tenant": ot}


# ------------------------------------------------------------------------------------ isolation


def test_every_method_rejects_foreign_ids_and_changes_nothing(store, runs, engine, tenant, agent):
    b = tenant + "_b"
    mine, current, others = world(runs, store, tenant, agent)
    other_agent, oa = others["agent"]
    foreign_run = run_of(engine, b, others["tenant"], 1)
    own_other_run = run_of(engine, tenant, others["bob"], 1)
    other_agent_run = run_of(engine, tenant, oa, 1)
    fresh = add_turn(engine, tenant, agent, current, 1)
    before = rows(engine, (tenant, b))
    q, items = vec(1536, 5), [TurnText("user", "x", vec(1536, 5))]

    def idx(t, a, c, r, **kw):
        return store.index_turn(t, a, c, r, items, embedding_model=MODEL, **kw)

    for t, a in ((b, agent), (tenant, other_agent)):  # another tenant's or agent's conversation
        with pytest.raises(ConversationNotFound):
            idx(t, a, current, fresh)
        with pytest.raises(ConversationNotFound):
            search(store, t, a, current, q)
        with pytest.raises(ConversationNotFound):
            store.explain_search_recall(t, a, current, q, embedding_model=MODEL)
        assert store.recall_neighbours(t, a, mine, 1, 3, current_conversation_id=mine) == []
        assert store.recall_neighbours(t, a, mine, 2, 0, current_conversation_id=mine) == []
    for run in (foreign_run, own_other_run, other_agent_run, str(uuid.uuid4())):  # a foreign run
        with pytest.raises(RunNotFound):
            idx(tenant, agent, current, run)
    for run in (foreign_run, own_other_run):  # the event of a foreign run rolls the inserts back too
        with pytest.raises(RunNotFound):
            idx(tenant, agent, current, fresh, event=MemoryEvent(run_id=run, event_key="embed:index:1"))
    assert store.delete_conversation(b, mine) is False
    assert store.forget_resource(b, "nobody").message_embeddings == 0
    assert store.forget_resource(tenant, "ana", agent_id=str(uuid.uuid4())).message_embeddings == 0
    assert store.delete_message_embeddings_before(tenant + "_c", datetime.now(timezone.utc)) == 0
    assert rows(engine, (tenant, b)) == before  # not one byte moved
    assert [t for t in store.message_embedding_tenants_before(datetime.now(timezone.utc) + timedelta(days=1))
            if t in (tenant, b)] == sorted((tenant, b))


def test_search_never_returns_foreign_rows_even_when_they_are_nearer_and_many(store, runs, engine, tenant, agent):
    b = tenant + "_b"
    mine = conversation(runs, tenant, agent, resource="ana", turns=2)
    current = conversation(runs, tenant, agent, resource="ana")
    near = lambda k: vec(1536, 7, noise=0.0) if k == 0 else vec(1536, 7, 7 + k)  # noqa: E731
    put(store, tenant, agent, mine, 1, vec(1536, 7, 8, 9), vec(1536, 7, 8, 10))  # mine: further from axis 7
    foreign = [(tenant, agent, "bob"), (tenant, str(uuid.uuid4()), "ana"), (b, agent, "ana")]
    for t, a, res in foreign:
        c = conversation(runs, t, a, resource=res, turns=40)
        for n in range(1, 41):  # 40 turns x 2 rows of exact matches per look-alike world
            put(store, t, a, c, n, near(0), near(0))
    before = rows(engine, (tenant, b))
    hits = search(store, tenant, agent, current, near(0), top_k=10)
    assert {h.conversation_id for h in hits} == {mine} and len(hits) == 2  # filtered, yet still complete
    assert {h.run_id for h in hits} == {run_of(engine, tenant, mine, 1)}
    assert {h.conversation_id for h in search(store, tenant, agent, mine, near(0), scope="conversation")} == {mine}
    assert rows(engine, (tenant, b)) == before


# ------------------------------------------------------------------------------ model separation


def test_rows_under_another_model_or_dimension_are_excluded(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=4)
    current = conversation(runs, tenant, agent, resource="ana")
    put(store, tenant, agent, conv, 1, vec(1536, 3), model="test:m-one")  # same dims, another model
    put(store, tenant, agent, conv, 2, vec(768, 3), model=MODEL)  # same model name, other dims
    put(store, tenant, agent, conv, 3, vec(1024, 3), model="test:m-two")  # other model and dims
    put(store, tenant, agent, conv, 4, vec(1536, 3), vec(1536, 3, 4), model=MODEL)
    assert {(h.turn, h.role) for h in search(store, tenant, agent, current, vec(1536, 3))} == {(4, "user"), (4, "assistant")}
    assert {h.turn for h in search(store, tenant, agent, current, vec(1536, 3), embedding_model="test:m-one")} == {1}
    assert {h.turn for h in search(store, tenant, agent, current, vec(768, 3))} == {2}
    assert {h.turn for h in search(store, tenant, agent, current, vec(1024, 3), embedding_model="test:m-two")} == {3}
    assert search(store, tenant, agent, current, vec(1024, 3)) == []  # right dims, wrong model
    assert search(store, tenant, agent, current, vec(768, 3), embedding_model="test:m-one") == []
    # the text of a neighbour is the same under any model: neighbours do not filter on it
    assert {m.turn for m in store.recall_neighbours(tenant, agent, conv, 2, 1, current_conversation_id=conv)} == {1, 2, 3}


# ------------------------------------------------------------------------------------ exclusions


def test_excluded_turns_never_return_and_do_not_shrink_top_k(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=6)
    twin = conversation(runs, tenant, agent, resource="ana", turns=6)
    for n in range(1, 7):  # turn n is nearer to the query the smaller n is
        put(store, tenant, agent, conv, n, ang(1 - 0.1 * n, other=100 + n), ang(1 - 0.1 * n, other=100 + n))
        put(store, tenant, agent, twin, n, vec(1536, 9, 100 + n))
    for scope in ("resource", "conversation"):
        got = search(store, tenant, agent, conv, vec(1536, 0), scope=scope, top_k=2, exclude_turns=[1, 2, 3])
        assert len(got) == 2  # exclusion happens before the limit, so top_k is still filled
        assert all(not (h.conversation_id == conv and h.turn in (1, 2, 3)) for h in got)
        assert [h.turn for h in got] == [4, 4] and {h.conversation_id for h in got} == {conv}
    for exclude in ([], [3], [1, 2, 3, 4, 5, 6], [99], [-1, 0]):
        for h in search(store, tenant, agent, conv, vec(1536, 0), exclude_turns=exclude):
            assert not (h.conversation_id == conv and h.turn in exclude)
        for m in store.recall_neighbours(tenant, agent, conv, 3, 3, current_conversation_id=conv, exclude_turns=exclude):
            assert m.turn not in exclude
    assert [m.turn for m in store.recall_neighbours(tenant, agent, conv, 3, 1, current_conversation_id=conv, exclude_turns=[3])] == [2, 2, 4, 4]
    assert store.recall_neighbours(tenant, agent, conv, 3, 3, current_conversation_id=conv, exclude_turns=range(1, 7)) == []
    assert search(store, tenant, agent, conv, vec(1536, 0), scope="conversation", exclude_turns=range(1, 7)) == []
    # exclusion is of this conversation's turns only
    assert {h.conversation_id for h in search(store, tenant, agent, conv, vec(1536, 9),
                                              exclude_turns=range(1, 7))} == {twin}
    # exclusion applies to the current conversation's rows only: twin's turn 2 still returns
    assert [m.turn for m in store.recall_neighbours(tenant, agent, twin, 2, 0, current_conversation_id=conv,
                                                    exclude_turns=[2])] == [2]  # twin has user rows only
    assert store.recall_neighbours(tenant, agent, conv, 2, 0, current_conversation_id=conv, exclude_turns=[2]) == []


# ---------------------------------------------------------------------------------------- scope


def test_scope_conversation_vs_resource_and_resourceless_conversations(store, runs, engine, tenant, agent):
    a = conversation(runs, tenant, agent, resource="ana", turns=1)
    b = conversation(runs, tenant, agent, resource="ana", turns=1)
    n1 = conversation(runs, tenant, agent, resource=None, turns=1)
    n2 = conversation(runs, tenant, agent, resource=None, turns=1)
    for c in (a, b, n1, n2):
        put(store, tenant, agent, c, 1, vec(1536, 2), vec(1536, 2))
    got = lambda c, **kw: {h.conversation_id for h in search(store, tenant, agent, c, vec(1536, 2), **kw)}  # noqa: E731
    assert got(a) == {a, b} and got(b) == {a, b}
    assert got(a, scope="conversation") == {a} and got(b, scope="conversation") == {b}
    for scope in ("resource", "conversation"):  # no resource id: itself only, never the other NULL-resource one
        assert got(n1, scope=scope) == {n1} and got(n2, scope=scope) == {n2}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id IS NULL"),
                            {"t": tenant}).scalar_one() == 4


# ------------------------------------------------------------------------------------ idempotency


def test_index_turn_twice_never_replaces_text_or_embedding(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    first = put(store, tenant, agent, conv, 1, vec(1536, 4), vec(1536, 5), user="original", answer="original")
    assert (first.inserted, first.existing) == (("user", "assistant"), ())
    before = rows(engine, (tenant,))
    again = put(store, tenant, agent, conv, 1, vec(1536, 40), vec(1536, 50), user=f"{SECRET} new", answer="new")
    assert (again.inserted, again.existing) == ((), ("user", "assistant"))
    other_model = put(store, tenant, agent, conv, 1, vec(768, 4), vec(768, 5), model="test:other")  # other dims too
    assert other_model.inserted == () and other_model.existing == ("user", "assistant")
    assert rows(engine, (tenant,)) == before  # same ids, texts, vectors, models, created_at
    assert [h.text for h in search(store, tenant, agent, conv, vec(1536, 4), top_k=1)] == ["original 1"]
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND text LIKE :s",
                 t=tenant, s=f"%{SECRET}%") == 0


def test_a_second_call_fills_only_the_missing_role(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=1)
    assert put(store, tenant, agent, conv, 1, vec(1536, 4), user="first").inserted == ("user",)
    before = {r[7]: r for r in rows(engine, (tenant,))}
    both = put(store, tenant, agent, conv, 1, vec(1536, 8), vec(1536, 9), user="second", answer="late")
    assert (both.inserted, both.existing) == (("assistant",), ("user",))
    after = {r[7]: r for r in rows(engine, (tenant,))}
    assert after["user"] == before["user"] and after["assistant"][8] == "late 1"
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 2


# ---------------------------------------------------------------------------- min_score and top_k


@pytest.fixture
def graded(store, runs, engine, tenant, agent):
    """Turns whose user rows have cosine 0.99, 0.9, 0.7, 0.5, 0.2 and -0.4 to vec(0), indexed out of order."""
    conv = conversation(runs, tenant, agent, resource="ana", turns=6)
    scores = {1: 0.5, 2: 0.99, 3: 0.2, 4: 0.9, 5: -0.4, 6: 0.7}
    for n, c in scores.items():
        put(store, tenant, agent, conv, n, ang(c, other=200 + n))
    return conv, scores, conversation(runs, tenant, agent, resource="ana")


def test_results_are_ordered_by_exact_distance(store, tenant, agent, graded):
    conv, scores, current = graded
    hits = search(store, tenant, agent, current, vec(1536, 0))
    assert [h.turn for h in hits] == [2, 4, 6, 1, 3, 5]
    for h in hits:
        assert h.score == pytest.approx(scores[h.turn], abs=1e-5) and h.score == pytest.approx(1 - h.distance)
    assert [h.distance for h in hits] == sorted(h.distance for h in hits)
    for k in (1, 2, 5, 6, 50):  # top_k is a prefix of the full ranking; more than exist returns all
        assert [h.turn for h in search(store, tenant, agent, current, vec(1536, 0), top_k=k)] == [2, 4, 6, 1, 3, 5][:k]


def test_min_score_cuts_after_ranking_and_composes_with_top_k(store, tenant, agent, graded):
    conv, scores, current = graded
    q = vec(1536, 0)
    turns = lambda **kw: [h.turn for h in search(store, tenant, agent, current, q, **kw)]  # noqa: E731
    assert turns(min_score=0.8) == [2, 4]
    assert turns(min_score=0.69) == [2, 4, 6] and turns(min_score=0.71) == [2, 4]  # the 0.7 boundary
    assert turns(min_score=0.2 - 1e-4) == [2, 4, 6, 1, 3]
    assert turns(min_score=0.0) == [2, 4, 6, 1, 3]  # a negative cosine never passes a non-negative floor
    assert turns(min_score=0.8, top_k=1) == [2] and turns(min_score=0.1, top_k=2) == [2, 4]
    assert turns(min_score=0.995) == []  # nothing near enough: an empty list, not an error
    assert turns(min_score=1.0) == []
    assert all(h.score >= 0.8 for h in search(store, tenant, agent, current, q, min_score=0.8))
    exact = search(store, tenant, agent, current, ang(0.99, other=202), min_score=0.999)
    assert [h.turn for h in exact] == [2]  # a near-identical query still passes a very high floor


def test_equal_distances_have_a_stable_order(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="ana", turns=3)
    for n in (1, 2, 3):
        put(store, tenant, agent, conv, n, vec(1536, 6), vec(1536, 6))
    first = [(h.id, h.distance) for h in search(store, tenant, agent, conv, vec(1536, 6))]
    assert len(first) == 6 and [i for i, _ in first] == sorted(i for i, _ in first)
    assert [(h.id, h.distance) for h in search(store, tenant, agent, conv, vec(1536, 6))] == first


# ---------------------------------------------------------------------------------------- EXPLAIN


@pytest.mark.parametrize("dims", [768, 1024, 1536])
@pytest.mark.parametrize("scope", ["resource", "conversation"])
def test_explain_uses_only_the_hnsw_index_of_the_dimension(store, runs, engine, tenant, agent, dims, scope):
    model = f"test:hash{dims}"
    conv = conversation(runs, tenant, agent, resource="ana", turns=3)
    for n in (1, 2, 3):
        put(store, tenant, agent, conv, n, vec(dims, n), vec(dims, n + 7), model=model)
    # Fresh statistics on every run, so the plan never depends on whether autovacuum analyzed the
    # session's table earlier (with statistics, a btree index plus a sort wins on so few rows; the
    # diagnostic turns sorts off, so only the ordered vector index can win).
    with engine.begin() as conn:
        conn.execute(text("ANALYZE message_embeddings"))
    plan = store.explain_search_recall(tenant, agent, conv, vec(dims, 1), embedding_model=model, scope=scope, top_k=3,
                                       exclude_turns=[3], min_score=0.1)
    assert f"ix_message_embeddings_embedding_{dims}" in plan, plan
    for other in {768, 1024, 1536} - {dims}:
        assert f"ix_message_embeddings_embedding_{other}" not in plan
    with engine.connect() as conn:  # a diagnostic only: it leaves no setting, row or lock behind
        assert conn.execute(text("SHOW enable_seqscan")).scalar() == "on"
        assert conn.execute(text("SHOW enable_sort")).scalar() == "on"
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 6
    assert [h.turn for h in search(store, tenant, agent, conv, vec(dims, 1), embedding_model=model,
                                   top_k=1)] == [1]


# --------------------------------------------------------------------------- cascade and privacy


def secret_rows(engine, tenant):
    return count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND text LIKE :s",
                 t=tenant, s=f"%{SECRET}%")


def test_delete_conversation_removes_its_embeddings_and_the_planted_secret(store, runs, engine, tenant, agent):
    doomed = conversation(runs, tenant, agent, resource="ana", turns=2)
    kept = conversation(runs, tenant, agent, resource="ana", turns=1)
    current = conversation(runs, tenant, agent, resource="ana")
    put(store, tenant, agent, doomed, 1, vec(1536, 3), vec(1536, 4), user=f"my key is {SECRET}")
    put(store, tenant, agent, doomed, 2, vec(1536, 3), user="other")
    put(store, tenant, agent, kept, 1, vec(1536, 3), vec(1536, 4), user="kept")
    assert secret_rows(engine, tenant) == 1 and len(search(store, tenant, agent, current, vec(1536, 3))) == 5
    assert store.delete_conversation(tenant, doomed) is True
    assert secret_rows(engine, tenant) == 0
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE conversation_id = :c", c=doomed) == 0
    assert {(h.conversation_id, h.turn) for h in search(store, tenant, agent, current, vec(1536, 3))} == {(kept, 1)}
    assert store.recall_neighbours(tenant, agent, doomed, 1, 3, current_conversation_id=doomed) == []
    assert all(SECRET not in m.text for m in store.recall_neighbours(tenant, agent, kept, 1, 3, current_conversation_id=kept))
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=tenant) == 2
    assert store.delete_conversation(tenant, doomed) is False  # idempotent
    with pytest.raises(ConversationNotFound):
        search(store, tenant, agent, doomed, vec(1536, 3))


def test_forget_resource_removes_that_resource_only(store, runs, engine, tenant, agent):
    b, other_agent = tenant + "_b", str(uuid.uuid4())
    mine = conversation(runs, tenant, agent, resource="ana", turns=2)
    hers = conversation(runs, tenant, other_agent, resource="ana", turns=1)
    bob = conversation(runs, tenant, agent, resource="bob", turns=1)
    lone = conversation(runs, tenant, agent, resource=None, turns=1)
    theirs = conversation(runs, b, agent, resource="ana", turns=1)
    for t, a, c, n in ((tenant, agent, mine, 1), (tenant, agent, mine, 2), (tenant, other_agent, hers, 1),
                       (tenant, agent, bob, 1), (tenant, agent, lone, 1), (b, agent, theirs, 1)):
        put(store, t, a, c, n, vec(1536, 3), vec(1536, 4), user=f"{SECRET} {n}")
    only_agent = store.forget_resource(tenant, "ana", agent_id=agent)
    assert isinstance(only_agent, ForgetResult) and only_agent.message_embeddings == 4  # 2 turns x 2 roles
    left = lambda t, res: count(  # noqa: E731
        engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id = :r", t=t, r=res)
    assert (left(tenant, "ana"), left(tenant, "bob"), left(b, "ana")) == (2, 2, 2)  # the other agent's rows stay
    everything = store.forget_resource(tenant, "ana")
    assert everything.message_embeddings == 2
    assert (left(tenant, "ana"), left(tenant, "bob"), left(b, "ana")) == (0, 2, 2)
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND resource_id IS NULL",
                 t=tenant) == 2  # a conversation without a resource id is no resource's
    assert store.forget_resource(tenant, "ana").message_embeddings == 0  # idempotent
    assert count(engine, "SELECT count(*) FROM runs WHERE tenant_id = :t", t=tenant) == 5  # turns' own text stays
    probe = conversation(runs, tenant, agent, resource="ana")
    assert search(store, tenant, agent, probe, vec(1536, 3)) == []
    bob_now = conversation(runs, tenant, agent, resource="bob")
    assert {h.conversation_id for h in search(store, tenant, agent, bob_now, vec(1536, 3))} == {bob}
    assert secret_rows(engine, tenant) == 2 and secret_rows(engine, b) == 1  # bob's and lone's user rows
    with_convs = store.forget_resource(tenant, "bob", include_conversations=True)
    assert with_convs.message_embeddings == 2 and set(with_convs.conversations) == {bob, bob_now}
    assert secret_rows(engine, tenant) == 1  # only the resource-less conversation's secret is left
    assert store.delete_conversation(tenant, lone) is True and secret_rows(engine, tenant) == 0
    assert secret_rows(engine, b) == 1  # another tenant's is never touched


# ------------------------------------------------------------------------------------- retention


def test_retention_deletes_only_older_rows_oldest_first_for_the_right_tenant(store, runs, engine, tenant, agent):
    b = tenant + "_b"
    conv = conversation(runs, tenant, agent, resource="ana", turns=5)
    theirs = conversation(runs, b, agent, resource="ana", turns=1)
    for n in range(1, 6):
        put(store, tenant, agent, conv, n, vec(1536, n), vec(1536, n + 9))
    put(store, b, agent, theirs, 1, vec(1536, 1), vec(1536, 2))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=10)
    ages = {1: 40, 2: 30, 3: 20, 4: 10, 5: 1}  # turn 4 sits exactly on the cutoff: not older, so it stays
    with engine.begin() as conn:
        for n, days in ages.items():
            conn.execute(text("UPDATE message_embeddings SET created_at = :ts WHERE tenant_id = :t AND turn = :n"),
                         {"ts": cutoff if n == 4 else now - timedelta(days=days), "t": tenant, "n": n})
        conn.execute(text("UPDATE message_embeddings SET created_at = :ts WHERE tenant_id = :t"),
                     {"ts": now - timedelta(days=99), "t": b})
    listed = store.message_embedding_tenants_before(cutoff)
    assert {tenant, b} <= set(listed) and listed == sorted(listed)
    assert tenant not in store.message_embedding_tenants_before(now - timedelta(days=50))
    assert store.delete_message_embeddings_before(tenant, now - timedelta(days=50)) == 0
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=3) == 3  # turn 1 (2 rows) + one of turn 2
    gone = {m.turn for m in store.recall_neighbours(tenant, agent, conv, 3, 3, current_conversation_id=conv)}
    assert 1 not in gone and count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND turn = 2",
                                   t=tenant) == 1  # oldest first
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=3) == 3  # the rest of turn 2, turn 3
    assert store.delete_message_embeddings_before(tenant, cutoff, limit=3) == 0
    assert {m.turn for m in store.recall_neighbours(tenant, agent, conv, 3, 3, current_conversation_id=conv)} == {4, 5}
    assert count(engine, "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t", t=b) == 2
    assert tenant not in store.message_embedding_tenants_before(cutoff) and b in store.message_embedding_tenants_before(cutoff)
    assert store.delete_message_embeddings_before(b, cutoff) == 2
    assert b not in store.message_embedding_tenants_before(cutoff)
    assert len(store.message_embedding_tenants_before(now + timedelta(days=1), limit=1)) <= 1
    assert count(engine, "SELECT count(*) FROM runs WHERE tenant_id = :t", t=tenant) == 5  # only embeddings are aged out


# ------------------------------------------------------------------------------------- pickling


def test_errors_and_results_pickle(store, runs, engine, tenant, agent):
    """DBOS pickles step errors and results: the errors the new methods raise and their results."""
    conv = conversation(runs, tenant, agent, resource="ana", turns=2)
    foreign = str(uuid.uuid4())
    with pytest.raises(RunNotFound) as run_err:
        store.index_turn(tenant, agent, conv, foreign, [TurnText("user", "x", vec(1536, 1))], embedding_model=MODEL)
    with pytest.raises(ConversationNotFound) as conv_err:
        search(store, tenant + "_b", agent, conv, vec(1536, 1))
    for exc in (run_err.value, conv_err.value):
        back = pickle.loads(pickle.dumps(exc))
        assert type(back) is type(exc) and str(back) == str(exc) and back.args == exc.args
    assert pickle.loads(pickle.dumps(run_err.value)).run_id == foreign
    result = put(store, tenant, agent, conv, 1, vec(1536, 1), vec(1536, 2))
    hit = search(store, tenant, agent, conv, vec(1536, 1))[0]
    neighbour = store.recall_neighbours(tenant, agent, conv, 1, 0, current_conversation_id=conv)[0]
    forgot = store.forget_resource(tenant, "ana")
    for obj in (result, hit, neighbour, forgot, TurnText("user", "t", [0.5, 0.5])):
        back = pickle.loads(pickle.dumps(obj))
        assert type(back) is type(obj) and back == obj
    assert isinstance(result, IndexTurnResult) and isinstance(hit, RecallHit) and isinstance(neighbour, RecallMessage)
