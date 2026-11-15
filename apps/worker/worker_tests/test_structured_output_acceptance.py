"""AC.4 structured output runtime: the tester's acceptance tests.

Modes and the retry boundary, value-free errors, no deltas, processors (nested PII, a schema broken
by a processor, moderation once), crash recovery at the candidate and at each new step, structured
turns in memory, the text-spec golden lists, workflow agent steps, and the object-key question.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_worker.capabilities import schema_errors
from worker_tests.conftest import event_rows, wait_terminal
from worker_tests.test_agent_run import DeltaListener
from worker_tests.test_processors_runtime import EMAIL, PII, moderation
from worker_tests.test_step_layout_ac import SCHEMA, run
from worker_tests.test_structured_output import by_type, keys, stored_messages, structured
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture

MODES = ["tool", "native", "prompted"]
LEAK = "LEAKVALUE-4711"
NESTED = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"contact": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}},
                "required": ["contact"],
            },
        },
    },
    "required": ["summary", "people"],
}
NESTED_VALUE = {
    "summary": f"reach {EMAIL} please",
    "people": [{"contact": f"mail {EMAIL}", "tags": ["ok", f"cc {EMAIL}"]}, {"contact": "none"}],
}


def usage_keys(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)})]


def everything(events) -> str:
    return json.dumps([(k, t, d) for _, k, t, d in events], default=str)


# --------------------------------------------------------------------------- modes and retries


@pytest.mark.parametrize("mode", MODES)
def test_a_valid_first_candidate_completes_without_any_retry(worker, client, tenant, mode):
    run_id, row = run(worker, client, tenant, structured(mode=mode), "hi")
    assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    assert keys(events) == ["start", "model:0:final", "end"]
    assert not by_type(events, "output_retry")
    assert json.loads(row["output"]) == {"answer": "x", "score": 1} and row["output_format"] == "json"
    assert usage_keys(worker.engine, run_id) == ["model:0"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("retries", [0, 1, 3])
def test_never_valid_stops_after_exactly_max_retries_plus_one_candidates(worker, client, tenant, mode, retries):
    spec = structured(model="structured-never", mode=mode, max_retries=retries)
    run_id, row = run(worker, client, tenant, spec, "x")
    assert row["status"] == "failed" and row["output"] is None
    events = event_rows(worker.engine, run_id)
    assert len(by_type(events, "model_message")) == retries + 1
    assert [d["model_request"] for d in by_type(events, "output_retry")] == list(range(retries))
    assert usage_keys(worker.engine, run_id) == sorted(f"model:{i}" for i in range(retries + 1))
    [failed] = by_type(events, "run_failed")
    assert failed["error_code"] == "output_invalid"
    assert not by_type(events, "run_completed")


@pytest.mark.parametrize("mode", MODES)
def test_one_bad_candidate_fails_at_zero_retries_and_passes_at_one(worker, client, tenant, mode):
    bad = json.dumps({"bad_first": True})
    run_id, row = run(worker, client, tenant, structured(mode=mode, max_retries=0), bad)
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert len(by_type(events, "model_message")) == 1 and not by_type(events, "output_retry")
    assert by_type(events, "run_failed")[0]["error_code"] == "output_invalid"
    run_id, row = run(worker, client, tenant, structured(mode=mode, max_retries=1), bad)
    assert row["status"] == "completed", row
    assert len(by_type(event_rows(worker.engine, run_id), "output_retry")) == 1


@pytest.mark.parametrize("mode", MODES)
def test_the_run_never_sees_events_for_the_output_tool(worker, client, tenant, mode):
    run_id, _ = run(worker, client, tenant, structured(mode=mode), json.dumps({"bad_first": True}))
    events = event_rows(worker.engine, run_id)
    assert not [t for _, _, t, _ in events if t in ("tool_call", "tool_result")]
    assert "final_result" not in json.dumps([d for _, k, t, d in events if t != "model_message"])


# --------------------------------------------------------------------------- leaks

INVALID = {"output": {"answer": 5, "score": LEAK, "extra": LEAK, LEAK: 1}}
OPEN_SCHEMA = {  # additionalProperties is only ever true or false (the schema subset): keys are never path segments
    "type": "object",
    "properties": {"inner": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}},
    "required": ["inner"],
}


@pytest.mark.parametrize("mode", MODES)
def test_error_paths_in_retry_events_and_failures_never_contain_candidate_values(worker, client, tenant, mode):
    # The scripted model re-reads the prompt only in tool mode; in native/prompted the retry answers validly.
    spec = structured(mode=mode, max_retries=1)
    run_id, row = run(worker, client, tenant, spec, json.dumps(INVALID))
    events = event_rows(worker.engine, run_id)
    [retry] = by_type(events, "output_retry")
    assert retry["errors"] and all(p == "$" or p.startswith("$.") for p in retry["errors"])
    blob = json.dumps([retry, row["error"], by_type(events, "run_failed")])
    assert LEAK not in blob and "4711" not in blob
    run_id, row = run(worker, client, tenant, structured(mode=mode, max_retries=0), json.dumps(INVALID))
    assert row["status"] == "failed"
    [failed] = by_type(event_rows(worker.engine, run_id), "run_failed")
    assert failed["error_code"] == "output_invalid" and LEAK not in json.dumps([failed, row["error"]])


@pytest.mark.parametrize("mode", MODES)
def test_no_candidate_value_reaches_the_retry_feedback_the_model_is_sent(worker, client, tenant, mode):
    spec = structured(mode=mode, max_retries=1)
    run_id, _ = run(worker, client, tenant, spec, json.dumps(INVALID))
    events = event_rows(worker.engine, run_id)
    assert by_type(events, "output_retry")
    for _, key, type_, data in events:
        if type_ == "model_message" or key == "start":
            continue  # the candidate is the model's own record; the input holds the prompt
        assert LEAK not in json.dumps(data), key


def test_retry_error_paths_name_only_schema_properties_never_model_chosen_keys(worker, client, tenant):
    spec = structured(schema=OPEN_SCHEMA, max_retries=1)
    prompt = json.dumps({"output": {"inner": {"n": "no", EMAIL: LEAK}, LEAK: EMAIL}})
    run_id, row = run(worker, client, tenant, spec, prompt)
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    [retry] = by_type(events, "output_retry")
    assert retry["errors"] == ["$.inner.n"]
    blob = json.dumps([d for _, k, t, d in events if t != "model_message" and k != "start"] + [row["error"]])
    assert EMAIL not in blob and LEAK not in blob


@pytest.mark.parametrize("mode", MODES)
def test_no_deltas_are_streamed_for_a_structured_spec(worker, client, tenant, mode):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, structured(mode=mode), json.dumps({"bad_first": True}))
        assert row["status"] == "completed", row
        import time

        time.sleep(1.0)
        assert listener.for_run(run_id) == []
    finally:
        listener.close()


def test_text_specs_still_stream_deltas(worker, client, tenant):
    from worker_tests.conftest import ticket_spec

    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, ticket_spec(tools=(), model="echo"), "hello there")
        assert row["status"] == "completed"
        import time

        time.sleep(1.0)
        assert listener.for_run(run_id)
    finally:
        listener.close()


@pytest.mark.parametrize("mode", MODES)
def test_traces_and_logs_hold_no_rejected_candidate_value(worker, client, tenant, telemetry, caplog, mode):  # noqa: F811
    from worker_tests.test_processors_runtime_acceptance import _everything, _wait_spans

    caplog.set_level("DEBUG")
    run_id, row = run(worker, client, tenant, structured(mode=mode, max_retries=1), json.dumps(INVALID))
    assert by_type(event_rows(worker.engine, run_id), "output_retry")
    spans, logs = _wait_spans(telemetry[1], run_id)
    assert any(s.name == "eap.run" for s in spans)
    blob = _everything(spans, logs) + caplog.text
    assert LEAK not in blob


# --------------------------------------------------------------------------- processors


def pii_spec(schema: dict[str, Any], *extra: Any, mode: str = "tool") -> dict[str, Any]:
    spec = structured(schema=schema, mode=mode)
    spec["output_processors"] = [PII, *extra]
    return spec


@pytest.mark.parametrize("mode", MODES)
def test_pii_is_redacted_inside_nested_strings_everywhere_stored(worker, client, tenant, mode):
    run_id, row = run(worker, client, tenant, pii_spec(NESTED, mode=mode), json.dumps({"output": NESTED_VALUE}))
    assert row["status"] == "completed", row
    out = json.loads(row["output"])
    red = "[REDACTED:email]"
    assert out["summary"] == f"reach {red} please"
    assert out["people"][0] == {"contact": f"mail {red}", "tags": ["ok", f"cc {red}"]}
    assert out["people"][1] == {"contact": "none"}
    events = event_rows(worker.engine, run_id)
    assert EMAIL not in everything([e for e in events if e[1] != "start"])
    assert EMAIL not in json.dumps(stored_messages(worker.engine, run_id)[1:])
    assert EMAIL not in json.dumps(row["output"])
    [done] = by_type(events, "run_completed")
    assert done["output_json"] == out and json.dumps(out, sort_keys=True, separators=(",", ":")) == row["output"]


def test_a_processor_that_breaks_the_schema_is_output_blocked_and_leaks_nothing(worker, client, tenant):
    schema = {"type": "object", "properties": {"nested": {"type": "object", "properties": {
        "c": {"type": "string", "const": EMAIL}}, "required": ["c"]}}, "required": ["nested"]}
    run_id, row = run(worker, client, tenant, pii_spec(schema), "x")
    assert row["status"] == "failed" and row["output"] is None
    events = event_rows(worker.engine, run_id)
    [failed] = by_type(events, "run_failed")
    assert failed["error_code"] == "output_blocked" and failed["reason"] == "schema_invalid_after_processing"
    assert not by_type(events, "run_completed")
    assert EMAIL not in everything([e for e in events if e[1] != "start"]) + (row["error"] or "")
    assert stored_messages(worker.engine, run_id) in (None, []) or EMAIL not in json.dumps(
        stored_messages(worker.engine, run_id))


def test_moderation_is_called_exactly_once_across_retries(worker, client, worker_settings, tenant):
    from worker_tests.test_conversation import step_names

    spec = pii_spec(SCHEMA, moderation())
    run_id, row = run(worker, client, tenant, spec, json.dumps({"bad_first": True}))
    assert row["status"] == "completed", row
    assert step_names(worker_settings.dbos_system_schema, run_id).count("eap-moderator__model.request") == 1
    assert [k for k in usage_keys(worker.engine, run_id) if k.startswith("moderation")] == ["moderation:output:1:final"]
    # A never-valid run (two candidates, no final pass) never reaches moderation at all.
    never = pii_spec(SCHEMA, moderation())
    never["model"]["name"] = "structured-never"
    run_id, row = run(worker, client, tenant, never, "x")
    assert row["status"] == "failed"
    assert not [k for k in usage_keys(worker.engine, run_id) if k.startswith("moderation")]


def test_candidate_and_final_processor_outcome_keys(worker, client, tenant):
    run_id, row = run(worker, client, tenant, pii_spec(SCHEMA, moderation()), json.dumps({"bad_first": True}))
    assert row["status"] == "completed"
    got = [k for k in keys(event_rows(worker.engine, run_id)) if k.startswith("processor:")]
    assert got == ["processor:output:0:model:0", "processor:output:0:model:1", "processor:output:1:final"]


def test_a_rejected_candidate_is_never_redacted_into_validity_and_retries_see_no_values(worker, client, tenant):
    spec = pii_spec(SCHEMA)
    run_id, row = run(worker, client, tenant, spec, json.dumps({"output": {"answer": EMAIL, "score": "no"}}))
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert EMAIL not in everything([e for e in events if e[1] != "start"])


def test_pii_in_an_object_key_is_redacted_with_additional_properties_true(worker, client, tenant):
    """No email survives pii_redact, keys the model chose (additionalProperties true) included."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
              "additionalProperties": True}
    run_id, row = run(worker, client, tenant, pii_spec(schema), json.dumps({"output": {"a": "ok", EMAIL: "v"}}))
    assert row["status"] == "completed", row
    assert EMAIL not in row["output"]
    assert json.loads(row["output"]) == {"a": "ok", "[REDACTED:email]": "v"}
    assert EMAIL not in json.dumps([d for _, k, _, d in event_rows(worker.engine, run_id) if k != "start"])


