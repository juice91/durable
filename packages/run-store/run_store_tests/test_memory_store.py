"""MemoryStore (RM.5): conversation resource ids, working memory (merge, replace, ledger, versions),
observations (idempotent insert, ranges, reflection), the snapshot, deletions, isolation."""

from __future__ import annotations

import json
import os
import pickle
import uuid

import pytest
from sqlalchemy import text

from eap_db.testing import SCHEMA_ENV
from eap_run_store import (
    ConversationIdConflict,
    ConversationNotFound,
    ConversationResourceConflict,
    MemoryEvent,
    MemoryLedgerConflict,
    MemoryStore,
    RunNotFound,
    ResourceBusy,
    RunStore,
    TurnInProgress,
    WorkingMemoryVersionConflict,
    apply_history_limits,
    working_memory_ledger_key,
)
from eap_schemas.memory import HistoryConfig, WorkingMemoryConfig, WorkingMemoryInvalid

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "prefs": {"type": "object", "properties": {"food": {"type": "string"}, "drink": {"type": "string"}}},
    },
}


# ------------------------------------------------------------------------------------- fixtures


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"mem_t_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    cleanup_tenant(engine, tenant)
    cleanup_tenant(engine, tenant + "_b")


def cleanup_tenant(engine, tenant: str) -> None:
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM side_effects WHERE tenant_id = :t",
            "DELETE FROM working_memories WHERE tenant_id = :t",
            "DELETE FROM memory_observations WHERE tenant_id = :t",
            "DELETE FROM run_model_requests WHERE tenant_id = :t",
            "DELETE FROM execution_events WHERE tenant_id = :t",
            "DELETE FROM runs WHERE tenant_id = :t",
            "DELETE FROM conversations WHERE tenant_id = :t",
        ):
            conn.execute(text(sql), {"t": tenant})


def conversation(runs: RunStore, tenant: str, agent: str, resource: str | None = "u1", turns: int = 0, **kw) -> str:
    conv, _ = runs.create_conversation(tenant, agent, resource_id=resource, **kw)
    for n in range(1, turns + 1):
        add_turn(runs.engine, tenant, agent, conv["id"], n)
    return conv["id"]


def add_turn(engine, tenant: str, agent: str, conv: str, n: int, status: str = "completed", size: int = 10) -> str:
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
                " conversation_id, turn, created_at, updated_at)"
                " VALUES (:id, :t, 'agent', :a, 1, :s, :i, :o, CAST(:m AS jsonb), :c, :n, now(), now())"
            ),
            {
                "id": run_id, "t": tenant, "a": agent, "s": status, "i": f"in {n}", "o": "x" * size,
                "m": json.dumps([{"kind": "request", "turn": n}]), "c": conv, "n": n,
            },
        )
    return run_id


def run_of(engine, tenant: str, conv: str, n: int) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id FROM runs WHERE tenant_id = :t AND conversation_id = :c AND turn = :n"),
            {"t": tenant, "c": conv, "n": n},
        ).scalar_one()


def count(engine, sql: str, **params) -> int:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def merge(store, tenant, agent, patch, key="u1", **kw):
    return store.update_working_memory(
        tenant, agent, scope="resource", scope_key=key, patch=patch, json_schema=SCHEMA, **kw
    )


# ------------------------------------------------------------------------- conversation resource


def test_conversation_resource_id_is_set_once_and_immutable(runs, tenant, agent):
    conv_id = str(uuid.uuid4())
    conv, created = runs.create_conversation(tenant, agent, conversation_id=conv_id, resource_id="user:ana@x.io")
    assert created and conv["resource_id"] == "user:ana@x.io"
    again, created = runs.create_conversation(tenant, agent, conversation_id=conv_id, resource_id="user:ana@x.io")
    assert not created and again["resource_id"] == "user:ana@x.io"
    for other in ("user:bob", None):
        with pytest.raises(ConversationResourceConflict) as exc:
            runs.create_conversation(tenant, agent, conversation_id=conv_id, resource_id=other)
        assert isinstance(exc.value, ConversationIdConflict)  # the API's existing 409
    assert runs.get_conversation(tenant, conv_id)["resource_id"] == "user:ana@x.io"
    legacy, _ = runs.create_conversation(tenant, agent)
    assert legacy["resource_id"] is None


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "a/b", "ü"])
def test_conversation_resource_id_pattern(runs, tenant, agent, bad):
    with pytest.raises(ValueError):
        runs.create_conversation(tenant, agent, resource_id=bad)


