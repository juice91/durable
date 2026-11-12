"""Milestone 2, task 4 (API half): each started run stores the request span's trace context and
returns its trace id; the enqueue carries the traceparent for DBOS; logs carry the trace id.

Telemetry is switched on with in-memory exporters (no OTLP endpoint needed); with it off (every
other API test) the same routes store and return nulls and enqueue exactly the hand-off contract.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

import eap_observability as obs
from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, requires_pg
from .test_runs import FakeDBOSClient, make_agent, start
from .test_conversations import new_conversation, turn

pytestmark = requires_pg

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


@pytest.fixture
def traced(monkeypatch):
    """(client, app, fake DBOS client, finished_spans(), finished_logs()) with telemetry on."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    provider = obs.setup_telemetry("eap-api", exporter=span_exporter)
    obs.setup_logging("eap-api", exporter=log_exporter)
    fake = FakeDBOSClient()
    app = create_app(database_url=PG_URL)  # its setup calls return the providers installed above
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenant = f"t{uuid.uuid4().hex[:10]}"

    def spans():
        provider.force_flush()
        return span_exporter.get_finished_spans()

    def logs():
        obs._state.logger_provider.force_flush()
        return [r.log_record for r in log_exporter.get_finished_logs()]

    try:
        with TestClient(app) as client:
            yield client, app, fake, tenant, spans, logs
    finally:
        cleanup_tenant(app.state.database_engine, tenant)
        app.state.database_engine.dispose()
        obs.shutdown_telemetry()


def _stored(app, run_id: str) -> tuple[str | None, str | None]:
    with app.state.database_engine.connect() as conn:
        row = conn.execute(text("SELECT trace_id, traceparent FROM runs WHERE id = :r"), {"r": run_id}).one()
    return row[0], row[1]


def _server_span(spans, route: str):
    matches = [s for s in spans() if s.name == f"POST {route}" and s.kind.name == "SERVER"]
    assert len(matches) == 1, [s.name for s in spans()]
    return matches[0]


def test_start_run_stores_request_trace_context_and_returns_trace_id(traced):
    client, app, fake, tenant, spans, logs = traced
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    r = start(client, h, agent_id, run_id)
    assert r.status_code == 201, r.text
    trace_id = r.json()["trace_id"]

    stored_trace_id, traceparent = _stored(app, run_id)
    m = TRACEPARENT.match(traceparent)
    assert m and stored_trace_id == trace_id == m.group(1)
    # The traceparent is the API request's server span: the worker's run span becomes its child.
    server = _server_span(spans, "/api/v1/agents/{agent_id}/runs")
    assert format(server.context.trace_id, "032x") == trace_id
    assert format(server.context.span_id, "016x") == m.group(2)

    # DBOS parents its workflow span on the same traceparent (durable workflow attribute).
    options, _args = fake.calls[-1]
    assert options["attributes"] == {"dbos.otelContext": {"traceparent": traceparent}}

    assert client.get(f"/api/v1/runs/{run_id}", headers=h).json()["trace_id"] == trace_id

    # A retry (same run_id) keeps the first request's trace.
    again = start(client, h, agent_id, run_id)
    assert again.status_code == 200 and again.json()["trace_id"] == trace_id
    assert fake.calls[-1][0]["attributes"] == {"dbos.otelContext": {"traceparent": traceparent}}


def test_conversation_turn_stores_trace_context(traced):
    client, app, fake, tenant, spans, _ = traced
    h = get_auth_header(tenant, "alice")
    conv_id = new_conversation(client, h, make_agent(client, h))
    run_id = str(uuid.uuid4())
    r = turn(client, h, conv_id, run_id)
    assert r.status_code == 201, r.text
    stored_trace_id, traceparent = _stored(app, run_id)
    assert r.json()["trace_id"] == stored_trace_id is not None
    server = _server_span(spans, "/api/v1/conversations/{conversation_id}/runs")
    assert TRACEPARENT.match(traceparent).group(2) == format(server.context.span_id, "016x")
    assert fake.calls[-1][0]["attributes"] == {"dbos.otelContext": {"traceparent": traceparent}}


def test_request_logs_carry_the_trace_id(traced):
    client, _, _, tenant, _, logs = traced
    h = get_auth_header(tenant, "alice")
    agent_id = make_agent(client, h)
    trace_id = start(client, h, agent_id).json()["trace_id"]
    queued = [r for r in logs() if isinstance(r.body, str) and r.body.startswith("run ") and "queued" in r.body]
    assert queued, [r.body for r in logs()]
    assert format(queued[-1].trace_id, "032x") == trace_id
