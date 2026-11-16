"""Task 16: a finished run's spans are written to the `spans` table by the worker, once."""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, enqueue_run, ticket_spec, wait_for, wait_terminal


def _spans_cleanup(worker, tenant):
    yield
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": tenant})


def _spans(engine, tenant, run_id) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM spans WHERE tenant_id = :t AND run_id = :r ORDER BY start, span_id"),
            {"t": tenant, "r": str(run_id)},
        ).mappings()
        return [dict(r) for r in rows]


def test_finished_agent_run_records_the_trace_spans(worker, client, tenant):
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="My printer is on fire",
        app_version=APP_VERSION,
    )
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    # the step runs inside the workflow, which returns after finishing: wait for the result
    client.retrieve_workflow(str(run_id)).get_result()
    rows = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="spans")
    kinds = [r["kind"] for r in rows]
    assert kinds.count("run") == 1 and kinds.count("model_request") == 2 and kinds.count("tool_call") == 1
    root = next(r for r in rows if r["kind"] == "run")
    assert root["parent_id"] is None and root["status"] == "ok" and root["end"] is not None
    assert {r["span_id"] for r in rows} == {f"run:{run_id}", f"{run_id}:model:0", f"{run_id}:model:1",
                                           next(r["span_id"] for r in rows if r["kind"] == "tool_call")}


def test_recording_twice_does_not_duplicate_rows(worker, client, tenant):
    from eap_worker.spans_step import materialise_spans

    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="again", app_version=APP_VERSION,
    )
    wait_terminal(worker.run_store, tenant, run_id)
    client.retrieve_workflow(str(run_id)).get_result()
    before = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="spans")
    assert asyncio.run(materialise_spans(str(run_id), tenant)) == len(before)
    assert len(_spans(worker.engine, tenant, run_id)) == len(before)


def test_a_failed_run_still_records_spans(worker, client, tenant):
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant,
        spec=ticket_spec(tools=("tickets.create", "payments.refund")), input_text="refund me",
        app_version=APP_VERSION,
    )
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    client.retrieve_workflow(str(run_id)).get_result()
    rows = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="spans of the failed run")
    assert [r["kind"] for r in rows] == ["run"] and rows[0]["status"] == "error"


def test_an_unfinished_run_records_nothing(worker, tenant):
    from uuid import uuid4

    from eap_worker.spans_step import materialise_spans

    run_id = uuid4()
    worker.run_store.create_run(tenant, uuid4(), 1, "queued", run_id)
    assert asyncio.run(materialise_spans(str(run_id), tenant)) == 0
    assert _spans(worker.engine, tenant, run_id) == []
