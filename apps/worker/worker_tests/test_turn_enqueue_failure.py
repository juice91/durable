"""A turn whose hand-off raised although DBOS accepted it (input hardening review, finding 1).

The API marks such a turn failed with ENQUEUE_FAILED_ERROR, and a failed turn is terminal, so a
later turn may start before a worker reaches the first turn's workflow. When it does, the start
step (under the conversation lock) must leave the first turn failed, and the workflow must stop
before any event, model call or tool. Without a later turn, the workflow revives the turn as before.
"""

from __future__ import annotations

import uuid

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows
from worker_tests.test_conversation import HISTORY_SPEC, step_names

from eap_run_store import ENQUEUE_FAILED_ERROR, TERMINAL_RUN_STATUSES


def _enqueue_existing_turn(client, store, tenant: str, conv: dict, run_id: uuid.UUID, input_text: str) -> None:
    enqueue_run(client, store, tenant_id=tenant, spec=HISTORY_SPEC, input_text=input_text, app_version=APP_VERSION,
                run_id=run_id, agent_id=uuid.UUID(conv["agent_id"]), create_row=False)


def test_overtaken_turn_does_not_run_when_its_accepted_workflow_starts(worker, client, worker_settings, tenant):
    store = worker.run_store
    conv, _ = store.create_conversation(tenant, uuid.uuid4(), title="overtaken")
    first, _ = store.create_turn(tenant, conv["id"], 1, "first", uuid.uuid4())
    # The API's enqueue raised (DBOS recorded it all the same): 503, and the turn is marked failed.
    assert store.mark_enqueue_failed(tenant, first)
    # The failed turn is terminal, so the client starts the next turn ...
    second, created = store.create_turn(tenant, conv["id"], 1, "second", uuid.uuid4())
    assert created
    # ... and only now does a worker reach the first turn's accepted workflow.
    _enqueue_existing_turn(client, store, tenant, conv, first, "first")

    result = client.retrieve_workflow(str(first)).get_result()
    assert result == {"status": "failed", "error": ENQUEUE_FAILED_ERROR, "error_code": "superseded"}
    run = store.get_run(tenant, first)
    assert (run["status"], run["error"], run["output"]) == ("failed", ENQUEUE_FAILED_ERROR, None)
    # No model call, no event, no tool: the start step is the only recorded step, and its output
    # (superseded=True) is what a replay of this workflow branches on.
    assert step_names(worker_settings.dbos_system_schema, first) == ["eap.start_run"]
    assert event_rows(worker.engine, first) == []
    # The conversation keeps exactly one active turn: the second.
    active = [t["id"] for t in store.list_turns(tenant, conv["id"]) if t["status"] not in TERMINAL_RUN_STATUSES]
    assert active == [str(second)]


def test_failed_turn_without_a_later_turn_is_revived_by_its_workflow(worker, client, tenant):
    store = worker.run_store
    conv, _ = store.create_conversation(tenant, uuid.uuid4(), title="revived")
    run_id, _ = store.create_turn(tenant, conv["id"], 1, "hello", uuid.uuid4())
    assert store.mark_enqueue_failed(tenant, run_id)
    _enqueue_existing_turn(client, store, tenant, conv, run_id, "hello")

    # The run is already terminal (failed) before the worker reaches it: wait for the workflow.
    assert client.retrieve_workflow(str(run_id)).get_result()["status"] == "completed"
    run = store.get_run(tenant, run_id)
    assert (run["status"], run["error"]) == ("completed", None)
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "model:0:final", "end"]
    assert store.create_turn(tenant, conv["id"], 1, "next", uuid.uuid4())[1]
