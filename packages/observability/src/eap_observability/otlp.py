"""Decode, validate and clean an OTLP/HTTP trace export from outside the platform (external traces).

`decode_export(content_type, body)` reads an `ExportTraceServiceRequest` in either encoding
(`application/x-protobuf`, `application/json`; no gRPC) into plain `RawSpan`s: this is the only place
that touches the OpenTelemetry protobuf classes (the API may not import OpenTelemetry). `prepare_spans`
then drops what cannot be stored and cleans what can: ids and timestamps are checked, the attribute
count and size are bounded, and every attribute value passes `redact_attribute` (credential-like keys
hide the value; strings lose bearer tokens, `sk-` keys, URL passwords and platform API keys). A
tenant or key named in an attribute is never read: the caller decides the tenant.

`encode_response(content_type, rejected, message)` answers with the `ExportTraceServiceResponse`: `{}` or
an empty message when every span was accepted, else `partialSuccess` with `rejectedSpans` and
`errorMessage`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTracePartialSuccess,
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

from eap_observability.redaction import REDACTED, redact, redact_attribute

PROTOBUF = "application/x-protobuf"
JSON = "application/json"

MAX_SPANS_PER_REQUEST = 1000
MAX_ATTRIBUTES_PER_SPAN = 64
MAX_RESOURCE_ATTRIBUTES = 32
MAX_ATTRIBUTE_KEY_CHARS = 128
MAX_ATTRIBUTE_VALUE_CHARS = 2000
MAX_ARRAY_ITEMS = 32
MAX_VALUE_DEPTH = 3
MAX_NAME_CHARS = 256
MAX_SERVICE_NAME_CHARS = 128
MAX_STATUS_MESSAGE_CHARS = 300
MAX_FUTURE = 24 * 3600 * 10**9  # a span starting more than a day ahead is a client clock bug
MIN_START_NS = 946684800 * 10**9  # 2000-01-01

SPAN_KINDS = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_SPAN_ID = re.compile(r"[0-9a-f]{16}")
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


class OtlpDecodeError(ValueError):
    """The body is not a valid export request in the stated encoding (HTTP 400)."""


class OtlpTooManySpans(ValueError):
    """More than MAX_SPANS_PER_REQUEST spans in one export (HTTP 413)."""


@dataclass
class RawSpan:
    trace_id: str
    span_id: str
    parent_span_id: str
    name: str
    kind: int
    start_ns: int
    end_ns: int
    status_code: int
    status_message: str
    attributes: dict[str, Any]
    resource: dict[str, Any]
    scope: str


@dataclass
class Prepared:
    spans: list[dict[str, Any]] = field(default_factory=list)
    rejected: int = 0
    error: str | None = None


# ----------------------------------------------------------------------------- decode


def _hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value or "").lower()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        raise OtlpDecodeError("a numeric field is not a number") from None


def _any_json(value: Any, depth: int = 0) -> Any:
    if not isinstance(value, dict) or depth > MAX_VALUE_DEPTH:
        return None
    if "stringValue" in value:
        return str(value["stringValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        return _int(value["intValue"])
    if "doubleValue" in value:
        try:
            return float(value["doubleValue"])
        except (TypeError, ValueError):
            return None
    if "bytesValue" in value:
        return f"<{len(str(value['bytesValue']))} bytes>"
    if "arrayValue" in value:
        items = (value["arrayValue"] or {}).get("values") or []
        return [_any_json(v, depth + 1) for v in items[:MAX_ARRAY_ITEMS]]
    if "kvlistValue" in value:
        return _kv_json((value["kvlistValue"] or {}).get("values") or [], depth + 1)
    return None


def _kv_json(items: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(items, list):
        raise OtlpDecodeError("attributes must be a list")
    out: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _any_json(item.get("value"), depth)
    return out


def _any_pb(value: AnyValue, depth: int = 0) -> Any:
    which = value.WhichOneof("value")
    if which is None or depth > MAX_VALUE_DEPTH:
        return None
    if which == "string_value":
        return value.string_value
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return value.int_value
    if which == "double_value":
        return value.double_value
    if which == "bytes_value":
        return f"<{len(value.bytes_value)} bytes>"
    if which == "array_value":
        return [_any_pb(v, depth + 1) for v in list(value.array_value.values)[:MAX_ARRAY_ITEMS]]
    return _kv_pb(value.kvlist_value.values, depth + 1)


def _kv_pb(items: Any, depth: int = 0) -> dict[str, Any]:
    return {kv.key: _any_pb(kv.value, depth) for kv in items}


def _decode_json(body: bytes) -> list[RawSpan]:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise OtlpDecodeError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise OtlpDecodeError("body must be an ExportTraceServiceRequest object")
    spans: list[RawSpan] = []
    for rs in data.get("resourceSpans") or []:
        if not isinstance(rs, dict):
            raise OtlpDecodeError("resourceSpans entries must be objects")
        resource = _kv_json(((rs.get("resource") or {}).get("attributes")) or [])
        for ss in rs.get("scopeSpans") or []:
            if not isinstance(ss, dict):
                raise OtlpDecodeError("scopeSpans entries must be objects")
            scope = str(((ss.get("scope") or {}).get("name")) or "")
            for sp in ss.get("spans") or []:
                if not isinstance(sp, dict):
                    raise OtlpDecodeError("spans entries must be objects")
                if len(spans) >= MAX_SPANS_PER_REQUEST:
                    raise OtlpTooManySpans
                status = sp.get("status") or {}
                spans.append(RawSpan(
                    trace_id=_hex(sp.get("traceId")), span_id=_hex(sp.get("spanId")),
                    parent_span_id=_hex(sp.get("parentSpanId")), name=str(sp.get("name") or ""),
                    kind=_int(sp.get("kind")), start_ns=_int(sp.get("startTimeUnixNano")),
                    end_ns=_int(sp.get("endTimeUnixNano")), status_code=_int(status.get("code")),
                    status_message=str(status.get("message") or ""),
                    attributes=_kv_json(sp.get("attributes") or []), resource=resource, scope=scope,
                ))
    return spans


def _decode_protobuf(body: bytes) -> list[RawSpan]:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except DecodeError:
        raise OtlpDecodeError("body is not a valid protobuf ExportTraceServiceRequest") from None
    spans: list[RawSpan] = []
    for rs in request.resource_spans:
        resource = _kv_pb(rs.resource.attributes)
        for ss in rs.scope_spans:
            for sp in ss.spans:
                if len(spans) >= MAX_SPANS_PER_REQUEST:
                    raise OtlpTooManySpans
                spans.append(RawSpan(
                    trace_id=_hex(sp.trace_id), span_id=_hex(sp.span_id), parent_span_id=_hex(sp.parent_span_id),
                    name=sp.name, kind=int(sp.kind), start_ns=sp.start_time_unix_nano, end_ns=sp.end_time_unix_nano,
                    status_code=int(sp.status.code), status_message=sp.status.message,
                    attributes=_kv_pb(sp.attributes), resource=resource, scope=ss.scope.name,
                ))
    return spans


def media_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def decode_export(content_type: str | None, body: bytes) -> list[RawSpan]:
    """The spans of an export request. Raises OtlpDecodeError (400) for a malformed body and
    OtlpTooManySpans (413) past MAX_SPANS_PER_REQUEST; an unsupported content type is the caller's
    415 (`media_type`)."""
    kind = media_type(content_type)
    if kind == PROTOBUF:
        return _decode_protobuf(body)
    if kind == JSON:
        return _decode_json(body)
    raise OtlpDecodeError(f"unsupported content type {kind or '(none)'}")


# ----------------------------------------------------------------------------- validate and clean


def _text(value: str, limit: int) -> str:
    return redact(_UNSTORABLE.sub("", value), max_length=limit)


def _clean_attributes(raw: dict[str, Any], limit: int) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if len(cleaned) >= limit:
            break
        key = _UNSTORABLE.sub("", str(key))[:MAX_ATTRIBUTE_KEY_CHARS]
        if not key:
            continue
        cleaned[key] = _strip(redact_attribute(key, value, max_length=MAX_ATTRIBUTE_VALUE_CHARS))
    return cleaned


def _strip(value: Any) -> Any:
    if isinstance(value, str):
        return _UNSTORABLE.sub("", value)
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, dict):
        return {_UNSTORABLE.sub("", str(k)): _strip(v) for k, v in value.items()}
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None  # JSONB cannot hold NaN or Infinity
    return value


def _ts(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)


def _problem(span: RawSpan, now_ns: int) -> str | None:
    if not _TRACE_ID.fullmatch(span.trace_id) or span.trace_id == "0" * 32:
        return "trace_id must be 16 bytes (32 hex characters), not all zero"
    if not _SPAN_ID.fullmatch(span.span_id) or span.span_id == "0" * 16:
        return "span_id must be 8 bytes (16 hex characters), not all zero"
    if span.parent_span_id and (not _SPAN_ID.fullmatch(span.parent_span_id) or span.parent_span_id == "0" * 16):
        return "parent_span_id must be empty or 8 bytes (16 hex characters)"
    if span.start_ns < MIN_START_NS or span.start_ns > now_ns + MAX_FUTURE:
        return "start_time_unix_nano is missing or out of range"
    if span.end_ns and span.end_ns < span.start_ns:
        return "end_time_unix_nano is before the start"
    if span.end_ns > now_ns + MAX_FUTURE:
        return "end_time_unix_nano is out of range"
    return None


def prepare_spans(raw: list[RawSpan], now_ns: int) -> Prepared:
    """Rows for the span store from the decoded spans, plus how many were rejected and the first
    reason. `now_ns` is the clock (a parameter for tests)."""
    out = Prepared()
    for span in raw:
        reason = _problem(span, now_ns)
        if reason is not None:
            out.rejected += 1
            out.error = out.error or reason
            continue
        resource = _clean_attributes(span.resource, MAX_RESOURCE_ATTRIBUTES)
        attributes = _clean_attributes(span.attributes, MAX_ATTRIBUTES_PER_SPAN)
        attributes["otel.span_kind"] = SPAN_KINDS.get(span.kind, "unspecified")
        if span.scope:
            attributes["otel.scope"] = _text(span.scope, MAX_ATTRIBUTE_KEY_CHARS)
        if span.status_message:
            attributes["otel.status_message"] = _text(span.status_message, MAX_STATUS_MESSAGE_CHARS)
        service = resource.get("service.name")
        out.spans.append({
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_id": span.parent_span_id or None,
            "kind": "external",
            "name": _text(span.name, MAX_NAME_CHARS) or "(unnamed span)",
            "start": _ts(span.start_ns),
            "end": _ts(span.end_ns) if span.end_ns else None,
            "status": "error" if span.status_code == 2 else ("ok" if span.end_ns else "incomplete"),
            "service_name": service[:MAX_SERVICE_NAME_CHARS] if isinstance(service, str) and service != REDACTED else None,
            "attributes": attributes,
            "resource_attributes": resource,
        })
    return out


# ----------------------------------------------------------------------------- response


def encode_response(content_type: str | None, rejected: int = 0, message: str | None = None) -> tuple[bytes, str]:
    """The `ExportTraceServiceResponse` body and its content type, in the request's encoding."""
    partial = rejected > 0
    if media_type(content_type) == PROTOBUF:
        response = ExportTraceServiceResponse()
        if partial:
            response.partial_success.CopyFrom(
                ExportTracePartialSuccess(rejected_spans=rejected, error_message=(message or "")[:500])
            )
        return response.SerializeToString(), PROTOBUF
    body: dict[str, Any] = {}
    if partial:
        body["partialSuccess"] = {"rejectedSpans": rejected, "errorMessage": (message or "")[:500]}
    return json.dumps(body, separators=(",", ":")).encode(), JSON


__all__ = [
    "JSON", "PROTOBUF", "MAX_SPANS_PER_REQUEST", "MAX_ATTRIBUTES_PER_SPAN", "OtlpDecodeError", "OtlpTooManySpans",
    "Prepared", "RawSpan", "decode_export", "encode_response", "media_type", "prepare_spans",
]
