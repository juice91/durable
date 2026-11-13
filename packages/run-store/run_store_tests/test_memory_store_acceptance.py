"""RM.5 acceptance for MemoryStore: isolation of every public method, working memory rules, the
ledger, observation ranges, snapshot invariants and history limits, privacy deletions, pickling.

Complements test_memory_store.py and test_memory_store_concurrency.py (helpers are imported)."""

from __future__ import annotations

import copy
import json
import math
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
    RunStore,
    TurnInProgress,
    WorkingMemoryVersionConflict,
    working_memory_ledger_key,
)
from eap_run_store.memory import ResourceBusy, ledger_row_key
from eap_schemas.memory import HistoryConfig, WorkingMemoryConfig, WorkingMemoryInvalid

from .test_memory_store import (
    SCHEMA,
    SECRET,
    add_turn,
    cleanup_tenant,
    conversation,
    count,
    merge,
    plant,
    run_of,
    secret_hits,
    seqs_contiguous,
    usage_state,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

OPEN_SCHEMA = {"type": "object", "properties": {}}  # accepts any model-written key
KEY_SECRET = "zz-secret-key-9913"
VAL_SECRET = "zz-secret-value-4402"


@pytest.fixture
def store(engine):
    return MemoryStore(engine)


@pytest.fixture
def runs(engine):
    return RunStore(engine)


@pytest.fixture
def tenant():
    return f"memacc_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def agent():
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def _cleanup(engine, tenant):
    yield
    cleanup_tenant(engine, tenant)
    cleanup_tenant(engine, tenant + "_b")


def dump(engine, tenant: str) -> dict:
    """Every row of every table memory touches for one tenant (for before/after comparison)."""
    queries = {
        "conversations": "SELECT id, agent_id, resource_id, title FROM conversations WHERE tenant_id = :t ORDER BY id",
        "working_memories": "SELECT agent_id, scope, scope_key, conversation_id, format, content::text, version,"
                            " updated_by_run_id, updated_at FROM working_memories WHERE tenant_id = :t ORDER BY 1,2,3",
        "observations": "SELECT id, agent_id, conversation_id, kind, covers_from_turn, covers_to_turn, content,"
                        " superseded_by FROM memory_observations WHERE tenant_id = :t ORDER BY id",
        "ledger": "SELECT key, state, result::text FROM side_effects WHERE tenant_id = :t ORDER BY key",
        "runs": "SELECT id, status, input, output, error, messages::text, request_context::text, conversation_id,"
                " turn, input_tokens, output_tokens, cost_usd FROM runs WHERE tenant_id = :t ORDER BY id",
        "events": "SELECT run_id, seq, type, data::text, event_key FROM execution_events WHERE tenant_id = :t"
                  " ORDER BY run_id, seq",
        "usage": "SELECT run_id, request_key, input_tokens, output_tokens FROM run_model_requests"
                 " WHERE tenant_id = :t ORDER BY 1, 2",
    }
    with engine.connect() as conn:
        return {k: [tuple(r) for r in conn.execute(text(q), {"t": tenant})] for k, q in queries.items()}


# ------------------------------------------------------------------------------------- isolation


def populate(store, runs, engine, tenant, agent) -> dict:
    """Tenant A's world: a resource note, a conversation note, observations, a ledger row, an event."""
    conv = conversation(runs, tenant, agent, resource="u1", turns=5)
    run_id = run_of(engine, tenant, conv, 5)
    key = working_memory_ledger_key(run_id, "call-1")
    merge(store, tenant, agent, {"name": "Ana", "tags": ["a"]}, ledger_key=key, run_id=run_id,
          event=MemoryEvent(run_id=run_id, event_key="memory_updated:call-1"))
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# conv note")
    a = store.insert_observation(tenant, agent, conv, covers_from_turn=1, covers_to_turn=2, content="obs a").observation
    b = store.insert_observation(tenant, agent, conv, covers_from_turn=3, covers_to_turn=4, content="obs b").observation
    return {"conv": conv, "run_id": run_id, "key": key, "obs": [a.id, b.id]}


ATTACKERS = [("tenant_b", True, False), ("agent_b", False, True), ("tenant_b_agent_b", True, True)]


@pytest.mark.parametrize("name,other_tenant,other_agent", ATTACKERS)
def test_every_read_and_delete_method_finds_nothing_for_another_tenant_or_agent(
    store, runs, engine, tenant, agent, name, other_tenant, other_agent
):
    ids = populate(store, runs, engine, tenant, agent)
    t = tenant + "_b" if other_tenant else tenant
    a = str(uuid.uuid4()) if other_agent else agent
    before = dump(engine, tenant)
    conv = ids["conv"]

    assert store.get_working_memory(t, a, "resource", "u1") is None
    assert store.get_working_memory(t, a, "conversation", conv) is None
    assert store.list_working_memories(t, a) == []
    assert store.list_observations(t, conv, agent_id=a) == []
    if other_tenant:
        assert store.list_observations(t, conv) == []
        assert store.list_observations(t, conv, include_superseded=True) == []
        assert store.delete_observation(t, conv, ids["obs"][0]) == 0
        assert store.delete_observations(t, conv) == 0
        assert store.delete_conversation(t, conv) is False
    assert store.delete_observations(t, conv, agent_id=a) == 0
    assert store.delete_working_memory(t, a, "resource", "u1") is False
    assert store.delete_working_memory(t, a, "conversation", conv) is False
    assert store.delete_working_memory(t, a, "resource", "u1", expected_version=0) is False
    with pytest.raises(WorkingMemoryVersionConflict):  # sees no note: version 0, not A's version 1
        store.delete_working_memory(t, a, "resource", "u1", expected_version=1)

    for call in (
        lambda: store.load_snapshot(t, a, conv, working=WorkingMemoryConfig(scope="conversation"), observations=True),
        lambda: store.insert_observation(t, a, conv, covers_from_turn=5, covers_to_turn=5, content="x"),
        lambda: store.reflect(t, a, conv, supersede_ids=ids["obs"], content="x"),
        lambda: store.update_working_memory(t, a, scope="conversation", scope_key=conv, content="# stolen"),
        lambda: store.update_working_memory(
            t, a, scope="conversation", scope_key=conv, patch={"name": "x"}, json_schema=SCHEMA),
    ):
        with pytest.raises(ConversationNotFound):
            call()
    assert dump(engine, tenant) == before  # nothing of tenant A changed


@pytest.mark.parametrize("name,other_tenant,other_agent", ATTACKERS)
def test_a_write_cannot_append_an_event_to_another_tenants_or_agents_run(
    store, runs, engine, tenant, agent, name, other_tenant, other_agent
):
    ids = populate(store, runs, engine, tenant, agent)
    t = tenant + "_b" if other_tenant else tenant
    a = str(uuid.uuid4()) if other_agent else agent
    before = dump(engine, tenant)
    with pytest.raises(RunNotFound):
        merge(store, t, a, {"x": 1}, key="u9", run_id=ids["run_id"],
              event=MemoryEvent(run_id=ids["run_id"], event_key="foreign"))
    assert store.get_working_memory(t, a, "resource", "u9") is None  # rolled back with the event
    assert dump(engine, tenant) == before


@pytest.mark.parametrize("name,other_tenant,other_agent", ATTACKERS)
def test_resource_writes_and_forget_by_another_tenant_or_agent_touch_only_their_own_rows(
    store, runs, engine, tenant, agent, name, other_tenant, other_agent
):
    ids = populate(store, runs, engine, tenant, agent)
    t = tenant + "_b" if other_tenant else tenant
    a = str(uuid.uuid4()) if other_agent else agent
    before = dump(engine, tenant)
    # Same resource id "u1": a write lands in the attacker's own (tenant, agent) row, A's stays as is.
    merge(store, t, a, {"name": "Mallory"}, key="u1")
    assert store.get_working_memory(t, a, "resource", "u1").content == {"name": "Mallory"}
    mine = store.get_working_memory(tenant, agent, "resource", "u1")
    assert mine.content == {"name": "Ana", "tags": ["a"]} and mine.version == 1
    # the same ledger key in another tenant is a different ledger row
    if other_tenant:
        w = merge(store, t, a, {"name": "again"}, key="u2", ledger_key=ids["key"])
        assert w.replayed is False and w.version == 1
        assert store.get_working_memory(t, a, "resource", "u2").content == {"name": "again"}
    forgot = store.forget_resource(t, "u1", agent_id=a, include_conversations=True)
    assert (forgot.working_memories, forgot.conversations) == (1, ())  # only the attacker's own note
    assert store.get_working_memory(t, a, "resource", "u1") is None
    if other_tenant:
        store.delete_working_memory(t, a, "resource", "u2")
        everything = store.forget_resource(t, "u1", include_conversations=True)
        assert (everything.working_memories, everything.conversations) == (0, ())
    else:
        everything = store.forget_resource(t, "u1", agent_id=a, include_conversations=True)
        assert (everything.working_memories, everything.conversations) == (0, ())
    after = dump(engine, tenant)
    assert after == before
    assert runs.get_conversation(tenant, ids["conv"]) is not None


def test_another_agents_write_naming_a_ledger_key_is_not_answered_from_the_first_agents_result(
    store, runs, engine, tenant, agent
):
    ids = populate(store, runs, engine, tenant, agent)
    other = str(uuid.uuid4())
    before = dump(engine, tenant)
    w = merge(store, tenant, other, {"name": "Other"}, key="u7", ledger_key=ids["key"])
    assert w.replayed is False  # not agent A's replayed (version, changed_keys) result
    assert store.get_working_memory(tenant, other, "resource", "u7").content == {"name": "Other"}
    assert [r for r in dump(engine, tenant)["working_memories"] if r[0] == agent] == [
        r for r in before["working_memories"] if r[0] == agent
    ]


# --------------------------------------------------------------------------------- working memory


def note(store, tenant, agent, key="u1", scope="resource"):
    return store.get_working_memory(tenant, agent, scope, key)


def test_markdown_replace_keeps_only_the_last_text_and_counts_versions(store, tenant, agent):
    texts = ["# a", "", "# ü 日本語 🙂\n- x", "# a"]
    for n, body in enumerate(texts, 1):
        w = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content=body)
        assert (w.version, w.format, w.changed_keys, w.replayed) == (n, "markdown", (), False)
        assert note(store, tenant, agent).content == body
    assert count(engine=store.engine, sql="SELECT count(*) FROM working_memories WHERE tenant_id = :t", t=tenant) == 1


