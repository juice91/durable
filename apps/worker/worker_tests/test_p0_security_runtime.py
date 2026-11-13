"""P0.21 security tests in the worker (in-process worker, real Postgres and DBOS, scripted models).

- Identity: `RunDeps.tenant_id` / `run_id` come from the run (the hand-off arguments), never from
  `request_context`, even when the row's `request_context` JSON is tampered with in SQL to carry
  `tenant_id`, `user_id` and `run_id`; undeclared `{{context.<identity>}}` placeholders are never
  substituted.
- Processor bypass: each conversation turn's input is checked on its own; a secret split across
  turns is not joined (documented scope); a later turn's history carries the earlier turn's
  PROCESSED text, never the raw text.
- Tool results are out of scope for processors (documented and asserted).
"""

from __future__ import annotations

import json
import uuid

from pydantic_ai import RunContext
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_conversation import start_turn
from worker_tests.test_processors_runtime import EMAIL, PII, run_messages, stored_text

FLAG = {"type": "regex_blocklist", "config": {"patterns": ["FLAG_ME"], "action": "block"}}
WHOAMI_TOOL = "p021.whoami"
LEAKY_TOOL = "p021.leaky"
LEAKY_TEXT = f"contact {EMAIL} about FLAG_ME"

CONTEXT_FIELDS = [{"name": "customer", "type": "string", "required": True}]
IDENTITY_INSTRUCTIONS = (
    "For {{context.customer}} / {{context.tenant_id}} / {{context.user_id}} / {{context.run_id}}."
)


def _context_spec(model: str, tools: list[str]) -> dict:
    return {
        **ticket_spec(tools=tuple(tools), model=model),
        "instructions": IDENTITY_INSTRUCTIONS,
        "context_fields": CONTEXT_FIELDS,
    }


def _register(worker, monkeypatch, tool_id, fn, description) -> None:
    if tool_id not in worker.registry.tool_ids():
        worker.registry.register(tool_id, fn, description=description)
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))


def _whoami(ctx: RunContext) -> str:
    return json.dumps(
        {
            "tenant_id": ctx.deps.tenant_id,
            "run_id": str(ctx.deps.run_id),
            "request_context": dict(ctx.deps.request_context),
        },
        sort_keys=True,
    )


def _leaky(ctx: RunContext) -> str:
    return LEAKY_TEXT


def _tampered_run(worker, client, tenant, spec) -> tuple[uuid.UUID, dict]:
    """Insert the run row as the API would (validated context), then overwrite its request_context
    in SQL with identity keys, then enqueue with the run's real tenant and run id."""
    store = worker.run_store
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    store.create_run(tenant, agent_id, 1, "who am I", run_id, request_context={"customer": "Ada"},
                     model_id=f"{spec['model']['provider']}:{spec['model']['name']}")
    tampered = {"customer": "Ada", "tenant_id": "t_evil_tenant", "user_id": "mallory", "run_id": str(uuid.uuid4()),
                "undeclared": "smuggled"}
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE runs SET request_context = CAST(:c AS jsonb) WHERE id = :r"),
                     {"c": json.dumps(tampered), "r": str(run_id)})
    enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text="who am I", app_version=APP_VERSION,
                run_id=run_id, agent_id=agent_id, create_row=False)
    return run_id, tampered


def _event_tenants(engine, run_id) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(
            text("SELECT DISTINCT tenant_id FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}
        ).scalars())


# --------------------------------------------------------------------------- identity


def test_tool_deps_identity_comes_from_the_run_even_with_a_tampered_request_context(worker, client, tenant, monkeypatch):
    """The tool sees `ctx.deps.tenant_id` / `run_id` of the run. The worker filters the stored
    context to the spec's declared fields (eap_worker.runtime.declared_context, defence in depth):
    the tampered row's reserved and undeclared keys never reach the tool, and nothing is written
    for another tenant."""
    _register(worker, monkeypatch, WHOAMI_TOOL, _whoami, "returns the run's identity and context")
    spec = _context_spec("first-tool", [WHOAMI_TOOL])
    run_id, tampered = _tampered_run(worker, client, tenant, spec)
    row = wait_terminal(worker.run_store, tenant, run_id)
    assert row["status"] == "completed", row["error"]
    (result,) = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    seen = json.loads(result["content"] if isinstance(result["content"], str) else json.dumps(result["content"]))
    assert seen["tenant_id"] == tenant
    assert seen["run_id"] == str(run_id)
    assert seen["request_context"] == {"customer": "Ada"}  # only the declared field
    assert _event_tenants(worker.engine, run_id) == {tenant}
    assert worker.run_store.get_run("t_evil_tenant", run_id) is None
    with worker.engine.connect() as conn:
        evil = conn.execute(text(
            "SELECT (SELECT count(*) FROM runs WHERE tenant_id = 't_evil_tenant')"
            " + (SELECT count(*) FROM execution_events WHERE tenant_id = 't_evil_tenant')"
            " + (SELECT count(*) FROM side_effects WHERE tenant_id = 't_evil_tenant')"
            " + (SELECT count(*) FROM run_model_requests WHERE tenant_id = 't_evil_tenant')"
        )).scalar_one()
    assert evil == 0


