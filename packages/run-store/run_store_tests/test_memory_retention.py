"""MemoryStore retention (RM.15) and the forget-covers-every-memory-table check.

- `delete_retention_batch` per kind: only rows older than the cutoff, only the given tenant;
  observations take the rows they superseded with them; forget watermarks, conversations and runs
  stay; conversations (and resource notes) with a queued or running turn are never touched; the
  conversation-keyed kinds page by cursor; `retention_tenants` pages by tenant id.
- Forced interleavings with delete_conversation, forget_resource and the memory writers (the
  `interleave` helper of the knowledge store tests): the sweep never waits for a lock they hold
  (it skips), and they wait for the sweep's locks only until it commits; no deadlock either way.
- Forget coverage: every memory-related table found in the migrated schema (an FK to
  conversations, a `resource_id` or `conversation_id` column, or a memory or embedding name) must
  be covered by this test's map; data planted for a resource in each one is gone after a
  tenant-wide forget with conversations. A new memory table without coverage fails the test.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import RETENTION_KINDS, Base, MemoryStore, RetentionBatch, RunStore, TurnText

from .test_knowledge_store import vec
from .test_knowledge_store_concurrency import interleave
from .test_memory_store import add_turn, cleanup_tenant, conversation, count, merge, run_of

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

MODEL = "test:hash"
CONV_LOCK = ("FROM conversations", "FOR UPDATE")
SWEEP_LOCK = ("FROM conversations", "SKIP LOCKED")


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"ret_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def tenant2():
    return f"ret_u_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant, tenant2):
    yield
    for t in (tenant, tenant2):
        cleanup_tenant(engine, t)


def cutoff(days: float = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def age(engine, tenant: str, table: str, days: int = 3, where: str = "TRUE", **params) -> None:
    """Back-date rows of `table` (created_at, and updated_at where it exists)."""
    cols = "created_at = now() - make_interval(days => :d)"
    if table in ("working_memories", "memory_forget_watermarks"):
        cols += ", updated_at = now() - make_interval(days => :d)"
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET {cols} WHERE tenant_id = :t AND ({where})"),
                     {"t": tenant, "d": days, **params})


def index(store, engine, tenant, agent, conv, n):
    return store.index_turn(
        tenant, agent, conv, run_of(engine, tenant, conv, n),
        [TurnText("user", f"q{n}", vec(1536, n)), TurnText("assistant", f"a{n}", vec(1536, n + 1))],
        embedding_model=MODEL,
    )


def obs(store, tenant, agent, conv, frm, to, content="o"):
    return store.insert_observation(tenant, agent, conv, covers_from_turn=frm, covers_to_turn=to, content=content).observation


def sweep_all(store, kind, tenant, cut, limit=1000) -> RetentionBatch:
    """Run batches until done; returns the totals as one RetentionBatch."""
    deleted = skipped = 0
    after = None
    for _ in range(1000):
        b = store.delete_retention_batch(kind, tenant, cut, limit=limit, after=after)
        deleted, skipped = deleted + b.deleted, skipped + b.skipped
        if b.done:
            return RetentionBatch(kind, deleted, skipped, True)
        after = b.next_after
    raise AssertionError("retention never finished")


def rows(engine, table, tenant, where="TRUE", **params) -> int:
    return count(engine, f"SELECT count(*) FROM {table} WHERE tenant_id = :t AND ({where})", t=tenant, **params)


def seed(store, runs, engine, tenant, agent, resource="ana"):
    """One conversation with 3 completed turns and every kind of memory: turns 1-2 old, 3 new."""
    conv = conversation(runs, tenant, agent, resource=resource, turns=3)
    for n in (1, 2, 3):
        index(store, engine, tenant, agent, conv, n)
    o1 = obs(store, tenant, agent, conv, 1, 1)
    o2 = obs(store, tenant, agent, conv, 2, 2)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    merge(store, tenant, agent, {"name": "Ana"}, key=resource)
    age(engine, tenant, "message_embeddings", where="turn <= 2")
    age(engine, tenant, "memory_observations", where="id = ANY(CAST(:ids AS uuid[]))", ids=[o1.id, o2.id])
    obs(store, tenant, agent, conv, 3, 3)  # new
    age(engine, tenant, "working_memories")  # both notes old
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO memory_forget_watermarks (tenant_id, conversation_id, forgotten_through_turn, updated_at)"
            " VALUES (:t, :c, 1, now() - interval '30 days')"), {"t": tenant, "c": conv})
    return conv


# ------------------------------------------------------------------------- cutoff and tenant


def test_retention_deletes_only_older_rows_of_the_tenant(store, runs, engine, tenant, tenant2, agent):
    conv = seed(store, runs, engine, tenant, agent)
    other = seed(store, runs, engine, tenant2, agent)
    cut = cutoff()

    assert sweep_all(store, "message_embeddings", tenant, cut).deleted == 4  # turns 1-2, user and assistant
    assert sweep_all(store, "observations", tenant, cut).deleted == 2
    assert sweep_all(store, "conversation_working_memory", tenant, cut).deleted == 1
    assert sweep_all(store, "resource_working_memory", tenant, cut).deleted == 1

    assert sorted(count(engine, "SELECT array_agg(turn ORDER BY turn) FROM message_embeddings WHERE tenant_id = :t",
                        t=tenant)) == [3, 3]
    assert [o.covers_to_turn for o in store.list_observations(tenant, conv)] == [3]
    assert rows(engine, "working_memories", tenant) == 0
    # kept: the forget watermark (no retention), the conversation and its runs
    assert rows(engine, "memory_forget_watermarks", tenant) == 1
    assert runs.get_conversation(tenant, conv) is not None and rows(engine, "runs", tenant) == 3
    # the other tenant is untouched
    assert rows(engine, "message_embeddings", tenant2) == 6
    assert rows(engine, "memory_observations", tenant2, "conversation_id = :c", c=other) == 3
    assert rows(engine, "working_memories", tenant2) == 2
    # idempotent: a second sweep finds nothing
    for kind in RETENTION_KINDS:
        assert sweep_all(store, kind, tenant, cut).deleted == 0


def test_cutoff_is_per_kind(store, runs, engine, tenant, agent):
    seed(store, runs, engine, tenant, agent)
    # rows are 3 days old: a 5-day cutoff keeps all of them
    for kind in RETENTION_KINDS:
        assert sweep_all(store, kind, tenant, cutoff(5)).deleted == 0
    assert sweep_all(store, "observations", tenant, cutoff(1)).deleted == 2
    assert rows(engine, "message_embeddings", tenant) == 6  # another kind, not swept


def test_observation_retention_takes_superseded_rows(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=3)
    a, b = obs(store, tenant, agent, conv, 1, 1), obs(store, tenant, agent, conv, 2, 2)
    refl = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r").observation
    obs(store, tenant, agent, conv, 3, 3)
    # only the reflection is old (its superseded rows are new): deleting it must take them too (FK)
    age(engine, tenant, "memory_observations", where="id = CAST(:id AS uuid)", id=refl.id)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 3
    assert [o.covers_to_turn for o in store.list_observations(tenant, conv)] == [3]


def test_superseded_old_rows_go_and_their_new_reflection_stays(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    a, b = obs(store, tenant, agent, conv, 1, 1), obs(store, tenant, agent, conv, 2, 2)
    age(engine, tenant, "memory_observations")
    refl = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r").observation
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 2
    assert [o.id for o in store.list_observations(tenant, conv)] == [refl.id]


# ------------------------------------------------------------------------- running turns


def test_retention_never_touches_a_conversation_with_a_running_turn(store, runs, engine, tenant, agent):
    conv = seed(store, runs, engine, tenant, agent)
    add_turn(engine, tenant, agent, conv, 4, status="running")
    cut = cutoff()
    emb = sweep_all(store, "message_embeddings", tenant, cut)
    o = sweep_all(store, "observations", tenant, cut)
    wm = sweep_all(store, "conversation_working_memory", tenant, cut)
    note = sweep_all(store, "resource_working_memory", tenant, cut)  # its resource has the live turn
    assert (emb.deleted, o.deleted, wm.deleted, note.deleted) == (0, 0, 0, 0)
    assert (o.skipped, wm.skipped) == (1, 1)
    assert rows(engine, "message_embeddings", tenant) == 6 and rows(engine, "memory_observations", tenant) == 3
    assert rows(engine, "working_memories", tenant) == 2

    with engine.begin() as conn:  # the turn ends: the next sweep takes the old rows
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE tenant_id = :t AND turn = 4"), {"t": tenant})
    assert [sweep_all(store, k, tenant, cut).deleted for k in RETENTION_KINDS] == [4, 2, 1, 1]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_resource_note_of_another_agent_is_not_blocked(store, runs, engine, tenant, agent, status):
    other = str(uuid.uuid4())
    busy = conversation(runs, tenant, other, resource="ana")
    add_turn(engine, tenant, other, busy, 1, status=status)
    merge(store, tenant, agent, {"name": "Ana"}, key="ana")
    merge(store, tenant, other, {"name": "Ana"}, key="ana")
    age(engine, tenant, "working_memories")
    assert sweep_all(store, "resource_working_memory", tenant, cutoff()).deleted == 1
    assert store.get_working_memory(tenant, other, "resource", "ana") is not None  # the busy agent's stays
    assert store.get_working_memory(tenant, agent, "resource", "ana") is None


# ------------------------------------------------------------------------- paging


def test_conversation_kinds_page_by_cursor(store, runs, engine, tenant, agent):
    convs = sorted(conversation(runs, tenant, agent, turns=1) for _ in range(3))
    for c in convs:
        obs(store, tenant, agent, c, 1, 1)
    add_turn(engine, tenant, agent, convs[1], 2, status="running")  # the middle one is busy
    age(engine, tenant, "memory_observations")
    b1 = store.delete_retention_batch("observations", tenant, cutoff(), limit=2)
    assert (b1.deleted, b1.skipped, b1.done, b1.next_after) == (1, 1, False, convs[1])
    b2 = store.delete_retention_batch("observations", tenant, cutoff(), limit=2, after=b1.next_after)
    assert (b2.deleted, b2.skipped, b2.done, b2.next_after) == (1, 0, True, None)
    assert rows(engine, "memory_observations", tenant, "content <> ''") == 1  # the busy conversation's


def test_row_kinds_finish_below_the_limit(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=3)
    for n in (1, 2, 3):
        index(store, engine, tenant, agent, conv, n)
    age(engine, tenant, "message_embeddings")
    b = store.delete_retention_batch("message_embeddings", tenant, cutoff(), limit=4)
    assert (b.deleted, b.done, b.next_after) == (4, False, None)
    b = store.delete_retention_batch("message_embeddings", tenant, cutoff(), limit=4)
    assert (b.deleted, b.done) == (2, True)


def test_retention_tenants_pages_and_validates(store, runs, engine, tenant, tenant2, agent):
    for t in (tenant, tenant2):
        merge(store, t, agent, {"name": "x"}, key="ana")
    age(engine, tenant, "working_memories")
    age(engine, tenant2, "working_memories")
    found = store.retention_tenants("resource_working_memory", cutoff(), limit=10_000)
    assert {tenant, tenant2} <= set(found) and found == sorted(found)
    first = min(tenant, tenant2)
    assert first not in store.retention_tenants("resource_working_memory", cutoff(), after=first)
    assert tenant not in store.retention_tenants("conversation_working_memory", cutoff())
    with pytest.raises(ValueError):
        store.retention_tenants("kb_chunks", cutoff())
    with pytest.raises(ValueError):
        store.delete_retention_batch("observations", tenant, datetime.now())  # naive cutoff
    with pytest.raises(ValueError):
        store.delete_retention_batch("observations", "", cutoff())


# ------------------------------------------------------------------------- interleavings


def _old_conversation(store, runs, engine, tenant, agent, resource="ana"):
    conv = conversation(runs, tenant, agent, resource=resource, turns=1)
    obs(store, tenant, agent, conv, 1, 1)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    merge(store, tenant, agent, {"name": "Ana"}, key=resource)
    index(store, engine, tenant, agent, conv, 1)
    for table in ("memory_observations", "working_memories", "message_embeddings"):
        age(engine, tenant, table)
    return conv


def _sweep(store, kind, tenant):
    return lambda: store.delete_retention_batch(kind, tenant, cutoff())


@pytest.mark.parametrize("kind", ["observations", "conversation_working_memory"])
def test_sweep_skips_a_conversation_delete_conversation_holds(store, runs, engine, tenant, agent, kind):
    conv = _old_conversation(store, runs, engine, tenant, agent)
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    sweep = ("sweep", _sweep(store, kind, tenant))
    deleted, swept = interleave(engine, delete, CONV_LOCK, sweep, blocks=False)
    assert deleted is True and (swept.deleted, swept.skipped, swept.done) == (0, 1, True)
    assert rows(engine, "memory_observations", tenant) == 0 and rows(engine, "working_memories", tenant,
                                                                         "scope = 'conversation'") == 0


@pytest.mark.parametrize("kind", ["observations", "conversation_working_memory"])
def test_delete_conversation_waits_for_the_sweep(store, runs, engine, tenant, agent, kind):
    conv = _old_conversation(store, runs, engine, tenant, agent)
    sweep = ("sweep", _sweep(store, kind, tenant))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    swept, deleted = interleave(engine, sweep, SWEEP_LOCK, delete, blocks=True)
    assert swept.deleted == 1 and deleted is True
    assert runs.get_conversation(tenant, conv) is None


@pytest.mark.parametrize("kind", ["observations", "conversation_working_memory"])
def test_forget_waits_for_the_sweep_and_the_sweep_skips_forget(store, runs, engine, tenant, agent, kind):
    conv = _old_conversation(store, runs, engine, tenant, agent)
    sweep = ("sweep", _sweep(store, kind, tenant))
    forget = ("forget", lambda: store.forget_resource(tenant, "ana", include_conversations=True))
    swept, forgot = interleave(engine, sweep, SWEEP_LOCK, forget, blocks=True)
    assert swept.deleted == 1 and forgot.conversations == (conv,)

    conv2 = _old_conversation(store, runs, engine, tenant, agent, resource="bob")
    forget2 = ("forget", lambda: store.forget_resource(tenant, "bob"))
    sweep2 = ("sweep", _sweep(store, kind, tenant))
    forgot2, swept2 = interleave(engine, forget2, CONV_LOCK, sweep2, blocks=False)
    assert forgot2.working_memories == 1 and (swept2.deleted, swept2.skipped) == (0, 1)
    assert runs.get_conversation(tenant, conv2) is not None


def test_working_memory_writer_and_sweep_never_deadlock(store, runs, engine, tenant, agent):
    """update_working_memory (conversation scope) holds the conversation FOR KEY SHARE, which the
    sweep's FOR NO KEY UPDATE does not conflict with. Paused before the note: the sweep deletes the
    old note and commits; the writer then finds no note and writes a fresh one (harmless: the old
    one was past retention)."""
    conv = _old_conversation(store, runs, engine, tenant, agent)
    write = ("write", lambda: store.update_working_memory(
        tenant, agent, scope="conversation", scope_key=conv, content="# new"))
    sweep = ("sweep", _sweep(store, "conversation_working_memory", tenant))
    written, swept = interleave(engine, write, ("FROM conversations", "FOR KEY SHARE"), sweep, blocks=False)
    assert swept.deleted == 1 and written.version == 1
    assert store.get_working_memory(tenant, agent, "conversation", conv).content == "# new"


def test_sweep_waits_for_a_note_write_then_keeps_the_fresh_note(store, runs, engine, tenant, agent):
    """The writer holds the note FOR UPDATE: the sweep waits on that row only (the writer never
    waits for the sweep), then re-checks the age on the committed row and keeps it."""
    conv = _old_conversation(store, runs, engine, tenant, agent)
    write = ("write", lambda: store.update_working_memory(
        tenant, agent, scope="conversation", scope_key=conv, content="# new"))
    sweep = ("sweep", _sweep(store, "conversation_working_memory", tenant))
    written, swept = interleave(engine, write, ("FROM working_memories", "FOR UPDATE"), sweep, blocks=True)
    assert written.version == 2 and (swept.deleted, swept.skipped) == (0, 0)
    assert store.get_working_memory(tenant, agent, "conversation", conv).content == "# new"


def test_resource_note_sweep_skips_a_note_forget_holds(store, runs, engine, tenant, agent):
    _old_conversation(store, runs, engine, tenant, agent)
    forget = ("forget", lambda: store.forget_resource(tenant, "ana"))
    sweep = ("sweep", _sweep(store, "resource_working_memory", tenant))
    forgot, swept = interleave(engine, forget, ("DELETE FROM working_memories",), sweep, blocks=False)
    assert forgot.working_memories == 1 and (swept.deleted, swept.done) == (0, True)


def test_forget_waits_for_the_resource_note_sweep(store, runs, engine, tenant, agent):
    _old_conversation(store, runs, engine, tenant, agent)
    sweep = ("sweep", _sweep(store, "resource_working_memory", tenant))
    forget = ("forget", lambda: store.forget_resource(tenant, "ana"))
    swept, forgot = interleave(engine, sweep, ("FROM working_memories", "SKIP LOCKED"), forget, blocks=True)
    assert swept.deleted == 1 and forgot.working_memories == 0


def test_embedding_sweep_and_forget_never_deadlock(store, runs, engine, tenant, agent):
    _old_conversation(store, runs, engine, tenant, agent)
    sweep = ("sweep", _sweep(store, "message_embeddings", tenant))
    forget = ("forget", lambda: store.forget_resource(tenant, "ana"))
    swept, forgot = interleave(engine, sweep, ("FROM message_embeddings", "SKIP LOCKED"), forget, blocks=True)
    assert swept.deleted == 2 and forgot.message_embeddings == 0
    assert rows(engine, "message_embeddings", tenant) == 0


# ------------------------------------------------------------------------- retention markers


def _text_rows(engine, tenant) -> int:
    return rows(engine, "memory_observations", tenant, "content <> ''")


def _shape(store, tenant, conv):
    return [(o.kind, o.covers_from_turn, o.covers_to_turn, o.is_marker)
            for o in store.list_observations(tenant, conv, include_markers=True)]


def test_all_old_conversation_keeps_its_coverage(store, runs, engine, tenant, agent):
    """Every observation is old: one marker covers 1..end, the snapshot shows no observation text
    and no old turn, so nothing is re-observed; a later sweep leaves the marker alone."""
    conv = conversation(runs, tenant, agent, turns=4)
    a, b = obs(store, tenant, agent, conv, 1, 1), obs(store, tenant, agent, conv, 2, 2)
    store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r")
    obs(store, tenant, agent, conv, 3, 4)
    age(engine, tenant, "memory_observations")
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 4
    assert _shape(store, tenant, conv) == [("observation", 1, 4, True)]
    assert _text_rows(engine, tenant) == 0 and rows(engine, "memory_observations", tenant) == 1
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    assert (snap.observations, snap.covered_to_turn, snap.turn_numbers) == ((), 4, ())
    assert store.list_observations(tenant, conv) == []
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 0
    assert store.retention_tenants("observations", cutoff()).count(tenant) == 0


def test_active_conversation_keeps_a_gap_free_prefix(store, runs, engine, tenant, agent):
    """Old head, new tail: the head becomes a marker, the tail stays; later the next rows age out
    and join the marker (one marker, never a gap); reflection works around the marker."""
    conv = conversation(runs, tenant, agent, turns=5)
    o1, o2 = obs(store, tenant, agent, conv, 1, 1), obs(store, tenant, agent, conv, 2, 2)
    o3 = obs(store, tenant, agent, conv, 3, 3)
    age(engine, tenant, "memory_observations", where="id = ANY(CAST(:ids AS uuid[]))", ids=[o1.id, o2.id])
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 2
    assert _shape(store, tenant, conv) == [("observation", 1, 2, True), ("observation", 3, 3, False)]
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    assert [o.id for o in snap.observations] == [o3.id] and snap.covered_to_turn == 3
    assert snap.turn_numbers == (4, 5)

    o4 = obs(store, tenant, agent, conv, 4, 4)
    refl = store.reflect(tenant, agent, conv, supersede_ids=[o3.id, o4.id], content="r34")  # marker aside
    assert refl.status == "created" and (refl.observation.covers_from_turn, refl.observation.covers_to_turn) == (3, 4)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=refl.observation.id)
    obs(store, tenant, agent, conv, 5, 5)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 3  # the reflection and its two rows
    assert _shape(store, tenant, conv) == [("reflection", 1, 4, True), ("observation", 5, 5, False)]


def test_old_row_after_a_newer_one_is_blanked_in_place(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    obs(store, tenant, agent, conv, 1, 1)
    o2 = obs(store, tenant, agent, conv, 2, 2)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=o2.id)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 1
    assert _shape(store, tenant, conv) == [("observation", 1, 1, False), ("observation", 2, 2, True)]
    age(engine, tenant, "memory_observations")
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 1  # now the head: one marker
    assert _shape(store, tenant, conv) == [("observation", 1, 2, True)]


def _newest(store, engine, tenant, conv):
    """(the last row GET lists, markers included; the store's coverage end)."""
    listed = store.list_observations(tenant, conv, include_markers=True)
    covered = count(engine, "SELECT coalesce(max(covers_to_turn), 0) FROM memory_observations"
                    " WHERE tenant_id = :t AND conversation_id = :c AND superseded_by IS NULL", t=tenant, c=conv)
    return (listed[-1] if listed else None), covered


def test_reflection_supersedes_a_marker_inside_its_range(store, runs, engine, tenant, agent):
    """The race: the observer reads A 1..10, B 11..20, C 21..30 as visible, then the sweep blanks B
    (old, after the newer A) before the reflection lands: the reflection over all three is stale.
    The retry over the visible rows A and C covers 1..30 and supersedes the marker B with them, so no
    active row overlaps: the last row GET lists is the store's newest, and deleting it (the API's
    delete-newest) takes the marker along, leaving no turn hidden."""
    conv = conversation(runs, tenant, agent, turns=31)
    a = obs(store, tenant, agent, conv, 1, 10)
    b = obs(store, tenant, agent, conv, 11, 20)
    c = obs(store, tenant, agent, conv, 21, 30)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=b.id)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 1
    assert _shape(store, tenant, conv) == [("observation", 1, 10, False), ("observation", 11, 20, True),
                                           ("observation", 21, 30, False)]
    assert store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id, c.id], content="r").status == "stale"

    refl = store.reflect(tenant, agent, conv, supersede_ids=[a.id, c.id], content="r")
    assert refl.status == "created" and set(refl.superseded) == {a.id, b.id, c.id}
    assert _shape(store, tenant, conv) == [("reflection", 1, 30, False)]
    last, covered = _newest(store, engine, tenant, conv)
    assert last is not None and refl.observation is not None
    assert last.id == refl.observation.id and last.covers_to_turn == covered == 30
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    assert [o.id for o in snap.observations] == [refl.observation.id] and snap.turn_numbers == (31,)

    assert store.delete_observation(tenant, conv, last.id, only_latest=True) == 4  # with A, B and C
    assert _newest(store, engine, tenant, conv) == (None, 0)
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    assert (snap.observations, snap.covered_to_turn) == ((), 0) and snap.turn_numbers[0] == 1


