"""AC.4 structured output runtime: the implementer's focused tests.

Unit tests for the schema check (value-free errors, the 2 KiB cap), the per-run output type, the
new errors and the observer's turn rendering; end-to-end runs through the in-process worker for the
three modes, a retry, exhausted retries, native on a model without native support, output
processors (redaction, a redaction that breaks the schema, moderation once on the final object),
history, semantic recall and retrieve. The acceptance tests (crash recovery, the API) are the
tester's.
"""

from __future__ import annotations

import json
import pickle
import uuid
from typing import Any

import pytest
from pydantic_ai.output import NativeOutput, PromptedOutput, ToolOutput
from sqlalchemy import text

from eap_schemas import AgentSpec
from eap_schemas.output import MAX_OUTPUT_ERROR_BYTES, OutputInvalid
from eap_worker.capabilities import (
    RETRY_HEADING,
    error_paths,
    output_type_for,
    retries_for,
    retry_message,
    schema_errors,
)
from eap_worker.errors import (
    SCHEMA_INVALID_AFTER_PROCESSING,
    OutputBlocked,
    OutputModeUnsupported,
    classify,
)
from eap_worker.observational import turn_text
from worker_tests.conftest import event_rows, ticket_spec
from worker_tests.test_conversation import start_turn, step_names
from worker_tests.test_processors_runtime import PII, moderation
from worker_tests.test_retrieve import PRINTER, ingest, kb_tenant, make_kb, rag_spec  # noqa: F401 - fixture
from worker_tests.test_semantic_recall import embeddings, recall_spec
from worker_tests.test_step_layout_ac import SCHEMA, mem_tenant, run, with_output  # noqa: F401 - fixture
from worker_tests.test_working_memory import conversation, turn
from worker_tests.conftest import wait_terminal
from worker_tests.test_observational_memory import wait_workflow
from worker_tests.test_crash_recovery import crash_schema  # noqa: F401 - fixture


def structured(model: str = "structured", schema: dict[str, Any] | None = None, **output: Any) -> dict[str, Any]:
    spec = ticket_spec(tools=(), model=model)
    spec["output"] = {"json_schema": schema or SCHEMA, **output}
    return spec


def by_type(events, type_: str) -> list[dict]:
    return [d for _, _, t, d in events if t == type_]


def keys(events) -> list[str]:
    return [k for _, k, _, _ in events]


def stored_messages(engine, run_id) -> list[dict]:
    with engine.connect() as conn:
        return conn.execute(text("SELECT messages FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one()


# --------------------------------------------------------------------------- unit


def test_schema_errors_name_paths_and_schema_facts_never_candidate_values():
    schema = {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["a", "b"]},
            "n": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
        },
        "required": ["kind", "n", "missing"],
        "additionalProperties": False,
    }
    secret = "SECRET-VALUE-ada@example.com"
    errors = schema_errors(schema, {"kind": secret, "n": 0, "tags": [secret, 5], secret: 1})
    rendered = retry_message(errors)
    assert secret not in rendered and "ada@example.com" not in rendered
    assert ("$", "missing required property \"missing\"") in errors
    assert ("$", "additional properties are not allowed") in errors
    assert ("$.kind", 'must be one of ["a", "b"]') in errors
    assert ("$.n", "must be >= 1") in errors
    assert ("$.tags[1]", "expected type string") in errors
    assert rendered.startswith(RETRY_HEADING)
    assert schema_errors(schema | {"required": ["kind", "n"]}, {"kind": "a", "n": 2}) == []
    assert schema_errors(SCHEMA, {"answer": "x", "score": float("nan")}) == [("$.score", "must be a finite number")]


def test_retry_message_is_capped_at_ten_errors_and_2_kib():
    props = {f"p{i:03d}_" + "x" * 300: {"type": "integer"} for i in range(30)}
    schema = {"type": "object", "properties": props}
    errors = schema_errors(schema, {k: "no" for k in props})
    assert len(errors) == 10
    message = retry_message(errors)
    assert len(message.encode("utf-8")) <= MAX_OUTPUT_ERROR_BYTES
    assert message.count("\n- ") < 10  # whole lines only, cut at the cap