# --------------------------------------------------------------------------------- working memory


def test_template_mode_replaces_and_counts_versions(store, runs, tenant, agent):
    first = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# User\n- Name: Ana")
    second = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# User\n- Name: Bo")
    assert (first.version, second.version, second.format, second.changed_keys) == (1, 2, "markdown", ())
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.content == "# User\n- Name: Bo" and note.version == 2 and note.format == "markdown"
    with pytest.raises(WorkingMemoryInvalid):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="x" * (16 * 1024 + 1))
    assert store.get_working_memory(tenant, agent, "resource", "u1").version == 2


def test_schema_mode_deep_merges(store, tenant, agent):
    merge(store, tenant, agent, {"name": "Ana", "tags": ["a", "b"], "prefs": {"food": "rice"}})
    w = merge(store, tenant, agent, {"tags": ["c"], "prefs": {"drink": "tea"}, "name": None})
    assert w.version == 2 and w.changed_keys == ("name", "prefs", "tags")
    assert store.get_working_memory(tenant, agent, "resource", "u1").content == {
        "tags": ["c"], "prefs": {"food": "rice", "drink": "tea"}
    }


def test_invalid_patch_writes_nothing(store, tenant, agent):
    merge(store, tenant, agent, {"name": "Ana"})
    with pytest.raises(WorkingMemoryInvalid) as exc:
        merge(store, tenant, agent, {"age": "old"}, ledger_key="wm:r:bad")
    assert "age" in str(exc.value) and "old" not in str(exc.value)
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.version == 1 and note.content == {"name": "Ana"}
    with pytest.raises(WorkingMemoryInvalid):
        merge(store, tenant, agent, ["not", "an", "object"])
    with pytest.raises(ValueError):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch={"a": 1})  # no schema


def test_replace_in_schema_mode_and_if_match(store, tenant, agent):
    with pytest.raises(WorkingMemoryVersionConflict):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content={"name": "A"},
                                    json_schema=SCHEMA, expected_version=3)
    w = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content={"name": "A"},
                                    json_schema=SCHEMA, expected_version=0)
    assert w.version == 1
    w = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content={"age": 3},
                                    json_schema=SCHEMA, expected_version=1)
    assert w.version == 2 and w.changed_keys == ("age", "name")
    with pytest.raises(WorkingMemoryVersionConflict) as exc:
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content={"age": 4},
                                    json_schema=SCHEMA, expected_version=1)
    assert (exc.value.expected, exc.value.actual) == (1, 2)
    with pytest.raises(WorkingMemoryVersionConflict):
        store.delete_working_memory(tenant, agent, "resource", "u1", expected_version=1)
    assert store.delete_working_memory(tenant, agent, "resource", "u1", expected_version=2)
    assert not store.delete_working_memory(tenant, agent, "resource", "u1")


def test_mode_change_from_markdown_starts_json_from_empty(store, tenant, agent):
    store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# old note")
    w = merge(store, tenant, agent, {"name": "Ana"})
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert (w.version, note.format, note.content) == (2, "json", {"name": "Ana"})


def test_ledger_replay_returns_first_result_and_writes_once(store, engine, tenant, agent):
    key = working_memory_ledger_key("run-1", "call-1")
    first = merge(store, tenant, agent, {"name": "Ana"}, ledger_key=key)
    replay = merge(store, tenant, agent, {"name": "Changed", "age": 9}, ledger_key=key)
    assert first.replayed is False and replay.replayed is True
    assert (replay.version, replay.changed_keys, replay.format, replay.scope) == (1, ("name",), "json", "resource")
    note = store.get_working_memory(tenant, agent, "resource", "u1")
    assert note.version == 1 and note.content == {"name": "Ana"}
    assert count(engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t", t=tenant) == 1
    stored = count(engine, "SELECT result::text FROM side_effects WHERE tenant_id = :t", t=tenant)
    assert "Ana" not in stored  # the ledger never holds memory content
    # a ledger key that is not a completed working memory write
    from eap_run_store import ledger_row_key

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO side_effects (tenant_id, key, state, created_at) VALUES (:t, :k, 'reserved', now())"),
                     {"t": tenant, "k": ledger_row_key("wm:x:y", agent)})
    with pytest.raises(MemoryLedgerConflict):
        merge(store, tenant, agent, {"name": "B"}, ledger_key="wm:x:y")


