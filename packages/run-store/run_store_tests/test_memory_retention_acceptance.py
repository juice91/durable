"""RM.15 acceptance for MemoryStore retention (on top of test_memory_retention.py).

Per kind (only older rows, per tenant, a kind alone), the strict cutoff, cursor paging with rows
inserted mid-sweep, queued turns, the observation-insert interleavings, and what history and
coverage do once old observations are gone.

Decided change (RM.15 review, 2026-11-14): retention removes an old observation's TEXT but keeps
its turns covered with a text-free marker row (content '', approx_tokens 0; never rendered, listed
or swept again). Observation counts below therefore count rows that still hold text
(`TEXT_ROWS`), and the "coverage drops, old turns come back, the observer starts again" outcomes
documented earlier became "coverage stays, nothing comes back, nothing is re-observed".
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import RETENTION_KINDS, MemoryStore, RunStore
from eap_schemas.memory import HistoryConfig

from .test_knowledge_store_concurrency import interleave
from .test_memory_retention import SWEEP_LOCK, age, cutoff, index, obs, rows, seed, sweep_all
from .test_memory_store import add_turn, cleanup_tenant, conversation, count

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
    return f"rta_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def tenant2():
    return f"rta_u_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant, tenant2):
    yield
    for t in (tenant, tenant2):
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM message_embeddings WHERE tenant_id = :t"), {"t": t})


TEXT_ROWS = "content <> ''"  # observation rows that still hold text (retention markers excluded)


def totals(engine, tenant: str) -> dict[str, int]:
    return {
        "emb": rows(engine, "message_embeddings", tenant),
        "obs": rows(engine, "memory_observations", tenant, TEXT_ROWS),
        "conv_wm": rows(engine, "working_memories", tenant, "scope = 'conversation'"),
        "res_wm": rows(engine, "working_memories", tenant, "scope = 'resource'"),
    }


BEFORE = {"emb": 6, "obs": 3, "conv_wm": 1, "res_wm": 1}  # what seed() plants: 4/2/1/1 are old
AFTER = {
    "message_embeddings": {**BEFORE, "emb": 2},
    "observations": {**BEFORE, "obs": 1},
    "conversation_working_memory": {**BEFORE, "conv_wm": 0},
    "resource_working_memory": {**BEFORE, "res_wm": 0},
}


# ------------------------------------------------------------------------- per kind


@pytest.mark.parametrize("kind", RETENTION_KINDS)
def test_a_kind_deletes_only_its_old_rows_of_its_tenant(store, runs, engine, tenant, tenant2, agent, kind):
    seed(store, runs, engine, tenant, agent)
    seed(store, runs, engine, tenant2, str(uuid.uuid4()), resource="bea")
    assert totals(engine, tenant) == BEFORE == totals(engine, tenant2)

    swept = sweep_all(store, kind, tenant, cutoff())
    assert swept.deleted == sum(BEFORE.values()) - sum(AFTER[kind].values())
    assert totals(engine, tenant) == AFTER[kind]  # the other kinds and the new rows stay
    assert totals(engine, tenant2) == BEFORE  # another tenant is untouched, however old
    assert sweep_all(store, kind, tenant, cutoff()).deleted == 0  # idempotent
    # conversations, runs and the forget marks are never memory
    assert rows(engine, "conversations", tenant) == 1 and rows(engine, "runs", tenant) == 3
    assert rows(engine, "memory_forget_watermarks", tenant) == 1


def test_a_kind_that_is_not_swept_is_kept(store, runs, engine, tenant, agent):
    """The sweeper only touches the kinds it is asked for (the worker's `keep` and the resource
    note opt-in are config: a kind absent from the run is never passed to the store)."""
    seed(store, runs, engine, tenant, agent)
    for kind in ("message_embeddings", "observations", "conversation_working_memory"):
        sweep_all(store, kind, tenant, cutoff())
    assert totals(engine, tenant) == {"emb": 2, "obs": 1, "conv_wm": 0, "res_wm": 1}
    sweep_all(store, "resource_working_memory", tenant, cutoff())
    assert totals(engine, tenant)["res_wm"] == 0


def test_the_cutoff_is_strict_and_per_row(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    index(store, engine, tenant, agent, conv, 1)
    with engine.begin() as conn:
        ts = conn.execute(text(
            "UPDATE message_embeddings SET created_at = now() - interval '2 days' WHERE tenant_id = :t"
            " RETURNING created_at"), {"t": tenant}).scalars().first()
    assert sweep_all(store, "message_embeddings", tenant, ts).deleted == 0  # created_at == cutoff: kept
    assert rows(engine, "message_embeddings", tenant) == 2
    assert sweep_all(store, "message_embeddings", tenant, ts + timedelta(microseconds=1)).deleted == 2


# ------------------------------------------------------------------------- batching


def old_conversation(store, runs, engine, tenant, agent, resource="ana") -> str:
    """One conversation with one completed turn and an old observation and conversation note."""
    conv = conversation(runs, tenant, agent, resource=resource, turns=1)
    o = obs(store, tenant, agent, conv, 1, 1)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=o.id)
    age(engine, tenant, "working_memories", where="conversation_id = :c", c=conv)
    return conv


def conversation_beyond(store, runs, engine, tenant, agent, cursor, *, above: bool) -> str:
    """An old conversation whose id sorts after (or before) the sweep's cursor."""
    for _ in range(200):
        conv = old_conversation(store, runs, engine, tenant, agent, resource=f"x{uuid.uuid4().hex[:6]}")
        if (conv > cursor) is above:
            return conv
        store.delete_conversation(tenant, conv)  # the wrong side: not part of the scenario
    raise AssertionError("no conversation id on that side of the cursor")


