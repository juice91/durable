"""RM.12 acceptance: working memory and history limits through the real API and the worker.

Complements test_working_memory.py (not duplicated here): the end-to-end acceptance criterion through
the API (resource and conversation scope), merge/replace semantics, schema violations that echo no
value, redaction, crash and replay, concurrency, history trimming, read_only, the API PUT versus the
model write, step layout and privacy (events, run errors, spans, logs).
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from eap_run_store import MemoryStore
from eap_worker.memory import WM_UPDATED
from worker_tests.conftest import APP_DB_URL, APP_VERSION, DBOS_DB_URL, event_rows, ticket_spec, wait_terminal
from worker_tests.test_conversation import start_turn, step_names
from worker_tests.test_crash_recovery import crash_schema  # noqa: F401 - fixture
from worker_tests.test_processors_runtime import BASELINE
from worker_tests.test_telemetry import _wait_for_run_span, telemetry  # noqa: F401 - fixture
from worker_tests.test_working_memory import (
    MEMORY_TOOL_TURN,
    MEMORY_TURN,
    SCHEMA,
    _crash_run,
    by_type,
    conversation,
    memory_spec,
    note,
    turn,
)

SECRET = "Zq9-secret-value"
SECRET_KEY = "zq9_secret_key"


def auth(tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer dev:{tenant}:alice"}


@pytest.fixture
def mem_tenant(worker, tenant):
    yield tenant
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM working_memories WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM agent_versions WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM agents WHERE tenant_id = :t"), {"t": tenant})


@pytest.fixture
def api(worker, worker_settings, monkeypatch) -> Iterator:
    """The real API app whose launcher enqueues on the worker's DBOS schema."""
    from dbos import DBOSClient
    from fastapi.testclient import TestClient

    from eap_api.app import create_app
    from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    dbos = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=worker_settings.dbos_system_schema)
    app = create_app(database_url=APP_DB_URL)
    launcher = DBOSRunLauncher(LauncherSettings(DBOS_DB_URL, worker_settings.dbos_system_schema, APP_VERSION), client=dbos)
    app.dependency_overrides[get_run_launcher] = lambda: launcher
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        dbos.destroy()
        app.state.database_engine.dispose()


# ----------------------------------------------------------------------------- API helpers


def make_agent(api, tenant, spec) -> str:
    r = api.post("/api/v1/agents", json={"spec": spec}, headers=auth(tenant))
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert api.post(f"/api/v1/agents/{agent_id}/versions", headers=auth(tenant)).status_code == 201
    return agent_id


def api_conv(api, tenant, agent_id, resource_id: str | None = None) -> str:
    body = {} if resource_id is None else {"resource_id": resource_id}
    r = api.post(f"/api/v1/agents/{agent_id}/conversations", json=body, headers=auth(tenant))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def api_turn(api, worker, tenant, conv_id, input_text) -> dict:
    run_id = str(uuid.uuid4())
    r = api.post(f"/api/v1/conversations/{conv_id}/runs", json={"run_id": run_id, "input": input_text},
                 headers=auth(tenant))
    assert r.status_code == 201, r.text
    return wait_terminal(worker.run_store, tenant, uuid.UUID(run_id), timeout=90)


def put_note(api, tenant, url, content, if_match=None):
    headers = {**auth(tenant), **({} if if_match is None else {"If-Match": str(if_match)})}
    return api.put(url, json={"content": content}, headers=headers)


# ----------------------------------------------------------------------------- acceptance criterion


def test_acceptance_remember_name_through_the_api_resource_scope(api, worker, mem_tenant):
    spec = memory_spec(working={"json_schema": SCHEMA})
    agent = make_agent(api, mem_tenant, spec)
    c1 = api_conv(api, mem_tenant, agent, "ana")
    row = api_turn(api, worker, mem_tenant, c1, "remember name=Ada")
    assert row["status"] == "completed", row["error"]
    assert row["output"].startswith(WM_UPDATED.format(version=1))
    # Turn 2 of the same conversation sees it in its instructions (the echo of the rendered block).
    row = api_turn(api, worker, mem_tenant, c1, "what do you know?")
    assert row["status"] == "completed" and '"name": "Ada"' in row["output"]
    # A new conversation with the same resource sees it (resource scope).
    c2 = api_conv(api, mem_tenant, agent, "ana")
    assert '"name": "Ada"' in api_turn(api, worker, mem_tenant, c2, "hello")["output"]
    # Another resource does not.
    c3 = api_conv(api, mem_tenant, agent, "bob")
    out = api_turn(api, worker, mem_tenant, c3, "hello")["output"]
    assert "Ada" not in out and "{}" in out
    # The API sees the same note.
    r = api.get(f"/api/v1/agents/{agent}/memory/resources/ana/working", headers=auth(mem_tenant))
    assert r.status_code == 200 and r.json()["content"] == {"name": "Ada"} and r.json()["version"] == 1
    assert api.get(f"/api/v1/agents/{agent}/memory/resources/bob/working", headers=auth(mem_tenant)).status_code == 404


