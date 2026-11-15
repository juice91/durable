"""Loops D12 in the worker (LP.8): the scheduled `eap.cancel_reconcile`, the start steps of tool runs
stopping on a terminal run, and the finish/append steps accepting dropped/refused writes.
Focused implementer tests (in-process worker; the API is not involved)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

# Imported at collection, so the workflow is registered before the session worker launches DBOS.
from eap_worker.cancel_reconcile import (
    CANCEL_RECONCILE_SCHEDULE_NAME,
    CancelReconcileConfigError,
    ReconcileConfig,
    apply_cancel_reconcile_schedule,
    cancel_reconcile,
)
from eap_worker.workflow import append_event_step, finish_run_step
from worker_tests.conftest import APP_VERSION, event_rows, wait_for
from worker_tests.tool_run_helpers import enqueue_tool_run

# --------------------------------------------------------------------------- configuration


def test_config_defaults_env_off_and_bad_values():
    assert ReconcileConfig.from_env({}) == ReconcileConfig("0 * * * * *", 30, 100, False)
    cfg = ReconcileConfig.from_env({
        "EAP_CANCEL_RECONCILE_SCHEDULE": "*/5 * * * * *",
        "EAP_CANCEL_RECONCILE_AFTER_SECONDS": "2",
        "EAP_CANCEL_RECONCILE_LIMIT": "7",
    })
    assert (cfg.schedule, cfg.after_seconds, cfg.limit) == ("*/5 * * * * *", 2, 7)
    assert cfg.to_context() == {"after_seconds": 2, "limit": 7}
    assert ReconcileConfig.from_env({"EAP_CANCEL_RECONCILE_SCHEDULE": "OFF"}).off is True
    for env in ({"EAP_CANCEL_RECONCILE_AFTER_SECONDS": "0"}, {"EAP_CANCEL_RECONCILE_LIMIT": "x"},
                {"EAP_CANCEL_RECONCILE_LIMIT": "1001"}):
        with pytest.raises(CancelReconcileConfigError):
            ReconcileConfig.from_env(env)
    err = CancelReconcileConfigError("EAP_CANCEL_RECONCILE_LIMIT", "x")
    import pickle

    assert str(pickle.loads(pickle.dumps(err))) == str(err)


def test_context_tolerates_garbage():
    assert ReconcileConfig.from_context(None) == ReconcileConfig()
    assert ReconcileConfig.from_context({"after_seconds": -1, "limit": "9"}) == ReconcileConfig()
    assert ReconcileConfig.from_context({"after_seconds": 5, "limit": 9}).to_context() == {"after_seconds": 5, "limit": 9}


# --------------------------------------------------------------------------- the sweep


def _backdate_cancel(engine, run_id, seconds: int = 120) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE execution_events SET ts = ts - make_interval(secs => :s) WHERE run_id = :r AND event_key = 'cancel'"),
            {"s": seconds, "r": str(run_id)},
        )


def test_sweep_finishes_an_unfinished_cancel_and_its_dbos_tree(worker, client, tenant):
    from dbos import DBOS

    run_id, child_id, fresh_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run_id)
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", fresh_id)
    worker.run_store.create_child_agent_run(tenant, uuid.uuid4(), 1, "hi", child_id, run_id, "ask")
    # The run's DBOS workflow, enqueued for an app version no worker serves: it stays ENQUEUED.
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run", "workflow_id": str(run_id),
         "app_version": "never-served"},
        str(run_id), tenant, str(uuid.uuid4()), "{}",
    )
    for rid in (run_id, fresh_id):
        worker.event_log.append(tenant, rid, "cancel", "cancel_requested", {"requested_by": "alice"})
    _backdate_cancel(worker.engine, run_id)  # the API "died" after the request; fresh_id is too new

    tick = datetime.now(timezone.utc)
    summary = DBOS.start_workflow(cancel_reconcile, tick, {"after_seconds": 30, "limit": 1000}).get_result()
    assert summary["finished"] >= 1 and summary["failed"] == 0

    for rid in (run_id, child_id):
        run = worker.run_store.get_run(tenant, rid)
        assert run["status"] == "failed", rid
        ends = [e for e in event_rows(worker.engine, rid) if e[1] == "end"]
        assert len(ends) == 1 and ends[0][3]["error_code"] == "run_cancelled"
    assert client.retrieve_workflow(str(run_id)).get_status().status == "CANCELLED"
    # A cancel newer than the threshold is left to the API.
    assert worker.run_store.get_run(tenant, fresh_id)["status"] == "queued"
    assert [e[1] for e in event_rows(worker.engine, fresh_id)] == ["cancel"]
    # The next tick no longer lists the finished run.
    listed = {r["run_id"] for r in worker.run_store.find_unfinished_cancels(limit=1000)}
    assert str(run_id) not in listed


def test_schedule_is_applied_and_removed(worker):
    from dbos import DBOS

    try:
        assert apply_cancel_reconcile_schedule(ReconcileConfig(schedule="*/30 * * * * *", after_seconds=5, limit=10)) == "applied"
        stored = DBOS.get_schedule(CANCEL_RECONCILE_SCHEDULE_NAME)
        assert stored["schedule"] == "*/30 * * * * *" and stored["context"] == {"after_seconds": 5, "limit": 10}
        assert apply_cancel_reconcile_schedule(ReconcileConfig(off=True)) == "removed"
        assert DBOS.get_schedule(CANCEL_RECONCILE_SCHEDULE_NAME) is None
    finally:
        DBOS.delete_schedule(CANCEL_RECONCILE_SCHEDULE_NAME)


# --------------------------------------------------------------------------- worker steps


def test_a_terminal_tool_run_is_not_run(worker, client, tenant):
    """mark_running refuses a terminal run (D12): the workflow writes nothing and calls no tool."""
    run_id = uuid.uuid4()
    worker.run_store.create_tool_run(tenant, "tickets.create", '{"body":"y","title":"x"}', run_id)
    assert worker.run_store.finish_with_end(tenant, run_id, "failed", None, "RunCancelled: the run was cancelled")
    enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "x", "body": "y"}, app_version=APP_VERSION, run_id=run_id, create_row=False,
    )
    result = wait_for(
        lambda: client.retrieve_workflow(str(run_id)).get_status().status == "SUCCESS"
        and client.retrieve_workflow(str(run_id)).get_result(),
        timeout=60, what="the tool run workflow to return",
    )
    assert result == {"status": "failed", "error": "RunCancelled: the run was cancelled", "error_code": "run_ended"}
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["end"]
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 0


def test_append_and_finish_steps_accept_dropped_and_refused(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run_id)
    worker.event_log.append(tenant, run_id, "start", "run_started", {})
    worker.run_store.cancel_run_tree(tenant, run_id)
    end_seq = [e[0] for e in event_rows(worker.engine, run_id) if e[1] == "end"][0]

    async def late_finish() -> int:
        seq = await append_event_step(str(run_id), tenant, "step:a:completed", "step_completed", {"output": 1})
        again = await append_event_step(str(run_id), tenant, "end", "run_completed", {"output": "x"})
        await finish_run_step(str(run_id), tenant, "completed", "x", None)
        assert again == seq
        return seq

    assert asyncio.run(late_finish()) == end_seq
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "end"]
    run = worker.run_store.get_run(tenant, run_id)
    assert run["status"] == "failed" and run["output"] is None


def test_a_terminal_kb_ingest_run_is_not_run(worker, client, tenant):
    """Same for kb_ingest: no event, the document is never marked ingesting, nothing embedded."""
    from eap_run_store import KnowledgeStore

    from worker_tests.kb_ingest_helpers import (
        chunk_stats, cleanup_knowledge, create_kb, enqueue_kb_ingest, ingest_input, markdown_doc, upload,
    )

    store = KnowledgeStore(worker.engine)
    try:
        kb_id = create_kb(store, tenant)
        document_id, version, run_id = upload(store, tenant, kb_id, markdown_doc(2))
        worker.run_store.create_kb_ingest_run(tenant, kb_id, ingest_input(document_id, version), run_id)
        assert worker.run_store.finish_with_end(tenant, run_id, "failed", None, "RunCancelled: the run was cancelled")
        enqueue_kb_ingest(
            client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=ingest_input(document_id, version),
            app_version=APP_VERSION, run_id=run_id,
        )
        result = wait_for(
            lambda: client.retrieve_workflow(str(run_id)).get_status().status == "SUCCESS"
            and client.retrieve_workflow(str(run_id)).get_result(),
            timeout=60, what="the ingest workflow to return",
        )
        assert result["error_code"] == "run_ended" and result["status"] == "failed"
        assert [e[1] for e in event_rows(worker.engine, run_id)] == ["end"]
        assert chunk_stats(worker.engine, tenant, document_id) == []
    finally:
        cleanup_knowledge(worker.engine, tenant)