def test_event_commits_with_the_write(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    event = MemoryEvent(run_id=run_id, event_key="memory_updated:call-1")
    key = working_memory_ledger_key(run_id, "call-1")
    merge(store, tenant, agent, {"name": "Ana"}, ledger_key=key, run_id=run_id, event=event)
    merge(store, tenant, agent, {"name": "Ana"}, ledger_key=key, run_id=run_id, event=event)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT type, data FROM execution_events WHERE run_id = :r"), {"r": run_id}).all()
    assert len(rows) == 1 and rows[0].type == "memory_updated"
    assert rows[0].data == {"scope": "resource", "format": "json", "version": 1, "changed_keys": ["name"]}
    assert store.get_working_memory(tenant, agent, "resource", "u1").updated_by_run_id == run_id
    # an event for an unknown run rolls the whole write back
    with pytest.raises(RunNotFound):
        merge(store, tenant, agent, {"age": 1}, ledger_key="wm:none:1",
              event=MemoryEvent(run_id=str(uuid.uuid4()), event_key="e"))
    assert store.get_working_memory(tenant, agent, "resource", "u1").version == 1
    assert count(engine, "SELECT count(*) FROM side_effects WHERE tenant_id = :t", t=tenant) == 1


def test_conversation_scope_checks_tenant_and_agent(store, runs, tenant, agent):
    conv = conversation(runs, tenant, agent)
    other_agent_conv = conversation(runs, tenant, str(uuid.uuid4()))
    other_tenant_conv = conversation(runs, tenant + "_b", agent)
    w = store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    assert w.version == 1
    for key in (other_agent_conv, other_tenant_conv, "missing"):
        with pytest.raises(ConversationNotFound):
            store.update_working_memory(tenant, agent, scope="conversation", scope_key=key, content="# c")


def test_working_memory_tenant_and_agent_isolation(store, tenant, agent):
    other_agent = str(uuid.uuid4())
    merge(store, tenant, agent, {"name": "Ana"})
    merge(store, tenant, other_agent, {"name": "Other"})
    merge(store, tenant + "_b", agent, {"name": "Tenant B"})
    assert store.get_working_memory(tenant, agent, "resource", "u1").content == {"name": "Ana"}
    assert store.get_working_memory(tenant, other_agent, "resource", "u1").content == {"name": "Other"}
    assert store.get_working_memory(tenant + "_b", agent, "resource", "u1").content == {"name": "Tenant B"}
    assert store.get_working_memory(tenant, agent, "resource", "u2") is None
    assert [n.scope_key for n in store.list_working_memories(tenant, agent)] == ["u1"]
    assert store.delete_working_memory(tenant + "_b", agent, "resource", "u1")
    assert store.get_working_memory(tenant, agent, "resource", "u1") is not None
    with pytest.raises(ValueError):
        store.get_working_memory(tenant, agent, "resource", "bad key")
    with pytest.raises(ValueError):
        store.get_working_memory(tenant, agent, "thread", "u1")


# ----------------------------------------------------------------------------------- observations


def test_observation_insert_is_idempotent_and_contiguous(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=8)
    run_id = run_of(engine, tenant, conv, 4)
    usage = {"model_id": "test:observer", "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "usage_measured": True, "request_key": "memory:observe:4"}
    event = MemoryEvent(run_id=run_id, event_key="memory_observed:4", usage=usage)
    first = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=4,
                                     content="turn 1..4 notes", run_id=run_id, event=event)
    again = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=4,
                                     content="different text", run_id=run_id, event=event)
    assert first.status == "created" and again.status == "existing"
    assert again.observation == first.observation and again.observation.content == "turn 1..4 notes"
    assert first.observation.approx_tokens == 4
    assert count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=tenant) == 1
    assert count(engine, "SELECT count(*) FROM execution_events WHERE run_id = :r", r=run_id) == 1
    assert count(engine, "SELECT count(*) FROM run_model_requests WHERE run_id = :r", r=run_id) == 1

    assert store.insert_observation(tenant, agent, conv, covers_from_turn=3, covers_to_turn=6,
                                    content="x").status == "overlap"
    gap = store.insert_observation(tenant, agent, conv, covers_from_turn=6, covers_to_turn=7, content="x")
    assert (gap.status, gap.covered_to_turn) == ("gap", 4)
    nxt = store.insert_observation(tenant, agent, conv, covers_from_turn=5, covers_to_turn=6, content="5..6")
    assert (nxt.status, nxt.covered_to_turn) == ("created", 6)
    with pytest.raises(ValueError):
        store.insert_observation(tenant, agent, conv, covers_from_turn=0, covers_to_turn=1, content="x")