def test_acceptance_conversation_scope_stays_per_conversation(api, worker, mem_tenant):
    agent = make_agent(api, mem_tenant, memory_spec(working={"scope": "conversation"}))
    c1 = api_conv(api, mem_tenant, agent, "ana")
    c2 = api_conv(api, mem_tenant, agent, "ana")
    assert api_turn(api, worker, mem_tenant, c1, "remember name=Ada")["status"] == "completed"
    assert "Ada" in api_turn(api, worker, mem_tenant, c1, "hi")["output"]
    assert "Ada" not in api_turn(api, worker, mem_tenant, c2, "hi")["output"]
    r = api.get(f"/api/v1/conversations/{c1}/memory/working", headers=auth(mem_tenant))
    assert r.status_code == 200 and "Ada" in r.json()["content"]
    assert api.get(f"/api/v1/conversations/{c2}/memory/working", headers=auth(mem_tenant)).status_code == 404


def test_default_resource_is_the_callers_user_id(api, worker, mem_tenant):
    agent = make_agent(api, mem_tenant, memory_spec(working={"json_schema": SCHEMA}))
    c1 = api_conv(api, mem_tenant, agent)  # no resource_id: the principal's user id ("alice")
    assert api_turn(api, worker, mem_tenant, c1, "remember name=Ada")["status"] == "completed"
    r = api.get(f"/api/v1/agents/{agent}/memory/resources/alice/working", headers=auth(mem_tenant))
    assert r.status_code == 200 and r.json()["content"] == {"name": "Ada"}


def test_api_put_and_model_write_share_one_version_sequence(api, worker, mem_tenant):
    agent = make_agent(api, mem_tenant, memory_spec(working={"json_schema": SCHEMA}))
    url = f"/api/v1/agents/{agent}/memory/resources/ana/working"
    conv = api_conv(api, mem_tenant, agent, "ana")
    assert api_turn(api, worker, mem_tenant, conv, "remember name=Ada")["status"] == "completed"  # v1
    r = put_note(api, mem_tenant, url, {"name": "Bea", "age": 7}, if_match=1)  # v2
    assert r.status_code == 200 and r.json()["version"] == 2 and r.headers["etag"].strip('"W/') == "2"
    # A stale If-Match loses; nothing changes.
    assert put_note(api, mem_tenant, url, {"name": "X"}, if_match=1).status_code == 409
    # The model's merge builds on the PUT (replace, then merge) and continues the sequence.
    assert api_turn(api, worker, mem_tenant, conv, "remember age=8")["output"].startswith(WM_UPDATED.format(version=3))
    got = api.get(url, headers=auth(mem_tenant)).json()
    assert got["content"] == {"name": "Bea", "age": 8} and got["version"] == 3
    assert put_note(api, mem_tenant, url, {"name": "Cy"}, if_match=3).json()["version"] == 4
    # The next turn sees the edit in its instructions as of its start.
    assert '"name": "Cy"' in api_turn(api, worker, mem_tenant, conv, "hello")["output"]


# ----------------------------------------------------------------------------- merge vs replace

RICH = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "prefs": {"type": "object", "properties": {"color": {"type": "string"}, "tone": {"enum": ["formal", "casual"]}}},
    },
}


def raw(patch=None, content=None) -> str:
    return json.dumps({"patch": patch} if patch is not None else {"content": content})


