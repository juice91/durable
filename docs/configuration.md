# Configuration

Settings are environment variables of the API and the worker. The Compose quick start reads them from `.env`
([the example file](../.env.example)); the development stack also reads `.dev.env`, and
[the development example](../.dev.env.example) documents many more settings with their defaults. On Kubernetes they come
from the ConfigMap and Secrets in `deploy/k8s`.

Never commit keys. The Compose quick start generates the platform's own secrets itself.

## Core

| Variable | Purpose |
| --- | --- |
| `EAP_DATABASE_URL` | The application database. |
| `DBOS_SYSTEM_DATABASE_URL` | The database for durable workflow state. |
| `EAP_APP_VERSION`, `EAP_EXECUTOR_ID` | Recovery is bound to both: a restarted worker recovers the runs its executor id owned. |
| `EAP_SECRETS_KEY` | Key that encrypts stored secrets (base64 of 32 random bytes). |

## Sign-in and access

| Variable | Purpose |
| --- | --- |
| `EAP_AUTH_MODE` | `dev` or `production`. Unset refuses to start. |
| `EAP_IAM_IDENTITY` | Identity providers, in order ([Identity and access](iam.md)). |
| `EAP_IAM_POLICY` | The one policy engine. |
| `EAP_IAM_MEMBERSHIP` | The membership source, at most one. |
| `EAP_IAM_ALLOW_TRANSITIONAL` | `1` accepts, in production, a policy under which every signed-in user is admin. |
| `EAP_STUDIO_AUTH` | How Studio signs people in: `local`, `zitadel` or `access` ([admin account](admin-account.md)). |

## Models

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `ANTHROPIC_API_KEY` | Direct providers. |
| `EAP_LITELLM_BASE_URL`, `EAP_LITELLM_API_KEY` | An OpenAI-compatible gateway. |
| `EAP_ALLOWED_MODELS`, `EAP_ALLOWED_EMBEDDING_MODELS` | Allowlists ([Model providers](guides/model-providers.md)). |
| `EAP_OPENAI_EMBEDDING_API_KEY`, `EAP_LITELLM_EMBEDDING_BASE_URL`, `EAP_LITELLM_EMBEDDING_API_KEY` | Embedding credentials. |
| `EAP_MODEL_PRICES_FILE` | Price table for cost accounting. |
| `EAP_ALLOWED_TOOLS` | Built-in tools that may be used. Unset allows all of them. |

## Inbound surfaces (off by default)

| Variable | Purpose |
| --- | --- |
| `EAP_WEBHOOKS_ENABLED`, `EAP_WEBHOOK_BASE_URL` | [Inbound webhooks](webhooks.md). |
| `EAP_EXTERNAL_ACCESS_ENABLED`, `EAP_EXTERNAL_BASE_URL` | [External agents](external-access.md) over MCP and A2A. |

## Workflows, knowledge and memory

| Variable | Purpose |
| --- | --- |
| `EAP_WORKFLOW_MAX_RUN_SECONDS` | No step starts once a run is older than this (14 days by default). |
| `EAP_WORKFLOW_MAX_STEP_EXECUTIONS` | Largest number of step executions one run may need, loops included. |
| `EAP_SCHEDULE_TICK` | How often due schedules are checked; `off` disables them. |
| `EAP_KB_MAX_DOCUMENT_BYTES`, `EAP_KB_MAX_INGEST_TOKENS` | Upload and ingest budgets. |
| `EAP_MEMORY_RETENTION_DAYS` | Delete memory older than this. Unset keeps it. |

## Observability

| Variable | Purpose |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Where traces are exported. Unset turns export off. |
| `EAP_LOKI_URL` | Loki server for Studio's log search. |

## Compose quick start only

| Variable | Purpose |
| --- | --- |
| `EAP_STUDIO_PORT` | Studio's port on this machine (3000). |
| `EAP_GRAFANA_PORT` | Grafana's port (3001). |
| `COMPOSE_PROFILES` | `observability` adds the Grafana stack. |