def test_error_paths_from_pydantic_details():
    assert error_paths([{"loc": ("a", 0, "b c")}, {"loc": ()}, {"loc": ("a", 0, "b c")}]) == ['$.a[0]["b c"]', "$"]
    assert error_paths("a message") == []


def test_output_type_and_retries_per_mode_and_none_without_output():
    plain = AgentSpec.model_validate(ticket_spec(tools=()))
    assert output_type_for(plain) is None and retries_for(plain) is None
    tool = AgentSpec.model_validate(structured(max_retries=4))
    assert isinstance(output_type_for(tool), ToolOutput) and output_type_for(tool).name == "final_result"
    assert retries_for(tool) == {"output": 4}
    assert retries_for(AgentSpec.model_validate(structured())) == {"output": 2}
    assert isinstance(output_type_for(AgentSpec.model_validate(structured(mode="native"))), NativeOutput)
    assert isinstance(output_type_for(AgentSpec.model_validate(structured(mode="prompted"))), PromptedOutput)
    spec = AgentSpec.model_validate(structured())
    before = json.dumps(spec.output.json_schema, sort_keys=True)
    output_type_for(spec)
    assert json.dumps(spec.output.json_schema, sort_keys=True) == before  # the spec's schema is never annotated


def test_new_errors_pickle_and_classify():
    for exc, code in (
        (OutputInvalid(2, ("$.a",)), "output_invalid"),
        (OutputModeUnsupported("native"), "output_mode_unsupported"),
        (OutputBlocked(-1, "output_schema", SCHEMA_INVALID_AFTER_PROCESSING), "output_blocked"),
        (OutputBlocked(0, "pii_redact"), "output_blocked"),
    ):
        again = pickle.loads(pickle.dumps(exc))
        assert str(again) == str(exc)
        failure = classify(again)
        assert failure.code == code
    assert classify(OutputBlocked(-1, "x", SCHEMA_INVALID_AFTER_PROCESSING)).reason == SCHEMA_INVALID_AFTER_PROCESSING
    assert classify(OutputBlocked(0, "pii_redact")).reason is None


def test_turn_text_renders_the_accepted_object_as_the_answer():
    messages = [
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "classify"}]},
        {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "final_result", "tool_call_id": "a",
                                        "args": {"bad": 1}}]},
        {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_name": "final_result", "tool_call_id": "a",
                                       "content": "- $.x: expected type integer", "outcome": "retried"}]},
        {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "final_result", "tool_call_id": "b",
                                        "args": '{"score": 1, "answer": "x"}'}]},
        {"kind": "request", "parts": [{"part_kind": "tool-return", "tool_name": "final_result", "tool_call_id": "b",
                                       "content": "Final result processed.", "outcome": "success"}]},
    ]
    assert turn_text({"messages": messages}) == 'User: classify\nAssistant: {"answer":"x","score":1}'
    native = [
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "q"}]},
        {"kind": "response", "parts": [{"part_kind": "text", "content": "{bad"}]},
        {"kind": "request", "parts": [{"part_kind": "retry-feedback", "content": "fix it"}]},
        {"kind": "response", "parts": [{"part_kind": "text", "content": '{"answer":"x"}'}]},
    ]
    assert turn_text({"messages": native}) == 'User: q\nAssistant: {"answer":"x"}'


# --------------------------------------------------------------------------- end to end


