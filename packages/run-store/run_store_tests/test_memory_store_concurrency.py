"""MemoryStore (RM.5) under concurrency: merges never lose fields, replace is last writer wins, a
ledger key writes once, the snapshot never mixes an observation with the turns it covers, and the
lock order (conversation, working memory, observations, run, ledger) never deadlocks.

Forced interleavings pause one call right after a chosen SQL statement (the helpers of the RM.4
concurrency tests), run the competing call, check whether it waits, then release the first.
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest

from eap_db.testing import SCHEMA_ENV
from eap_run_store import ConversationNotFound, MemoryEvent, MemoryStore, RunStore, working_memory_ledger_key
from eap_schemas.memory import WorkingMemoryConfig

from .test_knowledge_store_concurrency import interleave
from .test_memory_store import SCHEMA, cleanup_tenant, conversation, count, merge, run_of

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

WM_LOCK = ("FROM working_memories", "FOR UPDATE")


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"memc_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    cleanup_tenant(engine, tenant)


# ------------------------------------------------------------------------ merge and replace


def test_two_merges_of_different_keys_into_one_resource_row_both_survive(store, engine, tenant, agent):
    merge(store, tenant, agent, {"tags": ["seed"]})
    a = ("merge_a", lambda: merge(store, tenant, agent, {"name": "Ana"}))
    b = ("merge_b", lambda: merge(store, tenant, agent, {"prefs": {"food": "rice"}}))
    first, second = interleave(engine, a, WM_LOCK, b, blocks=True)  # b waits for a's row lock
    assert (first.version, second.version) == (2, 3)
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.content == {"tags": ["seed"], "name": "Ana", "prefs": {"food": "rice"}} and note.version == 3


def test_two_first_merges_race_on_the_insert_and_both_survive(store, engine, tenant, agent):
    # a has seen no row and is about to insert; b inserts the row first; a then merges on top of it.
    a = ("first_a", lambda: merge(store, tenant, agent, {"name": "Ana"}))
    b = ("first_b", lambda: merge(store, tenant, agent, {"age": 7}))
    first, second = interleave(engine, a, WM_LOCK, b, blocks=False)
    assert {first.version, second.version} == {1, 2}
    assert store.get_working_memory(tenant, agent, "resource", "u1").content == {"name": "Ana", "age": 7}


def test_many_concurrent_merges_lose_nothing(store, tenant, agent):
    def worker(i: int) -> None:
        for j in range(5):
            merge(store, tenant, agent, {"prefs": {"food": f"{i}-{j}"}} if j == 4 else {"tags": [f"{i}"]})
            store.update_working_memory(
                tenant, agent, scope="resource", scope_key="u1",
                patch={f"k{i}_{j}": j}, json_schema=SCHEMA,  # additionalProperties defaults to true
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.version == 60
    assert {k for k in note.content if k.startswith("k")} == {f"k{i}_{j}" for i in range(6) for j in range(5)}


def test_template_replace_is_last_writer_wins(store, engine, tenant, agent):
    write = lambda body: store.update_working_memory(  # noqa: E731
        tenant, agent, scope="resource", scope_key="u1", content=body
    )
    write("# start")
    first, second = interleave(engine, ("replace_a", lambda: write("# from A")), WM_LOCK,
                               ("replace_b", lambda: write("# from B")), blocks=True)
    assert (first.version, second.version) == (2, 3)
    assert store.get_working_memory(tenant, agent, "resource", "u1").content == "# from B"


# --------------------------------------------------------------------------------------- ledger


def test_concurrent_calls_with_one_ledger_key_write_once(store, engine, tenant, agent):
    key = working_memory_ledger_key("run-x", "call-1")
    a = ("ledger_a", lambda: merge(store, tenant, agent, {"name": "A"}, ledger_key=key))
    b = ("ledger_b", lambda: merge(store, tenant, agent, {"name": "B"}, ledger_key=key))
    # a has checked the ledger (no row) and is paused; b writes and records the key; a then finds the
    # key taken at its ledger insert, undoes its own write and returns b's result.
    first, second = interleave(engine, a, ("FROM side_effects",), b, blocks=False)
    assert second.replayed is False and first.replayed is True
    assert first.version == second.version == 1
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.version == 1 and note.content == {"name": "B"}
    assert count(engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t", t=tenant) == 1


# ------------------------------------------------------------------------------------- snapshot


def _snapshot(store, tenant, agent, conv):
    return store.load_snapshot(tenant, agent, conv, observations=True, working=WorkingMemoryConfig(json_schema=SCHEMA))


def test_snapshot_taken_before_an_observation_keeps_its_turns(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=5)
    snap_call = ("snapshot", lambda: _snapshot(store, tenant, agent, conv))
    observe = ("observe", lambda: (
        store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=3, content="1..3"),
        merge(store, tenant, agent, {"name": "late"}),
    ))
    # The snapshot has read the conversation row (its snapshot is fixed) and is paused; the
    # observation and a memory write commit meanwhile without waiting for it.
    before, _ = interleave(engine, snap_call, ("FROM conversations",), observe, blocks=False)
    assert before.observations == () and before.covered_to_turn == 0
    assert before.turn_numbers == (1, 2, 3, 4, 5) and before.working_memory is None
    after = _snapshot(store, tenant, agent, conv)
    assert [o.covers_to_turn for o in after.observations] == [3] and after.turn_numbers == (4, 5)
    assert after.working_memory.content == {"name": "late"}


def test_snapshot_sees_a_reflection_wholly_or_not_at_all(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=6)
    a = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="a").observation
    b = store.insert_observation(tenant, agent, conv, covers_from_turn=3, covers_to_turn=4, content="b").observation
    reflect = ("reflect", lambda: store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r"))
    before, _ = interleave(engine, ("snapshot", lambda: _snapshot(store, tenant, agent, conv)),
                           ("FROM conversations",), reflect, blocks=False)
    assert [o.kind for o in before.observations] == ["observation", "observation"]
    after = _snapshot(store, tenant, agent, conv)
    assert [(o.kind, o.covers_to_turn) for o in after.observations] == [("reflection", 4)]
    for snap in (before, after):
        assert snap.turn_numbers == (5, 6) and snap.covered_to_turn == 4


def test_snapshots_under_concurrent_observers_never_mix_or_drop_turns(store, runs, engine, tenant, agent):
    turns = 40
    conv = conversation(runs, tenant, agent, turns=turns)
    stop = threading.Event()
    seen: list = []
    errors: list = []

    def observer() -> None:
        try:
            ids = []
            for start in range(1, turns, 2):
                w = store.insert_observation(tenant, agent, conv, covers_from_turn=start, covers_to_turn=start + 1,
                                             content=f"{start}")
                ids.append(w.observation.id)
                if len(ids) == 4:  # fold the last four rows (and any earlier reflection) into one
                    active = [o.id for o in store.list_observations(tenant, conv)]
                    store.reflect(tenant, agent, conv, supersede_ids=active, content="r")
                    ids = []
        except BaseException as exc:  # reported below
            errors.append(exc)
        finally:
            stop.set()

    def reader() -> None:
        try:
            while not stop.is_set():
                seen.append(_snapshot(store, tenant, agent, conv))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=observer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not errors, errors
    assert len(seen) > 3
    for snap in seen:
        covered = snap.covered_to_turn
        # the active rows cover exactly 1..covered, gap-free and without overlap
        spans = sorted((o.covers_from_turn, o.covers_to_turn) for o in snap.observations)
        expected_from = 1
        for lo, hi in spans:
            assert lo == expected_from
            expected_from = hi + 1
        assert expected_from == covered + 1
        # and the history is exactly the turns after them
        assert snap.turn_numbers == tuple(range(covered + 1, turns + 1))


# ------------------------------------------------------------------------------------ lock order


def _conv_merge(store, tenant, agent, conv, run_id=None, key="call-1"):
    return store.update_working_memory(
        tenant, agent, scope="conversation", scope_key=conv, patch={"name": "Ana"}, json_schema=SCHEMA,
        run_id=run_id, event=None if run_id is None else MemoryEvent(run_id=run_id, event_key=f"memory_updated:{key}"),
    )


def test_delete_conversation_waits_for_a_conversation_scoped_write(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    write = ("write", lambda: _conv_merge(store, tenant, agent, conv, run_id))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    written, deleted = interleave(engine, write, ("FROM runs", "FOR UPDATE"), delete, blocks=True)
    assert written.version == 1 and deleted is True
    assert store.get_working_memory(tenant, agent, "conversation", conv) is None


def test_a_conversation_scoped_write_after_delete_finds_no_conversation(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    write = ("write", lambda: _conv_merge(store, tenant, agent, conv))
    pause_needles = ("FROM conversations", "FOR UPDATE")
    with pytest.raises(ConversationNotFound):
        interleave(engine, delete, pause_needles, write, blocks=True)
    assert count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=tenant) == 0


def test_observer_and_working_memory_write_do_not_block_each_other(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    observe = ("observe", lambda: store.insert_observation(tenant, agent, conv, covers_from_turn=1,
                                                           covers_to_turn=2, content="x"))
    write = ("write", lambda: _conv_merge(store, tenant, agent, conv))
    obs, wm = interleave(engine, observe, ("FROM conversations", "FOR NO KEY UPDATE"), write, blocks=False)
    assert obs.status == "created" and wm.version == 1


def test_two_observers_of_one_range_serialise(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=5)
    a = ("observe_a", lambda: store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=3,
                                                       content="a"))
    b = ("observe_b", lambda: store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=4,
                                                       content="b"))
    first, second = interleave(engine, a, ("FROM conversations", "FOR NO KEY UPDATE"), b, blocks=True)
    assert first.status == "created" and (second.status, second.covered_to_turn) == ("overlap", 3)


def test_resource_write_with_event_versus_delete_conversation(store, runs, engine, tenant, agent):
    """A resource-scoped write holds the note and then the run row; delete_conversation takes the
    conversation, then the run rows. Neither order deadlocks."""
    for paused, needles, blocks in (("write", ("FROM runs", "FOR UPDATE"), True), ("write", WM_LOCK, False)):
        conv = conversation(runs, tenant, agent, turns=1)
        run_id = run_of(engine, tenant, conv, 1)
        write = (paused, lambda: merge(store, tenant, agent, {"name": "Ana"}, run_id=run_id,
                                       event=MemoryEvent(run_id=run_id, event_key="memory_updated:c")))
        delete = ("delete", lambda: store.delete_conversation(tenant, conv))
        written, deleted = interleave(engine, write, needles, delete, blocks=blocks)
        assert deleted is True and written.version >= 1
        assert count(engine, "SELECT count(*) FROM execution_events WHERE run_id = :r", r=run_id) == 1


def test_create_turn_and_delete_conversation_serialise(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    turn = ("turn", lambda: runs.create_turn(tenant, conv, 1, "hi", uuid.uuid4()))
    with pytest.raises(ConversationNotFound):
        interleave(engine, delete, ("FROM conversations", "FOR UPDATE"), turn, blocks=True)



# ------------------------------------------------------------------- forget and delete races


def test_forget_refuses_while_a_turn_is_writing_the_note(store, runs, engine, tenant, agent):
    from eap_run_store import ResourceBusy
    from .test_memory_store import add_turn

    conv = conversation(runs, tenant, agent, resource="u1", turns=1)
    live = add_turn(engine, tenant, agent, conv, 2, status="running")
    key = working_memory_ledger_key(live, "call-1")
    write = ("write", lambda: merge(store, tenant, agent, {"name": "Ana"}, ledger_key=key, run_id=live,
                                    event=MemoryEvent(run_id=live, event_key="memory_updated:call-1")))
    forget = ("forget", lambda: store.forget_resource(tenant, "u1", include_conversations=True))
    # The running turn holds the note's lock; forget does not wait for it: it refuses at once.
    with pytest.raises(ResourceBusy):
        interleave(engine, write, WM_LOCK, forget, blocks=False)
    assert store.get_working_memory(tenant, agent, "resource", "u1").version == 1
    assert count(engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t", t=tenant) == 1
    # a crash-replay of that write still finds its ledger row (nothing was forgotten)
    assert merge(store, tenant, agent, {"name": "Ana"}, ledger_key=key, run_id=live).replayed is True


def test_no_turn_starts_while_forget_holds_the_resource(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="u1", turns=1)
    merge(store, tenant, agent, {"name": "Ana"})
    forget = ("forget", lambda: store.forget_resource(tenant, "u1"))
    turn = ("turn", lambda: runs.create_turn(tenant, conv, 1, "hi", uuid.uuid4()))
    result, (run_id, created) = interleave(engine, forget, ("FROM conversations", "FOR UPDATE"), turn, blocks=True)
    assert result.working_memories == 1 and created  # the turn starts after the forget committed
    assert store.get_working_memory(tenant, agent, "resource", "u1") is None


def test_conversation_scoped_write_waits_for_forget_of_its_resource(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="u1", turns=1)
    forget = ("forget", lambda: store.forget_resource(tenant, "u1", include_conversations=True))
    write = ("write", lambda: _conv_merge(store, tenant, agent, conv))
    # The write waits for forget's lock. It then lands either between forget's note transaction and
    # the conversation delete (which removes it) or after that delete (ConversationNotFound).
    try:
        interleave(engine, forget, ("FROM conversations", "FOR UPDATE"), write, blocks=True)
    except ConversationNotFound:
        pass
    assert runs.get_conversation(tenant, conv) is None
    assert count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=tenant) == 0


@pytest.mark.parametrize("op", ["observe", "reflect"])
@pytest.mark.parametrize("paused", ["delete", "writer"])
def test_delete_conversation_versus_observation_writers(store, runs, engine, tenant, agent, op, paused):
    conv = conversation(runs, tenant, agent, turns=4)
    a = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="a").observation
    if op == "observe":
        writer = ("writer", lambda: store.insert_observation(tenant, agent, conv, covers_from_turn=3,
                                                             covers_to_turn=4, content="b"))
    else:
        writer = ("writer", lambda: store.reflect(tenant, agent, conv, supersede_ids=[a.id], content="r"))
    delete = ("delete", lambda: store.delete_conversation(tenant, conv))
    if paused == "delete":
        with pytest.raises(ConversationNotFound):
            interleave(engine, delete, ("FROM conversations", "FOR UPDATE"), writer, blocks=True)
    else:
        written, deleted = interleave(engine, writer, ("FROM conversations", "FOR NO KEY UPDATE"), delete,
                                      blocks=True)
        assert written.status == "created" and deleted is True
    assert count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=tenant) == 0
    assert runs.get_conversation(tenant, conv) is None