def test_undeclared_keys_and_values_are_redacted_but_declared_names_are_kept(worker, client, tenant):
    """Values are redacted everywhere and an undeclared key is redacted, but a declared property
    name is fixed by the schema and kept even when a redacting processor matches it."""
    schema = {"type": "object", "properties": {"a": {"type": "string"}, "secret_note": {"type": "string"}},
              "required": ["a"], "additionalProperties": True}
    spec = structured(schema=schema)
    spec["output_processors"] = [
        PII, {"type": "regex_blocklist", "config": {"patterns": ["secret"], "action": "redact"}},
    ]
    value = {"a": f"x {EMAIL}", "secret_note": "a secret", EMAIL: "y"}
    run_id, row = run(worker, client, tenant, spec, json.dumps({"output": value}))
    assert row["status"] == "completed", row
    assert json.loads(row["output"]) == {
        "a": "x [REDACTED:email]", "secret_note": "a [REDACTED:regex]", "[REDACTED:email]": "y",
    }


def test_two_keys_that_redact_to_the_same_key_block_the_output(worker, client, tenant):
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
              "additionalProperties": True}
    value = {"a": "ok", EMAIL: "1", "bob@example.com": "2"}
    run_id, row = run(worker, client, tenant, pii_spec(schema), json.dumps({"output": value}))
    assert row["status"] == "failed" and row["output"] is None
    events = event_rows(worker.engine, run_id)
    [failed] = [d for _, _, t, d in events if t == "run_failed"]
    assert failed["error_code"] == "output_blocked" and failed["reason"] == "schema_invalid_after_processing"
    assert EMAIL not in json.dumps([d for _, k, _, d in events if k != "start"])