@pytest.mark.parametrize("mode", ["tool", "native", "prompted"])
def test_each_mode_retries_once_then_returns_a_valid_object(worker, client, worker_settings, tenant, mode):
    run_id, row = run(worker, client, tenant, structured(mode=mode), json.dumps({"bad_first": True}))
    assert row["status"] == "completed", row
    assert row["output"] == '{"answer":"x","score":1}' and row["output_format"] == "json"
    events = event_rows(worker.engine, run_id)
    assert keys(events) == ["start", "model:0:final", "output:0:retry", "model:1:final", "end"]
    [retry] = by_type(events, "output_retry")
    assert retry == {"model_request": 0, "errors": ["$.answer", "$.score"], "cause": "model_retry"}
    assert all(m["output_candidate"] is True for m in by_type(events, "model_message"))
    assert not by_type(events, "tool_call") and not by_type(events, "tool_result")
    [done] = by_type(events, "run_completed")
    assert done == {"output": row["output"], "output_json": {"answer": "x", "score": 1}}


def test_text_specs_still_complete_with_text_and_no_format(worker, client, tenant):
    run_id, row = run(worker, client, tenant, ticket_spec(tools=(), model="echo"), "hi")
    assert row["status"] == "completed" and row["output"] == "Echo: hi" and row["output_format"] is None
    [done] = by_type(event_rows(worker.engine, run_id), "run_completed")
    assert done == {"output": "Echo: hi"}
    assert all("output_candidate" not in m for m in by_type(event_rows(worker.engine, run_id), "model_message"))


def test_exhausted_retries_end_output_invalid_after_max_retries_plus_one_candidates(worker, client, tenant):
    run_id, row = run(worker, client, tenant, structured(model="structured-never", max_retries=1), "x")
    assert row["status"] == "failed" and row["output"] is None
    events = event_rows(worker.engine, run_id)
    assert len(by_type(events, "model_message")) == 2 and len(by_type(events, "output_retry")) == 1
    [failed] = by_type(events, "run_failed")
    assert failed["error_code"] == "output_invalid"
    assert row["error"] == failed["error"] and "output schema" in row["error"]


def test_native_mode_on_a_model_without_native_support_is_output_mode_unsupported(worker, client, tenant):
    run_id, row = run(worker, client, tenant, structured(model="structured-no-native", mode="native"), "x")
    assert row["status"] == "failed"
    [failed] = by_type(event_rows(worker.engine, run_id), "run_failed")
    assert failed["error_code"] == "output_mode_unsupported"
    # Tool mode on the same model works.
    _, row = run(worker, client, tenant, structured(model="structured-no-native"), "x")
    assert row["status"] == "completed"


EMAIL_OUTPUT = {"output": {"answer": "write to ada@example.com [1]", "score": 1}}


def test_pii_redact_applies_to_string_leaves_everywhere_it_is_stored(worker, client, tenant):
    spec = structured()
    spec["output_processors"] = [PII]
    run_id, row = run(worker, client, tenant, spec, json.dumps(EMAIL_OUTPUT))
    assert row["status"] == "completed", row
    assert json.loads(row["output"]) == {"answer": "write to [REDACTED:email] [1]", "score": 1}
    events = event_rows(worker.engine, run_id)
    # The input holds the email (no input processors); every output-side record is redacted.
    assert "ada@example.com" not in json.dumps([d for _, k, _, d in events if k != "start"])
    messages = stored_messages(worker.engine, run_id)
    assert "ada@example.com" not in json.dumps([m for m in messages if m["kind"] == "response"])
    assert "[REDACTED:email]" in json.dumps(messages)
    [pii] = [d for _, k, _, d in events if k == "processor:output:0:model:0"]
    assert pii["action"] == "redact" and pii["match_counts"] == {"email": 1}
    [done] = by_type(events, "run_completed")
    assert done["output_json"]["answer"] == "write to [REDACTED:email] [1]"  # [n] markers untouched


def test_a_redaction_that_breaks_the_schema_is_output_blocked(worker, client, tenant):
    schema = {"type": "object", "properties": {"contact": {"type": "string", "const": "ada@example.com"}},
              "required": ["contact"]}
    spec = structured(schema=schema)
    spec["output_processors"] = [PII]
    run_id, row = run(worker, client, tenant, spec, "x")
    assert row["status"] == "failed" and row["output"] is None
    events = event_rows(worker.engine, run_id)
    [failed] = by_type(events, "run_failed")
    assert failed["error_code"] == "output_blocked" and failed["reason"] == SCHEMA_INVALID_AFTER_PROCESSING
    assert "ada@example.com" not in json.dumps([d for _, _, _, d in events])


