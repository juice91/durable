# Durable

Durable is a self-hosted platform for building, running and observing AI agents and workflows.
Every run is a durable record in Postgres: a crash never loses a run, and an external side effect
is never performed twice.

Studio is the web console. A FastAPI control plane records runs and puts them on a queue. A DBOS
worker executes every agent, tool call and workflow as a durable workflow, one recorded step at a
time. The API and the worker never call each other; they meet in Postgres.

## Features

| Area | What is in this repository |
| --- | --- |
| **Agents** | Drafts, immutable published versions, compare and restore. One generic Pydantic AI agent; the model, toolset, processors, knowledge bases and memory are resolved per run from the published version. |
| **Runs** | Idempotent start by `run_id`, a gap-free event log with per-event keys, SSE streaming that resumes from any sequence number, token usage and cost per model request, one OpenTelemetry trace per run. |
| **Conversations** | Multi-turn runs with history trimming; one active turn per conversation, enforced under a row lock. |
| **Tools and MCP** | Built-in tools (see `docs/tools/`), tenant-registered MCP servers over Streamable HTTP, and an in-platform MCP runtime with a catalog. Egress is policy-checked; header secrets are encrypted and never returned. |
| **Workflows** | A declarative interpreter with agent steps, tool calls, conditions, parallel branches, loops and human approvals. Branch conditions are data, not code. Approvals survive worker restarts; approve, reject and timeout race for one key and exactly one wins. |
| **Knowledge bases** | Upload, extract, chunk, embed (pgvector, HNSW) and retrieve with citations. Ingest is itself a durable workflow that resumes from the last committed batch. Source sync keeps external document sets current. |
| **Memory** | Working memory (template or JSON schema), observational memory (observer and reflector), semantic recall, retention sweeps and a forget API with an audit record. |
| **Guardrails** | Declarative processors on input and output: PII redaction, RE2 blocklists, length limits, moderation. |
| **Evals** | Datasets, scorers and experiments, with comparison across agent versions. |
| **Observability** | Traces, tenant-tagged logs, metrics, external OTLP trace ingest, and a trace view in Studio. |
| **Multi-tenancy** | Every table and query is tenant-scoped; another tenant's id answers 404. A dev bearer for local work, and tenant API keys with scopes (`agui:run`, `mcp`, `manage`, `otlp`). |
| **AG-UI** | The durable event log is the source of truth; AG-UI is an output adapter for external frontends. See `docs/ag-ui.md` and `examples/copilotkit-agui`. |

## Architecture

```
Studio (Next.js) ──HTTPS / SSE──▶ API (FastAPI)
                                     │ insert run, enqueue
                                     ▼
                                  Postgres  ◀──events, NOTIFY── Worker (DBOS + Pydantic AI)
                           eap_app · eap_dbos · pgvector            │
                                                                    ├──▶ model gateway (allowlisted)
                                                                    └──▶ MCP servers (egress policy)
         all three ──OTLP──▶ Grafana LGTM (Tempo, Loki, Prometheus)
```

Design rules the code enforces:

- **One run, one DBOS workflow.** The workflow id is the run id. Recovery replays recorded steps instead of re-executing them.
- **Events are idempotent and gap-free.** Each event has a unique key; `seq` is assigned under a row lock.
- **Side effects are reserve, call, complete.** A crash between call and complete ends the run in `needs_attention`; the call is never repeated.
- **Non-determinism is recorded.** Clocks, "latest version" lookups and deadlines are read once inside a step.
- **Package boundaries are build failures.** The import-linter contracts in `pyproject.toml` keep, for example, the API from importing the worker, the agent runtime or Pydantic AI, and allow only `eap_observability` to import OpenTelemetry.
- **Alembic owns the schema.** The API and worker refuse to start unless the database is at the latest migration.

The crash-injection suite in `tests/recovery` kills real API and worker processes (`os._exit(137)`,
the same as `kill -9`) at named points and checks the rules above. See
[tests/recovery/README.md](./tests/recovery/README.md).

## Quick start

Prerequisites: Python 3.13, [uv](https://docs.astral.sh/uv/), Node with pnpm 9, and Docker (for
Postgres and the Grafana LGTM stack).

```bash
git clone https://github.com/juice91/durable.git && cd durable
uv sync
pnpm install

cp .env.example .env            # local Postgres credentials
cp .dev.env.example .dev.env    # model gateway settings and optional overrides

bash scripts/dev-up.sh          # postgres, lgtm, worker, api, studio; migrations run after postgres
bash scripts/dev-status.sh      # one line per service
```

Then open Studio at <http://localhost:3000>. The API listens on `127.0.0.1:8000`, Grafana on
`127.0.0.1:3002`, and Postgres on `127.0.0.1:54329`. In dev mode the API accepts
`Authorization: Bearer dev:<tenant>:<user>`.

Without a model gateway you can still run agents with the scripted test models (`test:ticket`,
`test:hash`, `test:retrieve`, `test:moderation` and others in `apps/worker/src/eap_worker/models.py`).
The test suites use them, so no test calls a real LLM.

Stop the app services with `bash scripts/dev-down.sh`; `--all` also stops the Docker services.

## Repository layout

Python modules use the `eap_` prefix, and the root `pyproject.toml` and `package.json` keep their
original project names, for historical reasons. The product is Durable.