def test_reflection_supersedes_and_replays(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=8)
    a = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="a").observation
    b = store.insert_observation(tenant, agent, conv, covers_from_turn=3, covers_to_turn=5, content="b").observation
    c = store.insert_observation(tenant, agent, conv, covers_from_turn=6, covers_to_turn=6, content="c").observation
    # not exactly the active rows up to the end: stale, nothing written
    assert store.reflect(tenant, agent, conv, supersede_ids=[b.id], content="r").status == "stale"
    r = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="reflection")
    assert r.status == "created" and set(r.superseded) == {a.id, b.id}
    assert (r.observation.kind, r.observation.covers_from_turn, r.observation.covers_to_turn) == ("reflection", 1, 5)
    replay = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="again")
    assert replay.status == "existing" and replay.observation.id == r.observation.id
    active = store.list_observations(tenant, conv)
    assert [(o.kind, o.covers_to_turn) for o in active] == [("reflection", 5), ("observation", 6)]
    everything = store.list_observations(tenant, conv, include_superseded=True)
    assert {o.id: o.superseded_by for o in everything}[a.id] == r.observation.id
    # a second reflection over the first reflection and c
    r2 = store.reflect(tenant, agent, conv, supersede_ids=[r.observation.id, c.id], content="r2")
    assert r2.status == "created" and (r2.observation.covers_from_turn, r2.observation.covers_to_turn) == (1, 6)
    # deleting a reflection deletes everything it folded in
    assert store.delete_observation(tenant, conv, r2.observation.id) == 5
    assert store.list_observations(tenant, conv, include_superseded=True) == []
    assert store.delete_observation(tenant, conv, r2.observation.id) == 0


def test_observation_isolation(store, runs, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="mine")
    with pytest.raises(ConversationNotFound):
        store.insert_observation(tenant + "_b", agent, conv, covers_from_turn=1, covers_to_turn=1, content="x")
    with pytest.raises(ConversationNotFound):
        store.insert_observation(tenant, str(uuid.uuid4()), conv, covers_from_turn=3, covers_to_turn=3, content="x")
    assert store.list_observations(tenant + "_b", conv) == []
    assert store.list_observations(tenant, conv, agent_id=str(uuid.uuid4())) == []
    assert store.delete_observations(tenant + "_b", conv) == 0
    assert len(store.list_observations(tenant, conv)) == 1
    assert store.delete_observations(tenant, conv) == 1


# --------------------------------------------------------------------------------------- snapshot


def test_snapshot_excludes_covered_turns_and_applies_limits(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="u1", turns=6)
    add_turn(engine, tenant, agent, conv, 7, status="failed")
    add_turn(engine, tenant, agent, conv, 8, status="completed")
    merge(store, tenant, agent, {"name": "Ana"})
    store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=3, content="1..3")

    snap = store.load_snapshot(tenant, agent, conv, before_turn=9, working=WorkingMemoryConfig(json_schema=SCHEMA),
                               observations=True)
    assert snap.resource_id == "u1" and snap.working_memory.content == {"name": "Ana"}
    assert (snap.working_memory_scope, snap.working_memory_key) == ("resource", "u1")
    assert snap.covered_to_turn == 3 and [o.covers_to_turn for o in snap.observations] == [3]
    assert snap.turn_numbers == (4, 5, 6, 8)
    assert snap.turns[0]["messages"] == [{"kind": "request", "turn": 4}] and snap.turns[0]["approx_tokens"] > 0

    assert store.load_snapshot(tenant, agent, conv, before_turn=6, observations=True).turn_numbers == (4, 5)
    # observations off: every completed turn, no observation
    off = store.load_snapshot(tenant, agent, conv)
    assert off.turn_numbers == (1, 2, 3, 4, 5, 6, 8) and off.observations == () and off.working_memory is None
    limited = store.load_snapshot(tenant, agent, conv, history=HistoryConfig(last_turns=2))
    assert limited.turn_numbers == (6, 8) and limited.trimmed_turns == 5


