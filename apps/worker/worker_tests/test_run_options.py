"""Run options in the worker (milestone P0, task P0.11).

The effective model, the request context and the draft flag come back from `eap.start_run`'s
recorded output (read from the run row); instructions render `{{context.<name>}}`; tools get a
read-only `RunDeps.request_context`; a draft run (hand-off version 0) runs its spec_json snapshot.
"""

from __future__ import annotations

import json
import pickle
import uuid

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import (
    APP_VERSION,
    DBOS_DB_URL,
    enqueue_run,
    event_rows,
    wait_terminal,
)

CONTEXT_SPEC = {
    "spec_version": 1,
    "name": "Context agent",
    "instructions": "Greet {{context.customer}} on the {{context.plan}} plan. Literal {{context.unknown}} stays.",
    "model": {"provider": "test", "name": "echo"},
    "tools": [],
    "model_settings": {},
    "context_fields": [
        {"name": "customer", "type": "string", "required": True},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
    ],
}
RENDERED = "Greet Ada on the pro plan. Literal {{context.unknown}} stays."


def _create(store, tenant, spec, input_text, *, version=1, **options):
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    store.create_run(tenant, agent_id, version, input_text, run_id, **options)
    return run_id, agent_id


def _enqueue(client, store, tenant, spec, input_text, run_id, agent_id, version=1):
    return enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text=input_text, app_version=APP_VERSION,
                       run_id=run_id, agent_id=agent_id, version=version, create_row=False)


def _request_models(engine, run_id) -> list[str]:
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(
            text("SELECT model_id FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)}
        )]


# --------------------------------------------------------------------------- unit


def test_request_context_is_read_only_picklable_and_hides_values():
    from eap_runtime import RequestContext

    ctx = RequestContext({"customer": "secret-value", "plan": "pro"})
    assert dict(ctx) == {"customer": "secret-value", "plan": "pro"}
    assert ctx.get("missing") is None
    with pytest.raises(TypeError):
        ctx["customer"] = "x"  # type: ignore[index]
    with pytest.raises(AttributeError):
        ctx._data = {}  # type: ignore[misc]
    assert "secret-value" not in repr(ctx) and "customer" in repr(ctx)
    assert dict(pickle.loads(pickle.dumps(ctx))) == dict(ctx)


def test_run_deps_default_to_an_empty_context(worker):
    from eap_runtime import RunDeps
    from eap_schemas import AgentSpec

    deps = worker.tool_deps_for(str(uuid.uuid4()), "t_unit")
    assert isinstance(deps, RunDeps) and dict(deps.request_context) == {}
    spec = AgentSpec.model_validate(CONTEXT_SPEC)
    deps = worker.deps_for(str(uuid.uuid4()), "t_unit", spec, {"customer": "Ada"})
    assert dict(deps.request_context) == {"customer": "Ada"}
    assert "Ada" not in repr(deps)  # deps never print context values


def test_effective_model_falls_back_to_the_spec_for_old_checkpoints():
    from eap_schemas import AgentSpec
    from eap_worker.workflow import effective_model_id

    spec = AgentSpec.model_validate(CONTEXT_SPEC)
    assert effective_model_id(spec, {}) == "test:echo"  # recorded before run options existed
    assert effective_model_id(spec, {"model_id": None}) == "test:echo"
    assert effective_model_id(spec, {"model_id": "test:context"}) == "test:context"


# --------------------------------------------------------------------------- in-process runs


