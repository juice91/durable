"""AC.5 crash recovery (tester): kill the worker during a fallback attempt, right after the answered
request's event, and between check_budget and the model request; recover in a fresh process.
Afterwards: one run_completed (or the same failure), one usage row per request key, a gap-free seq,
the same answering model. Real subprocesses, always stopped in `finally`."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from dbos import DBOSClient

from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_limits_fallback import events_by_key, fallbacks, spec_with, usage_rows


def crash_and_recover(worker, tenant, crash_schema, tmp_path, spec, prompt, crash: dict[str, str]):
    """Crash once at `crash`, recover in a fresh process, return (run_id, terminal run row)."""
    executor_id = f"lf-crash-{uuid.uuid4().hex[:8]}"
    store = worker.run_store
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    procs: list[WorkerProcess] = []
    try:
        crashing = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash),
                                 tmp_path / "worker-crash.log")
        procs.append(crashing)
        crashing.wait_ready()
        run_id = enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text=prompt,
                             app_version=CRASH_APP_VERSION)
        assert crashing.wait_exit() == 137, (tmp_path / "worker-crash.log").read_text()
        assert store.get_run(tenant, run_id)["status"] == "running"
        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-recover.log")
        procs.append(recovering)
        return run_id, wait_terminal(store, tenant, run_id, timeout=90)
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()


def assert_clean(worker, run_id, *, completed: int) -> list[tuple[int, str, str, dict]]:
    events = event_rows(worker.engine, run_id)
    seqs = [e[0] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs  # gap-free
    assert len({e[1] for e in events}) == len(events)  # one event per key
    assert sum(1 for e in events if e[2] == "run_completed") == completed
    keys = [r["request_key"] for r in usage_rows(worker.engine, run_id)]
    assert len(keys) == len(set(keys)), keys  # no duplicate usage row
    return events


@pytest.mark.parametrize(
    "crash",
    [
        pytest.param({"EAP_CRASH_AT": "model_request"}, id="during-the-fallback-attempt"),
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "model:0:final"},
                     id="after-the-answered-request"),
    ],
)
def test_fallback_run_recovers_with_the_same_answering_model(worker, tenant, crash_schema, tmp_path, crash):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("priced"))
    run_id, row = crash_and_recover(worker, tenant, crash_schema, tmp_path, spec, "hello", crash)
    assert row["status"] == "completed" and row["output"] == "Echo: hello", (row["error"], row["output"])
    events = assert_clean(worker, run_id, completed=1)
    assert [e[1] for e in events] == ["start", "model:0:final", "end"]
    message = events_by_key(worker.engine, run_id)["model:0:final"][1]
    assert message["model_id"] == "test:priced" and message["fallback_index"] == 1
    assert [(r["request_key"], r["model_id"], r["cost_usd"]) for r in usage_rows(worker.engine, run_id)] == [
        ("model:0", "test:priced", Decimal("0.02"))]


def priced_spec(cap: str) -> dict[str, Any]:
    return spec_with(model="priced", tools=("clock.now",), limits={"max_cost_usd": cap})


@pytest.mark.parametrize(("cap", "completed"), [("0.0401", 1), ("0.05", 1)])
def test_crash_between_check_budget_and_the_request_decides_identically(
    worker, tenant, crash_schema, tmp_path, cap, completed
):
    crash = {"EAP_CRASH_AT": "after_check_budget", "EAP_CRASH_EVENT_KEY": "budget:1"}
    run_id, row = crash_and_recover(worker, tenant, crash_schema, tmp_path, priced_spec(cap), '{"rounds": 2}', crash)
    assert row["status"] == "completed", (row["error"], row["output"])
    assert_clean(worker, run_id, completed=completed)
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1", "model:2"]


def test_crash_after_check_budget_then_the_cap_refuses_the_same_way(worker, tenant, crash_schema, tmp_path):
    """Cap 0.04: request 1 passes (0.02 < 0.04), request 2 is refused (0.04 >= 0.04), crash or not."""
    crash = {"EAP_CRASH_AT": "after_check_budget", "EAP_CRASH_EVENT_KEY": "budget:1"}
    run_id, row = crash_and_recover(worker, tenant, crash_schema, tmp_path, priced_spec("0.04"), '{"rounds": 2}', crash)
    assert row["status"] == "failed" and row["error"] == "RunBudgetExceeded: the run reached its cost limit"
    assert_clean(worker, run_id, completed=0)
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert (end["error_code"], Decimal(end["spent_usd"])) == ("run_budget_exceeded", Decimal("0.04"))
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1"]