def test_history_token_limit_drops_whole_turns_oldest_first():
    turns = [{"turn": n, "approx_tokens": t} for n, t in [(1, 50), (2, 30), (3, 30), (4, 30)]]
    kept, dropped = apply_history_limits(turns, HistoryConfig(last_turns=20, max_tokens=70))
    assert [t["turn"] for t in kept] == [3, 4] and dropped == 2
    kept, _ = apply_history_limits(turns, HistoryConfig(last_turns=20, max_tokens=10))
    assert kept == []
    assert apply_history_limits(turns, None) == (turns, 0)


def test_snapshot_scopes_and_isolation(store, runs, tenant, agent):
    legacy = conversation(runs, tenant, agent, resource=None, turns=1)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=legacy, content="# conv")
    resource_cfg = WorkingMemoryConfig()
    snap = store.load_snapshot(tenant, agent, legacy, working=resource_cfg)
    assert snap.working_memory_key is None and snap.working_memory is None  # no resource: resource scope off
    conv_cfg = WorkingMemoryConfig(scope="conversation")
    assert store.load_snapshot(tenant, agent, legacy, working=conv_cfg).working_memory.content == "# conv"
    with pytest.raises(ConversationNotFound):
        store.load_snapshot(tenant, str(uuid.uuid4()), legacy)
    with pytest.raises(ConversationNotFound):
        store.load_snapshot(tenant + "_b", agent, legacy)
    # another agent's resource note is not this agent's
    other = str(uuid.uuid4())
    merge(store, tenant, other, {"name": "Other"})
    conv = conversation(runs, tenant, agent, resource="u1")
    assert store.load_snapshot(tenant, agent, conv, working=WorkingMemoryConfig(json_schema=SCHEMA)).working_memory is None


# -------------------------------------------------------------------------------------- deletions


def test_delete_conversation(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="u1", turns=2)
    run1 = run_of(engine, tenant, conv, 1)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    merge(store, tenant, agent, {"name": "Ana"})
    store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="x")
    assert store.delete_conversation(tenant + "_b", conv) is False
    assert store.delete_conversation(tenant, conv) is True
    assert runs.get_conversation(tenant, conv) is None
    assert count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=tenant) == 0
    assert store.get_working_memory(tenant, agent, "conversation", conv) is None
    assert store.get_working_memory(tenant, agent, "resource", "u1") is not None  # the resource's, kept
    run = runs.get_run(tenant, run1)
    assert run is not None and run["status"] == "completed"
    assert (run["input"], run["output"], run["conversation_id"], run["turn"]) == (None, None, None, None)
    assert count(engine, "SELECT count(*) FROM runs WHERE id = :r AND messages IS NULL", r=run1) == 1
    assert store.delete_conversation(tenant, conv) is False  # idempotent