# --------------------------------------------------------------------------- crash and recovery

from worker_tests.test_crash_recovery import crash_schema  # noqa: E402,F401 - fixture

RETRY_PROMPT = json.dumps({"bad_first": True})


def _pii_mod(mode: str = "tool") -> dict[str, Any]:
    return pii_spec(SCHEMA, moderation(), mode=mode)


CRASH_CASES = {
    # name: (spec, env, prompt, events that must exist after the crash)
    "first_candidate_tool": (structured(), {"EAP_CRASH_AT": "output_candidate", "EAP_CRASH_EVENT_KEY": "model:0:final"},
                             RETRY_PROMPT, ["start"]),
    "first_candidate_prompted": (structured(mode="prompted"),
                                 {"EAP_CRASH_AT": "output_candidate", "EAP_CRASH_EVENT_KEY": "model:0:final"},
                                 RETRY_PROMPT, ["start"]),
    "second_candidate_native": (structured(mode="native"),
                                {"EAP_CRASH_AT": "output_candidate", "EAP_CRASH_EVENT_KEY": "model:1:final"},
                                RETRY_PROMPT, ["start", "model:0:final", "output:0:retry"]),
    "structured_processor_step": (_pii_mod(), {"EAP_CRASH_AT": "output_processors"}, RETRY_PROMPT, ["start"]),
    "final_moderation": (_pii_mod(), {"EAP_CRASH_AT": "moderation_request"}, RETRY_PROMPT,
                         ["start", "processor:output:0:model:0", "model:0:final", "output:0:retry",
                          "processor:output:0:model:1", "model:1:final"]),
    "after_retry_event": (structured(), {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "output:0:retry"},
                          RETRY_PROMPT, ["start", "model:0:final", "output:0:retry"]),
}


