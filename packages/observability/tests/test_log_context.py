"""Tenant and run ids on exported log records (P0.4): log_context, run_span, LogContextProcessor."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

import eap_observability as obs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    obs.shutdown_telemetry()
    yield
    obs.shutdown_telemetry()


@pytest.fixture
def logs():
    exporter = InMemoryLogRecordExporter()
    assert obs.setup_logging("test-service", exporter=exporter) is True

    def finished():
        obs._state.logger_provider.force_flush()
        return [r.log_record for r in exporter.get_finished_logs()]

    return finished


def _attrs(record) -> dict:
    return dict(record.attributes or {})


log = logging.getLogger("eap_test.log_context")


def test_records_outside_any_context_carry_no_tenant(logs):
    log.warning("outside")
    (record,) = logs()
    assert obs.TENANT_LOG_ATTRIBUTE not in _attrs(record)
    assert obs.RUN_LOG_ATTRIBUTE not in _attrs(record)


def test_log_context_adds_tenant_and_run_and_ends_with_the_block(logs):
    with obs.log_context(tenant_id="tenant-a", run_id="run-1"):
        log.warning("inside")
    log.warning("after")
    inside, after = logs()
    assert _attrs(inside)["eap.tenant_id"] == "tenant-a"
    assert _attrs(inside)["eap.run_id"] == "run-1"
    assert "eap.tenant_id" not in _attrs(after)
    assert "eap.run_id" not in _attrs(after)


def test_run_without_tenant_adds_nothing():
    with obs.log_context(tenant_id=None, run_id="run-1"):
        assert obs.current_log_context() == {}


def test_run_span_binds_the_run_for_logs(logs):
    with obs.run_span("run-9", "tenant-b", "agent", 1):
        assert obs.current_log_context() == {"eap.tenant_id": "tenant-b", "eap.run_id": "run-9"}
        log.warning("in run")
    assert obs.current_log_context() == {}
    (record,) = logs()
    assert _attrs(record)["eap.tenant_id"] == "tenant-b"
    assert _attrs(record)["eap.run_id"] == "run-9"


def test_run_span_binds_logs_even_when_telemetry_is_off():
    with obs.run_span("run-9", "tenant-b", "agent", 1):
        assert obs.current_log_context()["eap.tenant_id"] == "tenant-b"


def test_explicit_attribute_is_kept(logs):
    with obs.log_context(tenant_id="tenant-a", run_id="run-1"):
        log.warning("explicit", extra={"eap.run_id": "run-other"})
    (record,) = logs()
    assert _attrs(record)["eap.run_id"] == "run-other"
    assert _attrs(record)["eap.tenant_id"] == "tenant-a"


def test_other_handlers_on_the_provider_are_covered(logs):
    """A second OpenTelemetry handler (as DBOS adds with enable_otlp) gets the ids too."""
    from opentelemetry.instrumentation.logging.handler import LoggingHandler

    other = logging.getLogger("eap_test.other_library")
    other.propagate = False
    handler = LoggingHandler(logger_provider=obs._state.logger_provider)
    other.addHandler(handler)
    try:
        with obs.log_context(tenant_id="tenant-c", run_id="run-3"):
            other.warning("from another handler")
    finally:
        other.removeHandler(handler)
        other.propagate = True
    (record,) = logs()
    assert _attrs(record)["eap.tenant_id"] == "tenant-c"


def test_context_does_not_leak_across_threads_or_tasks(logs):
    seen: dict[str, dict] = {}

    def worker():
        seen["thread"] = obs.current_log_context()

    async def task():
        seen["task"] = obs.current_log_context()

    with obs.log_context(tenant_id="tenant-a", run_id="run-1"):
        t = threading.Thread(target=worker)  # a new thread starts with an empty context
        t.start()
        t.join()
    asyncio.run(task())
    assert seen == {"thread": {}, "task": {}}


def test_bind_in_a_task_ends_with_the_task():
    async def request():
        obs.bind_log_context(tenant_id="tenant-a")
        return obs.current_log_context()

    async def main():
        inside = await asyncio.create_task(request())
        return inside, obs.current_log_context()

    inside, outside = asyncio.run(main())
    assert inside == {"eap.tenant_id": "tenant-a"}
    assert outside == {}


def test_ids_survive_redaction(logs):
    with obs.log_context(tenant_id="t_1-x", run_id="00000000-0000-0000-0000-000000000001"):
        log.warning("redacted? token=abc123secret")
    (record,) = logs()
    assert "abc123secret" not in record.body
    assert _attrs(record)["eap.run_id"] == "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------- review fixes


@pytest.mark.parametrize(
    "key", ["eap-tenant-id", "eap_tenant_id", "eap tenant id", "EAP.TENANT_ID", "eap/run/id", "eap_run_id", "eap-run.id"]
)
def test_attributes_that_loki_would_fold_into_our_labels_are_dropped(logs, key):
    """Loki maps eap-tenant-id and eap_tenant_id to the same label as eap.tenant_id (last wins)."""
    with obs.log_context(tenant_id="tenant-a", run_id="run-1"):
        log.warning("spoof inside", extra={key: "victim"})
    log.warning("spoof outside", extra={key: "victim"})
    inside, outside = logs()
    assert key not in _attrs(inside)
    assert _attrs(inside)["eap.tenant_id"] == "tenant-a"
    assert _attrs(inside)["eap.run_id"] == "run-1"
    assert "victim" not in repr(_attrs(outside))


def test_unrelated_attributes_are_kept(logs):
    log.warning("x", extra={"eap.tenant_name": "kept", "eap_tenant_idx": "kept"})
    (record,) = logs()
    assert _attrs(record)["eap.tenant_name"] == "kept"
    assert _attrs(record)["eap_tenant_idx"] == "kept"


def test_loki_label_name():
    assert obs.logcontext.loki_label_name("eap.tenant_id") == "eap_tenant_id"
    assert obs.logcontext.is_impostor_key("eap-tenant-id")
    assert not obs.logcontext.is_impostor_key("eap.tenant_id")
    assert not obs.logcontext.is_impostor_key("eap.run_id")


def test_tenant_and_run_ids_are_never_redacted(logs):
    """A credential-shaped tenant id (sk-...) must stay searchable; the body is still redacted."""
    tenant = "sk-abcdefghij"
    with obs.log_context(tenant_id=tenant, run_id="sk-runid1234"):
        log.warning("leak sk-abcdefghij token=abc123secret")
    try:
        raise RuntimeError("sk-secretvalue12")
    except RuntimeError as exc:
        obs.log_failure(log, "failed", exc, code="boom", tenant_id=tenant, run_id="sk-runid1234", detail="sk-detail123")
    record, failure = logs()
    assert _attrs(record)["eap.tenant_id"] == tenant
    assert _attrs(record)["eap.run_id"] == "sk-runid1234"
    assert "abc123secret" not in record.body
    assert "sk-abcdefghij" not in record.body
    assert _attrs(failure)["eap.tenant_id"] == tenant
    assert _attrs(failure)["eap.run_id"] == "sk-runid1234"
    assert _attrs(failure)["eap.detail"] == "[REDACTED]"
    assert "sk-secretvalue12" not in repr(_attrs(failure)) + str(failure.body)