def test_delete_conversation_refuses_a_running_turn(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    add_turn(engine, tenant, agent, conv, 2, status="running")
    with pytest.raises(TurnInProgress):
        store.delete_conversation(tenant, conv)
    assert runs.get_conversation(tenant, conv) is not None


def test_forget_resource(store, runs, engine, tenant, agent):
    other = str(uuid.uuid4())
    merge(store, tenant, agent, {"name": "Ana"})
    merge(store, tenant, other, {"name": "Ana too"})
    merge(store, tenant, agent, {"name": "Bo"}, key="u2")
    merge(store, tenant + "_b", agent, {"name": "Ana in B"})
    c1 = conversation(runs, tenant, agent, resource="u1", turns=1)
    c2 = conversation(runs, tenant, other, resource="u1", turns=1)
    busy = conversation(runs, tenant, other, resource="u1")
    queued = add_turn(engine, tenant, other, busy, 1, status="queued")
    keep = conversation(runs, tenant, agent, resource="u2", turns=1)

    one = store.forget_resource(tenant, "u1", agent_id=agent)  # this agent has no turn in progress
    assert (one.working_memories, one.conversations) == (1, ())
    assert store.get_working_memory(tenant, other, "resource", "u1") is not None

    with pytest.raises(ResourceBusy) as exc:  # the other agent's queued turn blocks a tenant-wide forget
        store.forget_resource(tenant, "u1", include_conversations=True)
    assert exc.value.busy_conversations == (busy,)
    assert store.get_working_memory(tenant, other, "resource", "u1") is not None
    assert pickle.loads(pickle.dumps(exc.value)).busy_conversations == (busy,)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE id = :r"), {"r": queued})

    everything = store.forget_resource(tenant, "u1", include_conversations=True)
    assert everything.working_memories == 1
    assert set(everything.conversations) == {c1, c2, busy} and everything.busy_conversations == ()
    assert count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t AND scope_key = 'u1'",
                 t=tenant) == 0
    assert runs.get_conversation(tenant, keep) is not None
    assert store.get_working_memory(tenant, agent, "resource", "u2") is not None
    assert store.get_working_memory(tenant + "_b", agent, "resource", "u1") is not None
    again = store.forget_resource(tenant, "u1", include_conversations=True)
    assert (again.working_memories, again.conversations) == (0, ())


def test_new_errors_pickle():
    for exc in (
        WorkingMemoryVersionConflict("resource", "u1", 1, 2),
        MemoryLedgerConflict("wm:a:b", "reserved"),
        ConversationResourceConflict("c1"),
    ):
        back = pickle.loads(pickle.dumps(exc))
        assert type(back) is type(exc) and str(back) == str(exc)


# ------------------------------------------------------------------------------------- scrubbing

SECRET = "zz-planted-secret-7731"
SCRUB_SCHEMA = {"type": "object", "properties": {}}  # additional keys allowed: model-written key names


def plant(engine, tenant: str, agent: str, store, run_id: str, call: str = "call-1") -> None:
    """Text carrying SECRET in every place a turn writes it, plus usage that must survive."""
    from eap_run_store import EventLog

    log = EventLog(engine)
    usage = {"model_id": "test:echo", "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "usage_measured": True, "request_key": "model:0"}
    events = [
        ("run_started", {"input": SECRET, "agent_id": agent, "turn": 1}, None),
        ("model_message", {"model_request": {"parts": [{"content": SECRET}]}, "model_id": "test:echo",
                           "usage": {"input_tokens": 7, "output_tokens": 3}}, usage),
        ("tool_call", {"tool_name": "lookup", "tool_call_id": "t1", "args": {"q": SECRET}}, None),
        ("tool_result", {"tool_name": "lookup", "tool_call_id": "t1", "content": SECRET, "text": SECRET}, None),
        ("processor_result", {"processor": "pii_redact", "matches": 1, "details": [SECRET]}, None),
        ("run_completed", {"output": SECRET, "status": "completed"}, None),
    ]
    for i, (type_, data, use) in enumerate(events):
        log.append(tenant, uuid.UUID(run_id), f"e{i}", type_, data, usage=use)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET input = :s, output = :s, messages = CAST(:m AS jsonb), request_context = CAST(:m AS jsonb),"
                 " input_tokens = 7, output_tokens = 3, cost_usd = 0.5 WHERE id = :r"),
            {"s": SECRET, "m": json.dumps({"x": SECRET}), "r": run_id},
        )
    store.update_working_memory(
        tenant, agent, scope="resource", scope_key="u1", patch={SECRET: "v"}, json_schema=SCRUB_SCHEMA,
        ledger_key=working_memory_ledger_key(run_id, call), run_id=run_id,
        event=MemoryEvent(run_id=run_id, event_key=f"memory_updated:{call}"),
    )