@pytest.mark.parametrize("kind", ["observations", "conversation_working_memory"])
def test_cursor_paging_covers_everything_once_even_with_inserts_mid_sweep(store, runs, engine, tenant, agent, kind):
    original = {old_conversation(store, runs, engine, tenant, agent, resource=f"r{i}") for i in range(7)}
    table, where = {
        "observations": ("memory_observations", TEXT_ROWS),
        "conversation_working_memory": ("working_memories", "scope = 'conversation'"),
    }[kind]
    cut, deleted, seen, after, late = cutoff(), 0, [], None, []
    for batch_no in range(50):
        b = store.delete_retention_batch(kind, tenant, cut, limit=2, after=after)
        deleted += b.deleted
        seen.append((b.deleted, b.skipped))
        if b.done:
            break
        assert b.next_after is not None and (after is None or b.next_after > after)  # strictly forward
        after = b.next_after
        if batch_no == 0:  # rows arrive behind and ahead of the cursor while the sweep runs
            late = [conversation_beyond(store, runs, engine, tenant, agent, after, above=a) for a in (False, True)]
    else:
        raise AssertionError("never done")
    behind, ahead = late
    left = {r for r in _conversations_with(engine, table, tenant, where)}
    assert left == {behind}  # the one ahead of the cursor was swept in this run; the one behind waits
    assert deleted == len(original) + 1 and all(s == 0 for _, s in seen)  # every row exactly once
    assert sweep_all(store, kind, tenant, cutoff()).deleted == 1  # the next run takes the straggler
    assert not _conversations_with(engine, table, tenant, where) and ahead != behind


def _conversations_with(engine, table: str, tenant: str, where: str) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text(
            f"SELECT DISTINCT conversation_id FROM {table} WHERE tenant_id = :t AND ({where})"), {"t": tenant}).scalars())


def test_embedding_batches_are_exact_even_with_inserts_mid_sweep(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=5)
    for n in (1, 2, 3):
        index(store, engine, tenant, agent, conv, n)
    age(engine, tenant, "message_embeddings")  # 6 old rows
    cut = cutoff()
    first = store.delete_retention_batch("message_embeddings", tenant, cut, limit=4)
    assert (first.deleted, first.done) == (4, False)
    index(store, engine, tenant, agent, conv, 4)  # new rows (created_at = now, after the cutoff)
    age(engine, tenant, "message_embeddings", where="turn = 4", days=3)  # ...and old ones
    out = sweep_all(store, "message_embeddings", tenant, cut)
    assert first.deleted + out.deleted == 6 + 2 and rows(engine, "message_embeddings", tenant) == 0


# ------------------------------------------------------------------------- running turns


@pytest.mark.parametrize("kind", RETENTION_KINDS)
def test_a_queued_turn_protects_a_conversation_until_it_finishes(store, runs, engine, tenant, agent, kind):
    seed(store, runs, engine, tenant, agent)
    conv = _only_conversation(engine, tenant)
    add_turn(engine, tenant, agent, conv, 4, status="queued")
    cut = cutoff()
    held = sweep_all(store, kind, tenant, cut)
    assert held.deleted == 0 and totals(engine, tenant) == BEFORE
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE tenant_id = :t AND turn = 4"), {"t": tenant})
    assert sweep_all(store, kind, tenant, cut).deleted > 0  # a failed turn is over: swept later
    assert totals(engine, tenant) == AFTER[kind]


