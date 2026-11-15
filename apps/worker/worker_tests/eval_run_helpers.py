"""Helpers for the AC.11 eval runtime tests: publish an agent, build a suite, start an eval run as
the API does (RunStore.create_eval_run, then enqueue `eap.eval_run(run_id, tenant_id, input_json)`)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from eap_run_store.evals import EvalStore

JUDGE = {"provider": "test", "name": "judge"}


def create_suite(engine, tenant_id: str, agent_id: str, cases: list[dict[str, Any]],
                 evaluators: list[dict[str, Any]], name: str = "Triage") -> str:
    store = EvalStore(engine)
    suite = store.create_suite(tenant_id, agent_id, name, evaluators=evaluators)
    for case in cases:
        store.add_case(tenant_id, suite.id, case)
    return str(suite.id)


def start_eval(engine, run_store, client, *, tenant_id: str, agent_id: str, suite_id: str, app_version: str,
               version: int | None = 1, draft_spec: dict[str, Any] | None = None, max_concurrency: int = 4,
               input_json: str | None = None) -> uuid.UUID:
    snapshot = EvalStore(engine).snapshot(tenant_id, suite_id, agent_id=agent_id, max_concurrency=max_concurrency)
    run_id = uuid.uuid4()
    stored = snapshot.canonical_json()
    run_store.create_eval_run(tenant_id, agent_id, version, suite_id, stored, run_id, draft_spec=draft_spec)
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
         "app_version": app_version},
        str(run_id), tenant_id, input_json if input_json is not None else stored,
    )
    return run_id


def results(engine, tenant_id: str, run_id) -> list[Any]:
    return EvalStore(engine).list_case_results(tenant_id, run_id)


def usage_keys(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            text("SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key"),
            {"r": str(run_id)},
        )]


def child_runs(engine, run_id) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(
            text("SELECT id, parent_step_id, conversation_id, status FROM runs WHERE parent_run_id = :r"),
            {"r": str(run_id)},
        ))


def cleanup_evals(engine, tenant_id: str) -> None:
    from worker_tests.conftest import cleanup_tenant

    cleanup_tenant(engine, tenant_id)  # runs first: eval runs reference their suite
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM eval_suites WHERE tenant_id = :t"), {"t": tenant_id})