def test_json_merge_null_deletes_nested_objects_and_arrays_replace(store, tenant, agent):
    schema = {"type": "object", "properties": {
        "a": {"type": "object", "properties": {"b": {"type": "object", "properties": {
            "c": {"type": "string"}, "d": {"type": "string"}}}, "e": {"type": "string"}}},
        "list": {"type": "array", "items": {"type": "integer"}}, "keep": {"type": "string"}}}

    def patch(p):
        return store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch=p, json_schema=schema)

    patch({"a": {"b": {"c": "1", "d": "2"}, "e": "3"}, "list": [1, 2, 3], "keep": "k"})
    patch({"a": {"b": {"c": None}}})  # a nested null deletes only that key
    assert note(store, tenant, agent).content == {"a": {"b": {"d": "2"}, "e": "3"}, "list": [1, 2, 3], "keep": "k"}
    patch({"list": [9]})  # arrays replace, never concatenate
    patch({"list": []})
    assert note(store, tenant, agent).content["list"] == []
    patch({"nothing_here": None, "a": {"zzz": None}})  # deleting absent keys is a no-op
    patch({"a": {"e": None}})
    assert note(store, tenant, agent).content["a"] == {"b": {"d": "2"}}
    patch({"a": None})  # a whole object goes with one null
    assert note(store, tenant, agent).content == {"list": [], "keep": "k"}
    v = note(store, tenant, agent).version
    w = patch({})  # an empty patch is a write of the same content: version still counts
    assert (w.version, w.changed_keys) == (v + 1, ()) and note(store, tenant, agent).content == {"list": [], "keep": "k"}
    patch({"keep": None, "list": None})
    assert note(store, tenant, agent).content == {}  # a note may become empty: still a row
    assert note(store, tenant, agent).format == "json"


def test_merge_does_not_mutate_the_callers_patch(store, tenant, agent):
    p = {"prefs": {"food": "rice", "drink": None}, "tags": ["a"]}
    frozen = copy.deepcopy(p)
    merge(store, tenant, agent, p)
    assert p == frozen
    assert note(store, tenant, agent).content == {"prefs": {"food": "rice"}, "tags": ["a"]}


def test_schema_violations_write_nothing(store, engine, tenant, agent):
    conv = conversation(store_runs(engine), tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    merge(store, tenant, agent, {"name": "Ana"})
    before = dump(engine, tenant)
    bad_patches = [{"age": "old"}, {"name": 5}, {"tags": "nope"}, {"tags": [1]}, {"prefs": {"food": 3}},
                   {"prefs": "x"}, {"age": 1.5}]
    for n, bad in enumerate(bad_patches):
        with pytest.raises(WorkingMemoryInvalid):
            merge(store, tenant, agent, bad, ledger_key=f"wm:{run_id}:bad{n}", run_id=run_id,
                  event=MemoryEvent(run_id=run_id, event_key=f"bad{n}"))
        assert dump(engine, tenant) == before  # no version bump, no ledger row, no event, no updated_at
    # a rejected first write creates no row either, and a rejected replace changes nothing
    with pytest.raises(WorkingMemoryInvalid):
        merge(store, tenant, agent, {"age": "x"}, key="fresh")
    assert note(store, tenant, agent, "fresh") is None
    with pytest.raises(WorkingMemoryInvalid):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content={"age": "x"},
                                    json_schema=SCHEMA)
    assert dump(engine, tenant) == before
    # a failed call with a ledger key leaves the key free: the corrected retry writes
    w = merge(store, tenant, agent, {"age": 7}, ledger_key=f"wm:{run_id}:bad0")
    assert w.replayed is False and note(store, tenant, agent).content == {"name": "Ana", "age": 7}


