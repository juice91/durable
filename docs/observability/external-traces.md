# External OpenTelemetry traces

A program running outside the platform can send its OpenTelemetry traces to the platform with a
tenant API key. They appear in Studio's Traces page next to the platform's own, marked **External**,
with the service name. One standard endpoint replaces a per-framework SDK integration: anything that
can export OTLP over HTTP works (the OpenTelemetry SDKs, the OpenLLMetry and OpenInference
instrumentations, an OpenTelemetry Collector).

## Endpoint

`POST /api/v1/otlp/v1/traces` (OTLP/HTTP, the standard `/v1/traces` path under `/api/v1/otlp`).

- Content types: `application/x-protobuf` and `application/json`. `Content-Encoding: gzip` is accepted.
- Not supported: gRPC, and metrics and logs (`/v1/metrics`, `/v1/logs`) for now.
- Auth: `Authorization: Bearer pos_<id>_<secret>`, a tenant API key with the **`otlp` scope** (Studio, Settings,
  API keys: *Send traces (OTLP)*). In dev mode (`EAP_AUTH_MODE=dev`) the dev bearer works too. A key without the
  scope, such as an AG-UI or MCP key, is refused with 403; the `otlp` scope gives access to nothing else.
- The tenant is the key's. A `tenant` or `tenant.id` resource or span attribute is just an attribute and is never
  used to pick a tenant.

### Answers

| Status | When |
| --- | --- |
| 200 | Accepted. Body: the `ExportTraceServiceResponse` in the request's encoding: `{}` (JSON) or an empty message (protobuf). If some spans were dropped: `partialSuccess` with `rejectedSpans` and `errorMessage` (the first reason). |
| 400 | The body is not a valid export request in the stated encoding, or invalid gzip. |
| 401 | No key, or an unknown, revoked or expired one. |
| 403 | The key has no `otlp` scope, or its allowed origin does not match the `Origin` header (a browser exporter). |
| 413 | Body over 4 MiB (also after decompressing), or more than 1000 spans in one request. |
| 415 | Another content type (including `application/grpc`), or a `Content-Encoding` other than gzip. |
| 429 | The key's rate limit is used up; `Retry-After` says when to retry. |

A retried export is harmless: a span already stored (same trace id and span id) is kept as first stored.

## Limits and validation

| What | Limit |
| --- | --- |
| Request body | 4 MiB |
| Spans per request | 1000 (more is a 413 for the whole request) |
| Attributes per span | 64 (the rest are dropped); 32 resource attributes |
| Attribute key / value | 128 / 2000 characters (cut, with a marker); lists keep 32 items, nesting 3 levels |
| Span name | 256 characters |
| Rate | `EAP_OTLP_RATE_LIMIT_PER_MINUTE` requests per key per API replica (default 600, also the burst; 0 turns it off) |

A span is dropped (and counted in `rejectedSpans`) when its trace id is not 16 bytes or is all zero, its span id
is not 8 bytes or is all zero, its parent id is neither empty nor 8 bytes, its start time is missing, before
2000 or more than a day ahead, or its end time is before its start. Trace and span ids are stored as sent, so a
client can correlate them with its own logs.

## What is stored, and redaction

Each span keeps its name, start, end, status (`error` for `STATUS_CODE_ERROR`; a span without an end is
`incomplete`), parent link, OpenTelemetry kind and scope name, status message, the span's attributes and the
resource's attributes (with `service.name` as the service), plus the id of the key that sent it. Events and links
are not stored.

Before anything is stored, every attribute value passes the platform's redaction: bearer and basic tokens, `sk-`
keys, platform API keys and `user:password@` URLs inside strings become `[REDACTED]`, and the whole value of an
attribute whose key looks like a credential (`api_key`, `token`, `secret`, `password`, `authorization`, `cookie`,
...) is replaced. Redaction cannot recognise prompts or model output: it is the last line, not a substitute for not
sending them. If prompts or completions must not leave your program, do not record them as span attributes.

## Connect

Set the standard OpenTelemetry exporter variables; the key comes from the environment, never from code:

```sh
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://<host>/api/v1/otlp/v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer $PYDANTICOS_API_KEY"
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_SERVICE_NAME=my-agent
```

Python:

```python
# pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))  # reads OTEL_EXPORTER_OTLP_*
trace.set_tracer_provider(provider)

with trace.get_tracer("my-agent").start_as_current_span("answer") as span:
    span.set_attribute("pydanticos.run_id", "<a platform run id>")  # optional
```

JavaScript (Node):

```js
// npm i @opentelemetry/sdk-node @opentelemetry/exporter-trace-otlp-proto
import { NodeSDK } from "@opentelemetry/sdk-node";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";

const sdk = new NodeSDK({ traceExporter: new OTLPTraceExporter() }); // reads OTEL_EXPORTER_OTLP_*
sdk.start();
```

The JSON encoding (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`) works the same way. Send from a server: the endpoint
has no CORS allowance for arbitrary browser origins.

## Reading them back

- Studio: Traces, **Source** filter (All, Platform, External). An external trace shows its service as a badge; its
  sheet lists the resource attributes and the span tree with each span's attributes.
- `GET /api/v1/traces` lists the tenant's external traces (newest first, one item per trace id: `trace_id`,
  `name` (the root span), `service_name`, `start`, `end`, `span_count`, `status`); filters `service`, `q`,
  `status` (`ok` | `error`), `from`, `to`; cursor `before` / `next_before`.
- `GET /api/v1/traces/{trace_id}` reads one: `resource_attributes`, `spans` with attributes, `run_id`.
- `GET /api/v1/spans?source=external` lists the spans flat (every span carries `source`, `trace_id`,
  `service_name`; `source=platform` is the platform's own).

## Correlating with platform runs

Set the span attribute **`pydanticos.run_id`** to the id of a platform run (a UUID, for instance the run an agent
started for your program). When that run belongs to the key's tenant, `GET /traces/{trace_id}` returns it as
`run_id`, and Studio's external trace sheet links to the run's own trace. A run id of another tenant, or an unknown
one, is ignored.

## Operations

- `EAP_OTLP_RATE_LIMIT_PER_MINUTE` (default 600; 0 = off).
- `EAP_OTLP_FORWARD_ENDPOINT` (default empty = off): also forwards every received request, **unchanged** (so not
  redacted, and with whatever attributes the client set), to that OTLP/HTTP URL, for example the cluster's
  collector `http://lgtm:4318/v1/traces`. Best effort: a failure is logged and never fails the request.
- Storage: the `spans` table (migration `0032_external_spans`), `source = 'external'`. An external trace's
  spans share a `run_id` derived from the tenant and trace id; no run has it.