def _only_conversation(engine, tenant: str) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT id FROM conversations WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def test_only_the_busy_conversation_is_held_back(store, runs, engine, tenant, agent):
    busy = old_conversation(store, runs, engine, tenant, agent, resource="a")
    idle = old_conversation(store, runs, engine, tenant, agent, resource="b")
    add_turn(engine, tenant, agent, busy, 2, status="running")
    out = sweep_all(store, "observations", tenant, cutoff())
    assert (out.deleted, out.skipped) == (1, 1)
    assert _conversations_with(engine, "memory_observations", tenant, TEXT_ROWS) == {busy}
    sweep_all(store, "conversation_working_memory", tenant, cutoff())
    assert _conversations_with(engine, "working_memories", tenant, "scope = 'conversation'") == {busy}
    assert rows(engine, "conversations", tenant, "id = :c", c=idle) == 1  # kept: not memory


# ------------------------------------------------------------------------- concurrency


def test_sweep_skips_an_observation_insert_and_loses_nothing(store, runs, engine, tenant, agent):
    conv = old_conversation(store, runs, engine, tenant, agent)
    add_turn(engine, tenant, agent, conv, 2)
    write = ("insert", lambda: store.insert_observation(
        tenant, agent, conv, covers_from_turn=2, covers_to_turn=2, content="new"))
    sweep = ("sweep", lambda: store.delete_retention_batch("observations", tenant, cutoff()))
    written, swept = interleave(engine, write, ("FROM conversations", "NO KEY UPDATE"), sweep, blocks=False)
    assert written.status == "created" and (swept.deleted, swept.skipped, swept.done) == (0, 1, True)
    assert rows(engine, "memory_observations", tenant, TEXT_ROWS) == 2  # the insert committed, the old one waits
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 1
    assert [o.covers_to_turn for o in store.list_observations(tenant, conv)] == [2]


def test_observation_insert_waits_for_the_sweep_then_sees_its_result(store, runs, engine, tenant, agent):
    """The sweep holds the conversation lock until it commits; the writer waits (no deadlock), then
    runs against the swept state. Decided change: the old turn-1 observation's text is gone but a
    marker keeps turn 1 covered, so the turn-2 insert continues the prefix (`created`; formerly a
    `gap` that made the observer start again from turn 1)."""
    conv = old_conversation(store, runs, engine, tenant, agent)
    add_turn(engine, tenant, agent, conv, 2)
    sweep = ("sweep", lambda: store.delete_retention_batch("observations", tenant, cutoff()))
    write = ("insert", lambda: store.insert_observation(
        tenant, agent, conv, covers_from_turn=2, covers_to_turn=2, content="new"))
    swept, written = interleave(engine, sweep, SWEEP_LOCK, write, blocks=True)
    assert swept.deleted == 1 and (written.status, written.covered_to_turn) == ("created", 2)
    assert rows(engine, "memory_observations", tenant, TEXT_ROWS) == 1  # only the new one holds text
    assert [o.covers_to_turn for o in store.list_observations(tenant, conv)] == [2]
    marker = store.list_observations(tenant, conv, include_markers=True)[0]
    assert (marker.is_marker, marker.covers_from_turn, marker.covers_to_turn) == (True, 1, 1)


def test_embedding_sweep_skips_what_delete_conversation_holds(store, runs, engine, tenant, agent):
    conv = old_conversation(store, runs, engine, tenant, agent)
    index(store, engine, tenant, agent, conv, 1)
    age(engine, tenant, "message_embeddings")
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    sweep = ("sweep", lambda: store.delete_retention_batch("message_embeddings", tenant, cutoff()))
    deleted, swept = interleave(engine, delete, ("DELETE FROM message_embeddings",), sweep, blocks=False)
    assert deleted is True and (swept.deleted, swept.done) == (0, True)
    assert rows(engine, "message_embeddings", tenant) == 0  # the delete took them, none twice


def test_delete_conversation_waits_for_the_embedding_sweep(store, runs, engine, tenant, agent):
    conv = old_conversation(store, runs, engine, tenant, agent)
    index(store, engine, tenant, agent, conv, 1)
    age(engine, tenant, "message_embeddings")
    sweep = ("sweep", lambda: store.delete_retention_batch("message_embeddings", tenant, cutoff()))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    swept, deleted = interleave(engine, sweep, ("FROM message_embeddings", "SKIP LOCKED"), delete, blocks=True)
    assert swept.deleted == 2 and deleted is True and rows(engine, "message_embeddings", tenant) == 0


# ------------------------------------------------------------------------- forget guard fails