def secret_hits(engine, tenant: str, run_ids: list[str]) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            "events": conn.execute(text(
                "SELECT count(*) FROM execution_events WHERE tenant_id = :t AND run_id = ANY(:ids)"
                " AND data::text LIKE '%' || :s || '%'"), {"t": tenant, "ids": run_ids, "s": SECRET}).scalar_one(),
            "runs": conn.execute(text(
                "SELECT count(*) FROM runs r WHERE tenant_id = :t AND id = ANY(:ids)"
                " AND row_to_json(r)::text LIKE '%' || :s || '%'"), {"t": tenant, "ids": run_ids, "s": SECRET}).scalar_one(),
            "ledger": conn.execute(text(
                "SELECT count(*) FROM side_effects WHERE tenant_id = :t AND key LIKE 'wm:%'"
                " AND split_part(key, ':', 2) = ANY(:ids)"), {"t": tenant, "ids": run_ids}).scalar_one(),
        }


def usage_state(engine, tenant: str, run_ids: list[str]):
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT run_id, request_key, input_tokens, output_tokens FROM run_model_requests"
            " WHERE tenant_id = :t AND run_id = ANY(:ids) ORDER BY run_id, request_key"),
            {"t": tenant, "ids": run_ids}).all()
        totals = conn.execute(text(
            "SELECT id, input_tokens, output_tokens, cost_usd FROM runs WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": run_ids}).all()
    return [tuple(r) for r in rows], [tuple(r) for r in totals]


def seqs_contiguous(engine, run_ids: list[str]) -> bool:
    with engine.connect() as conn:
        for rid in run_ids:
            seqs = conn.execute(text("SELECT seq FROM execution_events WHERE run_id = :r ORDER BY seq"),
                                {"r": rid}).scalars().all()
            if seqs != list(range(len(seqs))) or not seqs:
                return False
    return True


def test_scrub_event_data_keeps_only_ids_codes_and_numbers():
    from eap_run_store import scrub_event_data

    out = scrub_event_data({"input": SECRET, "model_id": "m", "turn": 2, "ok": True, "x": None,
                            "usage": {"input_tokens": 3}, "args": {"q": SECRET}, "changed_keys": [SECRET],
                            "error": SECRET, "error_code": "E1"})
    assert out == {"model_id": "m", "turn": 2, "ok": True, "x": None, "usage": {"input_tokens": 3},
                   "error_code": "E1", "redacted": True}
    assert scrub_event_data(out) == out and scrub_event_data(None) == {"redacted": True}


