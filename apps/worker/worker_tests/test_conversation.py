"""Multi-turn conversations through the real worker: each turn's agent run receives the conversation's
previous COMPLETED turns as Pydantic AI message_history, loaded inside a DBOS step.

The `test:history` scripted model answers with what it received:
    "Turn {k}. You said: p1 | p2 | ... | pk. I answered {m} times before."
where p1..pk are every user prompt in the messages it was given and m the number of model
responses among them.
"""

from __future__ import annotations

import uuid

import psycopg
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, UserPromptPart

from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, enqueue_run, event_rows, ticket_spec, wait_terminal

HISTORY_SPEC = ticket_spec(tools=(), model="history")


def start_turn(client, store, tenant: str, conversation_id: str, input_text: str, spec=HISTORY_SPEC) -> uuid.UUID:
    """What POST /conversations/{id}/runs does: insert the numbered turn, then enqueue (same 6 args)."""
    conv = store.get_conversation(tenant, conversation_id)
    run_id, created = store.create_turn(tenant, conversation_id, 1, input_text, uuid.uuid4())
    assert created
    enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text=input_text, app_version=APP_VERSION,
                run_id=run_id, agent_id=uuid.UUID(conv["agent_id"]), create_row=False)
    return run_id


def step_names(schema: str, run_id, *, with_spans: bool = False) -> list[str]:
    """The run's DBOS step names in order. `eap.record_spans`, the last step of every top-level agent
    run (it can still be pending when the run's status turns terminal), is left out unless
    `with_spans`: the layout tests pin the steps a feature adds, and only the exact-sequence test of
    test_processors_runtime pins this one."""
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        names = [
            r[0]
            for r in conn.execute(
                f'SELECT function_name FROM "{schema}".operation_outputs WHERE workflow_uuid = %s ORDER BY function_id',
                (str(run_id),),
            )
        ]
    return names if with_spans else [n for n in names if n != "eap.record_spans"]


def test_turn_three_sees_turns_one_and_two_as_message_history(worker, client, worker_settings, tenant):
    store = worker.run_store
    conv, _ = store.create_conversation(tenant, uuid.uuid4(), title="memory")
    prompts = ["My name is Ada.", "I live in Lisbon.", "Where do I live and what is my name?"]
    run_ids = []
    outputs = []
    for prompt in prompts:
        run_id = start_turn(client, store, tenant, conv["id"], prompt)
        run = wait_terminal(store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        run_ids.append(run_id)
        outputs.append(run["output"])

    assert outputs == [
        "Turn 1. You said: My name is Ada.. I answered 0 times before.",
        "Turn 2. You said: My name is Ada. | I live in Lisbon.. I answered 1 times before.",
        "Turn 3. You said: My name is Ada. | I live in Lisbon. | Where do I live and what is my name?. "
        "I answered 2 times before.",
    ]

    # Each turn stores only its own new messages (no copy of the history): one request, one response.
    for run_id, prompt, output in zip(run_ids, prompts, outputs):
        stored = store.completed_turns_before(tenant, conv["id"], 99)
        row = next(t for t in stored if t["run_id"] == str(run_id))
        messages = ModelMessagesTypeAdapter.validate_python(row["messages"])
        assert [type(m) for m in messages] == [ModelRequest, ModelResponse]
        assert [p.content for p in messages[0].parts if isinstance(p, UserPromptPart)] == [prompt]
        assert messages[1].parts[0].content == output

        # Durable events stay per run: contiguous seq from 0, unique keys, the same story every turn.
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        assert [e[1] for e in events] == ["start", "model:0:final", "end"]

    # The history is read in its own DBOS step (so recovery replays it), once per turn.
    steps = step_names(worker_settings.dbos_system_schema, run_ids[2])
    assert steps.count("eap.load_history") == 1
    assert steps.index("eap.load_history") < steps.index("eap-agent__model.request_stream")


def test_failed_turns_are_not_part_of_the_history(worker, client, tenant):
    store = worker.run_store
    conv, _ = store.create_conversation(tenant, uuid.uuid4())
    first = start_turn(client, store, tenant, conv["id"], "Remember: blue.")
    assert wait_terminal(store, tenant, first)["status"] == "completed"
    # An unknown tool fails the run before any model request.
    broken = start_turn(client, store, tenant, conv["id"], "Forget it.", spec=ticket_spec(tools=("nope.missing",), model="history"))
    assert wait_terminal(store, tenant, broken)["status"] == "failed"
    third = start_turn(client, store, tenant, conv["id"], "What colour?")
    run = wait_terminal(store, tenant, third)
    assert run["output"] == "Turn 2. You said: Remember: blue. | What colour?. I answered 1 times before."


def test_a_run_without_a_conversation_keeps_the_milestone_1_step_layout(worker, client, worker_settings, tenant):
    store = worker.run_store
    run_id = enqueue_run(client, store, tenant_id=tenant, spec=HISTORY_SPEC, input_text="solo", app_version=APP_VERSION)
    run = wait_terminal(store, tenant, run_id)
    assert run["output"] == "Turn 1. You said: solo. I answered 0 times before."
    assert "eap.load_history" not in step_names(worker_settings.dbos_system_schema, run_id)