def store_runs(engine):
    return RunStore(engine)


def test_expected_version_conflicts_write_nothing_and_keep_the_ledger_key_free(store, engine, tenant, agent):
    def put(**kw):
        return store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", json_schema=SCHEMA, **kw)

    with pytest.raises(WorkingMemoryVersionConflict) as exc:  # the note must not exist yet: 0 is fine, 1 is not
        put(patch={"name": "A"}, expected_version=1)
    assert (exc.value.scope, exc.value.scope_key, exc.value.expected, exc.value.actual) == ("resource", "u1", 1, 0)
    assert note(store, tenant, agent) is None
    assert put(patch={"name": "A"}, expected_version=0).version == 1
    before = dump(engine, tenant)
    for expected in (0, 2, 99):
        with pytest.raises(WorkingMemoryVersionConflict) as exc:
            put(patch={"name": "B"}, expected_version=expected, ledger_key="wm:r:k")
        assert (exc.value.expected, exc.value.actual) == (expected, 1)
        assert dump(engine, tenant) == before  # nothing written, the ledger key not consumed
    with pytest.raises(WorkingMemoryVersionConflict):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# md", expected_version=2)
    assert put(patch={"name": "B"}, expected_version=1, ledger_key="wm:r:k").version == 2
    assert note(store, tenant, agent).content == {"name": "B"}
    # delete honours it too, and a missing note is version 0
    with pytest.raises(WorkingMemoryVersionConflict):
        store.delete_working_memory(tenant, agent, "resource", "u1", expected_version=1)
    assert note(store, tenant, agent).version == 2
    with pytest.raises(WorkingMemoryVersionConflict):
        store.delete_working_memory(tenant, agent, "resource", "nobody", expected_version=1)


def test_json_over_markdown_starts_from_empty_and_markdown_over_json_replaces(store, tenant, agent):
    store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content='{"name": "looks like json"}')
    w = merge(store, tenant, agent, {"age": 4})
    n = note(store, tenant, agent)
    assert (w.version, w.changed_keys, n.format, n.content) == (2, ("age",), "json", {"age": 4})
    w = store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# back to text")
    n = note(store, tenant, agent)
    assert (w.version, n.format, n.content) == (3, "markdown", "# back to text")
    # a null-only patch over markdown starts from {} too (nothing to delete)
    w = merge(store, tenant, agent, {"name": None})
    assert note(store, tenant, agent).content == {} and w.version == 4
    # the same on a conversation-scoped note
    conv = conversation(RunStore(store.engine), tenant, agent)
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# md")
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, patch={"name": "C"},
                                json_schema=SCHEMA)
    assert note(store, tenant, agent, conv, "conversation").content == {"name": "C"}


def test_the_16_kib_cap_through_the_store(store, engine, tenant, agent):
    edge = "x" * (16 * 1024)
    assert store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content=edge).version == 1
    before = dump(engine, tenant)
    for body in (edge + "x", "é" * (8 * 1024 + 1)):  # a UTF-8 byte cap, not a character cap
        with pytest.raises(WorkingMemoryInvalid):
            store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content=body)
    with pytest.raises(WorkingMemoryInvalid):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="fresh", content=edge + "x")
    assert dump(engine, tenant) == before and note(store, tenant, agent, "fresh") is None
    # json: a merge that grows the note past the cap is refused as a whole
    store.update_working_memory(tenant, agent, scope="resource", scope_key="j", patch={"name": "n" * 9000},
                                json_schema=OPEN_SCHEMA)
    snapshot = dump(engine, tenant)
    with pytest.raises(WorkingMemoryInvalid):
        store.update_working_memory(tenant, agent, scope="resource", scope_key="j", patch={"more": "m" * 8000},
                                    json_schema=OPEN_SCHEMA)
    assert dump(engine, tenant) == snapshot
    with pytest.raises(WorkingMemoryInvalid):  # a replace over the cap
        store.update_working_memory(tenant, agent, scope="resource", scope_key="j",
                                    content={"a": "z" * 17000}, json_schema=OPEN_SCHEMA)
    assert dump(engine, tenant) == snapshot


def nest(depth: int, leaf="v", key="k"):
    out: object = leaf
    for _ in range(depth):
        out = {key: out}
    return out


def test_the_depth_caps_through_the_store(store, engine, tenant, agent):
    merge(store, tenant, agent, {"name": "Ana"})
    too_deep_schema = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {
        "type": "object", "properties": {"c": {"type": "object", "properties": {"d": {"type": "string"}}}}}}}}}
    with pytest.raises(WorkingMemoryInvalid):  # schema objects nest at most 3 deep
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch={"a": {}},
                                    json_schema=too_deep_schema)
    ok_schema = {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {
        "type": "object", "properties": {"c": {"type": "string"}}}}}}}
    assert merge_ok(store, tenant, agent, ok_schema, {"a": {"b": {"c": "x"}}}).version == 2  # exactly depth 3
    with pytest.raises(WorkingMemoryInvalid):  # content nesting beyond the cap (16), well inside the schema
        store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch=nest(17, key="x"),
                                    json_schema=OPEN_SCHEMA)
    for depth in (17, 100, 5000):  # never a RecursionError either
        with pytest.raises(WorkingMemoryInvalid):
            store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content=nest(depth),
                                        json_schema=OPEN_SCHEMA)
        with pytest.raises(WorkingMemoryInvalid):
            store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch=nest(depth),
                                        json_schema=OPEN_SCHEMA)
    assert note(store, tenant, agent).content == {"name": "Ana", "a": {"b": {"c": "x"}}}
    assert note(store, tenant, agent).version == 2


def merge_ok(store, tenant, agent, schema, patch):
    return store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", patch=patch, json_schema=schema)


def test_errors_never_echo_content_or_model_written_keys(store, engine, tenant, agent):
    strict = {"type": "object", "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
              "additionalProperties": False}
    cases = [
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={KEY_SECRET: VAL_SECRET}, json_schema=strict),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={"name": {KEY_SECRET: VAL_SECRET}}, json_schema=strict),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={"n": VAL_SECRET}, json_schema=strict),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            content={KEY_SECRET: VAL_SECRET}, json_schema=strict),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={KEY_SECRET: nest(30, VAL_SECRET, KEY_SECRET)}, json_schema=OPEN_SCHEMA),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            content=VAL_SECRET * 5000),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={KEY_SECRET: VAL_SECRET * 5000}, json_schema=OPEN_SCHEMA),
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1",
                                            patch={KEY_SECRET: float("nan")}, json_schema=OPEN_SCHEMA),
    ]
    for n, call in enumerate(cases):
        with pytest.raises(WorkingMemoryInvalid) as exc:
            call()
        e = exc.value
        shown = " ".join([str(e), repr(e), repr(e.args), e.reason, e.path, getattr(e, "code", "")])
        assert KEY_SECRET not in shown and VAL_SECRET not in shown, f"case {n} echoed content: {shown[:200]}"
        assert VAL_SECRET not in shown
    assert dump(engine, tenant)["working_memories"] == []