def test_moderation_runs_once_on_the_final_object(worker, client, worker_settings, tenant):
    spec = structured()
    spec["output_processors"] = [PII, moderation()]
    run_id, row = run(worker, client, tenant, spec, json.dumps({"bad_first": True}))
    assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    assert [k for k in keys(events) if k.startswith("processor:")] == [
        "processor:output:0:model:0", "processor:output:0:model:1", "processor:output:1:final",
    ]
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    assert steps.count("eap-moderator__model.request") == 1
    assert steps.index("eap-moderator__model.request") < steps.index("eap.finish_run")
    with worker.engine.connect() as conn:
        rows = [r[0] for r in conn.execute(text("SELECT request_key FROM run_model_requests WHERE run_id = :r"),
                                           {"r": str(run_id)})]
    assert "moderation:output:1:final" in rows

    run_id, row = run(worker, client, tenant, spec, json.dumps({"output": {"answer": "FLAG_ME", "score": 1}}))
    assert row["status"] == "failed"
    [failed] = by_type(event_rows(worker.engine, run_id), "run_failed")
    assert failed["error_code"] == "output_blocked" and "reason" not in failed


def test_the_next_turn_receives_the_structured_turn_in_its_history(worker, client, tenant):
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="s")
    first = start_turn(client, worker.run_store, tenant, conv["id"], "classify me", spec=structured())
    assert wait_terminal(worker.run_store, tenant, first)["status"] == "completed"
    second_spec = ticket_spec(tools=(), model="history")
    run_id, created = worker.run_store.create_turn(tenant, conv["id"], 2, "and now?", uuid.uuid4())
    from worker_tests.conftest import APP_VERSION, enqueue_run

    enqueue_run(client, worker.run_store, tenant_id=tenant, spec=second_spec, input_text="and now?",
                app_version=APP_VERSION, run_id=run_id, agent_id=conv["agent_id"], create_row=False)
    row = wait_terminal(worker.run_store, tenant, run_id)
    assert row["status"] == "completed", row
    assert "classify me | and now?" in row["output"] and "answered 1 times before" in row["output"]


def test_semantic_recall_indexes_the_canonical_json(worker, client, worker_settings, mem_tenant):
    conv = conversation(worker, mem_tenant)
    spec = with_output(recall_spec(model="structured"))
    run_id, row = turn(worker, client, mem_tenant, conv, "hello", spec)
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert row["status"] == "completed", row
    rows = embeddings(worker.engine, conv["id"])
    assert [r["text"] for r in rows if r["role"] == "assistant"] == [row["output"]]