def test_reflection_keeps_a_head_marker_active(store, runs, engine, tenant, agent):
    """A marker before the reflection's start is not inside its range: it stays and keeps 1..2."""
    conv = conversation(runs, tenant, agent, turns=4)
    o1 = obs(store, tenant, agent, conv, 1, 2)
    o3, o4 = obs(store, tenant, agent, conv, 3, 3), obs(store, tenant, agent, conv, 4, 4)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=o1.id)
    sweep_all(store, "observations", tenant, cutoff())
    refl = store.reflect(tenant, agent, conv, supersede_ids=[o3.id, o4.id], content="r")
    assert set(refl.superseded) == {o3.id, o4.id}
    assert _shape(store, tenant, conv) == [("observation", 1, 2, True), ("reflection", 3, 4, False)]


def test_head_collapse_spans_overlapping_rows(store, runs, engine, tenant, agent):
    """The safeguard: should a reflection 1..30 and a marker 11..20 both be active (forged here, as
    data from before reflect superseded inner markers), the head collapses to ONE marker 1..30:
    min(from)..max(to), not head[0].from..head[-1].to (which lost turns 21..30)."""
    conv = conversation(runs, tenant, agent, turns=31)
    a = obs(store, tenant, agent, conv, 1, 10)
    mid = obs(store, tenant, agent, conv, 11, 20)
    c = obs(store, tenant, agent, conv, 21, 30)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=mid.id)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 1
    refl = store.reflect(tenant, agent, conv, supersede_ids=[a.id, c.id], content="r")
    with engine.begin() as conn:  # the overlap reflect no longer creates
        conn.execute(text("UPDATE memory_observations SET superseded_by = NULL WHERE tenant_id = :t AND id = :i"),
                     {"t": tenant, "i": mid.id})
    assert _shape(store, tenant, conv) == [("reflection", 1, 30, False), ("observation", 11, 20, True)]
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=refl.observation.id)
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 3  # the reflection and its two rows
    assert _shape(store, tenant, conv) == [("reflection", 1, 30, True)]
    assert rows(engine, "memory_observations", tenant) == 1
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    assert (snap.observations, snap.covered_to_turn, snap.turn_numbers) == ((), 30, (31,))