def test_merge_null_deletes_nested_merges_arrays_replace(worker, client, mem_tenant):
    agent = uuid.uuid4()
    spec = memory_spec(working={"json_schema": RICH})
    conv = conversation(worker, mem_tenant, agent)

    def stored():
        return note(worker, mem_tenant, agent, "resource", "user-1")

    _, row = turn(worker, client, mem_tenant, conv, raw({"name": "Ada", "tags": ["a", "b"], "prefs": {"color": "red"}}), spec)
    assert row["status"] == "completed", row
    _, row = turn(worker, client, mem_tenant, conv, raw({"prefs": {"tone": "casual"}}), spec)
    assert stored().content == {"name": "Ada", "tags": ["a", "b"], "prefs": {"color": "red", "tone": "casual"}}
    _, row = turn(worker, client, mem_tenant, conv, raw({"tags": ["z"]}), spec)
    assert stored().content["tags"] == ["z"]
    _, row = turn(worker, client, mem_tenant, conv, raw({"prefs": {"color": None}}), spec)
    assert stored().content == {"name": "Ada", "tags": ["z"], "prefs": {"tone": "casual"}}
    run_id, row = turn(worker, client, mem_tenant, conv, raw({"name": None}), spec)
    assert stored().content == {"tags": ["z"], "prefs": {"tone": "casual"}} and stored().version == 5
    [updated] = by_type(event_rows(worker.engine, run_id), "memory_updated")
    assert updated["changed_keys"] == ["name"] and updated["version"] == 5
    assert "Ada" not in json.dumps(updated)


def test_template_mode_content_replaces_everything(worker, client, mem_tenant):
    agent = uuid.uuid4()
    spec = memory_spec(working={"template": "# Notes\n- Topic:"})
    conv = conversation(worker, mem_tenant, agent)
    _, row = turn(worker, client, mem_tenant, conv, raw(content="# Notes\n- Topic: cats\n- Extra: yes"), spec)
    assert row["status"] == "completed", row
    _, row = turn(worker, client, mem_tenant, conv, raw(content="# Notes\n- Topic: dogs"), spec)
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.content == "# Notes\n- Topic: dogs" and stored.version == 2
    assert "Extra" not in stored.content
    # The next turn's block is the replaced note, not the template.
    _, row = turn(worker, client, mem_tenant, conv, "hello", spec)
    assert "dogs" in row["output"] and "cats" not in row["output"]


