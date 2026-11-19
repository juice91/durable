"""POST /api/v1/otlp/v1/traces: external OpenTelemetry traces, stored in the span store next to the
platform's own and read back through GET /traces, GET /traces/{id} and GET /spans."""

from __future__ import annotations

import gzip
import json
import time

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
from sqlalchemy import text

from .conftest import get_auth_header
from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

URL = "/api/v1/otlp/v1/traces"
TRACE = "0af7651916cd43dd8448eb211c80319c"
ROOT = "b7ad6b7169203331"
CHILD = "00f067aa0ba902b7"


def _kv(key: str, value) -> KeyValue:
    any_value = AnyValue()
    if isinstance(value, bool):
        any_value.bool_value = value
    elif isinstance(value, int):
        any_value.int_value = value
    else:
        any_value.string_value = str(value)
    return KeyValue(key=key, value=any_value)


def _proto(spans: list[dict] | None = None, service: str = "my-agent", resource: dict | None = None) -> bytes:
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    rs.resource.attributes.append(_kv("service.name", service))
    for k, v in (resource or {}).items():
        rs.resource.attributes.append(_kv(k, v))
    now = time.time_ns()
    for s in spans if spans is not None else _default_spans(now):
        span = rs.scope_spans.add().spans.add()
        span.trace_id = bytes.fromhex(s.get("trace_id", TRACE))
        span.span_id = bytes.fromhex(s["span_id"])
        if s.get("parent"):
            span.parent_span_id = bytes.fromhex(s["parent"])
        span.name = s.get("name", "work")
        span.start_time_unix_nano = s.get("start", now - 2_000_000_000)
        span.end_time_unix_nano = s.get("end", now - 1_000_000_000)
        span.status.CopyFrom(Status(code=s.get("status", 1)))
        for k, v in (s.get("attributes") or {}).items():
            span.attributes.append(_kv(k, v))
    return request.SerializeToString()


def _default_spans(now: int) -> list[dict]:
    return [
        {"span_id": ROOT, "name": "agent.run", "start": now - 3_000_000_000, "end": now - 1_000_000_000},
        {"span_id": CHILD, "parent": ROOT, "name": "llm.call", "start": now - 2_500_000_000, "end": now - 1_500_000_000},
    ]


def _json_body(span_id: str = ROOT, trace_id: str = TRACE, name: str = "json.op", service: str = "js-app") -> dict:
    now = time.time_ns()
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {"name": "lib"}, "spans": [{
            "traceId": trace_id, "spanId": span_id, "name": name, "kind": 2,
            "startTimeUnixNano": str(now - 2_000_000_000), "endTimeUnixNano": str(now - 1_000_000_000),
            "attributes": [{"key": "http.status_code", "value": {"intValue": "200"}}],
            "status": {"code": 2, "message": "boom"},
        }]}],
    }]}


PB = {"content-type": "application/x-protobuf"}


def _key(client, headers, scopes=("otlp",)) -> dict[str, str]:
    r = client.post("/api/v1/api-keys", json={"name": "otel", "scopes": list(scopes)}, headers=headers)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['key']}", "key_id": r.json()["id"]}


def _post(client, key, body: bytes, extra=None):
    headers = {"Authorization": key["Authorization"], **(extra or PB)}
    return client.post(URL, content=body, headers=headers)


def _cleanup(app, *tenants: str) -> None:
    with app.state.database_engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": t})