# ------------------------------------------------------------------------- forget coverage

# Every memory-related table and how to find a resource's rows in it (:t tenant, :rid resource,
# :convs the resource's conversations). A table found by `memory_tables` and missing here fails
# test_forget_resource_covers_every_memory_table: add it here AND make forget_resource cover it.
FORGET_COVERAGE = {
    "conversations": "resource_id = :rid",
    "working_memories": "(scope = 'resource' AND scope_key = :rid) OR conversation_id = ANY(CAST(:convs AS text[]))",
    "memory_observations": "conversation_id = ANY(CAST(:convs AS text[]))",
    "message_embeddings": "resource_id = :rid OR conversation_id = ANY(CAST(:convs AS text[]))",
    "memory_forget_watermarks": "conversation_id = ANY(CAST(:convs AS text[]))",
}
# Kept by design as the audit and cost record, with their text removed (checked separately).
AUDIT_KEPT = {"runs", "audit_log"}  # audit_log: the platform audit trail (IAM P0b): ids and outcomes, no text; it names a resource_id of any kind

_MEMORY_TABLES_SQL = """
SELECT t.table_name FROM information_schema.tables t
WHERE t.table_schema = current_schema() AND t.table_type = 'BASE TABLE' AND (
  t.table_name LIKE '%memor%' OR t.table_name LIKE '%embedding%'
  OR EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema = t.table_schema
             AND c.table_name = t.table_name AND c.column_name IN ('resource_id', 'conversation_id'))
  OR EXISTS (SELECT 1 FROM pg_constraint k
             WHERE k.contype = 'f' AND k.conrelid = format('%I.%I', t.table_schema, t.table_name)::regclass
             AND k.confrelid = format('%I.conversations', t.table_schema)::regclass))
ORDER BY 1
"""


