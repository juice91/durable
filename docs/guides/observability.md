# Observability

Every run leaves a record you can read afterwards: its events, its spans, what each model request used and cost, and who
decided what.

## In Studio

- **Traces** lists runs with filters for status, time, model, agent and kind. Opening one shows the span tree and
  timeline, the events, scores and logs. Feedback can be attached to a trace, and a run can be scored or added to a test
  suite.
- **Metrics** shows run counts, latency, tokens and cost over time, including how many runs were recovered after a
  restart.
- **Logs** searches the platform's logs. It needs a Loki server (`EAP_LOKI_URL`).

## Through the API

```sh
curl "$API/api/v1/runs?status=failed&kind=agent&limit=20" -H "Authorization: Bearer $TOKEN"
curl "$API/api/v1/runs/$RUN_ID/events" -H "Authorization: Bearer $TOKEN"
curl "$API/api/v1/runs/$RUN_ID/trace" -H "Authorization: Bearer $TOKEN"
curl "$API/api/v1/metrics/summary" -H "Authorization: Bearer $TOKEN"
curl "$API/api/v1/audit" -H "Authorization: Bearer $TOKEN"
```

## OpenTelemetry

The API and the worker export traces with OpenTelemetry (`OTEL_EXPORTER_OTLP_ENDPOINT`). With the Compose quick start,
turn on the bundled Grafana stack in `.env`:

```sh
COMPOSE_PROFILES=observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318
EAP_LOKI_URL=http://lgtm:3100
```

Grafana is then on `http://localhost:3001`. That stack has no authentication on its ingest and query APIs: use it for
trying out only.

## Traces from other programs

Anything that exports OTLP over HTTP can send its traces to the platform with an API key, and they appear in Studio next
to the platform's own: see [External OpenTelemetry traces](../observability/external-traces.md).