def test_protobuf_spans_are_stored_for_the_key_tenant_and_listed(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    other, oh = tenant_of()
    try:
        key = _key(client, h)
        r = _post(client, key, _proto(resource={"tenant_id": other, "tenant": other}))
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-protobuf")
        assert ExportTraceServiceResponse.FromString(r.content).ByteSize() == 0

        traces = client.get("/api/v1/traces", headers=h).json()
        assert [(t["trace_id"], t["source"], t["service_name"], t["name"], t["span_count"], t["status"]) for t in traces["items"]] == [
            (TRACE, "external", "my-agent", "agent.run", 2, "ok")
        ]
        detail = client.get(f"/api/v1/traces/{TRACE}", headers=h).json()
        assert {s["span_id"]: s["parent_id"] for s in detail["spans"]} == {ROOT: None, CHILD: ROOT}
        assert detail["resource_attributes"]["service.name"] == "my-agent"
        assert detail["service_name"] == "my-agent" and detail["run_id"] is None
        # a tenant claimed in the resource is ignored; the other tenant sees nothing
        assert client.get("/api/v1/traces", headers=oh).json()["items"] == []
        assert client.get(f"/api/v1/traces/{TRACE}", headers=oh).status_code == 404
        # the span list shows them, filterable by source
        spans = client.get("/api/v1/spans", params={"source": "external"}, headers=h).json()["items"]
        assert {s["span_id"] for s in spans} == {ROOT, CHILD}
        assert {s["source"] for s in spans} == {"external"} and {s["trace_id"] for s in spans} == {TRACE}
        assert client.get("/api/v1/spans", params={"source": "platform"}, headers=h).json()["items"] == []
        with app.state.database_engine.connect() as conn:
            row = conn.execute(text("SELECT api_key_id, tenant_id FROM spans WHERE tenant_id = :t LIMIT 1"), {"t": tenant}).one()
        assert row.api_key_id == key["key_id"] and row.tenant_id == tenant
        assert client.get("/api/v1/spans", headers=oh).json()["items"] == []
    finally:
        _cleanup(app, tenant, other)


def test_json_encoding_answers_empty_json_and_keeps_trace_ids(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        key = _key(client, h)
        r = _post(client, key, json.dumps(_json_body()).encode(), {"content-type": "application/json"})
        assert r.status_code == 200 and r.json() == {}
        detail = client.get(f"/api/v1/traces/{TRACE}", headers=h).json()
        span = detail["spans"][0]
        assert span["status"] == "error" and span["attributes"]["http.status_code"] == 200
        assert span["attributes"]["otel.span_kind"] == "server" and span["attributes"]["otel.status_message"] == "boom"
        # a retry of the same export stores nothing twice
        _post(client, key, json.dumps(_json_body()).encode(), {"content-type": "application/json; charset=utf-8"})
        assert client.get("/api/v1/traces", headers=h).json()["items"][0]["span_count"] == 1
        assert client.get("/api/v1/traces", params={"status": "error"}, headers=h).json()["items"][0]["trace_id"] == TRACE
        assert client.get("/api/v1/traces", params={"status": "ok"}, headers=h).json()["items"] == []
        assert client.get("/api/v1/traces", params={"service": "js-app"}, headers=h).json()["items"][0]["service_name"] == "js-app"
        assert client.get("/api/v1/traces", params={"service": "other"}, headers=h).json()["items"] == []
    finally:
        _cleanup(app, tenant)


def test_gzip_body_is_accepted(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        r = _post(client, _key(client, h), gzip.compress(_proto()), {**PB, "content-encoding": "gzip"})
        assert r.status_code == 200
        assert client.get("/api/v1/traces", headers=h).json()["items"][0]["span_count"] == 2
    finally:
        _cleanup(app, tenant)


def test_auth_and_scope_errors(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        assert client.post(URL, content=_proto(), headers=PB).status_code == 401
        bad = {"Authorization": "Bearer pos_" + "0" * 24 + "_nope"}
        assert client.post(URL, content=_proto(), headers={**bad, **PB}).status_code == 401
        no_scope = _key(client, h, scopes=("agui:run", "mcp"))
        r = _post(client, no_scope, _proto())
        assert r.status_code == 403 and "otlp" in r.json()["detail"]
        key = _key(client, h)
        assert client.delete(f"/api/v1/api-keys/{key['key_id']}", headers=h).status_code == 204
        assert _post(client, key, _proto()).status_code == 401
        # the dev bearer works in dev mode
        assert client.post(URL, content=_proto(), headers={**h, **PB}).status_code == 200
        assert client.get("/api/v1/traces", headers=h).json()["items"][0]["trace_id"] == TRACE
        # a key does not open the management routes
        otlp = _key(client, h)
        assert client.get("/api/v1/traces", headers={"Authorization": otlp["Authorization"]}).status_code == 401
    finally:
        _cleanup(app, tenant)


def test_content_type_and_body_errors(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        key = _key(client, h)
        assert _post(client, key, b"{}", {"content-type": "text/plain"}).status_code == 415
        assert _post(client, key, b"{}", {"content-type": "application/grpc"}).status_code == 415
        assert _post(client, key, b"{}", {**PB, "content-encoding": "br"}).status_code == 415
        assert _post(client, key, b"\xff\xff\xff not protobuf", PB).status_code == 400
        assert _post(client, key, b"[1,2]", {"content-type": "application/json"}).status_code == 400
        assert _post(client, key, b"nope", {"content-type": "application/json"}).status_code == 400
        # an empty protobuf request is a valid export of nothing
        assert _post(client, key, b"", PB).status_code == 200
    finally:
        _cleanup(app, tenant)


def test_payload_and_span_count_caps_are_413(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        key = _key(client, h)
        big = _post(client, key, b"x" * (4 * 1024 * 1024 + 1))
        assert big.status_code == 413 and "too large" in big.json()["detail"]
        bomb = _post(client, key, gzip.compress(b"\0" * (5 * 1024 * 1024)), {**PB, "content-encoding": "gzip"})
        assert bomb.status_code == 413
        now = time.time_ns()
        many = [{"span_id": f"{i + 1:016x}", "start": now - 2_000_000_000, "end": now - 1_000_000_000} for i in range(1001)]
        r = _post(client, key, _proto(many))
        assert r.status_code == 413 and "1000" in r.json()["detail"]
        assert client.get("/api/v1/traces", headers=h).json()["items"] == []
    finally:
        _cleanup(app, tenant)


def test_invalid_spans_are_dropped_with_partial_success(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        key = _key(client, h)
        now = time.time_ns()
        spans = [
            {"span_id": ROOT, "name": "good"},
            {"span_id": CHILD, "trace_id": "0" * 32, "name": "zero trace"},
            {"span_id": "1" * 16, "end": now - 9_000_000_000, "start": now - 1_000_000_000, "name": "ends first"},
            {"span_id": "2" * 16, "start": now + 10 * 24 * 3600 * 10**9, "end": 0 + now + 10 * 24 * 3600 * 10**9, "name": "future"},
        ]
        r = _post(client, key, _proto(spans))
        assert r.status_code == 200
        partial = ExportTraceServiceResponse.FromString(r.content).partial_success
        assert partial.rejected_spans == 3 and "trace_id" in partial.error_message
        assert client.get(f"/api/v1/traces/{TRACE}", headers=h).json()["spans"][0]["span_id"] == ROOT
        jr = _post(client, key, json.dumps(_json_body(span_id="zz")).encode(), {"content-type": "application/json"})
        assert jr.json()["partialSuccess"]["rejectedSpans"] == 1 and "span_id" in jr.json()["partialSuccess"]["errorMessage"]
    finally:
        _cleanup(app, tenant)


def test_attribute_values_are_redacted_and_bounded_before_storing(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        key = _key(client, h)
        secret = "sk-abcdefghijklmnopqrstuvwx"
        attrs = {
            "note": f"calling with {secret} now", "http.request.header.authorization": "Bearer abcdef123456",
            "db.password": "hunter2", "url": "https://user:pw123@host/x", "plain": "hello", "long": "a" * 5000,
            "platform_key": "pos_" + "a" * 24 + "_secretvalue12345",
        } | {f"k{i}": i for i in range(100)}
        r = _post(client, key, _proto([{"span_id": ROOT, "attributes": attrs}], resource={"api_key": "abc123xyz"}))
        assert r.status_code == 200
        stored = client.get(f"/api/v1/traces/{TRACE}", headers=h).json()
        blob = json.dumps(stored)
        for leak in (secret, "abcdef123456", "hunter2", "pw123", "secretvalue12345", "abc123xyz"):
            assert leak not in blob
        a = stored["spans"][0]["attributes"]
        assert a["plain"] == "hello" and a["db.password"] == "[REDACTED]" and len(a["long"]) < 2100
        assert len(a) <= 64 + 3  # bounded count (plus the otel.* markers)
        assert stored["resource_attributes"]["api_key"] == "[REDACTED]"
        with app.state.database_engine.connect() as conn:
            raw = conn.execute(text("SELECT attributes::text || resource_attributes::text FROM spans WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
        assert secret not in raw and "hunter2" not in raw
    finally:
        _cleanup(app, tenant)


def test_rate_limit_per_key_is_429_with_retry_after(pg, tenant_of, monkeypatch):
    client, app = pg
    monkeypatch.setenv("EAP_OTLP_RATE_LIMIT_PER_MINUTE", "2")
    app.state.otlp_limiter = None
    tenant, h = tenant_of()
    try:
        a, b = _key(client, h), _key(client, h)
        assert [_post(client, a, _proto([])).status_code for _ in range(2)] == [200, 200]
        r = _post(client, a, _proto([]))
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        assert _post(client, b, _proto([])).status_code == 200  # another key has its own bucket
    finally:
        _cleanup(app, tenant)


def test_run_id_attribute_links_a_platform_run_of_the_tenant_only(pg, tenant_of):
    from .test_runs import make_agent, start

    client, app = pg
    tenant, h = tenant_of()
    other, oh = tenant_of()
    try:
        agent_id = make_agent(client, h)
        import uuid

        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 201
        key = _key(client, h)
        _post(client, key, _proto([{"span_id": ROOT, "attributes": {"pydanticos.run_id": run_id}}]))
        assert client.get(f"/api/v1/traces/{TRACE}", headers=h).json()["run_id"] == run_id
        okey = _key(client, oh)
        _post(client, okey, _proto([{"span_id": ROOT, "attributes": {"pydanticos.run_id": run_id}}]))
        assert client.get(f"/api/v1/traces/{TRACE}", headers=oh).json()["run_id"] is None
        assert client.get("/api/v1/traces/not-a-trace-id", headers=h).status_code == 422
    finally:
        _cleanup(app, tenant, other)


def test_forward_endpoint_receives_the_raw_request(pg, tenant_of, monkeypatch):
    client, app = pg
    tenant, h = tenant_of()
    seen = []
    import eap_api.routes.otlp as otlp

    monkeypatch.setattr(otlp, "_forward", lambda endpoint, content_type, body: seen.append((endpoint, content_type, body)))
    try:
        key = _key(client, h)
        body = _proto()
        assert _post(client, key, body).status_code == 200
        assert seen == []  # off by default
        monkeypatch.setenv("EAP_OTLP_FORWARD_ENDPOINT", "http://collector:4318/v1/traces")
        assert _post(client, key, body).status_code == 200
        assert seen == [("http://collector:4318/v1/traces", "application/x-protobuf", body)]
    finally:
        _cleanup(app, tenant)