def test_identity_placeholders_are_not_substituted_from_a_tampered_request_context(worker, client, tenant):
    """`test:context` answers with its rendered instructions: only the declared field is
    substituted; `{{context.tenant_id}}` etc. stay as written although the tampered row has values."""
    spec = _context_spec("context", [])
    run_id, tampered = _tampered_run(worker, client, tenant, spec)
    row = wait_terminal(worker.run_store, tenant, run_id)
    assert row["status"] == "completed", row["error"]
    assert row["output"] == (
        "Instructions: For Ada / {{context.tenant_id}} / {{context.user_id}} / {{context.run_id}}."
    )
    for value in (tampered["tenant_id"], tampered["user_id"], tampered["run_id"]):
        assert value not in stored_text(worker, tenant, run_id)


# --------------------------------------------------------------------------- per-turn checks


def _turns(worker, client, tenant, spec, prompts) -> list[tuple[uuid.UUID, dict]]:
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="p021")
    out = []
    for prompt in prompts:
        run_id = start_turn(client, worker.run_store, tenant, conv["id"], prompt, spec=spec)
        out.append((run_id, wait_terminal(worker.run_store, tenant, run_id)))
    return out


def _processor_events(engine, run_id) -> list[tuple[str, str, dict]]:
    return [(k, d["action"], d["match_counts"]) for _, k, t, d in event_rows(engine, run_id) if t == "processor_result"]


def history_spec(**extra) -> dict:
    return {**ticket_spec(tools=(), model="history"), **extra}


def test_each_turn_is_checked_on_its_own_after_clean_turns(worker, client, tenant):
    spec = history_spec(input_processors=[PII, FLAG])
    turns = _turns(worker, client, tenant, spec, ["hello", f"mail {EMAIL}", "now FLAG_ME", "bye"])
    assert [r["status"] for _, r in turns] == ["completed", "completed", "failed", "completed"]
    (t1, _), (t2, r2), (t3, r3), (t4, r4) = turns
    assert _processor_events(worker.engine, t1) == [
        ("processor:input:0", "allow", {}), ("processor:input:1", "allow", {})
    ]
    assert _processor_events(worker.engine, t2) == [
        ("processor:input:0", "redact", {"email": 1}), ("processor:input:1", "allow", {})
    ]
    assert _processor_events(worker.engine, t3) == [
        ("processor:input:0", "allow", {}), ("processor:input:1", "block", {"pattern_0": 1})
    ]
    assert event_rows(worker.engine, t3)[-1][3]["error_code"] == "input_blocked"
    assert r2["output"] == "Turn 2. You said: hello | mail [REDACTED:email]. I answered 1 times before."
    # The blocked turn is not history; the redacted turn is, as its processed text.
    assert r4["output"] == "Turn 3. You said: hello | mail [REDACTED:email] | bye. I answered 2 times before."
    for run_id, _ in turns:
        assert EMAIL not in stored_text(worker, tenant, run_id)
        assert "FLAG_ME" not in stored_text(worker, tenant, run_id)


def test_a_secret_split_across_turns_is_not_joined_and_each_turn_is_checked_alone(worker, client, tenant):
    """Documented scope (Decisions: "Input processors apply to the turn's input"): processors see
    one turn's input, never a concatenation with earlier turns. Half an email per turn and
    `FLAG_` + `ME` therefore pass, while the whole value in one turn is caught. The model's history
    then holds both halves; that is the accepted limit of per-turn checks, recorded here."""
    local, domain = EMAIL.split("@")
    spec = history_spec(input_processors=[PII, FLAG])
    prompts = [f"{local}@", domain, "FLAG_", "ME", EMAIL, "FLAG_ME"]
    turns = _turns(worker, client, tenant, spec, prompts)
    assert [r["status"] for _, r in turns] == ["completed"] * 5 + ["failed"]
    for run_id, _ in turns[:4]:
        assert _processor_events(worker.engine, run_id) == [
            ("processor:input:0", "allow", {}), ("processor:input:1", "allow", {})
        ]
    assert _processor_events(worker.engine, turns[4][0])[0] == ("processor:input:0", "redact", {"email": 1})
    assert turns[4][1]["output"] == (
        f"Turn 5. You said: {local}@ | {domain} | FLAG_ | ME | [REDACTED:email]. I answered 4 times before."
    )
    assert event_rows(worker.engine, turns[5][0])[-1][3]["error_code"] == "input_blocked"


