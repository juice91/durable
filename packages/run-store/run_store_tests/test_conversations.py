"""Conversations: tenant-owned threads whose turns are runs (runs.conversation_id + runs.turn)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_run_store import (
    ConversationIdConflict,
    ConversationNotFound,
    RunIdConflict,
    RunStore,
    TurnInProgress,
)


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM execution_events WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = :t)"),
            {"t": tenant_id},
        )
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def other_tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def store(engine) -> RunStore:
    return RunStore(engine)


def test_create_get_list_conversation_is_tenant_scoped(store, tenant, other_tenant):
    agent_id = uuid4()
    conv, created = store.create_conversation(tenant, agent_id, title="Printer trouble")
    assert created
    assert conv["agent_id"] == str(agent_id) and conv["title"] == "Printer trouble" and conv["turn_count"] == 0
    assert store.get_conversation(tenant, conv["id"])["id"] == conv["id"]
    assert store.get_conversation(other_tenant, conv["id"]) is None
    assert [c["id"] for c in store.list_conversations(tenant)] == [conv["id"]]
    assert store.list_conversations(other_tenant) == []


def test_client_conversation_id_is_idempotent_and_never_crosses_tenants(store, tenant, other_tenant):
    conv_id = uuid4()
    agent_id = uuid4()
    first, created = store.create_conversation(tenant, agent_id, conversation_id=conv_id)
    again, created_again = store.create_conversation(tenant, agent_id, conversation_id=conv_id)
    assert created and not created_again and first["id"] == again["id"] == str(conv_id)
    with pytest.raises(ConversationIdConflict):
        store.create_conversation(other_tenant, uuid4(), conversation_id=conv_id)


def test_turns_are_numbered_and_the_next_waits_for_a_terminal_previous_turn(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    r1, created = store.create_turn(tenant, conv["id"], 1, "first", uuid4())
    assert created
    run1 = store.get_run(tenant, r1)
    assert run1["conversation_id"] == conv["id"] and run1["turn"] == 1 and run1["status"] == "queued"

    with pytest.raises(TurnInProgress) as busy:
        store.create_turn(tenant, conv["id"], 1, "second", uuid4())
    assert busy.value.run_id == str(r1)

    store.set_status(tenant, r1, "running")
    with pytest.raises(TurnInProgress):
        store.create_turn(tenant, conv["id"], 1, "second", uuid4())

    store.set_status(tenant, r1, "failed", error="boom")  # failed is terminal: the thread can go on
    r2, _ = store.create_turn(tenant, conv["id"], 1, "second", uuid4())
    assert store.get_run(tenant, r2)["turn"] == 2
    assert [r["turn"] for r in store.list_turns(tenant, conv["id"])] == [1, 2]
    assert store.get_conversation(tenant, conv["id"])["turn_count"] == 2


def test_retrying_a_turn_with_its_run_id_is_idempotent_even_while_it_runs(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    run_id = uuid4()
    assert store.create_turn(tenant, conv["id"], 1, "hello", run_id) == (run_id, True)
    store.set_status(tenant, run_id, "running")
    assert store.create_turn(tenant, conv["id"], 1, "hello", run_id) == (run_id, False)
    assert len(store.list_turns(tenant, conv["id"])) == 1


def test_turns_never_cross_tenants(store, tenant, other_tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    with pytest.raises(ConversationNotFound):
        store.create_turn(other_tenant, conv["id"], 1, "hi", uuid4())
    assert store.list_turns(other_tenant, conv["id"]) == []
    taken = uuid4()
    store.create_turn(tenant, conv["id"], 1, "hi", taken)
    other_conv, _ = store.create_conversation(other_tenant, uuid4())
    with pytest.raises(RunIdConflict):
        store.create_turn(other_tenant, other_conv["id"], 1, "hi", taken)


def test_a_run_cannot_point_at_another_tenants_conversation(engine, store, tenant, other_tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runs (id, tenant_id, agent_id, agent_version, status, created_at, updated_at, "
                    "conversation_id, turn) VALUES (:id, :t, :a, 1, 'queued', now(), now(), :c, 1)"
                ),
                {"id": str(uuid4()), "t": other_tenant, "a": str(uuid4()), "c": conv["id"]},
            )


def test_completed_history_skips_unfinished_and_failed_turns_and_messages_are_written_once(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    r1, _ = store.create_turn(tenant, conv["id"], 1, "one", uuid4())
    store.set_status(tenant, r1, "completed", output="ONE", messages=[{"kind": "request", "n": 1}])
    # A re-executed finish step must not replace the stored messages.
    store.set_status(tenant, r1, "completed", output="ONE", messages=[{"kind": "request", "n": 999}])
    r2, _ = store.create_turn(tenant, conv["id"], 1, "two", uuid4())
    store.set_status(tenant, r2, "failed", error="boom")
    r3, _ = store.create_turn(tenant, conv["id"], 1, "three", uuid4())

    history = store.completed_turns_before(tenant, conv["id"], 3)
    assert [(h["turn"], h["input"], h["output"]) for h in history] == [(1, "one", "ONE")]
    assert history[0]["messages"] == [{"kind": "request", "n": 1}]
    assert store.completed_turns_before(tenant, conv["id"], 1) == []
    assert store.get_run(tenant, r3)["turn"] == 3


# ---------------------------------------------------------------- P0.5: list paging, auto-title, rename

from eap_run_store import ConversationCursor, auto_title  # noqa: E402


def test_auto_title_is_one_line_and_at_most_60_characters():
    assert auto_title("  Hello\n\tthere  ") == "Hello there"
    assert auto_title("x" * 100) == "x" * 60
    assert auto_title("a\x00b c") == "a b c"
    assert auto_title(" \n\t ") is None
    assert auto_title(("word " * 20)) == ("word " * 12).rstrip()


def test_first_turn_titles_an_untitled_conversation_once(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    run1, _ = store.create_turn(tenant, conv["id"], 1, "My printer\nis on fire " + "!" * 80, uuid4())
    assert store.get_conversation(tenant, conv["id"])["title"] == ("My printer is on fire " + "!" * 80)[:60]
    store.set_status(tenant, run1, "completed", output="ok")
    store.create_turn(tenant, conv["id"], 1, "second question", uuid4())
    assert store.get_conversation(tenant, conv["id"])["title"].startswith("My printer is on fire")


def test_first_turn_never_overwrites_a_given_or_renamed_title(store, tenant):
    given, _ = store.create_conversation(tenant, uuid4(), title="Mine")
    store.create_turn(tenant, given["id"], 1, "hello", uuid4())
    assert store.get_conversation(tenant, given["id"])["title"] == "Mine"

    renamed, _ = store.create_conversation(tenant, uuid4())
    store.rename_conversation(tenant, renamed["id"], "Renamed")
    store.create_turn(tenant, renamed["id"], 1, "hello", uuid4())
    assert store.get_conversation(tenant, renamed["id"])["title"] == "Renamed"


def test_rename_is_tenant_scoped_and_does_not_reorder(store, tenant, other_tenant):
    agent = uuid4()
    first, _ = store.create_conversation(tenant, agent)
    second, _ = store.create_conversation(tenant, agent)
    with pytest.raises(ConversationNotFound):
        store.rename_conversation(other_tenant, first["id"], "stolen")
    with pytest.raises(ConversationNotFound):
        store.rename_conversation(tenant, uuid4(), "nobody")
    renamed = store.rename_conversation(tenant, first["id"], "New name")
    assert renamed["title"] == "New name" and renamed["updated_at"] == first["updated_at"]
    assert [c["id"] for c in store.list_conversations(tenant, agent)] == [second["id"], first["id"]]


def test_list_carries_turn_count_and_latest_turn_status(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    row = store.list_conversations(tenant)[0]
    assert (row["turn_count"], row["last_turn_status"], row["last_turn_run_id"]) == (0, None, None)
    run1, _ = store.create_turn(tenant, conv["id"], 1, "one", uuid4())
    store.set_status(tenant, run1, "completed", output="ok")
    run2, _ = store.create_turn(tenant, conv["id"], 1, "two", uuid4())
    row = store.list_conversations(tenant)[0]
    assert row["turn_count"] == 2 and row["last_turn_run_id"] == str(run2)
    assert row["last_turn_status"] == "queued" and row["last_turn_display_status"] == "queued"
    assert store.get_conversation(tenant, conv["id"])["last_turn_run_id"] == str(run2)


def test_keyset_paging_newest_activity_first_and_stable_under_new_turns(store, tenant, other_tenant):
    agent = uuid4()
    ids = [store.create_conversation(tenant, agent)[0]["id"] for _ in range(5)]
    store.create_conversation(other_tenant, agent)
    newest_first = list(reversed(ids))
    assert [c["id"] for c in store.list_conversations(tenant, agent)] == newest_first

    page1 = store.list_conversations(tenant, agent, limit=2)
    assert [c["id"] for c in page1] == newest_first[:2]
    # A new turn on a conversation of a later page moves it to the top while the client pages:
    # it is not repeated, and no conversation that did not move is skipped or repeated.
    store.create_turn(tenant, ids[0], 1, "bump", uuid4())
    cursor = ConversationCursor.decode(ConversationCursor.after(page1[-1]).encode())
    rest = store.list_conversations(tenant, agent, limit=10, before=cursor)
    assert [c["id"] for c in rest] == newest_first[2:4]
    assert store.list_conversations(tenant, agent)[0]["id"] == ids[0]


def test_paging_never_skips_conversations_sharing_updated_at(engine, store, tenant):
    agent = uuid4()
    ids = [store.create_conversation(tenant, agent)[0]["id"] for _ in range(4)]
    with engine.begin() as conn:
        conn.execute(text("UPDATE conversations SET updated_at = now() WHERE tenant_id = :t"), {"t": tenant})
    seen, cursor = [], None
    while True:
        page = store.list_conversations(tenant, agent, limit=3, before=cursor)
        seen += [c["id"] for c in page]
        if len(page) < 3:
            break
        cursor = ConversationCursor.after(page[-1])
    assert seen == sorted(ids, reverse=True)