def test_structured_retrieve_keeps_run_wide_numbers_and_no_final_result_tool_events(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    spec = with_output(rag_spec({"id": kb["id"]}))
    run_id, row = run(worker, client, kb_tenant, spec, "toner light")
    assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    tools = {d["tool_name"] for d in by_type(events, "tool_call")} | {d["tool_name"] for d in by_type(events, "tool_result")}
    assert tools == {"retrieve"}
    [result] = by_type(events, "tool_result")
    assert result["sources"][0]["n"] == 1
    assert "[1]" in json.loads(row["output"])["answer"]


# --------------------------------------------------------------------------- crash


@pytest.mark.parametrize("mode", ["tool", "native"])
def test_crash_before_the_candidate_event_recovers_one_event_and_the_same_object(
    worker, tenant, crash_schema, tmp_path, mode  # noqa: F811
):
    from dbos import DBOSClient

    from worker_tests.conftest import DBOS_DB_URL, enqueue_run as _enqueue
    from worker_tests.test_crash_recovery import APP_VERSION as _V
    from worker_tests.test_crash_recovery import WorkerProcess, _env

    executor_id = f"ac4-crash-{uuid.uuid4().hex[:8]}"
    env = _env(crash_schema, executor_id, EAP_CRASH_AT="output_candidate", EAP_CRASH_EVENT_KEY="model:1:final")
    first = WorkerProcess(env, tmp_path / "worker1.log")
    second = None
    client = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = _enqueue(client, worker.run_store, tenant_id=tenant, spec=structured(mode=mode),
                          input_text=json.dumps({"bad_first": True}), app_version=_V)
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        assert keys(event_rows(worker.engine, run_id)) == ["start", "model:0:final", "output:0:retry"]
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "worker2.log").read_text())
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        assert keys(events) == ["start", "model:0:final", "output:0:retry", "model:1:final", "end"]
        assert json.loads(row["output"]) == {"answer": "x", "score": 1}
        with worker.engine.connect() as conn:
            usage = [r[0] for r in conn.execute(text(
                "SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)})]
        assert usage == ["model:0", "model:1"]
    finally:
        for proc in (first, second):
            if proc is not None:
                proc.stop()
        if client is not None:
            client.destroy()


def test_scrub_retry_inputs_touches_only_retry_parts():
    from eap_worker.processors import scrub_retry_inputs

    detail = [{"type": "json_invalid", "loc": [], "msg": "Invalid JSON", "input": "ada@example.com"}]
    messages = [{"kind": "request", "parts": [
        {"part_kind": "retry-feedback", "content": detail},
        {"part_kind": "tool-return", "tool_name": "final_result", "outcome": "retried", "content": detail},
        {"part_kind": "tool-return", "tool_name": "lookup", "outcome": "success", "content": [{"input": "kept"}]},
    ]}]
    parts = scrub_retry_inputs(messages)[0]["parts"]
    assert "input" not in parts[0]["content"][0] and "input" not in parts[1]["content"][0]
    assert parts[2]["content"] == [{"input": "kept"}]


def test_structured_segment_processes_undeclared_keys_and_blocks_collisions():
    from eap_worker.processors import KEY_COLLISION_REASON, _apply_structured_segment

    pii = [PII]
    schema = {"type": "object", "properties": {"a": {"type": "string"},
                                              "items": {"type": "array", "items": {"type": "object"}}},
              "additionalProperties": True}
    out = _apply_structured_segment(pii, {"a": "ok", "ada@example.com": 1, "items": [{"bob@example.com": "x"}]}, schema)
    assert out["value"] == {"a": "ok", "[REDACTED:email]": 1, "items": [{"[REDACTED:email]": "x"}]}
    assert out["outcomes"][0]["action"] == "redact" and out["outcomes"][0]["match_counts"] == {"email": 2}
    collided = _apply_structured_segment(pii, {"ada@example.com": 1, "bob@example.com": 2}, schema)
    assert collided["value"] is None
    assert collided["outcomes"][-1]["action"] == "block" and collided["outcomes"][-1]["reason"] == KEY_COLLISION_REASON
    # A declared property name is never processed.
    declared = {"type": "object", "properties": {"ada@example.com": {"type": "integer"}}}
    assert _apply_structured_segment(pii, {"ada@example.com": 1}, declared)["value"] == {"ada@example.com": 1}


# --------------------------------------------------------------------------- review fixes


NAN_OUTPUT = '{"output": {"answer": "x", "score": NaN}}'  # json.loads in the scripted model gives a float nan
MAX_LENGTH = {"type": "max_length", "config": {"max_chars": 1000, "action": "truncate"}}


def usage_keys(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)})]


