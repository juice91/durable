"""eap_observability.otlp: decode both OTLP encodings, validate, clean, answer."""

from __future__ import annotations

import json
import time

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue, KeyValueList

from eap_observability import otlp
from eap_observability.redaction import redact_attribute

NOW = time.time_ns()
TRACE, SPAN = "0af7651916cd43dd8448eb211c80319c", "b7ad6b7169203331"


def _pb(attributes: list[KeyValue] | None = None, count: int = 1) -> bytes:
    request = ExportTraceServiceRequest()
    rs = request.resource_spans.add()
    rs.resource.attributes.append(KeyValue(key="service.name", value=AnyValue(string_value="svc")))
    for i in range(count):
        span = rs.scope_spans.add().spans.add()
        span.trace_id, span.span_id = bytes.fromhex(TRACE), (i + 1).to_bytes(8, "big")
        span.name, span.start_time_unix_nano, span.end_time_unix_nano = "op", NOW - 2, NOW - 1
        span.attributes.extend(attributes or [])
    return request.SerializeToString()


def test_protobuf_and_json_decode_to_the_same_shape():
    attrs = [
        KeyValue(key="s", value=AnyValue(string_value="v")),
        KeyValue(key="n", value=AnyValue(int_value=3)),
        KeyValue(key="a", value=AnyValue(array_value=ArrayValue(values=[AnyValue(string_value="x")]))),
        KeyValue(key="m", value=AnyValue(kvlist_value=KeyValueList(values=[KeyValue(key="k", value=AnyValue(bool_value=True))]))),
    ]
    pb = otlp.decode_export("application/x-protobuf; charset=utf-8", _pb(attrs))[0]
    body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]},
        "scopeSpans": [{"spans": [{"traceId": TRACE, "spanId": (1).to_bytes(8, "big").hex(), "name": "op",
            "startTimeUnixNano": str(NOW - 2), "endTimeUnixNano": NOW - 1, "attributes": [
                {"key": "s", "value": {"stringValue": "v"}}, {"key": "n", "value": {"intValue": "3"}},
                {"key": "a", "value": {"arrayValue": {"values": [{"stringValue": "x"}]}}},
                {"key": "m", "value": {"kvlistValue": {"values": [{"key": "k", "value": {"boolValue": True}}]}}}]}]}]}]}
    js = otlp.decode_export("application/json", json.dumps(body).encode())[0]
    assert pb == js
    assert pb.attributes == {"s": "v", "n": 3, "a": ["x"], "m": {"k": True}}


def test_bad_bodies_and_types():
    with pytest.raises(otlp.OtlpDecodeError):
        otlp.decode_export("application/json", b"{not json")
    with pytest.raises(otlp.OtlpDecodeError):
        otlp.decode_export("application/x-protobuf", b"\xff\xff\xff\xff")
    with pytest.raises(otlp.OtlpDecodeError):
        otlp.decode_export("text/plain", b"")
    with pytest.raises(otlp.OtlpTooManySpans):
        otlp.decode_export("application/x-protobuf", _pb(count=otlp.MAX_SPANS_PER_REQUEST + 1))


def test_prepare_rejects_bad_ids_and_times_and_keeps_good_spans():
    good = otlp.decode_export("application/x-protobuf", _pb())[0]
    def variant(**kw):
        return otlp.RawSpan(**{**good.__dict__, **kw})
    bad = [variant(trace_id="abc"), variant(span_id="0" * 16), variant(parent_span_id="xyz"),
           variant(start_ns=0), variant(end_ns=good.start_ns - 1), variant(start_ns=NOW + 10**15)]
    out = otlp.prepare_spans([good, *bad], NOW)
    assert len(out.spans) == 1 and out.rejected == len(bad) and out.error
    span = out.spans[0]
    assert span["trace_id"] == TRACE and span["parent_id"] is None and span["service_name"] == "svc"
    assert span["kind"] == "external" and span["status"] == "ok"


def test_prepare_bounds_and_redacts_attributes():
    attrs = [KeyValue(key=f"k{i}", value=AnyValue(string_value="Bearer abcdef123456 sk-abcdefghij")) for i in range(80)]
    attrs.append(KeyValue(key="password", value=AnyValue(string_value="x" * 10)))
    span = otlp.prepare_spans(otlp.decode_export("application/x-protobuf", _pb(attrs)), NOW).spans[0]
    values = json.dumps(span["attributes"])
    assert "abcdef123456" not in values and "sk-abcdefghij" not in values
    assert len(span["attributes"]) <= otlp.MAX_ATTRIBUTES_PER_SPAN + 3
    assert redact_attribute("my.api_key", 12345) == "[REDACTED]"
    assert redact_attribute("note", ["token=abc123"]) == ["token=[REDACTED]"]


def test_responses_in_both_encodings():
    assert otlp.encode_response("application/json") == (b"{}", "application/json")
    body, kind = otlp.encode_response("application/json", 2, "bad")
    assert json.loads(body) == {"partialSuccess": {"rejectedSpans": 2, "errorMessage": "bad"}}
    body, kind = otlp.encode_response("application/x-protobuf", 0)
    assert body == b"" and kind == "application/x-protobuf"
    body, _ = otlp.encode_response("application/x-protobuf", 2, "bad")
    parsed = ExportTraceServiceResponse.FromString(body)
    assert (parsed.partial_success.rejected_spans, parsed.partial_success.error_message) == (2, "bad")