```
apps/
  api/             FastAPI control plane (eap_api)
  worker/          DBOS worker: agent, tool, workflow, ingest, memory and retention workflows (eap_worker)
  studio/          Next.js console
packages/
  shared-schemas/  Pure contracts: agent and workflow specs, tools, processors, memory (eap_schemas)
  db/              Alembic migrations, the only schema owner (eap_db)
  run-store/       Runs, events, side effects, conversations, usage, memory stores (eap_run_store)
  agent-runtime/   Tool registry, run dependencies, run_side_effect (eap_runtime)
  knowledge/       Document extraction and chunking (eap_knowledge)
  kb-sources/      Knowledge source sync framework and adapter registry (eap_kb_sources)
  processors/      RE2 guardrail engine (eap_processors)
  secrets/         Encryption, egress policy, redaction (eap_secrets)
  observability/   The only package that imports OpenTelemetry (eap_observability)
  pricing/         Model price table and cost calculation (eap_pricing)
  mcp-runtime/     In-platform MCP server runtime (eap_mcp_runtime)
  blob-store/      Attachment storage, S3 compatible (eap_blob_store)
config/            Model price tables
examples/          copilotkit-agui, workflow definitions
infrastructure/    Docker Compose file for the dev Postgres and LGTM services
deploy/            Container images (deploy/docker), Kubernetes manifests (deploy/k8s), deploy scripts
tests/             Crash-injection matrix (recovery), AG-UI smoke test, deploy and dev-script tests
scripts/           Dev stack, test runner, CI helpers
docs/              Design documents and feature guides
```

## Testing

```bash
bash scripts/test.sh
```

This runs, in order: `uv run lint-imports`, every pytest suite from the repository root (including
`tests/recovery`), the API suite again against Postgres, the Studio vitest suite, the dev-stack
lifecycle test, and, when Docker is running, the pgvector upgrade test and the AG-UI smoke test.
Postgres-backed suites need the dev database (`bash scripts/dev-db.sh up`) and skip with a reason
when it is unreachable. Individual suites:

```bash
uv run pytest apps/api -q
uv run pytest tests/recovery
pnpm -C apps/studio test
```

CI (`.github/workflows/ci.yml`) runs on pull requests and pushes to `main`: import contracts and
DB-free unit tests, Studio lint, type check and vitest shards, and deploy manifest checks. A pull
request runs only the tests its change affects. See [docs/ci.md](./docs/ci.md) for the selection
rules.

## Configuration

Settings are `EAP_*` environment variables; [.dev.env.example](./.dev.env.example) documents them
with their defaults. The ones you will touch first:

| Variable | Purpose |
| --- | --- |
| `EAP_DATABASE_URL`, `DBOS_SYSTEM_DATABASE_URL` | Application and DBOS system databases (set in `.env`). |
| `EAP_AUTH_MODE` | `dev` (local bearer) or `api_key` (tenant keys only). Unset refuses to start. |
| `EAP_ALLOWED_MODELS`, `EAP_ALLOWED_EMBEDDING_MODELS` | Model allowlists, checked at publish, at run start and before every call. |
| `EAP_LITELLM_BASE_URL`, `LITELLM_API_KEY` (or `EAP_LITELLM_API_KEY_CMD`) | OpenAI-compatible model gateway. |
| `EAP_APP_VERSION`, `EAP_EXECUTOR_ID` | DBOS application version and worker identity; recovery is bound to both. |

## Design documents

The index is [docs/design/README.md](./docs/design/README.md).

| Document | English | 繁體中文 | Question it answers |
| --- | --- | --- | --- |
| System specification | [EN](./docs/design/system-spec.md) | [繁中](./docs/design/system-spec.zh-TW.md) | What must the system do: requirements, interfaces and acceptance criteria. |
| Architecture design | [EN](./docs/design/architecture.md) | [繁中](./docs/design/architecture.zh-TW.md) | How it is built: components, run lifecycle, the durable-execution contract and security boundaries. |
| AI-assisted development method | [EN](./docs/design/ai-assisted-development.md) | [繁中](./docs/design/ai-assisted-development.zh-TW.md) | How it was built: the workflow, quality gates and agent harness used to develop it. |

Feature guides live in `docs/`: [AG-UI](./docs/ag-ui.md), [concurrency](./docs/concurrency.md),
[evals](./docs/evals.md), [knowledge sources](./docs/kb-sources.md),
[knowledge import](./docs/knowledge-import.md), [MCP control](./docs/mcp-control.md),
[MCP servers](./docs/mcp-servers.md), [observability](./docs/observability/),
[providers](./docs/providers/), [tools](./docs/tools/) and [workflows](./docs/workflows/).

## Contributing

- [CONTRIBUTING.md](./CONTRIBUTING.md): setup, testing and commit conventions.
- [SECURITY.md](./SECURITY.md): how to report a vulnerability privately.
- [CODE_OF_CONDUCT.md](./CODE_OF_CONDUCT.md): community standards.
- [CLA.md](./CLA.md): every contributor must sign the Contributor License Agreement before a pull
  request can be merged. The CLA bot comments on your first pull request with instructions; one
  signature covers all of your future pull requests.

## Licence

Durable is licensed under the Apache License 2.0. See [LICENSE](./LICENSE) and [NOTICE](./NOTICE).
Third-party notices are in [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).
