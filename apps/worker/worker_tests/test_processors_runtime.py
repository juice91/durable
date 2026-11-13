"""P0.17 processors runtime in the worker (in-process worker, real Postgres and DBOS).

Input processors run before `start` in `eap.input_processors` steps (and `eap-moderator` model
requests); output processors run on each model response's text before its model_message. A block
ends the run with `input_blocked` / `output_blocked`. Specs without processors keep their exact DBOS
step layout (the BASELINE below was captured from the worker at 3eff23d, before this task).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import text

from eap_schemas.models_policy import ModelPolicy
from eap_worker.errors import INPUT_BLOCKED_MESSAGE, OUTPUT_BLOCKED_MESSAGE
from eap_worker.processors import parse_verdict, processed_message, processed_messages
from eap_worker.workflow import WITHHELD_INPUT
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_agent_run import DeltaListener
from worker_tests.test_conversation import start_turn, step_names

EMAIL = "ada.lovelace@example.com"

# DBOS step names per run shape, captured on the unchanged worker (HEAD 3eff23d) for the same specs.
BASELINE = {
    "ticket": [
        "eap.start_run", "eap.append_event", "eap-agent__dynamic_toolset__eap-spec-tools.get_tools",
        "eap-agent__model.request_stream", "eap.append_event", "eap.append_event", "eap-agent__event_stream_handler",
        "eap-agent__dynamic_toolset__eap-spec-tools.call_tool", "eap-agent__event_stream_handler", "eap.append_event",
        "eap-agent__dynamic_toolset__eap-spec-tools.get_tools", "eap-agent__model.request_stream", "eap.append_event",
        "eap.append_event", "eap.finish_run",
    ],
    "echo": [
        "eap.start_run", "eap.append_event", "eap-agent__dynamic_toolset__eap-spec-tools.get_tools",
        "eap-agent__model.request_stream", "eap.append_event", "eap.append_event", "eap.finish_run",
    ],
    "failed": ["eap.start_run", "eap.append_event", "eap.append_event", "eap.finish_run"],
    "turn": [
        "eap.start_run", "eap.append_event", "eap.load_history", "eap-agent__dynamic_toolset__eap-spec-tools.get_tools",
        "eap-agent__model.request_stream", "eap.append_event", "eap.append_event", "eap.finish_run",
    ],
}


def echo_spec(*, input_processors=(), output_processors=(), model: str = "echo") -> dict:
    spec = ticket_spec(tools=(), model=model)
    if input_processors:
        spec["input_processors"] = list(input_processors)
    if output_processors:
        spec["output_processors"] = list(output_processors)
    return spec


PII = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}
BLOCK_SECRET = {"type": "regex_blocklist", "config": {"patterns": ["SECRET"], "action": "block"}}


def moderation(model: str = "moderation") -> dict:
    return {"type": "moderation", "config": {"model": {"provider": "test", "name": model}}}


def run(worker, client, tenant, spec, input_text, **kw):
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text,
                         app_version=APP_VERSION, **kw)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def usage_keys(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return sorted(conn.execute(
            text("SELECT request_key FROM run_model_requests WHERE run_id = :r"), {"r": str(run_id)}
        ).scalars())


def run_messages(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one()


def stored_text(worker, tenant, run_id) -> str:
    """Everything stored for the run that a user can see: events, output, error, messages."""
    row = worker.run_store.get_run(tenant, run_id)
    return json.dumps(
        [event_rows(worker.engine, run_id), row["output"], row["error"], run_messages(worker.engine, run_id)],
        default=str,
    )


def keys_types(events):
    return [(k, t) for _, k, t, _ in events]


# --------------------------------------------------------------------------- unit


def test_parse_verdict_fails_closed_on_anything_but_a_verdict():
    assert parse_verdict('{"flagged": false, "categories": []}').flagged is False
    assert parse_verdict('```json\n{"flagged": true, "categories": ["hate"]}\n```').categories == ("hate",)
    for bad in ("", "no", '{"flagged": "maybe"}', '{"flagged": true, "categories": ["unknown"]}',
                '{"flagged": false, "categories": ["hate"]}', '{"flagged": true} trailing', "[1]"):
        assert parse_verdict(bad) is None, bad


def test_processed_message_replaces_text_and_drops_thinking():
    message = {"kind": "response", "parts": [
        {"part_kind": "thinking", "content": f"mail {EMAIL}"},
        {"part_kind": "text", "content": f"a {EMAIL}"},
        {"part_kind": "tool-call", "tool_name": "x", "args": {}, "tool_call_id": "c1"},
        {"part_kind": "text", "content": "b"},
    ]}
    out = processed_message(message, "a [REDACTED:email]b")
    assert [p["part_kind"] for p in out["parts"]] == ["text", "tool-call"]
    assert out["parts"][0]["content"] == "a [REDACTED:email]b"
    msgs = [{"kind": "request", "parts": []}, message, {"kind": "request", "parts": []}, message]
    patched = processed_messages(msgs, {1: "second"})
    assert patched[1] == message and patched[3]["parts"][0]["content"] == "second"


# --------------------------------------------------------------------------- step layout unchanged


def test_specs_without_processors_keep_their_exact_step_sequence(worker, client, worker_settings, tenant):
    schema = worker_settings.dbos_system_schema
    run_id, row = run(worker, client, tenant, ticket_spec(), "Laptop broken")
    assert row["status"] == "completed"
    assert step_names(schema, run_id) == BASELINE["ticket"]
    run_id, row = run(worker, client, tenant, echo_spec(), f"hi {EMAIL}")
    assert row["status"] == "completed" and row["output"] == f"Echo: hi {EMAIL}"
    assert step_names(schema, run_id) == BASELINE["echo"]
    failing = echo_spec() | {"model": {"provider": "openai", "name": "gpt-x"}}
    run_id, row = run(worker, client, tenant, failing, "x")
    assert row["status"] == "failed"
    assert step_names(schema, run_id) == BASELINE["failed"]
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="c")
    for prompt in ("p0", "p1"):
        run_id = start_turn(client, worker.run_store, tenant, conv["id"], prompt)
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        assert step_names(schema, run_id) == BASELINE["turn"]


# --------------------------------------------------------------------------- input


def test_input_pii_is_redacted_before_the_model_and_everywhere_stored(worker, client, worker_settings, tenant):
    run_id, row = run(worker, client, tenant, echo_spec(input_processors=[PII]), f"Mail me at {EMAIL} please")
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Echo: Mail me at [REDACTED:email] please"  # what the model received
    events = event_rows(worker.engine, run_id)
    assert keys_types(events) == [
        ("start", "run_started"), ("processor:input:0", "processor_result"),
        ("model:0:final", "model_message"), ("end", "run_completed"),
    ]
    assert events[0][3]["input"] == "Mail me at [REDACTED:email] please" and events[0][3]["input_processed"] is True
    assert events[1][3] == {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "redact",
                            "match_counts": {"email": 1}}
    assert EMAIL not in stored_text(worker, tenant, run_id)
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    assert steps[:3] == ["eap.start_run", "eap.input_processors", "eap.append_event"]


def test_input_block_fails_the_run_without_calling_the_model(worker, client, worker_settings, tenant):
    spec = echo_spec(input_processors=[PII, BLOCK_SECRET, moderation()])
    run_id, row = run(worker, client, tenant, spec, f"the SECRET is {EMAIL}")
    assert row["status"] == "failed"
    assert row["error"] == f"InputBlocked: {INPUT_BLOCKED_MESSAGE}"
    events = event_rows(worker.engine, run_id)
    assert keys_types(events) == [
        ("start", "run_started"), ("processor:input:0", "processor_result"),
        ("processor:input:1", "processor_result"), ("end", "run_failed"),
    ]
    assert events[0][3]["input"] == WITHHELD_INPUT
    assert events[2][3]["action"] == "block" and events[2][3]["match_counts"] == {"pattern_0": 1}
    assert events[-1][3]["error_code"] == "input_blocked"
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    assert not any("eap-agent__" in s or "moderator" in s for s in steps)  # no model, no moderation
    assert "SECRET" not in stored_text(worker, tenant, run_id) and EMAIL not in stored_text(worker, tenant, run_id)
    assert usage_keys(worker.engine, run_id) == []


def test_input_moderation_flags_and_records_its_usage_once(worker, client, worker_settings, tenant):
    spec = echo_spec(input_processors=[PII, moderation()])
    run_id, row = run(worker, client, tenant, spec, f"FLAG_ME {EMAIL}")
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    mod = events[2][3]
    assert events[2][1] == "processor:input:1"
    assert mod["action"] == "block" and mod["categories"] == ["illicit"] and mod["match_counts"] == {}
    assert mod["model_id"] == "test:moderation" and mod["usage"]["input_tokens"] > 0 and mod["cost_usd"] is not None
    assert events[-1][3]["error_code"] == "input_blocked"
    assert usage_keys(worker.engine, run_id) == ["moderation:input:1"]
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    assert steps.count("eap-moderator__model.request") == 1

    # Clean text passes moderation and the run is priced for both requests.
    run_id, row = run(worker, client, tenant, spec, "hello there")
    assert row["status"] == "completed" and row["output"] == "Echo: hello there"
    assert usage_keys(worker.engine, run_id) == ["model:0", "moderation:input:1"]
    assert event_rows(worker.engine, run_id)[2][3]["action"] == "allow"


def test_moderation_model_removed_from_allowlist_fails_closed(worker, client, tenant):
    original = worker.settings
    worker.settings = replace(original, allowed_models=ModelPolicy.parse("test:echo"))
    try:
        run_id, row = run(worker, client, tenant, echo_spec(input_processors=[moderation()]), "hello")
    finally:
        worker.settings = original
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert events[1][3] == {"phase": "input", "index": 0, "processor_type": "moderation", "action": "block",
                            "match_counts": {}, "reason": "model_not_allowed"}
    assert events[-1][3]["error_code"] == "input_blocked"
    assert usage_keys(worker.engine, run_id) == []


def test_unparsable_verdict_fails_closed(worker, client, tenant):
    run_id, row = run(worker, client, tenant, echo_spec(input_processors=[moderation("moderation-invalid")]), "hi")
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert events[1][3]["reason"] == "verdict_invalid" and events[1][3]["action"] == "block"
    assert usage_keys(worker.engine, run_id) == ["moderation:input:0"]  # the request still cost


def test_empty_verdict_fails_closed_on_the_first_priced_request(worker, client, worker_settings, tenant):
    run_id, row = run(worker, client, tenant, echo_spec(input_processors=[moderation("moderation-empty")]), "hi")
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert events[1][3]["reason"] == "verdict_invalid" and events[-1][3]["error_code"] == "input_blocked"
    # No output retry: one moderation request, and it is priced.
    assert step_names(worker_settings.dbos_system_schema, run_id).count("eap-moderator__model.request") == 1
    assert usage_keys(worker.engine, run_id) == ["moderation:input:0"]


# --------------------------------------------------------------------------- output


def test_output_processors_redact_stored_text_and_publish_no_deltas(worker, client, worker_settings, tenant):
    listener = DeltaListener()
    try:
        plain_id, plain = run(worker, client, tenant, echo_spec(), "streamed")
        checked_id, checked = run(worker, client, tenant, echo_spec(output_processors=[PII, moderation()]),
                                  f"write to {EMAIL}")
    finally:
        listener.close()
    assert plain["status"] == "completed" and listener.for_run(plain_id)  # the listener works
    assert checked["status"] == "completed", checked["error"]
    assert listener.for_run(checked_id) == []
    assert checked["output"] == "Echo: write to [REDACTED:email]"
    events = event_rows(worker.engine, checked_id)
    assert keys_types(events) == [
        ("start", "run_started"), ("processor:output:0:model:0", "processor_result"),
        ("processor:output:1:model:0", "processor_result"), ("model:0:final", "model_message"), ("end", "run_completed"),
    ]
    assert events[3][3]["text"] == "Echo: write to [REDACTED:email]"
    messages = run_messages(worker.engine, checked_id)
    assert messages[-1]["parts"][0]["content"] == "Echo: write to [REDACTED:email]"
    # The raw input is the user's (no input processors); the model's answer is never stored raw.
    assert EMAIL not in json.dumps([e[3] for e in events[1:]]) and EMAIL not in checked["output"]
    assert usage_keys(worker.engine, checked_id) == ["model:0", "moderation:output:1:model:0"]
    steps = step_names(worker_settings.dbos_system_schema, checked_id)
    assert steps.index("eap.output_processors") < steps.index("eap-moderator__model.request")


def test_output_block_never_stores_the_answer(worker, client, tenant):
    spec = echo_spec(output_processors=[{"type": "regex_blocklist", "config": {"patterns": ["Echo"]}}])
    run_id, row = run(worker, client, tenant, spec, "anything")
    assert row["status"] == "failed" and row["output"] is None and run_messages(worker.engine, run_id) is None
    assert row["error"] == f"OutputBlocked: {OUTPUT_BLOCKED_MESSAGE}"
    events = event_rows(worker.engine, run_id)
    assert keys_types(events) == [
        ("start", "run_started"), ("processor:output:0:model:0", "processor_result"),
        ("model:0:final", "model_message"), ("end", "run_failed"),
    ]
    assert events[2][3]["text"] == "" and events[2][3]["blocked"] is True and "message" not in events[2][3]
    assert events[2][3]["usage"] is not None  # the request is still recorded and priced
    assert events[-1][3]["error_code"] == "output_blocked"
    assert usage_keys(worker.engine, run_id) == ["model:0"]


def test_output_moderation_runs_once_on_the_final_answer_of_a_tool_run(worker, client, tenant):
    spec = ticket_spec() | {"output_processors": [PII, moderation()]}
    run_id, row = run(worker, client, tenant, spec, "Laptop broken")
    assert row["status"] == "completed", row["error"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    # Request 0 is a tool call without text: nothing to check. The answer is checked once.
    assert [k for k in keys if k.startswith("processor:")] == ["processor:output:0:model:1", "processor:output:1:model:1"]
    assert keys.index("processor:output:1:model:1") < keys.index("model:1:final")
    assert usage_keys(worker.engine, run_id) == ["model:0", "model:1", "moderation:output:1:model:1"]


def test_max_length_truncate_on_output(worker, client, tenant):
    spec = echo_spec(output_processors=[{"type": "max_length", "config": {"max_chars": 6, "action": "truncate"}}])
    run_id, row = run(worker, client, tenant, spec, "a long prompt")
    assert row["status"] == "completed" and row["output"] == "Echo: "
    assert event_rows(worker.engine, run_id)[1][3]["match_counts"] == {"chars_over_limit": len("Echo: a long prompt") - 6}


# --------------------------------------------------------------------------- history, workflows


def test_blocked_turn_is_excluded_from_the_next_turns_history(worker, client, tenant):
    spec = echo_spec(model="history", input_processors=[BLOCK_SECRET])
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="guarded")
    outputs = []
    for prompt in ("hello", "the SECRET plan", "again"):
        run_id = start_turn(client, worker.run_store, tenant, conv["id"], prompt, spec=spec)
        outputs.append(wait_terminal(worker.run_store, tenant, run_id))
    assert [r["status"] for r in outputs] == ["completed", "failed", "completed"]
    assert outputs[2]["output"] == "Turn 2. You said: hello | again. I answered 1 times before."


def test_workflow_agent_step_with_processors(worker, client, tenant):
    from worker_tests.workflow_run_helpers import (
        agent_spec,
        cleanup_definitions,
        enqueue_workflow_run,
        publish_agent,
        publish_workflow,
    )

    try:
        agent_id = publish_agent(worker.engine, tenant, agent_spec() | {"input_processors": [PII, BLOCK_SECRET]})
        wf = {
            "name": "Guarded",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            "start": "ask",
            "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}],
        }
        workflow_id, version_id = publish_workflow(worker.engine, tenant, wf)
        results = []
        for q in (f"mail {EMAIL}", "SECRET"):
            run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                          version_id=version_id, input_value={"q": q}, app_version=APP_VERSION)
            results.append((run_id, wait_terminal(worker.run_store, tenant, run_id)))
        (ok_id, ok), (blocked_id, blocked) = results
        assert ok["status"] == "completed" and ok["output"] == '"Echo: mail [REDACTED:email]"'
        assert blocked["status"] == "failed"
        failed = [e for e in event_rows(worker.engine, blocked_id) if e[1] == "step:ask:failed"][0][3]
        assert failed.get("error_code") == "input_blocked", failed
        # The agent's processors guard its child run (the workflow's own input is the workflow's).
        child_id = uuid.UUID([e for e in event_rows(worker.engine, ok_id) if e[1] == "step:ask:started"][0][3]["child_run_id"])
        assert EMAIL not in stored_text(worker, tenant, child_id)
        assert [k for _, k, _, _ in event_rows(worker.engine, child_id)][:2] == ["start", "processor:input:0"]
    finally:
        cleanup_definitions(worker.engine, tenant)


@pytest.mark.parametrize("phase", ["input", "output"])
def test_matched_text_never_reaches_logs(worker, client, tenant, caplog, phase):
    caplog.set_level("DEBUG")
    key = "input_processors" if phase == "input" else "output_processors"
    run_id, row = run(worker, client, tenant, echo_spec(**{key: [PII, moderation()]}), f"x {EMAIL} FLAG_ME")
    assert row["status"] == "failed"
    assert EMAIL not in caplog.text and "FLAG_ME" not in caplog.text


# --------------------------------------------------------------------------- thinking (review fixes)


def test_thinking_only_answer_is_not_checked_and_the_real_answer_is(worker, client, worker_settings, tenant):
    spec = echo_spec(model="think-first", output_processors=[PII, moderation()])
    run_id, row = run(worker, client, tenant, spec, f"hi {EMAIL}")
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Echo: hi [REDACTED:email]"
    events = event_rows(worker.engine, run_id)
    keys = [k for _, k, _, _ in events]
    # Request 0 (thinking only, retried by the agent) has nothing to check; request 1 is the answer.
    assert [k for k in keys if k.startswith("processor:")] == ["processor:output:0:model:1", "processor:output:1:model:1"]
    data = {k: d for _, k, _, d in events}
    assert data["processor:output:0:model:1"]["match_counts"] == {"email": 1}
    assert data["processor:output:1:model:1"]["action"] == "allow"  # no false verdict_invalid block
    assert usage_keys(worker.engine, run_id) == ["model:0", "model:1", "moderation:output:1:model:1"]
    assert step_names(worker_settings.dbos_system_schema, run_id).count("eap-moderator__model.request") == 1
    assert "pondering" not in stored_text(worker, tenant, run_id)  # thinking never stored


def test_thinking_beside_a_tool_call_is_never_stored(worker, client, tenant):
    tools = ("clock.now",)
    # Control: without output processors the model's thinking is stored as it always was.
    plain_id, plain = run(worker, client, tenant, ticket_spec(tools=tools, model="think-tool"), "go")
    assert plain["status"] == "completed", plain["error"]
    assert "SECRET" in stored_text(worker, tenant, plain_id)

    spec = ticket_spec(tools=tools, model="think-tool") | {"output_processors": [BLOCK_SECRET]}
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="thinking")
    run_id = start_turn(client, worker.run_store, tenant, conv["id"], "go", spec=spec)
    row = wait_terminal(worker.run_store, tenant, run_id)
    assert row["status"] == "completed" and row["output"] == "Done.", row["error"]
    assert "SECRET" not in stored_text(worker, tenant, run_id)
    history = worker.run_store.completed_turns_before(tenant, conv["id"], 99)
    assert history and "SECRET" not in json.dumps(history, default=str)