# ------------------------------------------------------------------------------------------ ledger


def test_replaying_a_ledger_key_with_different_content_returns_the_first_result_and_writes_nothing(
    store, runs, engine, tenant, agent
):
    # The design (plan, "Working memory" row): an existing key returns the stored result; it does NOT raise.
    conv = conversation(runs, tenant, agent, turns=1)
    run_id = run_of(engine, tenant, conv, 1)
    key = working_memory_ledger_key(run_id, "call-1")
    event = MemoryEvent(run_id=run_id, event_key="memory_updated:call-1")
    first = merge(store, tenant, agent, {"name": "First"}, ledger_key=key, run_id=run_id, event=event)
    after_first = dump(engine, tenant)
    # Review decision (RM.5 round 2): the same agent's key for another target or scope is a
    # MemoryLedgerConflict, never a replay of the first target's result; nothing changes either way.
    conflicts = [
        lambda: merge(store, tenant, agent, {"name": "Second"}, key="u2", ledger_key=key),  # other target
        lambda: store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c",
                                            ledger_key=key),  # other scope
    ]
    for n, call in enumerate(conflicts):
        with pytest.raises(MemoryLedgerConflict):
            call()
        assert dump(engine, tenant) == after_first, n
    variants = [
        lambda: merge(store, tenant, agent, {"name": "Second", "age": 3}, ledger_key=key),  # other content
        lambda: store.update_working_memory(tenant, agent, scope="resource", scope_key="u1", content="# text",
                                            ledger_key=key),  # other format
        lambda: merge(store, tenant, agent, {"name": "Second"}, ledger_key=key, expected_version=42),
        lambda: merge(store, tenant, agent, {"name": "Second"}, ledger_key=key, run_id=run_id,
                      event=MemoryEvent(run_id=run_id, event_key="another-event")),
    ]
    for n, call in enumerate(variants):
        replay = call()
        assert replay.replayed is True, n
        assert (replay.scope, replay.format, replay.version, replay.changed_keys) == (
            first.scope, first.format, first.version, first.changed_keys), n
        assert dump(engine, tenant) == after_first, n  # no note, version, event or ledger row changed
    assert note(store, tenant, agent).content == {"name": "First"}
    assert note(store, tenant, agent, "u2") is None
    assert note(store, tenant, agent, conv, "conversation") is None


def test_ledger_rows_hold_no_content_and_a_foreign_state_is_a_conflict(store, engine, tenant, agent):
    store.update_working_memory(
        tenant, agent, scope="resource", scope_key="u1", patch={KEY_SECRET: VAL_SECRET}, json_schema=OPEN_SCHEMA,
        ledger_key="wm:r:1")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT key, state, result::text FROM side_effects WHERE tenant_id = :t"),
                           {"t": tenant}).one()
    assert (row.key, row.state) == (ledger_row_key("wm:r:1", agent), "completed")  # scoped by agent
    assert VAL_SECRET not in row.result  # the ledger result never holds values
    for state, result in (("reserved", None), ("failed", None), ("completed", '{"other": 1}'), ("completed", None)):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO side_effects (tenant_id, key, state, result, created_at)"
                " VALUES (:t, :k, :s, CAST(:r AS jsonb), now())"),
                {"t": tenant, "k": ledger_row_key(f"wm:x:{state}:{result}", agent), "s": state, "r": result})
        before = dump(engine, tenant)
        with pytest.raises(MemoryLedgerConflict) as exc:
            merge(store, tenant, agent, {"name": "x"}, ledger_key=f"wm:x:{state}:{result}")
        assert exc.value.state == state and "name" not in str(exc.value)
        assert dump(engine, tenant) == before


def test_replay_survives_a_deleted_note_and_never_recreates_it(store, tenant, agent):
    first = merge(store, tenant, agent, {"name": "Ana"}, ledger_key="wm:r:9")
    assert store.delete_working_memory(tenant, agent, "resource", "u1")
    replay = merge(store, tenant, agent, {"name": "Ana"}, ledger_key="wm:r:9")
    assert replay.replayed and replay.version == first.version
    assert note(store, tenant, agent) is None  # forgotten means forgotten: a replay is not a write


def test_ledger_key_builder_rejects_blank_parts():
    assert working_memory_ledger_key("run", "call") == "wm:run:call"
    for run_id, call in (("", "c"), ("r", ""), ("", "")):
        with pytest.raises(ValueError):
            working_memory_ledger_key(run_id, call)


# ----------------------------------------------------------------------------------- observations


def obs(store, tenant, agent, conv, f, t, content="o"):
    return store.insert_observation(tenant, agent, conv, covers_from_turn=f, covers_to_turn=t, content=content)


def test_observation_range_rules_overlap_gap_existing(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=12)
    assert obs(store, tenant, agent, conv, 2, 3).status == "gap"  # nothing covered yet: must start at turn 1
    assert obs(store, tenant, agent, conv, 1, 3).status == "created"
    before = dump(engine, tenant)
    for f, t, status in [(1, 3, "existing"), (1, 5, "overlap"), (3, 5, "overlap"), (2, 2, "overlap"),
                         (5, 6, "gap"), (4, 4, "created")]:
        w = obs(store, tenant, agent, conv, f, t, "x")
        assert w.status == status, (f, t, w.status)
        if status in ("overlap", "gap"):
            assert w.observation is None and w.covered_to_turn == 3
            assert dump(engine, tenant) == before
    assert [(o.covers_from_turn, o.covers_to_turn) for o in store.list_observations(tenant, conv)] == [(1, 3), (4, 4)]
    # existing is decided by the slot (covers_to_turn), whatever from-turn the repeat claims
    again = obs(store, tenant, agent, conv, 4, 4, "different")
    assert again.status == "existing" and again.observation.content == "x" and again.covered_to_turn == 4
    # a single-turn row, then the contiguous run continues from the end
    for f, t in [(5, 5), (6, 9)]:
        assert obs(store, tenant, agent, conv, f, t).status == "created"
    assert store.load_snapshot(tenant, agent, conv, observations=True).covered_to_turn == 9
    for bad in ((0, 1), (3, 2), (-1, 4)):
        with pytest.raises(ValueError):
            obs(store, tenant, agent, conv, *bad)
    for empty in ("", "   \n"):
        with pytest.raises(ValueError):
            obs(store, tenant, agent, conv, 10, 10, empty)
    assert obs(store, tenant, agent, conv, 10, 10, "custom").observation.approx_tokens == 2  # ceil(6 / 4)
    explicit = store.insert_observation(tenant, agent, conv, covers_from_turn=11, covers_to_turn=11,
                                        content="abcdefgh", approx_tokens=77)
    assert explicit.observation.approx_tokens == 77