@pytest.mark.parametrize("case", list(CRASH_CASES))
def test_crash_recovers_exactly_once_with_the_same_object(worker, tenant, crash_schema, tmp_path, case):  # noqa: F811
    from dbos import DBOSClient

    from worker_tests.conftest import DBOS_DB_URL, enqueue_run as _enqueue
    from worker_tests.test_crash_recovery import APP_VERSION as V, WorkerProcess, _env

    spec, crash_env, prompt, before = CRASH_CASES[case]
    executor_id = f"ac4-acc-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, **crash_env), tmp_path / "w1.log")
    second = client = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = _enqueue(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt, app_version=V)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        pre = event_rows(worker.engine, run_id)
        assert keys(pre) == before and worker.run_store.get_run(tenant, run_id)["status"] == "running"
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "w2.log").read_text())
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))  # gap-free seq
        assert len(keys(events)) == len(set(keys(events)))
        assert events[: len(pre)] == pre  # nothing written before the crash changed
        assert len(by_type(events, "run_completed")) == 1 and len(by_type(events, "output_retry")) == 1
        assert [d["model_request"] for d in by_type(events, "model_message")] == [0, 1]
        assert json.loads(row["output"]) == {"answer": "x", "score": 1}
        assert by_type(events, "run_completed")[0]["output_json"] == {"answer": "x", "score": 1}
        assert row["output_format"] == "json"
        expected = ["model:0", "model:1"] + (["moderation:output:1:final"] if "output_processors" in spec else [])
        assert usage_keys(worker.engine, run_id) == sorted(expected)
    finally:
        for proc in (first, second):
            if proc is not None:
                proc.stop()
        if client is not None:
            client.destroy()