def test_delete_conversation_scrubs_events_and_ledger(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, resource="u1", turns=2)
    run_ids = [run_of(engine, tenant, conv, n) for n in (1, 2)]
    for n, rid in enumerate(run_ids):
        plant(engine, tenant, agent, store, rid, call=f"c{n}")
    other = conversation(runs, tenant, agent, resource="u9", turns=1)
    other_run = run_of(engine, tenant, other, 1)
    plant(engine, tenant, agent, store, other_run)
    before = usage_state(engine, tenant, run_ids)
    assert secret_hits(engine, tenant, run_ids) == {"events": 14, "runs": 2, "ledger": 2}

    assert store.delete_conversation(tenant, conv)
    assert secret_hits(engine, tenant, run_ids) == {"events": 0, "runs": 0, "ledger": 0}
    assert usage_state(engine, tenant, run_ids) == before and before[0]
    assert seqs_contiguous(engine, run_ids)
    with engine.connect() as conn:
        kept = conn.execute(text("SELECT type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
                            {"r": run_ids[0]}).all()
    assert [k.type for k in kept][:2] == ["run_started", "model_message"]
    assert kept[1].data == {"model_id": "test:echo", "usage": {"input_tokens": 7, "output_tokens": 3},
                            "redacted": True}
    # another conversation's runs are untouched
    assert secret_hits(engine, tenant, [other_run]) == {"events": 7, "runs": 1, "ledger": 1}


def test_forget_resource_scrubs_key_names_and_conversations(store, runs, engine, tenant, agent):
    kept_conv = conversation(runs, tenant, agent, resource="u1", turns=1)
    kept_run = run_of(engine, tenant, kept_conv, 1)
    plant(engine, tenant, agent, store, kept_run)
    # notes only: the writer runs lose their key names (memory_updated data, ledger), not their turn text
    store.forget_resource(tenant, "u1", agent_id=agent)
    with engine.connect() as conn:
        data = conn.execute(text("SELECT data FROM execution_events WHERE run_id = :r AND type = 'memory_updated'"),
                            {"r": kept_run}).scalar_one()
    assert SECRET not in json.dumps(data) and data["redacted"] is True
    assert secret_hits(engine, tenant, [kept_run])["ledger"] == 0
    assert secret_hits(engine, tenant, [kept_run])["events"] == 6  # the turn's own text stays

    before = usage_state(engine, tenant, [kept_run])
    result = store.forget_resource(tenant, "u1", include_conversations=True)
    assert result.conversations == (kept_conv,)
    assert secret_hits(engine, tenant, [kept_run]) == {"events": 0, "runs": 0, "ledger": 0}
    assert usage_state(engine, tenant, [kept_run]) == before
    assert seqs_contiguous(engine, [kept_run])


# ------------------------------------------------------------------------- review round 2 fixes


def test_writes_must_name_a_run_of_the_same_agent_and_conversation(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    other_conv = conversation(runs, tenant, agent, turns=2)
    foreign = run_of(engine, tenant, other_conv, 1)
    with pytest.raises(RunNotFound):  # conversation scope: a turn of another conversation
        store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c",
                                    run_id=foreign)
    with pytest.raises(RunNotFound):
        store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=1, content="x",
                                 run_id=foreign)
    with pytest.raises(RunNotFound):  # another agent's run, resource scope
        merge(store, tenant, str(uuid.uuid4()), {"name": "x"}, run_id=run_of(engine, tenant, conv, 1))
    assert count(engine, "SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=tenant) == 0
    assert count(engine, "SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=tenant) == 0


def test_ledger_key_is_per_agent_and_bound_to_its_target(store, tenant, agent):
    key = working_memory_ledger_key("run-1", "call-1")
    first = merge(store, tenant, agent, {"name": "A"}, ledger_key=key)
    other = merge(store, tenant, str(uuid.uuid4()), {"name": "B"}, ledger_key=key)
    assert first.replayed is False and other.replayed is False
    with pytest.raises(MemoryLedgerConflict):
        merge(store, tenant, agent, {"name": "A"}, key="u2", ledger_key=key)
    assert store.get_working_memory(tenant, agent, "resource", "u2") is None


def test_delete_conversation_deletes_every_side_effect_of_its_runs(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=1)
    keep_conv = conversation(runs, tenant, agent, turns=1)
    rid, keep = run_of(engine, tenant, conv, 1), run_of(engine, tenant, keep_conv, 1)
    with engine.begin() as conn:
        for k in (f"{rid}:tool", f"{rid}:mcp:call-1", f"{keep}:tool", "unrelated"):
            conn.execute(text("INSERT INTO side_effects (tenant_id, key, state, result, created_at)"
                              " VALUES (:t, :k, 'completed', CAST(:r AS jsonb), now())"),
                         {"t": tenant, "k": k, "r": json.dumps({"answer": SECRET})})
    store.delete_conversation(tenant, conv)
    with engine.connect() as conn:
        left = conn.execute(text("SELECT key FROM side_effects WHERE tenant_id = :t ORDER BY key"),
                            {"t": tenant}).scalars().all()
    assert left == sorted([f"{keep}:tool", "unrelated"])


def test_run_side_effect_prefix_delete_uses_the_pattern_index(engine, tenant):
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(conn.execute(text(
            "EXPLAIN DELETE FROM side_effects WHERE tenant_id = :t AND key LIKE 'wm:abc:%' ESCAPE '\\'"),
            {"t": tenant}).scalars())
    assert "ix_side_effects_tenant_key_pattern" in plan, plan


def test_scrub_drops_strings_that_are_not_identifiers():
    from eap_run_store import scrub_event_data

    out = scrub_event_data({"tool_name": "lookup.v2", "action": f"say {SECRET}", "code": "E_1", "status": "x" * 200})
    assert out == {"tool_name": "lookup.v2", "code": "E_1", "redacted": True}


def test_observation_cannot_run_past_the_last_completed_turn(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    add_turn(engine, tenant, agent, conv, 3, status="running")
    ahead = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=3, content="x")
    assert (ahead.status, ahead.observation) == ("ahead", None)
    assert store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2,
                                    content="x").status == "created"