def test_reflect_gives_stale_when_the_rows_are_not_exactly_the_active_prefix(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=9)
    a, b, c = (obs(store, tenant, agent, conv, f, t).observation for f, t in [(1, 2), (3, 4), (5, 6)])
    before = dump(engine, tenant)
    stale_calls = [
        [b.id],  # not the prefix
        [a.id, c.id],  # skips b
        [str(uuid.uuid4())],  # unknown
        [a.id, str(uuid.uuid4())],  # one unknown
    ]
    for ids in stale_calls:
        w = store.reflect(tenant, agent, conv, supersede_ids=ids, content="r")
        assert w.status == "stale" and w.observation is None and w.superseded == ()
        assert dump(engine, tenant) == before
    for bad in ([], ["not-a-uuid"], [a.id, ""]):
        with pytest.raises(ValueError):
            store.reflect(tenant, agent, conv, supersede_ids=bad, content="r")
    with pytest.raises(ValueError):
        store.reflect(tenant, agent, conv, supersede_ids=[a.id], content=" ")
    # a reflection of the first rows only (a prefix) is allowed; the rest stays active
    r = store.reflect(tenant, agent, conv, supersede_ids=[b.id, a.id], content="r1")  # order is irrelevant
    assert r.status == "created" and r.covered_to_turn == 6
    assert [(o.kind, o.covers_to_turn) for o in store.list_observations(tenant, conv)] == [
        ("reflection", 4), ("observation", 6)]
    # once folded in, the old rows can no longer be reflected on: stale, and a second reflection is not made
    again = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r-late")
    assert again.status == "existing" and again.observation.id == r.observation.id
    assert store.reflect(tenant, agent, conv, supersede_ids=[a.id, c.id], content="x").status == "stale"
    # ... and a reflection whose rows were deleted meanwhile is stale
    store.delete_observation(tenant, conv, c.id)
    assert store.reflect(tenant, agent, conv, supersede_ids=[c.id, r.observation.id], content="x").status == "stale"


def test_supersede_chains_and_recursive_delete(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=12)
    a, b, c, d = (obs(store, tenant, agent, conv, f, t, n).observation
                  for n, (f, t) in zip("abcd", [(1, 2), (3, 4), (5, 6), (7, 8)]))
    r1 = store.reflect(tenant, agent, conv, supersede_ids=[a.id, b.id], content="r1").observation
    r2 = store.reflect(tenant, agent, conv, supersede_ids=[r1.id, c.id], content="r2").observation
    r3 = store.reflect(tenant, agent, conv, supersede_ids=[r2.id, d.id], content="r3").observation
    assert (r3.covers_from_turn, r3.covers_to_turn, r3.kind) == (1, 8, "reflection")
    assert [o.id for o in store.list_observations(tenant, conv)] == [r3.id]
    everything = {o.id: o for o in store.list_observations(tenant, conv, include_superseded=True)}
    assert {k: v.superseded_by for k, v in everything.items()} == {
        a.id: r1.id, b.id: r1.id, r1.id: r2.id, c.id: r2.id, r2.id: r3.id, d.id: r3.id, r3.id: None}
    nxt = obs(store, tenant, agent, conv, 9, 9, "after")  # continues from the reflection's end
    assert nxt.status == "created" and nxt.covered_to_turn == 9
    assert obs(store, tenant, agent, conv, 8, 9).status == "existing"  # same slot (covers_to 9)

    # deleting a superseded middle row takes what it folded in, and leaves what folded it
    chain_before = dump(engine, tenant)
    assert store.delete_observation(tenant, conv, r1.id) == 3  # r1, a, b
    left = {o.id for o in store.list_observations(tenant, conv, include_superseded=True)}
    assert left == {c.id, r2.id, d.id, r3.id, nxt.observation.id}
    assert chain_before != dump(engine, tenant)
    # deleting the active reflection deletes the rest of its chain, not the newer observation
    assert store.delete_observation(tenant, conv, r3.id) == 4  # r3, r2, c, d
    assert [o.id for o in store.list_observations(tenant, conv, include_superseded=True)] == [nxt.observation.id]
    assert store.delete_observation(tenant, conv, r3.id) == 0  # idempotent
    assert store.delete_observation(tenant, conv, "not-a-uuid") == 0
    assert store.delete_observation(tenant, conv, str(uuid.uuid4())) == 0
    # a deleted prefix lets the observer start again at turn 1
    store.delete_observation(tenant, conv, nxt.observation.id)
    assert obs(store, tenant, agent, conv, 1, 12, "fresh").status == "created"


def test_delete_observation_stays_inside_its_conversation(store, runs, tenant, agent):
    c1 = conversation(runs, tenant, agent, turns=2)
    c2 = conversation(runs, tenant, agent, turns=2)
    o1 = obs(store, tenant, agent, c1, 1, 2).observation
    o2 = obs(store, tenant, agent, c2, 1, 2).observation
    assert store.delete_observation(tenant, c2, o1.id) == 0  # c1's row named through c2
    assert store.delete_observations(tenant, c1, agent_id=str(uuid.uuid4())) == 0
    assert len(store.list_observations(tenant, c1)) == 1 and len(store.list_observations(tenant, c2)) == 1
    assert store.delete_observations(tenant, c1) == 1
    assert store.list_observations(tenant, c2)[0].id == o2.id
    assert store.delete_observations(tenant, c1) == 0


# --------------------------------------------------------------------------------------- snapshot