def test_later_turns_get_the_processed_text_of_earlier_turns_never_the_raw_text(worker, client, tenant):
    """Turn 1's raw input stays in `runs.input` (the deferred P0.17 follow-up), but history is built
    from the stored messages, which hold only the processed prompt; turn 2 has no processors at all,
    so anything raw in its history would reach its model and its stored answer."""
    guarded = history_spec(input_processors=[PII])
    conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="p021-history")
    t1 = start_turn(client, worker.run_store, tenant, conv["id"], f"Mail me at {EMAIL} please", spec=guarded)
    r1 = wait_terminal(worker.run_store, tenant, t1)
    assert r1["status"] == "completed", r1["error"]
    assert r1["input"] == f"Mail me at {EMAIL} please"  # raw input kept on the row (known, deferred)
    assert EMAIL not in json.dumps(run_messages(worker.engine, t1))

    unguarded = history_spec()  # the next turn's version has no processors
    t2 = start_turn(client, worker.run_store, tenant, conv["id"], "again", spec=unguarded)
    r2 = wait_terminal(worker.run_store, tenant, t2)
    assert r2["status"] == "completed", r2["error"]
    assert r2["output"] == "Turn 2. You said: Mail me at [REDACTED:email] please | again. I answered 1 times before."
    assert EMAIL not in stored_text(worker, tenant, t2)


# --------------------------------------------------------------------------- tool results


def test_tool_results_are_not_processed_by_input_or_output_processors(worker, client, tenant, monkeypatch):
    """Tool results are out of scope for processors. Per the Decisions table, "Input processors
    apply to the turn's input" and output processors run on "every model response that has text ...
    text that accompanies tool calls gets the deterministic output processors but not moderation".
    A tool's return value is neither: it is not the turn's input and not model text. So a tool
    result containing an email and FLAG_ME is stored raw in its `tool_result` event and in the
    run's messages (and so in later turns' history), and does not block the run; the processors
    check only the user input and the model's final text ("Done."). Tools must not return data
    the agent may not hold."""
    _register(worker, monkeypatch, LEAKY_TOOL, _leaky, "returns text with PII and a blocked word")
    spec = {
        **ticket_spec(tools=(LEAKY_TOOL,), model="first-tool"),
        "input_processors": [PII, FLAG],
        "output_processors": [PII, FLAG],
    }
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="look it up",
                         app_version=APP_VERSION)
    row = wait_terminal(worker.run_store, tenant, run_id)
    assert row["status"] == "completed", row["error"]
    assert row["output"] == "Done."
    events = event_rows(worker.engine, run_id)
    assert _processor_events(worker.engine, run_id) == [
        ("processor:input:0", "allow", {}),
        ("processor:input:1", "allow", {}),
        ("processor:output:0:model:1", "allow", {}),
        ("processor:output:1:model:1", "allow", {}),
    ]
    (result,) = [d for _, _, t, d in events if t == "tool_result"]
    assert LEAKY_TEXT in json.dumps(result)
    assert LEAKY_TEXT in json.dumps(run_messages(worker.engine, run_id))


def test_declared_context_keeps_only_declared_non_reserved_fields():
    """Unit check of the worker's filter: undeclared keys and reserved identity names are dropped,
    even when a (legacy or tampered) spec declares a reserved name."""
    from eap_schemas import AgentSpec
    from eap_worker.runtime import declared_context

    spec = AgentSpec.model_validate(_context_spec("context", []))
    stored = {"customer": "Ada", "tenant_id": "t_evil", "user_id": "u", "run_id": "r", "other": 1}
    assert declared_context(spec, stored) == {"customer": "Ada"}
    assert declared_context(spec, None) == {}
    legacy = spec.model_copy(update={"context_fields": [
        *spec.context_fields, spec.context_fields[0].model_copy(update={"name": "tenant_id"})]})
    assert declared_context(legacy, stored) == {"customer": "Ada"}
