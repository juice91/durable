"""P1.C1: the `post_turn_done` event at the end of a completed memory turn's post-turn steps
(`memory:post_turn:{turn}`, {observe, index}, no text), through the existing eap.append_event step."""

from __future__ import annotations

import uuid

import pytest
from dbos import DBOSClient

from eap_worker.post_turn import index_outcome, observe_outcome
from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_observational_memory import (  # noqa: F401 - tenant fixture (cleans observations)
    OBSERVE_RUN,
    POST_TURN_DONE,
    by_key,
    fact,
    obs_spec,
    obs_turn,
    tenant,
    wait_workflow,
)
from worker_tests.test_semantic_recall import RECALL_TURN, recall_spec, rturn
from worker_tests.test_working_memory import MEMORY_TURN, conversation, memory_spec, turn

DONE = "post_turn_done"


def _done(worker, run_id) -> list[dict]:
    return [d for _, k, t, d in event_rows(worker.engine, run_id) if t == DONE]


# --------------------------------------------------------------------------- unit


def test_observe_outcome_maps_the_observer_summary():
    assert observe_outcome(None, False) == "off"
    assert observe_outcome({"observe": None, "reflect": None}, True) == "skipped"
    assert observe_outcome({"observe": "created", "reflect": None}, True) == "created"
    assert observe_outcome({"observe": "existing", "reflect": None}, True) == "created"  # a re-executed write
    assert observe_outcome({"observe": None, "reflect": "created"}, True) == "created"
    assert observe_outcome({"observe": "created", "reflect": "failed"}, True) == "failed"
    assert observe_outcome({"observe": "failed", "reflect": None}, True) == "failed"
    assert observe_outcome({"observe": None, "reflect": None, "error": "internal_error"}, True) == "failed"
    assert observe_outcome({"observe": "conversation_deleted", "reflect": None}, True) == "skipped"
    assert observe_outcome(None, True) == "failed"


def test_index_outcome_maps_the_index_result():
    assert index_outcome(None, False) == "off"
    assert index_outcome(None, True) == "failed"  # the index step itself failed
    for status, out in (("indexed", "done"), ("existing", "done"), ("failed", "failed"), ("empty", "skipped"),
                        ("skipped", "skipped"), ("conversation_deleted", "skipped")):
        assert index_outcome({"status": status}, True) == out


# --------------------------------------------------------------------------- end to end


def test_observational_only_quiet_turn_reports_skipped_and_index_off(worker, client, worker_settings, tenant):
    conv = conversation(worker, tenant, uuid.uuid4())
    run_id, row = obs_turn(worker, client, worker_settings, tenant, conv, fact(1, 20), obs_spec())
    assert row["status"] == "completed"
    assert _done(worker, run_id) == [{"observe": "skipped", "index": "off"}]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys[-2:] == ["end", "memory:post_turn:1"]
    assert step_names(worker_settings.dbos_system_schema, run_id) == MEMORY_TURN + OBSERVE_RUN


def test_an_observation_reports_created(worker, client, worker_settings, tenant):
    conv = conversation(worker, tenant, uuid.uuid4())
    spec = obs_spec(keep=0)
    run_id, _ = obs_turn(worker, client, worker_settings, tenant, conv, fact(1, 1000), spec)
    events = event_rows(worker.engine, run_id)
    assert by_key(events, "memory:observe:1")
    assert _done(worker, run_id) == [{"observe": "created", "index": "off"}]


def test_a_failed_observer_reports_failed(worker, client, worker_settings, tenant):
    conv = conversation(worker, tenant, uuid.uuid4())
    spec = obs_spec(keep=0, observer={"provider": "test", "name": "observer-invalid"})
    run_id, _ = obs_turn(worker, client, worker_settings, tenant, conv, fact(1, 1000), spec)
    assert _done(worker, run_id) == [{"observe": "failed", "index": "off"}]


def test_recall_only_reports_index_done_and_observe_off(worker, client, worker_settings, tenant):
    conv = conversation(worker, tenant, uuid.uuid4())
    run_id, row = rturn(worker, client, worker_settings, tenant, conv, "I like green tea", recall_spec())
    assert row["status"] == "completed"
    assert _done(worker, run_id) == [{"observe": "off", "index": "done"}]
    assert step_names(worker_settings.dbos_system_schema, run_id) == RECALL_TURN + POST_TURN_DONE


def test_both_features_report_both(worker, client, worker_settings, tenant):
    conv = conversation(worker, tenant, uuid.uuid4())
    spec = recall_spec(observational=obs_spec()["memory"]["observational"])
    run_id, _ = rturn(worker, client, worker_settings, tenant, conv, "hello there", spec)
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert _done(worker, run_id) == [{"observe": "skipped", "index": "done"}]


@pytest.mark.parametrize("memory", [{"working": {}}, {"history": {"last_turns": 5}}])
def test_no_event_without_post_turn_features(worker, client, worker_settings, tenant, memory):
    conv = conversation(worker, tenant, uuid.uuid4())
    run_id, row = turn(worker, client, tenant, conv, "hello", memory_spec(**memory))
    assert row["status"] == "completed"
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert _done(worker, run_id) == []
    assert step_names(worker_settings.dbos_system_schema, run_id) == MEMORY_TURN  # the golden RM.12 layout


def test_a_crash_after_the_event_write_records_it_once(worker, tenant, crash_schema, tmp_path):  # noqa: F811
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    spec = obs_spec()
    executor_id = f"ptd-crash-{uuid.uuid4().hex[:8]}"
    env = _env(crash_schema, executor_id, EAP_CRASH_AT="after_event_write", EAP_CRASH_EVENT_KEY="memory:post_turn:*")
    first = WorkerProcess(env, tmp_path / "worker1.log")
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id, created = worker.run_store.create_turn(tenant, conv["id"], 1, "hi", uuid.uuid4())
        assert created
        enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="hi",
                    app_version=APP_VERSION, run_id=run_id, agent_id=agent, create_row=False)
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        assert [k for _, k, _, _ in event_rows(worker.engine, run_id)][-1] == "memory:post_turn:1"
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        wait_workflow(crash_schema, run_id, timeout=90)
        keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
        assert keys.count("memory:post_turn:1") == 1 and keys[-1] == "memory:post_turn:1"
        assert _done(worker, run_id) == [{"observe": "skipped", "index": "off"}]
    finally:
        first.stop()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()