def add_sized(engine, tenant, agent, conv, n, chars, status="completed") -> int:
    """A turn whose stored messages are `chars` characters of text; returns its expected approx_tokens."""
    messages = [{"kind": "request", "text": "x" * chars}]
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
                " conversation_id, turn, created_at, updated_at)"
                " VALUES (:id, :t, 'agent', :a, 1, :s, 'i', 'o', CAST(:m AS jsonb), :c, :n, now(), now())"
            ),
            {"id": str(uuid.uuid4()), "t": tenant, "a": agent, "s": status, "m": json.dumps(messages), "c": conv, "n": n},
        )
    return math.ceil(len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"))) / 4)


def check_never_mixed(snap):
    """The invariant of RM.5: no turn of the history lies in a range an observation covers."""
    for o in snap.observations:
        covered = set(range(o.covers_from_turn, o.covers_to_turn + 1))
        assert not covered & set(snap.turn_numbers), (o.covers_from_turn, o.covers_to_turn, snap.turn_numbers)
    assert all(n > snap.covered_to_turn for n in snap.turn_numbers)
    assert snap.covered_to_turn == max((o.covers_to_turn for o in snap.observations), default=0)


def test_snapshot_never_returns_an_observation_with_the_turns_it_covers(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=10)
    add_turn(engine, tenant, agent, conv, 11, status="failed")
    add_turn(engine, tenant, agent, conv, 12, status="queued")
    full = store.load_snapshot(tenant, agent, conv, observations=True)
    assert full.turn_numbers == tuple(range(1, 11)) and full.observations == ()
    ids, end = [], 0
    for to in (2, 3, 7, 10):
        w = obs(store, tenant, agent, conv, end + 1, to, f"to {to}")
        ids.append(w.observation.id)
        end = to
        snap = store.load_snapshot(tenant, agent, conv, observations=True)
        check_never_mixed(snap)
        assert snap.turn_numbers == tuple(range(end + 1, 11)) and snap.covered_to_turn == end
        for before in (1, 4, 8, 11, 13):  # before_turn windows too
            check_never_mixed(store.load_snapshot(tenant, agent, conv, observations=True, before_turn=before))
        check_never_mixed(store.load_snapshot(tenant, agent, conv, observations=True, history=HistoryConfig(last_turns=1)))
    r = store.reflect(tenant, agent, conv, supersede_ids=ids[:2], content="r")  # 1..3
    snap = store.load_snapshot(tenant, agent, conv, observations=True)
    check_never_mixed(snap)
    assert [(o.kind, o.covers_to_turn) for o in snap.observations] == [("reflection", 3), ("observation", 7),
                                                                         ("observation", 10)]
    assert r.status == "created" and snap.covered_to_turn == 10 and snap.turns == ()
    # observations switched off: the covered turns are the history again, and no observation comes back
    off = store.load_snapshot(tenant, agent, conv, observations=False)
    assert off.observations == () and off.covered_to_turn == 0 and off.turn_numbers == tuple(range(1, 11))
    # an observation that runs past the last completed turn is rejected (review decision 9;
    # turn 11 failed, 12 queued) and the snapshot is unchanged
    assert obs(store, tenant, agent, conv, 11, 12, "ahead").status == "ahead"
    ahead = store.load_snapshot(tenant, agent, conv, observations=True)
    assert ahead.turns == () and ahead.covered_to_turn == 10 and ahead.trimmed_turns == 0
    # the snapshot is another agent's: nothing
    with pytest.raises(ConversationNotFound):
        store.load_snapshot(tenant, str(uuid.uuid4()), conv, observations=True)


def test_snapshot_only_counts_completed_turns_and_reads_without_writing(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=2)
    for n, status in ((3, "failed"), (4, "cancelled"), (5, "running"), (6, "queued")):
        add_turn(engine, tenant, agent, conv, n, status=status)
    before = dump(engine, tenant)
    snap = store.load_snapshot(tenant, agent, conv, history=HistoryConfig(last_turns=200))
    assert snap.turn_numbers == (1, 2) and snap.trimmed_turns == 0
    assert all(t["approx_tokens"] > 0 and t["run_id"] and t["messages"] for t in snap.turns)
    assert dump(engine, tenant) == before
    empty = store.load_snapshot(tenant, agent, conversation(runs, tenant, agent, resource=None))
    assert (empty.turns, empty.observations, empty.working_memory, empty.resource_id, empty.trimmed_turns) == (
        (), (), None, None, 0)


def test_history_limits_through_the_snapshot_and_trimmed_turns(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent, turns=0)
    sizes = [400, 40, 40, 200, 40, 40, 120, 40]  # chars of message text per turn 1..8
    tokens = {n: add_sized(engine, tenant, agent, conv, n, c) for n, c in enumerate(sizes, 1)}
    add_sized(engine, tenant, agent, conv, 9, 40, status="failed")  # never counted, never trimmed

    def load(**limits):
        return store.load_snapshot(tenant, agent, conv, history=HistoryConfig(**limits))

    s = load(last_turns=3)
    assert s.turn_numbers == (6, 7, 8) and s.trimmed_turns == 5
    assert [t["approx_tokens"] for t in s.turns] == [tokens[6], tokens[7], tokens[8]]
    assert load(last_turns=200).turn_numbers == tuple(range(1, 9)) and load(last_turns=200).trimmed_turns == 0
    assert load(last_turns=8).trimmed_turns == 0 and load(last_turns=1).turn_numbers == (8,)

    # max_tokens: whole turns, newest first until the budget is spent, oldest dropped
    budget = tokens[8] + tokens[7] + tokens[6]
    s = load(last_turns=200, max_tokens=budget)
    assert s.turn_numbers == (6, 7, 8) and s.trimmed_turns == 5
    assert load(last_turns=200, max_tokens=budget - 1).turn_numbers == (7, 8)  # never a half turn
    assert load(last_turns=200, max_tokens=tokens[8] - 1).turn_numbers == () and \
        load(last_turns=200, max_tokens=tokens[8] - 1).trimmed_turns == 8
    assert load(last_turns=200, max_tokens=sum(tokens.values())).turn_numbers == tuple(range(1, 9))
    # an oversized turn stops the walk: turn 4 (200 chars) does not fit, so nothing older is kept either
    four_to_eight = sum(tokens[n] for n in range(4, 9))
    assert load(last_turns=200, max_tokens=four_to_eight - 1).turn_numbers == (5, 6, 7, 8)
    assert load(last_turns=200, max_tokens=four_to_eight).turn_numbers == (4, 5, 6, 7, 8)
    # both limits: the stricter wins
    assert load(last_turns=2, max_tokens=10_000).turn_numbers == (7, 8)
    assert load(last_turns=5, max_tokens=tokens[8] + tokens[7]).turn_numbers == (7, 8)
    # no limits: everything and nothing trimmed
    none = store.load_snapshot(tenant, agent, conv)
    assert none.turn_numbers == tuple(range(1, 9)) and none.trimmed_turns == 0

    # covered turns are excluded BEFORE the limits, and are not counted as trimmed
    obs(store, tenant, agent, conv, 1, 4)
    s = store.load_snapshot(tenant, agent, conv, observations=True, history=HistoryConfig(last_turns=3))
    assert s.turn_numbers == (6, 7, 8) and s.trimmed_turns == 1 and s.covered_to_turn == 4
    s = store.load_snapshot(tenant, agent, conv, observations=True, history=HistoryConfig(last_turns=20))
    assert s.turn_numbers == (5, 6, 7, 8) and s.trimmed_turns == 0
    s = store.load_snapshot(tenant, agent, conv, observations=True,
                            history=HistoryConfig(last_turns=20, max_tokens=tokens[8] + tokens[7]))
    assert s.turn_numbers == (7, 8) and s.trimmed_turns == 2
    for limits in ({"last_turns": 0}, {"last_turns": 201}, {"max_tokens": 0}):
        with pytest.raises(ValueError):
            HistoryConfig(**limits)


def test_turn_tokens_use_messages_else_input_and_output(store, runs, engine, tenant, agent):
    conv = conversation(runs, tenant, agent)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
            " conversation_id, turn, created_at, updated_at) VALUES (:i, :t, 'agent', :a, 1, 'completed',"
            " 'a' || repeat('x', 39), repeat('y', 40), NULL, :c, 1, now(), now())"),
            {"i": str(uuid.uuid4()), "t": tenant, "a": agent, "c": conv})
    snap = store.load_snapshot(tenant, agent, conv)
    assert snap.turn_numbers == (1,) and snap.turns[0]["approx_tokens"] == 20  # ceil(80 / 4)