# --------------------------------------------------------------------------- memory

from eap_worker.observational import turn_text  # noqa: E402
from worker_tests.test_observational_memory import obs_spec, wait_workflow  # noqa: E402
from worker_tests.test_semantic_recall import embeddings, recall_spec  # noqa: E402
from worker_tests.test_step_layout_ac import mem_tenant, with_output  # noqa: E402,F401 - fixture
from worker_tests.test_working_memory import conversation, memory_spec, turn  # noqa: E402

CANON = '{"answer":"x","score":1}'


@pytest.mark.parametrize("mode", MODES)
def test_observer_turn_text_of_a_real_structured_turn_shows_the_object_as_the_answer(worker, client, tenant, mode):
    run_id, row = run(worker, client, tenant, structured(mode=mode), json.dumps({"bad_first": True}))
    assert row["status"] == "completed", row
    rendered = turn_text({"messages": stored_messages(worker.engine, run_id), "input": row["input"],
                          "output": row["output"]})
    last = rendered.splitlines()[-1]
    # Tool mode renders canonical JSON; native/prompted render the model's own text (same object).
    assert last.startswith("Assistant: ") and json.loads(last[len("Assistant: "):]) == {"answer": "x", "score": 1}
    if mode == "tool":
        assert last == f"Assistant: {CANON}"
    assert rendered.count("Assistant:") == 1  # the rejected candidate is not shown
    assert "final_result" not in rendered and "retry" not in rendered.lower()
    assert "Assistant called tool" not in rendered


def test_observational_memory_runs_over_structured_turns(worker, client, worker_settings, mem_tenant):
    conv = conversation(worker, mem_tenant)
    spec = with_output(obs_spec(model="structured"))
    for text_in in ("first", "second"):
        run_id, row = turn(worker, client, mem_tenant, conv, text_in, spec)
        wait_workflow(worker_settings.dbos_system_schema, run_id)
        assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    assert not [t for _, _, t, _ in events if t == "memory_observe_failed"]
    assert by_type(events, "run_completed")[0]["output"] == CANON


