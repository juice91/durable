"""Task 17: `eap.score_run` scores a finished run with the named scorers and records `score` events."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from eap_run_store.scorers import ScorerStore
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_for, wait_terminal


def _enqueue_score(client, run_id, tenant, scorer_ids) -> str:
    workflow_id = f"score:{run_id}:{uuid.uuid4().hex}"
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.score_run", "workflow_id": workflow_id,
         "app_version": APP_VERSION},
        str(run_id), tenant, scorer_ids,
    )
    return workflow_id


def test_score_on_demand_records_score_events(worker, client, tenant):
    store = ScorerStore(worker.engine)
    try:
        s1 = store.create(tenant, "has-ticket", {"kind": "contains", "value": "ticket", "case_sensitive": False})
        s2 = store.create(tenant, "has-refund", {"kind": "contains", "value": "refund", "case_sensitive": False})
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="My printer is on fire",
            app_version=APP_VERSION,
        )
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        assert not [e for e in event_rows(worker.engine, run_id) if e[2] == "score"]  # no scorer attached to the agent

        workflow_id = _enqueue_score(client, run_id, tenant, [s1.id, s2.id])
        result = client.retrieve_workflow(workflow_id).get_result()
        assert result == {"written": 2}
        events = {e[1]: e[3] for e in event_rows(worker.engine, run_id) if e[2] == "score"}
        assert set(events) >= {f"score:{s1.id}", f"score:{s2.id}"}
        assert events[f"score:{s1.id}"]["passed"] is True and events[f"score:{s2.id}"]["passed"] is False

        again = client.retrieve_workflow(_enqueue_score(client, run_id, tenant, [s1.id])).get_result()
        assert again == {"written": 0}  # a repeat writes nothing twice
        assert len([e for e in event_rows(worker.engine, run_id) if e[2] == "score"]) == 2
    finally:
        with worker.engine.begin() as conn:
            conn.execute(text("DELETE FROM scorers WHERE tenant_id = :t"), {"t": tenant})


def test_score_on_demand_skips_unknown_scorers_and_unfinished_runs(worker, client, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "queued", run_id)
    workflow_id = _enqueue_score(client, run_id, tenant, [str(uuid.uuid4())])
    assert client.retrieve_workflow(workflow_id).get_result() == {"written": 0}
