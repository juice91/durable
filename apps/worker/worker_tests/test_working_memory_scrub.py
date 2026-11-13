"""Checkpoint review fixes (RM.12): the working memory note is never stored in a turn's data.

runs.messages keep only the base instructions (no working memory block), update_working_memory's
arguments are a fixed placeholder in runs.messages and in the model_message / tool_call events, and
memory_updated names schema properties only. So after a forget or a note DELETE, no copy of the
note is left in the turn data, and history token counting never counts the block."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from eap_run_store import MemoryStore, turn_approx_tokens
from eap_schemas.memory import strip_working_memory_messages
from eap_worker.memory import (
    MemoryContext,
    instructions_with_memory,
    redacted_tool_args,
    scrub_memory_messages,
)
from eap_schemas.memory import WorkingMemoryConfig
from worker_tests.conftest import event_rows, ticket_spec
from worker_tests.test_conversation import step_names
from worker_tests.test_working_memory import (  # noqa: F401 - mem_tenant is a fixture
    MEMORY_TOOL_TURN,
    MEMORY_TURN,
    SCHEMA,
    by_type,
    conversation,
    mem_tenant,
    memory_spec,
    note,
    turn,
)

SECRET = "Zorblaxquint"
BASE = ticket_spec()["instructions"]


def _messages(worker, run_id) -> list:
    with worker.engine.connect() as conn:
        return conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one()


def _all_text(worker, run_ids) -> str:
    """Every stored copy of the turns' data: runs.messages, runs.output, every event's data."""
    out = []
    for run_id in run_ids:
        out.append(json.dumps(_messages(worker, run_id)))
        with worker.engine.connect() as conn:
            out.append(str(conn.execute(text("SELECT output FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one()))
        out.append(json.dumps([d for _, _, _, d in event_rows(worker.engine, run_id)]))
    return "\n".join(out)


def _requests(messages) -> list[dict]:
    return [m for m in messages if m.get("kind") == "request"]


def _calls(messages) -> list[dict]:
    return [p for m in messages if m.get("kind") == "response" for p in m["parts"] if p["part_kind"] == "tool-call"]


# --------------------------------------------------------------------------- unit


def test_scrub_strips_the_block_and_redacts_the_tool_args():
    working = WorkingMemoryConfig(json_schema=SCHEMA)
    ctx = MemoryContext(
        agent_id="a", conversation_id="c", resource_id="u", working=working, working_memory_scope="resource",
        working_memory_key="u", note={"format": "json", "content": {"name": SECRET}, "version": 1}, redact=None,
    )
    full = instructions_with_memory(BASE, ctx)
    assert SECRET in full
    messages = [
        {"kind": "request", "instructions": full, "parts": []},
        {"kind": "response", "parts": [
            {"part_kind": "tool-call", "tool_name": "update_working_memory", "tool_call_id": "t1",
             "args": {"patch": {"name": SECRET, "invented": "x"}}},
            {"part_kind": "tool-call", "tool_name": "tickets.create", "tool_call_id": "t2", "args": {"title": "kept"}},
        ]},
    ]
    out = scrub_memory_messages(messages, ctx)
    assert out[0]["instructions"] == BASE
    assert out[1]["parts"][0]["args"] == {"redacted": True, "fields": ["name"], "other_fields": 1}
    assert out[1]["parts"][1]["args"] == {"title": "kept"}
    assert SECRET not in json.dumps(out) and "invented" not in json.dumps(out)
    assert messages[0]["instructions"] == full  # the input is not mutated
    # Not a memory turn: unchanged (the same object).
    assert scrub_memory_messages(messages, None) is messages
    # Template mode, args given as a JSON string.
    assert redacted_tool_args(json.dumps({"content": SECRET}), WorkingMemoryConfig()) == {
        "redacted": True, "fields": ["content"],
    }


def test_legacy_stored_block_is_not_counted_in_history_tokens():
    ctx = MemoryContext(
        agent_id="a", conversation_id="c", resource_id="u", working=WorkingMemoryConfig(),
        working_memory_scope="resource", working_memory_key="u",
        note={"format": "markdown", "content": SECRET * 2000, "version": 3}, redact=None,
    )
    legacy = [{"kind": "request", "instructions": instructions_with_memory(BASE, ctx), "parts": []}]
    clean = [{"kind": "request", "instructions": BASE, "parts": []}]
    assert strip_working_memory_messages(legacy) == clean
    assert turn_approx_tokens({"messages": legacy}) == turn_approx_tokens({"messages": clean})
    # Only-block instructions become None; base text that merely mentions the heading stays.
    only = [{"kind": "request", "instructions": instructions_with_memory("", ctx), "parts": []}]
    assert strip_working_memory_messages(only)[0]["instructions"] is None
    mention = [{"kind": "request", "instructions": "See ## Working memory below.", "parts": []}]
    assert strip_working_memory_messages(mention) == mention


# --------------------------------------------------------------------------- real turns
#
# The note is seeded through the store (as Studio's PUT does), so it never appears in a user prompt.
# The scripted `memory` model's answer echoes the note as of the turn's start by design: that is
# model output (conversation data), so `_platform_copies` leaves model text out and checks every
# copy the platform itself makes: request instructions, tool-call arguments, event data.


def _seed(worker, tenant, agent, scope, key, content, schema=None):
    MemoryStore(worker.engine).update_working_memory(
        tenant, str(agent), scope=scope, scope_key=key, content=content, json_schema=schema,
    )


def _without_model_text(messages):
    out = []
    for m in messages or []:
        if m.get("kind") == "response":
            m = {**m, "parts": [p for p in m["parts"] if p["part_kind"] != "text"]}
        out.append(m)
    return out


def _platform_copies(worker, run_ids) -> str:
    out = []
    for run_id in run_ids:
        out.append(json.dumps(_without_model_text(_messages(worker, run_id))))
        for _, _, type_, data in event_rows(worker.engine, run_id):
            data = dict(data)
            if type_ == "model_message":
                data.pop("text", None)
                if isinstance(data.get("message"), dict):
                    data["message"] = _without_model_text([data["message"]])[0]
            if type_ == "run_completed":
                data.pop("output", None)
            if type_ == "tool_result":
                data.pop("content", None)  # "Working memory updated (version N)."
            out.append(json.dumps(data))
    return "\n".join(out)


def test_note_is_absent_from_turn_data_after_forget(worker, client, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    _seed(worker, mem_tenant, agent, "resource", "user-1", {"name": SECRET}, SCHEMA)
    # A turn that renders the note into its instructions (an echo model: its answer holds no note).
    read_id, row = turn(worker, client, mem_tenant, conv, "hello", memory_spec(working={"json_schema": SCHEMA}, model="echo"))
    assert row["status"] == "completed", row
    assert step_names(worker.settings.dbos_system_schema, read_id) == MEMORY_TURN
    assert SECRET not in _all_text(worker, (read_id,))  # not even model text here
    # A turn that writes (tool call), with the note in its instructions.
    write_id, row = turn(worker, client, mem_tenant, conv, "remember age=36", memory_spec(working={"json_schema": SCHEMA}))
    assert row["status"] == "completed", row
    assert step_names(worker.settings.dbos_system_schema, write_id) == MEMORY_TOOL_TURN  # layout unchanged
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {"name": SECRET, "age": 36}

    for run_id in (read_id, write_id):
        messages = _messages(worker, run_id)
        assert [r["instructions"] for r in _requests(messages)] == [BASE] * len(_requests(messages))
    [call] = _calls(_messages(worker, write_id))
    assert call["args"] == {"redacted": True, "fields": ["age"]}
    events = event_rows(worker.engine, write_id)
    [call_event] = by_type(events, "tool_call")
    assert call_event["args"] == {"redacted": True, "fields": ["age"]}
    [first] = [d for _, k, _, d in events if k == "model:0:final"]
    assert [p["args"] for p in first["message"]["parts"] if p["part_kind"] == "tool-call"] == [call_event["args"]]
    assert SECRET not in _platform_copies(worker, (read_id, write_id))

    MemoryStore(worker.engine).forget_resource(mem_tenant, "user-1", agent_id=str(agent))
    assert note(worker, mem_tenant, agent, "resource", "user-1") is None
    assert SECRET not in _all_text(worker, (read_id,))
    assert SECRET not in _platform_copies(worker, (read_id, write_id))

    # History token counting: the stored turns hold no block, so only the base is counted.
    snapshot = MemoryStore(worker.engine).load_snapshot(
        mem_tenant, str(agent), conv["id"], before_turn=99, working=None, observations=False, history=None,
    )
    assert [t["run_id"] for t in snapshot.turns] == [str(read_id), str(write_id)]
    for t in snapshot.turns:
        assert all(r["instructions"] == BASE for r in _requests(t["messages"]))
        assert SECRET not in json.dumps(_without_model_text(t["messages"]))
        assert t["approx_tokens"] == turn_approx_tokens({"messages": t["messages"]})


def test_template_note_is_absent_from_turn_data_after_delete(worker, client, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    _seed(worker, mem_tenant, agent, "conversation", conv["id"], f"# User\n- Name: {SECRET}")
    spec = memory_spec(working={"scope": "conversation"})
    write_id, row = turn(worker, client, mem_tenant, conv, "remember city=Lisbon", spec)
    assert row["status"] == "completed", row
    stored = note(worker, mem_tenant, agent, "conversation", conv["id"]).content
    assert SECRET in stored and "city: Lisbon" in stored  # the tool's `content` held the whole note
    read_id, row = turn(worker, client, mem_tenant, conv, "hello", memory_spec(working={"scope": "conversation"}, model="echo"))
    assert row["status"] == "completed", row
    [call] = by_type(event_rows(worker.engine, write_id), "tool_call")
    assert call["args"] == {"redacted": True, "fields": ["content"]}
    assert SECRET not in _platform_copies(worker, (write_id, read_id))
    assert MemoryStore(worker.engine).delete_working_memory(mem_tenant, str(agent), "conversation", conv["id"])
    assert SECRET not in _platform_copies(worker, (write_id, read_id))
    assert SECRET not in _all_text(worker, (read_id,))


def test_memory_updated_names_schema_properties_only(worker, client, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    patch = {"patch": {"name": "Ana", "secretinventedkey": "v"}}  # additionalProperties defaults to true
    run_id, row = turn(worker, client, mem_tenant, conv, json.dumps(patch), memory_spec(working={"json_schema": SCHEMA}))
    assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    [updated] = by_type(events, "memory_updated")
    assert updated["changed_keys"] == ["name"] and updated["other_changed_keys"] == 1
    [call] = by_type(events, "tool_call")
    assert call["args"] == {"redacted": True, "fields": ["name"], "other_fields": 1}
    # (The key is in the user's own prompt, so check the platform's copies one by one.)
    [first] = [d for _, k, _, d in events if k == "model:0:final"]
    copies = [updated, call, first["message"], _calls(_messages(worker, run_id))]
    assert "secretinventedkey" not in json.dumps(copies)
    # The stored note itself still holds the key (it is the note), only the turn data does not.
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {"name": "Ana", "secretinventedkey": "v"}