@pytest.mark.parametrize("mode", ["tool", "native"])
def test_memory_index_embeds_the_canonical_json_once_even_after_a_retry(worker, client, worker_settings, mem_tenant, mode):
    conv = conversation(worker, mem_tenant)
    spec = with_output(recall_spec(model="structured"), mode=mode)
    run_id, row = turn(worker, client, mem_tenant, conv, json.dumps({"bad_first": True}), spec)
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert row["status"] == "completed", row
    rows = embeddings(worker.engine, conv["id"])
    assert [r["role"] for r in rows] == ["assistant", "user"]
    assert [r["text"] for r in rows if r["role"] == "assistant"] == [CANON]


def test_a_text_turn_after_a_structured_turn_sees_the_object_text_in_history(worker, client, mem_tenant):
    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "one", with_output(memory_spec(model="structured", history={"last_turns": 5})))
    assert row["status"] == "completed", row
    messages = json.dumps(stored_messages(worker.engine, run_id))
    assert "answer" in messages and "Echo" not in messages
    run_id, row = turn(worker, client, mem_tenant, conv, "two", memory_spec(model="echo", history={"last_turns": 5}))
    assert row["status"] == "completed" and row["output_format"] is None and row["output"].startswith("Echo")


# --------------------------------------------------------------------------- text specs and workflows


def test_text_specs_keep_their_event_shape_and_result(worker, client, tenant):
    from worker_tests.conftest import ticket_spec

    run_id, row = run(worker, client, tenant, ticket_spec(tools=(), model="echo"), "hi")
    events = event_rows(worker.engine, run_id)
    assert keys(events) == ["start", "model:0:final", "end"]
    assert by_type(events, "run_completed") == [{"output": "Echo: hi"}]
    assert "output_candidate" not in json.dumps(by_type(events, "model_message"))
    assert row["output_format"] is None and row["output"] == "Echo: hi"


def test_a_text_spec_on_the_structured_scripted_model_echoes_text(worker, client, tenant):
    run_id, row = run(worker, client, tenant, structured(model="structured")["model"] and
                      {k: v for k, v in structured().items() if k != "output"}, "plain")
    assert row["status"] == "completed" and row["output"] == "Echo: plain" and row["output_format"] is None
    assert "output_json" not in by_type(event_rows(worker.engine, run_id), "run_completed")[0]


def _agent_workflow(agent_id: str, schema: dict | None = None) -> dict[str, Any]:
    spec = {"name": "S", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            "start": "ask", "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id,
                                       "input": {"prompt": {"$ref": "input.q"}}}]}
    if schema is not None:
        spec["output_schema"] = schema
    return spec


def test_a_workflow_agent_step_on_a_structured_agent_returns_the_object(worker, client, tenant):
    from worker_tests.conftest import APP_VERSION
    from worker_tests.workflow_run_helpers import (
        agent_spec, cleanup_definitions, enqueue_workflow_run, publish_agent, publish_workflow,
    )

    try:
        agent = {**agent_spec(model="structured"), "output": {"json_schema": SCHEMA}}
        agent_id = publish_agent(worker.engine, tenant, agent)
        workflow_id, version_id = publish_workflow(worker.engine, tenant, _agent_workflow(agent_id, SCHEMA))
        run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={"q": json.dumps({"bad_first": True})},
                                      app_version=APP_VERSION)
        row = wait_terminal(worker.run_store, tenant, run_id)
        assert row["status"] == "completed", row["error"]
        events = event_rows(worker.engine, run_id)
        [step] = [d for _, k, _, d in events if k == "step:ask:completed"]
        # AC.9: a structured agent step's output is the validated object (output_json), not its text.
        assert step["output"] == {"answer": "x", "score": 1}, step["output"]
        assert json.loads(row["output"]) == {"answer": "x", "score": 1}  # validated against the output schema
        child = worker.run_store.get_run(tenant, uuid.UUID(by_type(events, "step_started")[0]["child_run_id"]))
        assert child["status"] == "completed" and child["output_format"] == "json" and child["output"] == CANON
        child_events = event_rows(worker.engine, uuid.UUID(child["id"]))
        assert by_type(child_events, "run_completed")[0]["output_json"] == {"answer": "x", "score": 1}
        assert len(by_type(child_events, "output_retry")) == 1
    finally:
        cleanup_definitions(worker.engine, tenant)