def test_the_forget_guard_fails_when_a_memory_table_is_added(engine, store, runs, tenant, agent):
    """Create a throwaway table that matches the discovery pattern, in its own schema on the test
    schema's search path, and run the real guard test against it: it must fail, naming the table.
    Adding the table to FORGET_COVERAGE (the fix the message asks for) is what makes it pass."""
    from . import test_memory_retention as guard

    probe = f"retprobe_{uuid.uuid4().hex[:8]}"
    with engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
        conn.execute(text(f'CREATE TABLE "{schema}"."{probe}_memory_facts" (tenant_id text, fact text)'))
        conn.execute(text(f'CREATE TABLE "{schema}"."{probe}_notes" (tenant_id text, conversation_id text)'))
        conn.commit()
    names = {f"{probe}_memory_facts", f"{probe}_notes"}
    try:
        with engine.connect() as conn:
            assert guard.uncovered(guard.memory_tables(conn)) == names
        with pytest.raises(AssertionError) as exc:
            guard.test_forget_resource_covers_every_memory_table(store, runs, engine, tenant, agent)
        assert all(n in str(exc.value) for n in names) and "FORGET_COVERAGE" in str(exc.value)
    finally:
        with engine.begin() as conn:
            for n in names:
                conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{n}"'))
    with engine.connect() as conn:  # and it is green again once the throwaway tables are gone
        assert guard.uncovered(guard.memory_tables(conn)) == set()


# ------------------------------------------------------------------------- after old observations go


def snapshot(store, tenant, agent, conv, **kw):
    return store.load_snapshot(tenant, agent, conv, before_turn=99, observations=True, **kw)


def test_history_and_coverage_after_old_observations_are_deleted(store, runs, engine, tenant, agent):
    """Outcome, documented (decided change, see the module docstring): observations are the only
    thing that hides a turn's text from history, and the turns themselves (runs) are never deleted.
    When retention removes the text of the observations that covered old turns, a marker keeps
    those turns covered: coverage does not drop, the old turns do NOT come back into history, and
    the observer (whose next insert starts at covered + 1) does not observe them again. Formerly
    coverage dropped to 0 and the observer re-observed (and paid for) every kept turn."""
    conv = conversation(runs, tenant, agent, turns=6)
    o1, o2, o3 = (obs(store, tenant, agent, conv, a, b) for a, b in ((1, 2), (3, 4), (5, 6)))
    before = snapshot(store, tenant, agent, conv)
    assert (before.covered_to_turn, before.turn_numbers) == (6, ()) and len(before.observations) == 3

    age(engine, tenant, "memory_observations", where="id = ANY(CAST(:ids AS uuid[]))", ids=[o1.id, o2.id])
    sweep_all(store, "observations", tenant, cutoff())
    mid = snapshot(store, tenant, agent, conv)  # the newest observation still covers through turn 6
    assert [o.id for o in mid.observations] == [o3.id] and mid.covered_to_turn == 6 and mid.turn_numbers == ()

    age(engine, tenant, "memory_observations")  # now the last one goes too
    sweep_all(store, "observations", tenant, cutoff())
    after = snapshot(store, tenant, agent, conv)
    assert after.observations == () and after.covered_to_turn == 6  # nothing rendered, still covered
    assert after.turn_numbers == ()  # old turns stay out of history
    assert rows(engine, "memory_observations", tenant, TEXT_ROWS) == 0  # no observation text left
    markers = store.list_observations(tenant, conv, include_markers=True)
    assert [(m.covers_from_turn, m.covers_to_turn, m.is_marker) for m in markers] == [(1, 6, True)]
    assert rows(engine, "runs", tenant) == 6  # nothing of the conversation itself was deleted
    add_turn(engine, tenant, agent, conv, 7)
    nxt = store.insert_observation(tenant, agent, conv, covers_from_turn=7, covers_to_turn=7, content="next")
    assert nxt.status == "created" and snapshot(store, tenant, agent, conv).turn_numbers == ()
    assert sweep_all(store, "observations", tenant, cutoff()).deleted == 0  # markers are not swept again


def test_deleting_an_old_head_leaves_newer_coverage_in_place(store, runs, engine, tenant, agent):
    """Only the oldest observation is past the cutoff: its text goes, a marker keeps turns 1-2
    covered (so the active rows stay a gap-free prefix), and the newer one still covers its own
    turns; coverage is unchanged, so history does not grow."""
    conv = conversation(runs, tenant, agent, turns=4)
    o1 = obs(store, tenant, agent, conv, 1, 2)
    obs(store, tenant, agent, conv, 3, 4)
    age(engine, tenant, "memory_observations", where="id = CAST(:i AS uuid)", i=o1.id)
    sweep_all(store, "observations", tenant, cutoff())
    snap = snapshot(store, tenant, agent, conv)
    assert [(o.covers_from_turn, o.covers_to_turn) for o in snap.observations] == [(3, 4)]
    assert snap.covered_to_turn == 4 and snap.turn_numbers == ()
    every = store.list_observations(tenant, conv, include_markers=True)
    assert [(o.covers_from_turn, o.covers_to_turn, o.is_marker) for o in every] == [(1, 2, True), (3, 4, False)]
