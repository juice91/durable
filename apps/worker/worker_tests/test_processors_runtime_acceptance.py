"""P0.17 acceptance for the processors runtime, beyond the implementer's tests: moderation removed from
the allowlist mid-flight, chains that mix regex, PII and moderation (moderation in the middle), input
and output chains on one run, no NOTIFY deltas, a blocked turn left out of history, processors in a
workflow's child run, and matched text absent from events, logs, spans, errors and messages."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

import pytest

from eap_schemas.models_policy import ModelNotAllowed
from eap_worker.errors import OUTPUT_BLOCKED_MESSAGE
from worker_tests.conftest import APP_VERSION, event_rows, ticket_spec
from worker_tests.test_agent_run import DeltaListener
from worker_tests.test_conversation import start_turn
from worker_tests.test_processors_runtime import (
    BLOCK_SECRET, EMAIL, PII, echo_spec, keys_types, moderation, run, run_messages, stored_text, usage_keys,
)
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture
from worker_tests.conftest import wait_terminal

REDACT_FLAG = {"type": "regex_blocklist", "config": {"patterns": ["FLAG_ME"], "action": "redact"}}
REDACT_TOKEN = {"type": "regex_blocklist", "config": {"patterns": [r"tok-\d+"], "action": "redact"}}


class FlipPolicy:
    """An allowlist that permits `test:*` until the main model is first resolved (after the input
    chain), then forgets the moderation model: it is removed while the run is in flight."""

    entries = ("test:*",)

    def __init__(self) -> None:
        self.flipped = False

    def check(self, provider: str, name: str) -> None:
        if name == "echo":
            self.flipped = True
        elif name == "moderation" and self.flipped:
            raise ModelNotAllowed(provider, name)


# --------------------------------------------------------------------------- allowlist mid-flight


def test_moderation_model_removed_mid_flight_fails_closed_on_the_output_check(worker, client, tenant):
    spec = echo_spec(input_processors=[moderation()], output_processors=[moderation()])
    original, policy = worker.settings, FlipPolicy()
    worker.settings = replace(original, allowed_models=policy)
    try:
        run_id, row = run(worker, client, tenant, spec, "hello")
    finally:
        worker.settings = original
    assert policy.flipped
    assert row["status"] == "failed" and row["output"] is None and run_messages(worker.engine, run_id) is None
    assert row["error"] == f"OutputBlocked: {OUTPUT_BLOCKED_MESSAGE}"
    events = event_rows(worker.engine, run_id)
    assert keys_types(events) == [
        ("start", "run_started"), ("processor:input:0", "processor_result"),
        ("processor:output:0:model:0", "processor_result"), ("model:0:final", "model_message"), ("end", "run_failed"),
    ]
    assert events[1][3]["action"] == "allow"
    assert events[2][3] == {"phase": "output", "index": 0, "processor_type": "moderation", "action": "block",
                            "match_counts": {}, "reason": "model_not_allowed"}
    assert events[3][3]["blocked"] is True and events[3][3]["text"] == ""
    assert events[-1][3]["error_code"] == "output_blocked"
    # The input check was recorded and priced; the refused output check made no request.
    assert usage_keys(worker.engine, run_id) == ["model:0", "moderation:input:0"]


# --------------------------------------------------------------------------- chains


def test_chain_moderation_in_the_middle_sees_the_text_the_earlier_processors_produced(worker, client, tenant):
    # Redacting FLAG_ME before moderation means the moderator never sees it: the turn passes.
    spec = echo_spec(input_processors=[REDACT_FLAG, moderation(), REDACT_TOKEN, PII])
    run_id, row = run(worker, client, tenant, spec, f"FLAG_ME tok-42 {EMAIL}")
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Echo: [REDACTED:regex] [REDACTED:regex] [REDACTED:email]"
    events = event_rows(worker.engine, run_id)
    assert [k for _, k, _, _ in events] == [
        "start", "processor:input:0", "processor:input:1", "processor:input:2", "processor:input:3",
        "model:0:final", "end",
    ]
    assert [e[3]["processor_type"] for e in events[1:5]] == ["regex_blocklist", "moderation", "regex_blocklist",
                                                            "pii_redact"]
    assert [e[3]["index"] for e in events[1:5]] == [0, 1, 2, 3]
    assert events[2][3]["action"] == "allow" and "categories" not in events[2][3]
    assert usage_keys(worker.engine, run_id) == ["model:0", "moderation:input:1"]
    for needle in ("FLAG_ME", "tok-42", EMAIL):
        assert needle not in stored_text(worker, tenant, run_id)


def test_chain_moderation_first_flags_text_a_later_redactor_would_have_removed(worker, client, tenant):
    spec = echo_spec(input_processors=[moderation(), REDACT_FLAG, PII])
    run_id, row = run(worker, client, tenant, spec, f"FLAG_ME {EMAIL}")
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    # The chain stops at the block: no event for the processors after it, and no main model request.
    assert keys_types(events) == [("start", "run_started"), ("processor:input:0", "processor_result"),
                                  ("end", "run_failed")]
    assert events[1][3]["action"] == "block" and events[1][3]["categories"] == ["illicit"]
    assert events[0][3]["input"].startswith("[withheld")
    assert usage_keys(worker.engine, run_id) == ["moderation:input:0"]
    for needle in ("FLAG_ME", EMAIL):
        assert needle not in stored_text(worker, tenant, run_id)


def test_chain_block_after_an_allowed_moderation_still_records_the_moderation_once(worker, client, tenant):
    spec = echo_spec(input_processors=[PII, moderation(), BLOCK_SECRET])
    run_id, row = run(worker, client, tenant, spec, f"SECRET {EMAIL}")
    assert row["status"] == "failed" and row["error"].startswith("InputBlocked")
    events = event_rows(worker.engine, run_id)
    assert [k for _, k, _, _ in events] == ["start", "processor:input:0", "processor:input:1",
                                            "processor:input:2", "end"]
    assert [e[3]["action"] for e in events[1:4]] == ["redact", "allow", "block"]
    assert events[3][3]["match_counts"] == {"pattern_0": 1}
    assert usage_keys(worker.engine, run_id) == ["moderation:input:1"]
    assert "SECRET" not in stored_text(worker, tenant, run_id) and EMAIL not in stored_text(worker, tenant, run_id)


def test_pii_block_action_blocks_without_a_marker_and_without_the_value(worker, client, tenant):
    pii_block = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "block"}}
    run_id, row = run(worker, client, tenant, echo_spec(input_processors=[pii_block]), f"mail {EMAIL}")
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert events[1][3] == {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "block",
                            "match_counts": {"email": 1}}
    assert events[-1][3]["error_code"] == "input_blocked"
    assert EMAIL not in stored_text(worker, tenant, run_id)


def test_input_and_output_chains_on_one_run(worker, client, worker_settings, tenant):
    spec = echo_spec(input_processors=[REDACT_TOKEN, PII], output_processors=[REDACT_FLAG, moderation()])
    run_id, row = run(worker, client, tenant, spec, f"tok-7 FLAG_ME {EMAIL}")
    assert row["status"] == "completed", row["error"]
    # The model received the redacted input; the answer (its echo) has FLAG_ME redacted on output.
    assert row["output"] == "Echo: [REDACTED:regex] [REDACTED:regex] [REDACTED:email]"
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys == ["start", "processor:input:0", "processor:input:1", "processor:output:0:model:0", "processor:output:1:model:0",
                    "model:0:final", "end"]
    assert usage_keys(worker.engine, run_id) == ["model:0", "moderation:output:1:model:0"]
    assert run_messages(worker.engine, run_id)[-1]["parts"][0]["content"] == row["output"]
    for needle in ("tok-7", EMAIL):  # input-processed: gone everywhere
        assert needle not in stored_text(worker, tenant, run_id)
    # FLAG_ME is only redacted on output: the model's answer is stored without it.
    answers = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "model_message"]
    assert "FLAG_ME" not in json.dumps(answers) and "FLAG_ME" not in row["output"]
    assert "FLAG_ME" not in json.dumps(run_messages(worker.engine, run_id)[-1])


# --------------------------------------------------------------------------- deltas


@pytest.mark.parametrize("name,spec,streams", [
    ("none", echo_spec(), True),
    ("input_only", echo_spec(input_processors=[PII, moderation()]), True),
    ("output_pii", echo_spec(output_processors=[PII]), False),
    ("output_moderation", echo_spec(output_processors=[moderation()]), False),
    ("output_tool_run", ticket_spec() | {"output_processors": [PII]}, False),
    ("output_and_input", echo_spec(input_processors=[PII], output_processors=[PII]), False),
])
def test_deltas_are_published_only_for_specs_without_output_processors(worker, client, tenant, name, spec, streams):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, spec, f"streamed {name}")
        assert row["status"] == "completed", row["error"]
        time.sleep(0.6)  # NOTIFYs are asynchronous; give any stray one time to arrive
    finally:
        listener.close()
    assert bool(listener.for_run(run_id)) is streams


def test_no_delta_for_a_blocked_output(worker, client, tenant):
    listener = DeltaListener()
    try:
        spec = echo_spec(output_processors=[{"type": "regex_blocklist", "config": {"patterns": ["Echo"]}}])
        run_id, row = run(worker, client, tenant, spec, "secret answer")
        assert row["status"] == "failed"
        time.sleep(0.6)
    finally:
        listener.close()
    assert listener.for_run(run_id) == []


# --------------------------------------------------------------------------- history


def test_output_blocked_and_moderation_blocked_turns_are_left_out_of_the_next_turns_history(worker, client, tenant):
    spec = echo_spec(model="history", input_processors=[moderation()],
                     output_processors=[{"type": "regex_blocklist", "config": {"patterns": ["boom"]}}])
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="guarded")
    rows = []
    for prompt in ("hello", "FLAG_ME please", "boom now", "again"):
        run_id = start_turn(client, worker.run_store, tenant, conv["id"], prompt, spec=spec)
        rows.append(wait_terminal(worker.run_store, tenant, run_id))
    assert [r["status"] for r in rows] == ["completed", "failed", "failed", "completed"]
    assert rows[1]["error"].startswith("InputBlocked") and rows[2]["error"].startswith("OutputBlocked")
    assert rows[3]["output"] == "Turn 2. You said: hello | again. I answered 1 times before."
    # The blocked runs hold neither output nor messages; the next turn's stored messages hold no trace of them.
    for blocked in rows[1:3]:
        assert blocked["output"] is None
    assert "FLAG_ME" not in json.dumps(run_messages(worker.engine, rows[3]["id"]))
    assert "boom" not in json.dumps(run_messages(worker.engine, rows[3]["id"]))


# --------------------------------------------------------------------------- workflow child run


def _workflow(worker, tenant, agent_spec_extra):
    from worker_tests.workflow_run_helpers import agent_spec, publish_agent, publish_workflow

    agent_id = publish_agent(worker.engine, tenant, agent_spec() | agent_spec_extra)
    wf = {"name": "Guarded", "start": "ask",
          "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
          "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}]}
    return publish_workflow(worker.engine, tenant, wf)


def _run_workflow(worker, client, tenant, ids, q):
    from worker_tests.workflow_run_helpers import enqueue_workflow_run

    run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=ids[0], version_id=ids[1],
                                  input_value={"q": q}, app_version=APP_VERSION)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def test_workflow_agent_step_with_output_processors_and_moderation(worker, client, tenant):
    from worker_tests.workflow_run_helpers import cleanup_definitions

    try:
        ids = _workflow(worker, tenant, {"output_processors": [PII, moderation()]})
        ok_id, ok = _run_workflow(worker, client, tenant, ids, f"mail {EMAIL}")
        assert ok["status"] == "completed", ok["error"]
        assert ok["output"] == '"Echo: mail [REDACTED:email]"'
        child = uuid.UUID([e for e in event_rows(worker.engine, ok_id) if e[1] == "step:ask:started"][0][3]["child_run_id"])
        assert [k for _, k, _, _ in event_rows(worker.engine, child)] == [
            "start", "processor:output:0:model:0", "processor:output:1:model:0", "model:0:final", "end"]
        assert usage_keys(worker.engine, child) == ["model:0", "moderation:output:1:model:0"]
        assert EMAIL not in json.dumps([worker.run_store.get_run(tenant, child)["output"],
                                        run_messages(worker.engine, child)[-1:]])
        # An output block fails the step, and the workflow, with the block's code and no answer.
        bad_id, bad = _run_workflow(worker, client, tenant, ids, "FLAG_ME")
        assert bad["status"] == "failed"
        failed = [e for e in event_rows(worker.engine, bad_id) if e[1] == "step:ask:failed"][0][3]
        assert failed.get("error_code") == "output_blocked", failed
        assert "Echo" not in json.dumps(failed)
    finally:
        cleanup_definitions(worker.engine, tenant)


# --------------------------------------------------------------------------- spans, logs, errors


def _everything(spans, logs) -> str:
    """Every span (name, attributes, events) and log record (body, attributes) as one string."""
    out = []
    for s in spans:
        out.append(json.dumps({"name": s.name, "attrs": dict(s.attributes or {}),
                               "events": [(e.name, dict(e.attributes or {})) for e in s.events]}, default=str))
    for r in logs:
        out.append(json.dumps({"body": str(r.body), "attrs": dict(r.attributes or {})}, default=str))
    return "\n".join(out)


def _wait_spans(finished, run_id):
    from worker_tests.test_telemetry import _wait_for_run_span

    return _wait_for_run_span(finished, run_id)


@pytest.mark.parametrize("case", [
    ("input_blocked_by_moderation", {"input_processors": [PII, moderation()]}, f"FLAG_ME {EMAIL}"),
    ("input_blocked_by_regex", {"input_processors": [PII, BLOCK_SECRET]}, f"SECRET {EMAIL}"),
    ("input_redacted", {"input_processors": [PII]}, f"mail {EMAIL}"),
], ids=lambda c: c[0])
def test_input_matched_text_is_absent_from_spans_and_logs(worker, client, tenant, telemetry, caplog, case):  # noqa: F811
    _, extra, prompt = case
    caplog.set_level("DEBUG")
    run_id, row = run(worker, client, tenant, echo_spec(**extra), prompt)
    assert row["status"] in ("completed", "failed")
    _, finished = telemetry
    spans, logs = _wait_spans(finished, run_id)
    assert any(s.name == "eap.run" for s in spans)
    # A regex block ends the run before any model call; the others make at least one traced call.
    assert case[0] == "input_blocked_by_regex" or any(s.name.startswith("invoke_agent") for s in spans)
    blob = _everything(spans, logs) + caplog.text
    for needle in (EMAIL, "FLAG_ME", "SECRET"):
        if needle in prompt:
            assert needle not in blob, needle
    assert EMAIL not in stored_text(worker, tenant, run_id)


def test_output_matched_text_is_absent_from_processor_spans_and_logs(worker, client, tenant, telemetry, caplog):  # noqa: F811
    caplog.set_level("DEBUG")
    run_id, row = run(worker, client, tenant, echo_spec(output_processors=[PII, moderation()]), f"mail {EMAIL}")
    assert row["status"] == "completed"
    _, finished = telemetry
    spans, logs = _wait_spans(finished, run_id)
    assert any(s.name == "eap.run" for s in spans)
    assert EMAIL not in caplog.text and EMAIL not in "\n".join(str(r.body) for r in logs)
    names = [s.name for s in spans]
    assert any(n.startswith("chat scripted-moderation") for n in names) and "invoke_agent eap-moderator" in names  # the check was traced
    assert EMAIL not in _everything(spans, [])  # no span of the run, the moderation request included
    assert EMAIL not in stored_text(worker, tenant, run_id).replace(f"mail {EMAIL}", "")


def test_output_flagged_text_is_absent_from_spans_stored_error_and_messages(worker, client, tenant, telemetry, caplog):  # noqa: F811
    caplog.set_level("DEBUG")
    run_id, row = run(worker, client, tenant, echo_spec(output_processors=[moderation()]), "FLAG_ME")
    assert row["status"] == "failed" and row["output"] is None
    _, finished = telemetry
    spans, logs = _wait_spans(finished, run_id)
    assert "FLAG_ME" not in str(row["error"]) and "FLAG_ME" not in json.dumps(
        [e[3] for e in event_rows(worker.engine, run_id) if e[1] in ("model:0:final", "end", "processor:output:0:model:0")])
    assert run_messages(worker.engine, run_id) is None
    assert "FLAG_ME" not in caplog.text and "FLAG_ME" not in _everything(spans, [])
    assert any(s.name.startswith("chat scripted-moderation") for s in spans)
    # The answer "Echo: FLAG_ME" was the model's raw output: it must not be in any processor-related log.
    assert "FLAG_ME" not in "\n".join(str(r.body) for r in logs)


# --------------------------------------------------------------------------- per-response keys


def test_tool_run_checks_only_the_response_with_text_and_keys_it_by_request(worker, client, tenant):
    spec = ticket_spec() | {"output_processors": [PII, BLOCK_SECRET]}
    run_id, row = run(worker, client, tenant, spec, f"Laptop broken, mail {EMAIL}")
    assert row["status"] == "completed", row["error"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    # Request 0 is a tool call without text (nothing checked); request 1 is the answer.
    assert [k for k in keys if k.startswith("processor:")] == ["processor:output:0:model:1", "processor:output:1:model:1"]
    assert keys.index("processor:output:1:model:1") < keys.index("model:1:final")
    assert EMAIL not in json.dumps(run_messages(worker.engine, run_id)[-1])
    assert EMAIL not in json.dumps(row["output"])