def test_override_and_context_reach_the_model(worker, tenant, client):
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, CONTEXT_SPEC, "hi", request_context={"customer": "Ada", "plan": "pro"},
                               model_id="test:context", model_overridden=True)
    _enqueue(client, store, tenant, CONTEXT_SPEC, "hi", run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == f"Instructions: {RENDERED}"
    assert _request_models(worker.engine, run_id) == ["test:context"]
    final = [e for e in event_rows(worker.engine, run_id) if e[1] == "model:0:final"][0]
    assert final[3]["model_id"] == "test:context"


def test_context_values_never_reach_events_other_than_the_model_output(worker, tenant, client):
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, CONTEXT_SPEC, "hi", request_context={"customer": "Zq9xValue"},
                               model_id="test:echo")
    _enqueue(client, store, tenant, CONTEXT_SPEC, "hi", run_id, agent_id)
    assert wait_terminal(store, tenant, run_id)["status"] == "completed"
    start = event_rows(worker.engine, run_id)[0]
    assert start[1] == "start" and "Zq9xValue" not in json.dumps(start[3])
    assert "draft" not in start[3]  # a published-version run's run_started data is unchanged


def test_row_without_model_id_runs_the_spec_model(worker, tenant, client):
    # Optional fields only: a required one with no context fails (request_context_invalid, below).
    fields = [{**f, "required": False} for f in CONTEXT_SPEC["context_fields"]]
    spec = {**CONTEXT_SPEC, "model": {"provider": "test", "name": "context"}, "context_fields": fields}
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, spec, "hi")  # no options: a pre-P0.8 row
    _enqueue(client, store, tenant, spec, "hi", run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    # No context: declared fields render empty, undeclared placeholders stay as written.
    assert run["output"] == "Instructions: Greet  on the  plan. Literal {{context.unknown}} stays."


def test_missing_required_context_fails_before_any_model_request(worker, tenant, client):
    """A run whose context misses a required field (the API never lets a caller do this; a workflow
    agent step runs with an empty context) fails with request_context_invalid, no model request,
    and a fixed message with no values."""
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, CONTEXT_SPEC, "hi", request_context={"plan": "pro"},
                               model_id="test:echo")
    _enqueue(client, store, tenant, CONTEXT_SPEC, "hi", run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "RequestContextInvalid: the request context does not match the agent's context fields"
    events = event_rows(worker.engine, run_id)
    assert [e[1] for e in events] == ["start", "end"]
    assert events[-1][3]["error_code"] == "request_context_invalid"
    assert "pro" not in json.dumps(events[-1][3])
    assert _request_models(worker.engine, run_id) == []


def test_request_context_invalid_is_pickle_safe_and_classified():
    from eap_worker.errors import RequestContextInvalid, classify

    exc = pickle.loads(pickle.dumps(RequestContextInvalid()))
    assert str(exc) == "the request context does not match the agent's context fields"
    failure = classify(exc)
    assert (failure.status, failure.code, failure.error_type) == ("failed", "request_context_invalid", "RequestContextInvalid")


def test_check_request_context_ignores_undeclared_keys_and_specs_without_fields():
    from eap_schemas import AgentSpec
    from eap_worker.errors import RequestContextInvalid
    from eap_worker.workflow import _check_request_context

    spec = AgentSpec.model_validate(CONTEXT_SPEC)
    _check_request_context(spec, {"customer": "Ada", "extra": 1})
    for bad in (None, {}, {"customer": 3}, {"customer": "Ada", "plan": "gold"}):
        with pytest.raises(RequestContextInvalid):
            _check_request_context(spec, bad)
    plain = AgentSpec.model_validate({k: v for k, v in CONTEXT_SPEC.items() if k != "context_fields"})
    _check_request_context(plain, None)


def test_disallowed_override_fails_before_any_model_request(worker, tenant, client):
    store = worker.run_store
    run_id, agent_id = _create(store, tenant, CONTEXT_SPEC, "hi", model_id="anthropic:claude-not-allowed",
                               model_overridden=True)
    _enqueue(client, store, tenant, CONTEXT_SPEC, "hi", run_id, agent_id)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert [e[1] for e in events] == ["start", "end"]
    assert events[-1][3]["error_code"] == "model_not_allowed"
    assert _request_models(worker.engine, run_id) == []


def test_draft_run_runs_its_snapshot(worker, tenant, client):
    store = worker.run_store
    spec = {**CONTEXT_SPEC, "model": {"provider": "test", "name": "context"}}
    run_id, agent_id = _create(store, tenant, spec, "hi", version=None, draft_spec=spec,
                               request_context={"customer": "Ada", "plan": "pro"}, model_id="test:context")
    _enqueue(client, store, tenant, spec, "hi", run_id, agent_id, version=0)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == f"Instructions: {RENDERED}"
    assert run["agent_version"] is None and run["draft_spec"] == spec
    start = event_rows(worker.engine, run_id)[0]
    assert start[3]["version"] == 0 and start[3]["draft"] is True


def test_draft_safety_net_inserts_a_snapshot_not_version_zero(worker, tenant, client):
    """eap.start_run's idempotent insert (row missing) stores a draft run as the API would."""
    store = worker.run_store
    fields = [{**f, "required": False} for f in CONTEXT_SPEC["context_fields"]]  # the insert has no context
    spec = {**CONTEXT_SPEC, "model": {"provider": "test", "name": "context"}, "context_fields": fields}
    run_id = enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text="hi", app_version=APP_VERSION,
                         version=0, create_row=False)
    run = wait_terminal(store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["agent_version"] is None
    assert run["draft_spec"] == spec
    assert run["model_id"] == "test:context"
    assert event_rows(worker.engine, run_id)[0][3]["draft"] is True


# --------------------------------------------------------------------------- crash recovery


@pytest.mark.parametrize(
    "crash", [
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "start"}, id="after-start-run"),
        pytest.param({"EAP_CRASH_AT": "model_request"}, id="during-model-request"),
    ],
)
def test_crash_recovers_on_the_recorded_model_context_and_snapshot(worker, tenant, crash_schema, tmp_path, crash):
    """A draft run with an override and a context: kill the worker after eap.start_run is recorded,
    rewrite the row's model, context and snapshot while it is down, recover in a fresh process. The
    replay must use what the first attempt recorded."""
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
    from worker_tests.test_crash_recovery import WorkerProcess, _env

    store = worker.run_store
    spec = dict(CONTEXT_SPEC)  # spec model test:echo; the override is test:context
    run_id, agent_id = _create(store, tenant, spec, "hi", version=None, draft_spec=spec,
                               request_context={"customer": "Ada", "plan": "pro"},
                               model_id="test:context", model_overridden=True)
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    executor_id = f"opts-crash-{uuid.uuid4().hex[:8]}"
    procs: list[WorkerProcess] = []
    try:
        crashing = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash),
                                 tmp_path / "worker-crash.log")
        procs.append(crashing)
        crashing.wait_ready()
        enqueue_run(client, store, tenant_id=tenant, spec=spec, input_text="hi", app_version=CRASH_APP_VERSION,
                    run_id=run_id, agent_id=agent_id, version=0, create_row=False)
        assert crashing.wait_exit() == 137, (tmp_path / "worker-crash.log").read_text()
        assert store.get_run(tenant, run_id)["status"] == "running"

        edited = {**spec, "instructions": "EDITED DRAFT", "model": {"provider": "test", "name": "echo"}}
        with worker.engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET model_id = 'test:echo', request_context = CAST(:c AS jsonb),"
                     " draft_spec = CAST(:d AS jsonb) WHERE id = :r"),
                {"c": json.dumps({"customer": "Eve", "plan": "free"}), "d": json.dumps(edited), "r": str(run_id)},
            )

        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-recover.log")
        procs.append(recovering)
        run = wait_terminal(store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker-recover.log").read_text())
        assert run["output"] == f"Instructions: {RENDERED}"
        assert _request_models(worker.engine, run_id) == ["test:context"]
        events = event_rows(worker.engine, run_id)
        assert [e[1] for e in events] == ["start", "model:0:final", "end"]
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()


@pytest.fixture
def crash_schema():
    from worker_tests.conftest import drop_schema, new_schema

    schema = new_schema("eap_test_opts_crash")
    yield schema
    drop_schema(schema)
