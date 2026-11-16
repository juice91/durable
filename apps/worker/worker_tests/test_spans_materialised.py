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


def test_the_step_itself_is_idempotent(worker, client, tenant):
    from eap_worker.spans_step import record_spans_step

    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="step", app_version=APP_VERSION,
    )
    wait_terminal(worker.run_store, tenant, run_id)
    client.retrieve_workflow(str(run_id)).get_result()
    before = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="spans")
    for _ in range(2):  # the @DBOS.step, as the workflow calls it
        assert asyncio.run(record_spans_step(str(run_id), tenant)) == len(before)
    assert [r["span_id"] for r in _spans(worker.engine, tenant, run_id)] == [r["span_id"] for r in before]


def test_a_recording_failure_leaves_the_run_outcome_unchanged(worker, client, tenant, monkeypatch):
    from eap_run_store.spans import SpanStore

    def boom(self, *args, **kwargs):
        raise RuntimeError("spans table unavailable")

    monkeypatch.setattr(SpanStore, "replace_for_run", boom)
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="swallow", app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    result = client.retrieve_workflow(str(run_id)).get_result()
    assert run["status"] == "completed" and result["status"] == "completed" and run["output"]
    assert _spans(worker.engine, tenant, run_id) == []


def test_a_tool_run_records_its_spans(worker, client, tenant):
    from worker_tests.tool_run_helpers import enqueue_tool_run

    run_id = enqueue_tool_run(
        client, worker.run_store, tenant_id=tenant, tool_id="tickets.create",
        args={"title": "Printer", "body": "on fire"}, app_version=APP_VERSION,
    )
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    client.retrieve_workflow(str(run_id)).get_result()
    rows = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="tool run spans")
    assert [r["kind"] for r in rows].count("run") == 1 and "tool_call" in {r["kind"] for r in rows}


def test_a_workflow_run_records_its_spans_with_steps(worker, client, tenant):
    from worker_tests.workflow_run_helpers import (
        agent_spec, cleanup_definitions, demo_spec, enqueue_workflow_run, publish_agent, publish_workflow,
    )

    try:
        agent_id = publish_agent(worker.engine, tenant, agent_spec())
        workflow_id, version_id = publish_workflow(worker.engine, tenant, demo_spec(agent_id))
        run_id = enqueue_workflow_run(
            client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={"q": "hello"}, app_version=APP_VERSION,
        )
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        client.retrieve_workflow(str(run_id)).get_result()
        rows = wait_for(lambda: _spans(worker.engine, tenant, run_id), what="workflow spans")
        kinds = [r["kind"] for r in rows]
        assert kinds.count("workflow_step") >= 3 and "child_run" in kinds
        assert {r["workflow_id"] for r in rows} == {str(workflow_id)}
        children = wait_for(
            lambda: worker.run_store.list_runs(tenant, parent_run_id=run_id), what="child run"
        )
        assert _spans(worker.engine, tenant, children[0]["id"]) == []  # a child run has no rows of its own
    finally:
        cleanup_definitions(worker.engine, tenant)


def test_the_cancel_reconciler_records_the_cancelled_runs_spans(worker, tenant):
    from uuid import uuid4

    from eap_worker.cancel_reconcile import finish_cancel_step

    run_id = uuid4()
    worker.run_store.create_run(tenant, uuid4(), 1, "cancelled", run_id)
    worker.run_store.mark_running(tenant, run_id)
    assert finish_cancel_step(tenant, str(run_id))["status"] == "failed"
    rows = _spans(worker.engine, tenant, run_id)
    assert [r["kind"] for r in rows] == ["run"] and rows[0]["status"] == "error"


def _cancelled_by_the_api(worker, tenant, *, age_hours: int = 0):
    """A running top-level run ended by RunStore.cancel_run_tree, as POST /runs/{id}/cancel does."""
    from uuid import uuid4

    run_id = uuid4()
    worker.run_store.create_run(tenant, uuid4(), 1, "cancelled", run_id)
    worker.run_store.mark_running(tenant, run_id)
    worker.run_store.cancel_run_tree(tenant, run_id)
    if age_hours:
        with worker.engine.begin() as conn:
            conn.execute(text("UPDATE runs SET updated_at = now() - make_interval(hours => :h) WHERE id = :r"),
                         {"h": age_hours, "r": str(run_id)})
    return run_id


def test_the_reconcile_pass_records_spans_of_runs_the_api_cancelled(worker, tenant):
    from eap_worker.cancel_reconcile import record_missing_spans_step

    first = _cancelled_by_the_api(worker, tenant)
    second = _cancelled_by_the_api(worker, tenant)
    old = _cancelled_by_the_api(worker, tenant, age_hours=24)
    assert _spans(worker.engine, tenant, first) == []
    # bounded per tick, oldest first; other tenants' pending runs may share the table, so count ours
    recorded = record_missing_spans_step(500)
    assert recorded >= 2
    assert [r["kind"] for r in _spans(worker.engine, tenant, first)] == ["run"]
    assert [r["status"] for r in _spans(worker.engine, tenant, second)] == ["error"]
    assert _spans(worker.engine, tenant, old) == []  # ended long ago: no backfill
    again = record_missing_spans_step(500)
    assert len(_spans(worker.engine, tenant, first)) == 1 and again < recorded  # idempotent


def test_the_reconcile_pass_is_bounded_per_tick(worker, tenant):
    from eap_worker.cancel_reconcile import record_missing_spans_step

    runs = [_cancelled_by_the_api(worker, tenant) for _ in range(3)]
    assert record_missing_spans_step(1) == 1
    done = [r for r in runs if _spans(worker.engine, tenant, r)]
    assert len(done) <= 1


def test_a_failing_span_write_leaves_the_cancel_finish_and_the_pass_intact(worker, tenant, monkeypatch):
    from uuid import uuid4

    from eap_worker import cancel_reconcile

    def boom(*args, **kwargs):
        raise RuntimeError("spans table unavailable")

    monkeypatch.setattr(cancel_reconcile, "materialise_spans", boom)
    run_id = uuid4()
    worker.run_store.create_run(tenant, uuid4(), 1, "cancelled", run_id)
    worker.run_store.mark_running(tenant, run_id)
    assert cancel_reconcile.finish_cancel_step(tenant, str(run_id))["status"] == "failed"
    assert worker.run_store.get_run(tenant, run_id)["status"] == "failed"
    assert cancel_reconcile.record_missing_spans_step(50) == 0  # swallowed, the next tick retries
    assert _spans(worker.engine, tenant, run_id) == []