# ---------------------------------------------------------------------------------------- privacy


def plant_all(engine, tenant, agent, store, run_id, call="call-1") -> None:
    """test_memory_store.plant (events, input/output/messages/request_context, a model-written key and
    its ledger row) plus the secret in the run's error text."""
    plant(engine, tenant, agent, store, run_id, call=call)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET error = :s WHERE id = :r"), {"s": f"provider echoed {SECRET}", "r": run_id})


def world(store, runs, engine, tenant, agent):
    """Resource u1 with two conversations (c1, c2), resource u9 with c9; every run carries SECRET."""
    out = {}
    for name, resource, turns in (("c1", "u1", 2), ("c2", "u1", 1), ("c9", "u9", 1)):
        conv = conversation(runs, tenant, agent, resource=resource, turns=turns)
        run_ids = [run_of(engine, tenant, conv, n) for n in range(1, turns + 1)]
        for n, rid in enumerate(run_ids):
            plant_all(engine, tenant, agent, store, rid, call=f"{name}-{n}")
        out[name] = (conv, run_ids)
    c1 = out["c1"][0]
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=c1, content=f"# {SECRET}")
    obs(store, tenant, agent, c1, 1, 2, f"observed {SECRET}")
    return out


def rows_of(d: dict, run_ids: list[str]) -> dict:
    """The dump restricted to some runs (events, usage, runs, ledger rows keyed wm:{run_id}:...)."""
    keep = set(run_ids)
    return {
        "runs": [r for r in d["runs"] if r[0] in keep],
        "events": [r for r in d["events"] if r[0] in keep],
        "usage": [r for r in d["usage"] if r[0] in keep],
        "ledger": [r for r in d["ledger"] if r[0].split(":")[1] in keep],
    }


def test_delete_conversation_leaves_no_secret_and_touches_nothing_else(store, runs, engine, tenant, agent):
    w = world(store, runs, engine, tenant, agent)
    (c1, c1_runs), (c2, c2_runs), (c9, c9_runs) = w["c1"], w["c2"], w["c9"]
    before = dump(engine, tenant)
    assert secret_hits(engine, tenant, c1_runs)["events"] > 0 and secret_hits(engine, tenant, c1_runs)["runs"] == 2

    assert store.delete_conversation(tenant, c1) is True
    after = dump(engine, tenant)
    hits = secret_hits(engine, tenant, c1_runs)
    assert (hits["events"], hits["ledger"]) == (0, 0)
    assert after["conversations"] == [c for c in before["conversations"] if c[0] != c1]
    assert not [r for r in after["working_memories"] if r[2] == c1] and not after["observations"]
    assert SECRET not in json.dumps(after["observations"]) and f"# {SECRET}" not in json.dumps(
        [r for r in after["working_memories"] if r[1] == "conversation"])
    # cost record and audit trail: usage rows and run totals identical, seq contiguous, run statuses kept
    assert rows_of(after, c1_runs)["usage"] == rows_of(before, c1_runs)["usage"] and rows_of(before, c1_runs)["usage"]
    assert [(r[0], r[1], r[9], r[10], r[11]) for r in after["runs"] if r[0] in c1_runs] == [
        (r[0], r[1], r[9], r[10], r[11]) for r in before["runs"] if r[0] in c1_runs]
    assert seqs_contiguous(engine, c1_runs)
    assert [(e[0], e[1], e[2], e[4]) for e in after["events"] if e[0] in c1_runs] == [
        (e[0], e[1], e[2], e[4]) for e in before["events"] if e[0] in c1_runs]  # type, seq, key stay
    # the other conversation of the same resource, the other resource, the resource note: untouched
    assert rows_of(after, c2_runs + c9_runs) == rows_of(before, c2_runs + c9_runs)
    assert secret_hits(engine, tenant, c2_runs + c9_runs)["events"] == 14
    assert [r for r in after["working_memories"] if r[1] == "resource"] == [
        r for r in before["working_memories"] if r[1] == "resource"]
    assert store.delete_conversation(tenant, c1) is False
    assert dump(engine, tenant) == after  # idempotent


def test_delete_conversation_clears_every_run_column_that_can_hold_text(store, runs, engine, tenant, agent):
    w = world(store, runs, engine, tenant, agent)
    c1, c1_runs = w["c1"]
    store.delete_conversation(tenant, c1)
    with engine.connect() as conn:
        left = conn.execute(text(
            "SELECT id, key FROM runs r, json_each_text(row_to_json(r)) AS j(key, value)"
            " WHERE r.id = ANY(:ids) AND j.value LIKE '%' || :s || '%'"), {"ids": c1_runs, "s": SECRET}).all()
    assert left == [], f"deleted conversation's runs still hold the secret in columns {sorted({r.key for r in left})}"


def test_forget_resource_without_conversations_removes_notes_and_key_names_only(store, runs, engine, tenant, agent):
    w = world(store, runs, engine, tenant, agent)
    (c1, c1_runs), (c2, c2_runs), (c9, c9_runs) = w["c1"], w["c2"], w["c9"]
    u1_runs = c1_runs + c2_runs
    other_agent = str(uuid.uuid4())
    other_conv = conversation(runs, tenant, other_agent, resource="u1", turns=1)
    other_run = run_of(engine, tenant, other_conv, 1)
    plant_all(engine, tenant, other_agent, store, other_run, call="other")
    before = dump(engine, tenant)

    result = store.forget_resource(tenant, "u1", agent_id=agent)
    after = dump(engine, tenant)
    assert (result.working_memories, result.conversations, result.busy_conversations) == (1, (), ())
    assert store.get_working_memory(tenant, agent, "resource", "u1") is None
    # this agent's writer runs: no model-written key name (events, ledger) is left; turn text stays
    for rid in u1_runs:
        for e in after["events"]:
            if e[0] == rid and e[2] == "memory_updated":
                assert SECRET not in e[3] and '"redacted": true' in e[3]
    assert [r for r in after["ledger"] if r[0].split(":")[1] in u1_runs] == []
    # the turn text is the audit record here: only the 3 memory_updated events lost their key names
    assert secret_hits(engine, tenant, u1_runs)["events"] == 3 * 6
    assert all(c in after["conversations"] for c in before["conversations"])  # conversations stay
    assert [r for r in after["runs"] if r[0] in u1_runs] == [r for r in before["runs"] if r[0] in u1_runs]
    # not touched: the other agent's note and its writer run, the other resource, observations, usage
    assert after["working_memories"] == [r for r in before["working_memories"]
                                         if not (r[0] == agent and r[1] == "resource" and r[2] == "u1")]
    assert rows_of(after, c9_runs + [other_run]) == rows_of(before, c9_runs + [other_run])
    assert after["observations"] == before["observations"] and after["usage"] == before["usage"]
    assert seqs_contiguous(engine, u1_runs)
    assert store.forget_resource(tenant, "u1", agent_id=agent).working_memories == 0  # idempotent
    assert dump(engine, tenant) == after