def test_wrong_argument_shape_for_the_mode_is_a_tool_error_without_a_write(worker, client, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    # Schema mode offered `patch`; `content` is not accepted (and vice versa).
    _, row = turn(worker, client, mem_tenant, conv, raw(content="x"), memory_spec(working={"json_schema": RICH}))
    assert row["status"] == "completed" and row["output"].startswith("Memory error:")
    _, row = turn(worker, client, mem_tenant, conv, raw({"a": 1}), memory_spec(working={}))
    assert row["status"] == "completed" and row["output"].startswith("Memory error:")
    assert note(worker, mem_tenant, agent, "resource", "user-1") is None


# ----------------------------------------------------------------------------- violations and privacy

VIOLATIONS = [
    {"name": 123, SECRET_KEY: SECRET},  # wrong type next to an unknown key (the schema is strict)
    {"prefs": {"tone": SECRET}},  # enum violation
    {"tags": [SECRET, 5]},  # wrong item type
    {"prefs": SECRET},  # object expected
    {SECRET_KEY: {"x": SECRET}},  # an unknown key only
    {"prefs": {SECRET_KEY: SECRET}},  # an unknown nested key
]
STRICT = {**RICH, "additionalProperties": False,
          "properties": {**RICH["properties"], "prefs": {**RICH["properties"]["prefs"], "additionalProperties": False}}}


def _blob(*parts) -> str:
    return json.dumps(parts, default=str)


@pytest.mark.parametrize("patch", VIOLATIONS)
def test_schema_violation_is_a_tool_error_naming_the_path_only(worker, client, mem_tenant, telemetry, caplog, patch):
    caplog.set_level("DEBUG")
    _, finished = telemetry
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    spec = memory_spec(working={"json_schema": STRICT})
    run_id, row = turn(worker, client, mem_tenant, conv, raw(patch), spec)
    assert row["status"] == "completed", row["error"]
    assert row["output"].startswith("Memory error:")
    assert note(worker, mem_tenant, agent, "resource", "user-1") is None
    events = event_rows(worker.engine, run_id)
    assert by_type(events, "memory_updated") == []
    [result] = by_type(events, "tool_result")
    assert result["status"] == "error"
    for leaked in (SECRET, SECRET_KEY):
        assert leaked not in result["content"], result["content"]
        assert leaked not in row["output"], row["output"]
        assert leaked not in str(row["error"])
        # Everything the memory machinery and the run's outcome recorded (the input and the model's own
        # tool-call arguments are conversation data and legitimately hold what the test planted).
        outcome = [d for _, _, t, d in events if t in ("tool_result", "memory_updated", "run_failed", "end")]
        assert outcome and leaked not in json.dumps(outcome, default=str)
    spans, logs = _wait_for_run_span(finished, run_id)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for leaked in (SECRET, SECRET_KEY):
        assert leaked not in logged
        assert leaked not in _blob([dict(s.attributes) for s in spans], [str(s.events) for s in spans])
        assert leaked not in _blob([(r.body, dict(r.attributes or {})) for r in logs])


def test_successful_write_leaves_no_content_in_spans_logs_or_event_data(worker, client, mem_tenant, telemetry, caplog):
    caplog.set_level("DEBUG")
    _, finished = telemetry
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    spec = memory_spec(working={"json_schema": RICH})
    run_id, row = turn(worker, client, mem_tenant, conv, raw({"name": SECRET}), spec)
    assert row["status"] == "completed", row["error"]
    events = event_rows(worker.engine, run_id)
    [updated] = by_type(events, "memory_updated")
    assert SECRET not in json.dumps(updated) and "user-1" not in json.dumps(updated)
    spans, logs = _wait_for_run_span(finished, run_id)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "working memory updated" in logged and SECRET not in logged and "user-1" not in logged
    # The model's own spans may carry the conversation; the memory machinery's spans must not.
    mine = [s for s in spans if s.name.startswith("eap.") and "model" not in s.name]
    assert mine and SECRET not in _blob([dict(s.attributes) for s in mine])
    assert SECRET not in _blob([(r.body, dict(r.attributes or {})) for r in logs if "memory" in str(r.body)])


# ----------------------------------------------------------------------------- redaction

EMAIL = "ada.lovelace@example.com"
REDACT = {"kinds": ["email"]}


def test_email_in_template_content_and_in_patch_values_is_stored_redacted(worker, client, mem_tenant):
    agent = uuid.uuid4()
    tmpl = memory_spec(working={"scope": "conversation"}, redact=REDACT)
    conv = conversation(worker, mem_tenant, agent)
    run_id, row = turn(worker, client, mem_tenant, conv, raw(content=f"# User\n- Email: {EMAIL}"), tmpl)
    assert row["status"] == "completed", row["error"]
    stored = note(worker, mem_tenant, agent, "conversation", conv["id"])
    assert stored.content == "# User\n- Email: [REDACTED:email]" and EMAIL not in stored.content
    [updated] = by_type(event_rows(worker.engine, run_id), "memory_updated")
    assert updated["redacted"] == 1 and EMAIL not in json.dumps(updated)

    spec = memory_spec(working={"json_schema": RICH}, redact=REDACT)
    patch = {"name": EMAIL, "tags": [f"x {EMAIL}", "plain"], "prefs": {"color": f"<{EMAIL}>"}}
    run_id, row = turn(worker, client, mem_tenant, conversation(worker, mem_tenant, agent), raw(patch), spec)
    assert row["status"] == "completed", row["error"]
    content = note(worker, mem_tenant, agent, "resource", "user-1").content
    assert EMAIL not in json.dumps(content)
    assert content["name"] == "[REDACTED:email]" and content["tags"][1] == "plain"
    assert by_type(event_rows(worker.engine, run_id), "memory_updated")[0]["redacted"] == 3


def test_redaction_is_inherited_from_the_agents_input_pii_redact_processor(worker, client, mem_tenant):
    agent = uuid.uuid4()
    spec = memory_spec(working={"json_schema": RICH})
    spec["input_processors"] = [{"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}]
    _, row = turn(worker, client, mem_tenant, conversation(worker, mem_tenant, agent), raw({"name": f"mail {EMAIL}"}), spec)
    assert row["status"] == "completed", row["error"]
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {"name": "mail [REDACTED:email]"}


def test_without_any_redaction_config_the_value_is_stored_as_written(worker, client, mem_tenant):
    agent = uuid.uuid4()
    _, row = turn(worker, client, mem_tenant, conversation(worker, mem_tenant, agent), raw({"name": EMAIL}),
                  memory_spec(working={"json_schema": RICH}))
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {"name": EMAIL}


@pytest.mark.parametrize("patch", [{EMAIL: "x"}, {"prefs": {EMAIL: "x"}}, {"a": {EMAIL: {"b": 1}}}])
def test_a_patch_key_that_redaction_would_change_is_refused(worker, client, mem_tenant, caplog, patch):
    caplog.set_level("DEBUG")
    agent = uuid.uuid4()
    spec = memory_spec(working={"json_schema": RICH}, redact=REDACT)
    conv = conversation(worker, mem_tenant, agent)
    ok_id, row = turn(worker, client, mem_tenant, conv, raw({"name": "Ada"}), spec)
    assert row["status"] == "completed"
    run_id, row = turn(worker, client, mem_tenant, conv, raw(patch), spec)
    assert row["status"] == "completed" and row["output"].startswith("Memory error:"), row
    assert EMAIL not in row["output"] and EMAIL not in str(row["error"])
    events = event_rows(worker.engine, run_id)
    [result] = by_type(events, "tool_result")
    assert result["status"] == "error" and EMAIL not in result["content"]
    assert by_type(events, "memory_updated") == []
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.content == {"name": "Ada"} and stored.version == 1  # nothing written, nothing partial
    assert EMAIL not in "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("eap_worker.memory"))


def test_schema_property_names_and_plain_keys_survive_redaction(worker, client, mem_tenant):
    agent = uuid.uuid4()
    spec = memory_spec(working={"json_schema": RICH}, redact=REDACT)
    _, row = turn(worker, client, mem_tenant, conversation(worker, mem_tenant, agent),
                  raw({"name": "Ada", "extra_key": "v", "prefs": {"color": "red"}}), spec)
    assert row["status"] == "completed", row["error"]
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {
        "name": "Ada", "extra_key": "v", "prefs": {"color": "red"}}


# ----------------------------------------------------------------------------- two updates, replay, concurrency


@pytest.fixture
def two_updates(monkeypatch):
    """`test:memory` made to call update_working_memory twice in one turn: {"name": "A"}, then {"tags": ["x"]}."""
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from eap_worker import models

    patches = [{"name": "A"}, {"tags": ["x"]}]

    def plan(messages):
        done = len([p for p in models._this_turn(messages) if isinstance(p, ToolReturnPart)])
        return ("tool", {"patch": patches[done]}) if done < len(patches) else ("text", "both done")

    def respond(messages, info):
        kind, value = plan(messages)
        return ModelResponse(parts=[ToolCallPart("update_working_memory", value) if kind == "tool" else TextPart(value)])

    async def stream(messages, info):
        kind, value = plan(messages)
        if kind == "tool":
            yield {0: DeltaToolCall(name="update_working_memory", json_args=json.dumps(value))}
        else:
            yield value

    monkeypatch.setattr(models, "scripted_memory_model",
                        lambda: FunctionModel(respond, stream_function=stream, model_name="scripted-two"))


def test_two_updates_in_one_turn_make_two_versions_and_replay_writes_nothing_more(worker, client, mem_tenant, two_updates):
    import asyncio

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    from eap_worker.memory import MemoryContext, UpdateWorkingMemoryTool, with_memory
    from eap_schemas import AgentSpec

    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    spec = memory_spec(working={"json_schema": RICH})
    run_id, row = turn(worker, client, mem_tenant, conv, "go", spec)
    assert row["status"] == "completed" and row["output"] == "both done", row
    events = event_rows(worker.engine, run_id)
    updated = [(k, d) for _, k, t, d in events if t == "memory_updated"]
    assert [d["version"] for _, d in updated] == [1, 2] and len({k for k, _ in updated}) == 2
    assert [d["changed_keys"] for _, d in updated] == [["name"], ["tags"]]
    assert note(worker, mem_tenant, agent, "resource", "user-1").content == {"name": "A", "tags": ["x"]}

    # Recovery re-executes both call steps with their tool_call_ids: the ledger answers, nothing is written.
    ids = [d["tool_call_id"] for _, d in updated]
    aspec = AgentSpec.model_validate(spec)
    mem = MemoryContext(agent_id=str(agent), conversation_id=conv["id"], resource_id="user-1", working=aspec.memory.working,
                        working_memory_scope="resource", working_memory_key="user-1", note=None, redact=None)
    deps = with_memory(worker.deps_for(str(run_id), mem_tenant, aspec), mem)
    tool = UpdateWorkingMemoryTool(mem, lambda: MemoryStore(worker.engine))

    async def replay(tid, patch):
        ctx = RunContext(deps=deps, model=None, usage=RunUsage(), tool_call_id=tid)  # type: ignore[arg-type]
        return await tool.call({"patch": patch}, ctx, await tool.definition(ctx))

    assert asyncio.run(replay(ids[0], {"name": "A"})) == WM_UPDATED.format(version=1)
    assert asyncio.run(replay(ids[1], {"tags": ["x"]})) == WM_UPDATED.format(version=2)
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.version == 2 and stored.content == {"name": "A", "tags": ["x"]}
    assert len(by_type(event_rows(worker.engine, run_id), "memory_updated")) == 2


def test_concurrent_turns_of_two_conversations_of_one_resource_keep_both_keys(worker, client, mem_tenant):
    agent = uuid.uuid4()
    spec = memory_spec(working={"json_schema": RICH})
    convs = [conversation(worker, mem_tenant, agent) for _ in range(4)]
    patches = [{"name": "N"}, {"tags": ["t"]}, {"prefs": {"color": "c"}}, {"prefs": {"tone": "casual"}}]
    runs = [start_turn(client, worker.run_store, mem_tenant, c["id"], raw(p), spec) for c, p in zip(convs, patches)]
    rows = [wait_terminal(worker.run_store, mem_tenant, r, timeout=90) for r in runs]
    assert [r["status"] for r in rows] == ["completed"] * 4, [r["error"] for r in rows]
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.version == 4
    assert stored.content == {"name": "N", "tags": ["t"], "prefs": {"color": "c", "tone": "casual"}}


def test_concurrent_store_writes_never_lose_a_field(worker, mem_tenant):
    agent = uuid.uuid4()
    run_ids = [worker.run_store.create_turn(mem_tenant, conversation(worker, mem_tenant, agent)["id"], 1, "x", uuid.uuid4())[0]
               for _ in range(8)]
    store = MemoryStore(worker.engine)
    errors: list[BaseException] = []

    def write(i: int) -> None:
        try:
            store.update_working_memory(mem_tenant, str(agent), scope="resource", scope_key="user-1",
                                        patch={f"k{i}": i}, json_schema={"type": "object"}, ledger_key=f"wm:{run_ids[i]}:c{i}", run_id=str(run_ids[i]))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    stored = store.get_working_memory(mem_tenant, str(agent), "resource", "user-1")
    assert stored.version == 8 and stored.content == {f"k{i}": i for i in range(8)}


# ----------------------------------------------------------------------------- history limits


def _tool_turns(worker, client, tenant, conv, n: int, agent_spec=None) -> list[str]:
    """n memory turns, each with a tool call and its result (two model responses) in its stored messages."""
    spec = agent_spec or memory_spec(working={"json_schema": {"type": "object"}})
    prompts = [f"remember k{i}=v{i}" for i in range(1, n + 1)]
    for p in prompts:
        _, row = turn(worker, client, tenant, conv, p, spec)
        assert row["status"] == "completed", row["error"]
    return prompts


def _ask(worker, client, tenant, conv, history, input_text="five", **memory):
    spec = memory_spec(model="history", history=history, **memory)
    return turn(worker, client, tenant, conv, input_text, spec)


def test_last_turns_keeps_whole_turns_including_their_tool_call_and_result(worker, client, mem_tenant):
    conv = conversation(worker, mem_tenant)
    prompts = _tool_turns(worker, client, mem_tenant, conv, 4)
    _, row = _ask(worker, client, mem_tenant, conv, {"last_turns": 2})
    # Two whole turns x two model responses (the tool call and the answer); never a half turn.
    assert row["output"] == f"Turn 3. You said: {prompts[2]} | {prompts[3]} | five. I answered 4 times before."
    _, row = _ask(worker, client, mem_tenant, conv, {"last_turns": 1}, "six")  # the newest is the "five" ask
    assert row["output"] == "Turn 2. You said: five | six. I answered 1 times before."
    _, row = _ask(worker, client, mem_tenant, conv, {}, "seven")  # default 20: everything so far
    assert row["output"].startswith(f"Turn 7. You said: {prompts[0]} | {prompts[1]} |")


def test_max_tokens_trims_whole_turns_by_the_chars_over_four_estimate(worker, client, mem_tenant):
    from eap_run_store.memory import turn_approx_tokens

    def fresh():
        conv = conversation(worker, mem_tenant)
        prompts = _tool_turns(worker, client, mem_tenant, conv, 3)
        stored = worker.run_store.completed_turns_before(mem_tenant, conv["id"], 99)
        return conv, prompts, [turn_approx_tokens(t) for t in stored]

    conv, prompts, sizes = fresh()
    assert len(sizes) == 3 and all(s > 1 for s in sizes)
    two = sizes[1] + sizes[2]
    _, row = _ask(worker, client, mem_tenant, conv, {"max_tokens": two})
    assert row["output"] == f"Turn 3. You said: {prompts[1]} | {prompts[2]} | five. I answered 4 times before."
    conv, prompts, sizes = fresh()  # the older one no longer fits in one token less
    _, row = _ask(worker, client, mem_tenant, conv, {"max_tokens": sizes[1] + sizes[2] - 1})
    assert row["output"] == f"Turn 2. You said: {prompts[2]} | five. I answered 2 times before."
    # The newest turn alone does not fit: no half turn (no orphan tool call), none at all.
    conv, prompts, sizes = fresh()
    _, row = _ask(worker, client, mem_tenant, conv, {"max_tokens": sizes[2] - 1})
    assert row["status"] == "completed" and row["output"] == "Turn 1. You said: five. I answered 0 times before."


def test_last_turns_is_applied_before_max_tokens_and_the_byte_cap_last(worker, client, mem_tenant, monkeypatch):
    conv = conversation(worker, mem_tenant)
    prompts = _tool_turns(worker, client, mem_tenant, conv, 3)
    # last_turns=2 first (turn 1 is out whatever the budget), a huge token budget changes nothing.
    _, row = _ask(worker, client, mem_tenant, conv, {"last_turns": 2, "max_tokens": 100000})
    assert row["output"] == f"Turn 3. You said: {prompts[1]} | {prompts[2]} | five. I answered 4 times before."
    # The byte cap still applies last, whole turns oldest first.
    monkeypatch.setenv("EAP_MAX_HISTORY_BYTES", "1")
    _, row = _ask(worker, client, mem_tenant, conv, {"last_turns": 2})
    assert row["output"] == "Turn 1. You said: five. I answered 0 times before."


def test_history_never_includes_another_conversation_or_a_blocked_turn(worker, client, mem_tenant):
    other = conversation(worker, mem_tenant)
    _tool_turns(worker, client, mem_tenant, other, 2)
    conv = conversation(worker, mem_tenant)
    block = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "block"}}
    spec = memory_spec(model="history", history={"last_turns": 10})
    spec["input_processors"] = [block]
    _, row = turn(worker, client, mem_tenant, conv, "one", spec)
    assert row["status"] == "completed"
    _, blocked = turn(worker, client, mem_tenant, conv, f"mail {EMAIL}", spec)
    assert blocked["status"] != "completed" and blocked["output"] is None
    _, row = turn(worker, client, mem_tenant, conv, "three", spec)
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Turn 2. You said: one | three. I answered 1 times before."
    assert EMAIL not in row["output"] and "remember" not in row["output"]


# ----------------------------------------------------------------------------- read_only, no resource, layout


def test_read_only_renders_the_note_offers_no_tool_and_never_writes(worker, client, worker_settings, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    MemoryStore(worker.engine).update_working_memory(
        mem_tenant, str(agent), scope="resource", scope_key="user-1", patch={"name": "Ada"}, json_schema=RICH)
    spec = memory_spec(working={"json_schema": RICH, "read_only": True})
    run_id, row = turn(worker, client, mem_tenant, conv, "remember name=Bea", spec)
    assert row["status"] == "completed", row["error"]
    assert row["output"].startswith("Working memory:\n") and '"name": "Ada"' in row["output"]  # shown, no tool call
    events = event_rows(worker.engine, run_id)
    assert by_type(events, "tool_call") == [] and by_type(events, "memory_updated") == []
    assert step_names(worker_settings.dbos_system_schema, run_id) == MEMORY_TURN
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.content == {"name": "Ada"} and stored.version == 1
    # Even a hand-written tool call has nowhere to go: the tool is not offered (the model sees no tool).
    run_id, row = turn(worker, client, mem_tenant, conv, raw({"name": "Bea"}), spec)
    assert row["status"] == "completed" and by_type(event_rows(worker.engine, run_id), "tool_call") == []
    assert note(worker, mem_tenant, agent, "resource", "user-1").version == 1


def test_resource_scope_without_a_resource_has_no_tool_no_block_and_the_run_works(worker, client, worker_settings, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent, resource_id=None)
    spec = memory_spec(working={"json_schema": RICH}, history={"last_turns": 5})
    run_id, row = turn(worker, client, mem_tenant, conv, "remember name=Ada", spec)
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Working memory:\n(none)"
    events = event_rows(worker.engine, run_id)
    assert by_type(events, "tool_call") == [] and by_type(events, "memory_updated") == []
    assert step_names(worker_settings.dbos_system_schema, run_id) == MEMORY_TURN
    with worker.engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM working_memories WHERE tenant_id = :t"), {"t": mem_tenant}).scalar_one()
    assert n == 0
    # History limits still apply to such a conversation.
    hist = memory_spec(model="history", history={"last_turns": 1})
    _, row = turn(worker, client, mem_tenant, conv, "two", hist)
    assert row["output"] == "Turn 2. You said: remember name=Ada | two. I answered 1 times before."


def test_the_model_sees_the_note_as_of_the_turns_start(worker, client, mem_tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, mem_tenant, agent)
    spec = memory_spec(working={"json_schema": RICH})
    _, row = turn(worker, client, mem_tenant, conv, "remember name=Ada", spec)
    assert row["output"].endswith("Working memory:\n{}")  # the empty note it started with, not its own write
    _, row = turn(worker, client, mem_tenant, conv, "remember name=Bea", spec)
    assert row["output"].startswith(WM_UPDATED.format(version=2)) and row["output"].endswith('"name": "Ada"\n}')


def test_step_layout_without_memory_is_the_baseline_and_a_memory_turn_adds_exactly_load_memory(
        worker, client, worker_settings, mem_tenant):
    schema = worker_settings.dbos_system_schema
    conv = conversation(worker, mem_tenant)
    plain = ticket_spec(tools=(), model="history")
    run_id, row = turn(worker, client, mem_tenant, conv, "one", plain)
    assert step_names(schema, run_id) == BASELINE["turn"] and "eap.load_memory" not in step_names(schema, run_id)
    # A memory spec with no working memory configured still replaces load_history by load_memory, in its slot.
    run_id, row = turn(worker, client, mem_tenant, conv, "two", memory_spec(model="history", history={"last_turns": 3}))
    names = step_names(schema, run_id)
    assert "eap.load_history" not in names and names.count("eap.load_memory") == 1
    assert [n for n in names if n != "eap.load_memory"] == [n for n in BASELINE["turn"] if n != "eap.load_history"]
    assert names.index("eap.load_memory") == BASELINE["turn"].index("eap.load_history")
    run_id, _ = turn(worker, client, mem_tenant, conversation(worker, mem_tenant), "hi", memory_spec(working={}))
    assert step_names(schema, run_id) == MEMORY_TURN
    run_id, _ = turn(worker, client, mem_tenant, conversation(worker, mem_tenant), "remember a=b", memory_spec(working={"json_schema": RICH}))
    assert step_names(schema, run_id) == MEMORY_TOOL_TURN


def test_workflow_child_runs_keep_the_baseline_even_for_a_memory_spec(worker, client, worker_settings, mem_tenant):
    from worker_tests.workflow_run_helpers import (
        agent_spec, cleanup_definitions, demo_spec, enqueue_workflow_run, publish_agent, publish_workflow)

    try:
        spec = {**agent_spec(model="memory"), "memory": {"working": {"json_schema": RICH}}}
        agent_id = publish_agent(worker.engine, mem_tenant, spec)
        workflow_id, version_id = publish_workflow(worker.engine, mem_tenant, demo_spec(agent_id))
        run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=mem_tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={"q": "remember name=Ada"},
                                      app_version=APP_VERSION)
        run = wait_terminal(worker.run_store, mem_tenant, run_id)
        assert run["status"] == "completed", run["error"]
        children = worker.run_store.list_runs(mem_tenant, parent_run_id=run_id)
        assert children
        for child in children:
            assert child["output"] == "Working memory:\n(none)"  # no block and no tool in a child run
            names = step_names(worker_settings.dbos_system_schema, child["id"])
            assert "eap.load_memory" not in names and "eap.load_history" not in names
            assert names == BASELINE["echo"]
        assert note(worker, mem_tenant, uuid.UUID(agent_id), "resource", "alice") is None
    finally:
        cleanup_definitions(worker.engine, mem_tenant)


# ----------------------------------------------------------------------------- crash on top of an existing note


def test_crash_after_the_write_on_an_existing_note_increments_once(worker, mem_tenant, crash_schema, tmp_path):
    agent = uuid.uuid4()
    MemoryStore(worker.engine).update_working_memory(
        mem_tenant, str(agent), scope="resource", scope_key="user-1", patch={"age": 30}, json_schema=SCHEMA)
    run_id, row, at_crash = _crash_run(worker, mem_tenant, crash_schema, tmp_path, "after_tool_call",
                                       "remember name=Ana", agent)
    assert row["output"].startswith(WM_UPDATED.format(version=2))
    stored = note(worker, mem_tenant, agent, "resource", "user-1")
    assert stored.version == 2 and stored.content == {"age": 30, "name": "Ana"}
    with worker.engine.connect() as conn:
        ledger = conn.execute(text("SELECT count(*) FROM side_effects WHERE tenant_id = :t AND key LIKE :k"),
                              {"t": mem_tenant, "k": f"wm:{run_id}:%"}).scalar_one()
    assert ledger == 1
