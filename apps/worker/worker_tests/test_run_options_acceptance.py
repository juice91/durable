"""P0.11 acceptance: run options in the worker (adds to test_run_options.py, no duplicates).

A real tool reading ctx.deps.request_context; a model removed from the allowlist after the run
started is refused; a published-version crash with an override and context recovers on the recorded
model and context; context values never reach spans or logs.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid

import pytest
from dbos import DBOSClient
from pydantic_ai import RunContext
from sqlalchemy import text

from eap_schemas.models_policy import ModelPolicy
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, drop_schema, enqueue_run, event_rows, new_schema, wait_terminal
from worker_tests.test_telemetry import _wait_for_run_span, telemetry  # noqa: F401 - fixture

SECRET = "Zq9xCtxSecret"
CONTEXT_SPEC = {
    "spec_version": 1,
    "name": "Context agent",
    "instructions": "Greet {{context.customer}} on the {{context.plan}} plan.",
    "model": {"provider": "test", "name": "echo"},
    "tools": [],
    "model_settings": {},
    "context_fields": [
        {"name": "customer", "type": "string", "required": True},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
    ],
}


def _create(store, tenant, input_text="hi", **options):
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    store.create_run(tenant, agent_id, options.pop("version", 1), input_text, run_id, **options)
    return run_id, agent_id


def _enqueue(client, store, tenant, spec, run_id, agent_id, version=1, app_version=APP_VERSION):
    return enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text="hi", app_version=app_version,
                       run_id=run_id, agent_id=agent_id, version=version, create_row=False)


def _request_models(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            text("SELECT model_id FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)})]


# --------------------------------------------------------------------------- a real tool reads the context


def test_a_real_tool_reads_ctx_deps_request_context(worker, client, tenant, monkeypatch):
    tool_id = "acceptance.read_context"

    def read_context(ctx: RunContext) -> str:
        return json.dumps(dict(ctx.deps.request_context), sort_keys=True)

    if tool_id not in worker.registry.tool_ids():
        worker.registry.register(tool_id, read_context, description="returns the request context")
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))
    spec = {**CONTEXT_SPEC, "tools": [tool_id], "model": {"provider": "test", "name": "first-tool"}}
    ctx = {"customer": "Ada", "plan": "pro"}
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, request_context=ctx, model_id="test:first-tool")
    _enqueue(client, store, tenant, spec, run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    (result,) = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert result["status"] == "ok"
    assert json.loads(result["content"] if isinstance(result["content"], str) else json.dumps(result["content"])) == ctx


# --------------------------------------------------------------------------- allowlist re-check at start


def test_model_removed_from_the_allowlist_after_run_start_is_refused(worker, client, tenant, monkeypatch):
    """The override was allowed when the API accepted it; the policy shrank before the worker started."""
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, request_context={"customer": "Ada"},
                               model_id="test:context", model_overridden=True)
    policy = ModelPolicy.parse("test:echo")  # test:context is gone
    monkeypatch.setattr(worker, "settings", dataclasses.replace(worker.settings, allowed_models=policy))
    _enqueue(client, store, tenant, CONTEXT_SPEC, run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert [e[1] for e in events] == ["start", "end"]
    assert events[-1][3]["error_code"] == "model_not_allowed"
    assert _request_models(worker.engine, run_id) == []
    assert "Ada" not in json.dumps(events[-1][3])


def test_allowed_override_still_runs_under_a_narrowed_policy(worker, client, tenant, monkeypatch):
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, request_context={"customer": "Ada", "plan": "pro"},
                               model_id="test:context", model_overridden=True)
    policy = ModelPolicy.parse("test:context")
    monkeypatch.setattr(worker, "settings", dataclasses.replace(worker.settings, allowed_models=policy))
    _enqueue(client, store, tenant, CONTEXT_SPEC, run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert _request_models(worker.engine, run_id) == ["test:context"]


# --------------------------------------------------------------------------- crash recovery (published run)


@pytest.fixture
def crash_schema():
    schema = new_schema("eap_test_optsacc")
    yield schema
    drop_schema(schema)


@pytest.mark.parametrize(
    "crash", [
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "start"}, id="after-start-run"),
        pytest.param({"EAP_CRASH_AT": "model_request"}, id="during-model-request"),
    ],
)
def test_published_run_crash_recovers_on_the_recorded_model_and_context(worker, tenant, crash_schema, tmp_path, crash):
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
    from worker_tests.test_crash_recovery import WorkerProcess, _env

    store = worker.run_store
    run_id, agent_id = _create(store, tenant, request_context={"customer": "Ada", "plan": "pro"},
                               model_id="test:context", model_overridden=True)
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    executor_id = f"optsacc-{uuid.uuid4().hex[:8]}"
    procs: list[WorkerProcess] = []
    try:
        crashing = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash),
                                 tmp_path / "crash.log")
        procs.append(crashing)
        crashing.wait_ready()
        _enqueue(client, store, tenant, CONTEXT_SPEC, run_id, agent_id, app_version=CRASH_APP_VERSION)
        assert crashing.wait_exit() == 137, (tmp_path / "crash.log").read_text()
        assert store.get_run(tenant, run_id)["status"] == "running"
        # While the worker is down the row is rewritten: replay must still use the recorded values.
        with worker.engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET model_id = 'test:echo', request_context = CAST(:c AS jsonb) WHERE id = :r"),
                {"c": json.dumps({"customer": "Eve", "plan": "free"}), "r": str(run_id)},
            )
        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "recover.log")
        procs.append(recovering)
        run = wait_terminal(store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "recover.log").read_text())
        assert run["output"] == "Instructions: Greet Ada on the pro plan."
        assert _request_models(worker.engine, run_id) == ["test:context"]
        assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "model:0:final", "end"]
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()


# --------------------------------------------------------------------------- no context in spans or logs


def test_context_values_never_reach_spans_or_logs(worker, client, tenant, telemetry, caplog):  # noqa: F811
    _provider, finished = telemetry
    caplog.set_level(logging.DEBUG)
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, request_context={"customer": SECRET, "plan": "pro"},
                               model_id="test:context", model_overridden=True)
    _enqueue(client, store, tenant, CONTEXT_SPEC, run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert SECRET in run["output"]  # it did reach the model, via the instructions
    # The run row turns terminal before the `eap.run` span ends (see test_telemetry): wait for the span
    # instead of reading the exporter once, which came back empty on a loaded runner.
    spans, logs = _wait_for_run_span(finished, run_id)
    assert any(s.name == "eap.run" for s in spans), "telemetry captured nothing"
    traced = repr([(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events])
                   for s in spans])
    logged = caplog.text + repr([(r.body, dict(r.attributes or {})) for r in logs])
    assert SECRET not in traced
    assert SECRET not in logged
    events = json.dumps([e[3] for e in event_rows(worker.engine, run_id) if e[1] == "start"])
    assert SECRET not in events