def test_forget_resource_with_conversations_leaves_no_secret_in_that_resources_runs(store, runs, engine, tenant, agent):
    w = world(store, runs, engine, tenant, agent)
    (c1, c1_runs), (c2, c2_runs), (c9, c9_runs) = w["c1"], w["c2"], w["c9"]
    u1_runs = c1_runs + c2_runs
    other_agent = str(uuid.uuid4())
    oc = conversation(runs, tenant, other_agent, resource="u1", turns=1)
    other_run = run_of(engine, tenant, oc, 1)
    plant_all(engine, tenant, other_agent, store, other_run, call="other")
    busy = conversation(runs, tenant, agent, resource="u1")
    busy_run = add_turn(engine, tenant, agent, busy, 1, status="running")
    before = dump(engine, tenant)

    # Review decision 4: while a turn of the resource runs, forget refuses and changes nothing.
    with pytest.raises(ResourceBusy) as exc:
        store.forget_resource(tenant, "u1", agent_id=agent, include_conversations=True)
    assert exc.value.busy_conversations == (busy,) and exc.value.run_id == busy_run
    assert dump(engine, tenant) == before
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE id = :r"), {"r": busy_run})
    before = dump(engine, tenant)

    result = store.forget_resource(tenant, "u1", agent_id=agent, include_conversations=True)
    after = dump(engine, tenant)
    assert set(result.conversations) == {c1, c2, busy} and result.busy_conversations == ()
    hits = secret_hits(engine, tenant, u1_runs)
    assert (hits["events"], hits["ledger"]) == (0, 0)
    assert not [r for r in after["working_memories"] if r[0] == agent and r[1] == "conversation"]
    assert not [o for o in after["observations"] if o[2] in (c1, c2)]
    assert rows_of(after, u1_runs)["usage"] == rows_of(before, u1_runs)["usage"]
    assert seqs_contiguous(engine, u1_runs)
    # other agent's conversation (same resource id), other resource: untouched
    assert rows_of(after, c9_runs + [other_run]) == rows_of(before, c9_runs + [other_run])
    assert not any(c[0] == busy for c in after["conversations"]) and any(c[0] == oc for c in after["conversations"])
    assert store.get_working_memory(tenant, other_agent, "resource", "u1") is not None

    # then everything the tenant holds for the resource, across agents
    again = store.forget_resource(tenant, "u1", include_conversations=True)
    assert set(again.conversations) == {oc} and again.busy_conversations == ()
    assert secret_hits(engine, tenant, [other_run])["events"] == 0
    assert store.get_working_memory(tenant, other_agent, "resource", "u1") is None
    assert rows_of(dump(engine, tenant), c9_runs) == rows_of(before, c9_runs)
    assert dump(engine, tenant)["usage"] == before["usage"]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_conversation_during_a_turn_raises_and_changes_nothing(store, runs, engine, tenant, agent, status):
    w = world(store, runs, engine, tenant, agent)
    c1, c1_runs = w["c1"]
    live = add_turn(engine, tenant, agent, c1, 3, status=status)
    before = dump(engine, tenant)
    with pytest.raises(TurnInProgress) as exc:
        store.delete_conversation(tenant, c1)
    assert (exc.value.conversation_id, exc.value.run_id, exc.value.status) == (c1, live, status)
    assert dump(engine, tenant) == before  # not a single row scrubbed or deleted
    assert secret_hits(engine, tenant, c1_runs)["events"] > 0
    # once the turn is over, the delete goes through
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": live})
    assert store.delete_conversation(tenant, c1) is True
    assert secret_hits(engine, tenant, c1_runs)["events"] == 0


# ---------------------------------------------------------------------------------------- pickling


def test_every_new_error_round_trips_through_pickle_with_its_fields():
    cases = [
        WorkingMemoryVersionConflict("resource", "u1", 3, 7),
        WorkingMemoryVersionConflict("conversation", "c-1", 0, 1),
        MemoryLedgerConflict("wm:run:call", "reserved"),
        ConversationResourceConflict(uuid.uuid4()),
        ConversationResourceConflict("c-1"),
        WorkingMemoryInvalid("must be a string", "(root).a.b"),
        WorkingMemoryInvalid("the patch must be a JSON object"),
        TurnInProgress("c-1", "r-1", "running"),
        ConversationNotFound("t", "c-1"),
    ]
    for exc in cases:
        for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
            back = pickle.loads(pickle.dumps(exc, protocol))
            assert type(back) is type(exc) and str(back) == str(exc) and back.args == exc.args
            assert vars(back) == vars(exc)
        for clone in (copy.copy(exc), copy.deepcopy(exc)):
            assert type(clone) is type(exc) and str(clone) == str(exc)
    assert isinstance(pickle.loads(pickle.dumps(ConversationResourceConflict("c"))), ConversationIdConflict)
    back = pickle.loads(pickle.dumps(WorkingMemoryVersionConflict("resource", "u1", 3, 7)))
    assert (back.scope, back.scope_key, back.expected, back.actual) == ("resource", "u1", 3, 7)
    back = pickle.loads(pickle.dumps(MemoryLedgerConflict("k", "failed")))
    assert (back.key, back.state, back.code) == ("k", "failed", "memory_ledger_conflict")
    back = pickle.loads(pickle.dumps(WorkingMemoryInvalid("r", "p")))
    assert (back.reason, back.path, back.code) == ("r", "p", "working_memory_invalid")


def test_errors_raised_by_the_store_pickle_too(store, runs, engine, tenant, agent):
    merge(store, tenant, agent, {"name": "Ana"})
    raised = []
    for call in (
        lambda: merge(store, tenant, agent, {"age": "x"}),
        lambda: merge(store, tenant, agent, {"name": "B"}, expected_version=9),
        lambda: store.load_snapshot(tenant + "_b", agent, "missing"),
        lambda: runs.create_conversation(tenant, agent, conversation_id=conversation(runs, tenant, agent),
                                         resource_id="other"),
    ):
        with pytest.raises(Exception) as exc:
            call()
        raised.append(exc.value)
    assert [type(e) for e in raised] == [WorkingMemoryInvalid, WorkingMemoryVersionConflict, ConversationNotFound,
                                         ConversationResourceConflict]
    for e in raised:
        back = pickle.loads(pickle.dumps(e))
        assert type(back) is type(e) and str(back) == str(e)