def _is_memory_table(table) -> bool:
    name = table.name
    if "memor" in name or "embedding" in name or {"resource_id", "conversation_id"} & set(table.c.keys()):
        return True
    return any(fk.column.table.name == "conversations" for fk in table.foreign_keys)


def memory_tables(conn) -> set[str]:
    """Memory-related tables in the migrated schema, plus any SQLAlchemy model that looks like one
    (so a model added before its migration is caught too)."""
    found = set(conn.execute(text(_MEMORY_TABLES_SQL)).scalars())
    found |= {t.name for t in Base.metadata.sorted_tables if _is_memory_table(t)}
    return found


def uncovered(tables: set[str]) -> set[str]:
    return tables - set(FORGET_COVERAGE) - AUDIT_KEPT


def test_memory_table_discovery_catches_a_new_table(engine):
    """The guard itself: a new table with a conversation_id (or a memory name) is reported."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("CREATE TABLE ret_probe_notes (tenant_id text, conversation_id text)"))
            conn.execute(text("CREATE TABLE ret_probe_memory_facts (tenant_id text, fact text)"))
            assert uncovered(memory_tables(conn)) == {"ret_probe_notes", "ret_probe_memory_facts"}
        finally:
            trans.rollback()


def test_forget_resource_covers_every_memory_table(store, runs, engine, tenant, agent):
    with engine.connect() as conn:
        tables = memory_tables(conn)
    missing = uncovered(tables)
    assert not missing, (
        f"memory tables without forget coverage: {sorted(missing)}; make forget_resource delete the"
        " resource's rows in them and add them to FORGET_COVERAGE"
    )
    assert set(FORGET_COVERAGE) <= tables, "FORGET_COVERAGE names a table that no longer exists"

    other = str(uuid.uuid4())
    c1 = conversation(runs, tenant, agent, resource="ana", turns=2)
    c2 = conversation(runs, tenant, other, resource="ana", turns=1)
    keep = conversation(runs, tenant, agent, resource="bob", turns=1)
    for conv, a in ((c1, agent), (c2, other), (keep, agent)):
        index(store, engine, tenant, a, conv, 1)
        obs(store, tenant, a, conv, 1, 1)
        store.update_working_memory(tenant, a, scope="conversation", scope_key=conv, content="# c")
    merge(store, tenant, agent, {"name": "Ana"}, key="ana")
    merge(store, tenant, other, {"name": "Ana"}, key="ana")
    merge(store, tenant, agent, {"name": "Bob"}, key="bob")
    with engine.begin() as conn:  # an earlier forget's mark (and one on the kept resource)
        conn.execute(text(
            "INSERT INTO memory_forget_watermarks (tenant_id, conversation_id, forgotten_through_turn)"
            " VALUES (:t, :c, 1), (:t, :k, 1)"), {"t": tenant, "c": c2, "k": keep})
    run_ids = [run_of(engine, tenant, c1, 1), run_of(engine, tenant, c1, 2), run_of(engine, tenant, c2, 1)]
    params = {"rid": "ana", "convs": [c1, c2]}
    for table, where in FORGET_COVERAGE.items():
        assert rows(engine, table, tenant, where, **params) > 0, f"nothing planted in {table}"

    result = store.forget_resource(tenant, "ana", include_conversations=True)
    assert set(result.conversations) == {c1, c2}

    left = {table: rows(engine, table, tenant, where, **params) for table, where in FORGET_COVERAGE.items()}
    assert left == dict.fromkeys(FORGET_COVERAGE, 0)
    # the audit record stays, detached and without text
    assert count(engine, "SELECT count(*) FROM runs WHERE id = ANY(:ids) AND conversation_id IS NULL"
                 " AND input IS NULL AND output IS NULL AND messages IS NULL", ids=run_ids) == 3
    # the other resource's memory is untouched
    bob = {"rid": "bob", "convs": [keep]}
    assert all(rows(engine, table, tenant, where, **bob) > 0 for table, where in FORGET_COVERAGE.items())