@pytest.mark.parametrize("processors", [[], [PII], [MAX_LENGTH]], ids=["none", "pii_redact", "max_length"])
def test_a_nan_candidate_is_retried_then_output_invalid(worker, client, tenant, processors):
    spec = structured(max_retries=1)
    if processors:
        spec["output_processors"] = processors
    run_id, row = run(worker, client, tenant, spec, NAN_OUTPUT)
    assert row["status"] == "failed", row
    events = event_rows(worker.engine, run_id)
    main = [k for k in keys(events) if not k.startswith("processor:")]
    assert main == ["start", "model:0:final", "output:0:retry", "model:1:final", "end"]
    if processors:  # processed as text (not a finite JSON object): one outcome per candidate
        assert [k for k in keys(events) if k.startswith("processor:")] == [
            "processor:output:0:model:0", "processor:output:0:model:1"]
    stored = [m["message"]["parts"][0]["args"] for m in by_type(events, "model_message")]
    assert all(isinstance(a, str) and "NaN" in a for a in stored) if processors else True
    [failed] = by_type(events, "run_failed")
    assert failed["error_code"] == "output_invalid", failed
    assert [r["errors"] for r in by_type(events, "output_retry")] == [["$.score"]]
    assert usage_keys(worker.engine, run_id) == ["model:0", "model:1"]


def test_an_undeclared_key_never_appears_in_error_paths(worker, client, tenant):
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
              "additionalProperties": True}
    email = "ada@example.com"
    errors = schema_errors(schema, {"answer": "x", email: float("nan")})
    assert errors == [("$", "must be a finite number")]
    nested = {"type": "object", "properties": {"a": {"type": "object", "additionalProperties": {"type": "integer"}}}}
    assert schema_errors(nested, {"a": {email: "no"}}) == [("$.a", "expected type integer")]
    assert email not in retry_message(errors)

    run_id, row = run(worker, client, tenant, structured(schema=schema, max_retries=1),
                      '{"output": {"answer": "x", "ada@example.com": NaN}}')
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert [r["errors"] for r in by_type(events, "output_retry")] == [["$"]]
    assert email not in json.dumps([d for _, k, t, d in events if t in ("output_retry", "run_failed")])


def test_malformed_output_arguments_are_processed_as_the_models_text():
    from pydantic_ai.messages import ToolCallPart

    from eap_worker.workflow import _args_text, _candidate_value, _structured_args

    bad = ToolCallPart("final_result", '{"answer": "ada@example.com"', tool_call_id="c")
    assert _structured_args(bad) is None and _args_text(bad) == '{"answer": "ada@example.com"'
    assert _candidate_value(AgentSpec.model_validate(structured()), [bad], "") is None
    nan = ToolCallPart("final_result", {"answer": "x", "score": float("nan")}, tool_call_id="d")
    assert _structured_args(nan) is None and "NaN" in _args_text(nan)
    ok = ToolCallPart("final_result", '{"answer": "x", "score": 1}', tool_call_id="e")
    assert _structured_args(ok) == {"answer": "x", "score": 1}


def test_structured_retrieve_numbers_two_calls_run_wide_and_cites_them(worker, client, kb_tenant):
    import re

    from worker_tests.test_retrieve_numbering import _two_docs

    kb = _two_docs(worker, kb_tenant)
    spec = with_output(rag_spec({"id": kb["id"], "top_k": 2}, model="retrieve-multi"))
    rounds = [["how many days do refunds take"], ["toner light blinks"]]
    run_id, row = run(worker, client, kb_tenant, spec, json.dumps({"rounds": rounds}))
    assert row["status"] == "completed", row
    events = event_rows(worker.engine, run_id)
    results = by_type(events, "tool_result")
    assert [[s["n"] for s in r["sources"]] for r in results] == [[1, 2], [3, 4]]  # the 2nd call starts at k+1
    assert {d["tool_name"] for d in by_type(events, "tool_call")} == {"retrieve"}
    answer = json.loads(row["output"])["answer"]
    by_n = {s["n"]: s for r in results for s in r["sources"]}
    cited = re.findall(r"\[(\d+)\] ([^:;]+): (\S+?)(?:;|$)", answer)
    assert [int(n) for n, _, _ in cited] == [1, 2, 3, 4]
    for n, title, ref in cited:
        assert (by_n[int(n)]["title"], by_n[int(n)]["ref"]) == (title, ref)
