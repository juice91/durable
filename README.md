# Durable

[![CI](https://github.com/juice91/durable/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/juice91/durable/actions/workflows/ci.yml)
[![Licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](./LICENSE)

Durable is a self-hosted platform for building, running and observing AI agents and workflows.
Every run is a durable record in Postgres: a crash never loses a run, and an external side effect
is never performed twice.

Studio is the web console. A FastAPI control plane records runs and puts them on a queue. A DBOS
worker executes every agent, tool call and workflow as a durable workflow, one recorded step at a
time. The API and the worker never call each other; they meet in Postgres.

## Quick start

Try the whole platform on one machine with Docker Compose. You need Docker with Compose 2.24 or later,
about 8 GB of memory for the first build and 5 GB of disk.

```sh
git clone https://github.com/juice91/durable.git
cd durable
cp .env.example .env              # optional: add a model provider key
docker compose up -d --wait       # first run builds the images (about 10 minutes)
docker compose exec api setup-link
```

Open the printed link, choose a username and password, and you are in Studio.

[docs/quickstart-compose.md](./docs/quickstart-compose.md) covers model providers, upgrades, backups and
reset. To change the code instead, use the [development setup](#development-setup).

## Why Durable

| Problem | How Durable handles it |
| --- | --- |
| An agent dies mid-run, and the retry sends the e-mail again or places the order twice. | Every step is persisted, and a run resumes from its last completed step. External actions follow reserve, call, complete: a crash between call and complete ends the run in `needs_attention` instead of repeating the call. |
| A workflow waits hours or days for a human approval and breaks when the server restarts. | The wait state lives in Postgres. Approvals survive worker restarts, and approve, reject and timeout race for one key so exactly one wins. |
| Several customers or departments share one platform. | Every row and query is tenant-scoped; an id that belongs to another tenant answers 404. |
| Nobody can see what an agent did or what it cost. | One OpenTelemetry trace per run, and token usage and cost recorded per model request. |
| Business users need to build flows without writing code. | Studio builds workflows, knowledge bases (RAG) and memory settings declaratively. Definitions are versioned, can be compared, and can be restored. |
| Too many moving parts to operate. | One Postgres database and two processes (API and worker), plus Studio. Everything is self-hostable. |

## Features

| Area | What is in this repository |
| --- | --- |
| **Agents** | Drafts, immutable published versions, compare and restore. One generic Pydantic AI agent; the model, toolset, processors, knowledge bases and memory are resolved per run from the published version. Optional model fallback chains. |
| **Runs** | Idempotent start by `run_id`, a gap-free event log with per-event keys, SSE streaming that resumes from any sequence number, token usage and cost per model request, one OpenTelemetry trace per run. |
| **Conversations** | Multi-turn runs with history trimming; one active turn per conversation, enforced under a row lock. |
| **Tools and MCP** | Built-in tools, tenant-registered MCP servers over Streamable HTTP, and an in-platform MCP runtime with a catalog. Egress is policy-checked; header secrets are encrypted and never returned. |
| **Workflows** | A declarative interpreter with agent steps, tool calls, branches, parallel branches, `foreach` and `loop` blocks, `evaluate` steps and human approvals. Branch conditions are data, not code. |
| **Knowledge bases** | Upload, extract, chunk, embed (pgvector, HNSW) and retrieve with citations. Ingest is itself a durable workflow that resumes from the last committed batch. Source sync keeps external document sets current. |
| **Memory** | Working memory (template or JSON schema), observational memory (observer and reflector), semantic recall, retention sweeps and a forget API with an audit record. |
| **Guardrails** | Declarative processors on input and output: PII redaction, RE2 blocklists, length limits, moderation. |
| **Evals** | Datasets, scorers and experiments, with comparison across agent versions. |
| **Observability** | Traces, tenant-tagged logs, metrics, external OTLP trace ingest, and a trace view in Studio. |
| **Multi-tenancy** | Tenant-scoped data everywhere. Tenant API keys with scopes (`agui:run`, `mcp`, `manage`, `otlp`, `run`) and personal API keys. |
| **Sign-in and access** | A built-in admin account, ZITADEL or Cloudflare Access; optional roles and per-resource grants with OpenFGA. |
| **Webhooks and external agents** | Signed inbound webhooks that start a run, and run-only MCP and A2A servers for external agents. Both are off by default. |
| **AG-UI** | The durable event log is the source of truth; AG-UI is an output adapter for external frontends (`examples/copilotkit-agui`). |
| **Architect** | A built-in agent that turns a plain-language request into an agent or workflow draft and publishes it once a person approves. |

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

## Built on

Durable is built on the Pydantic ecosystem: one set of typed models is the contract between Studio,
the API, the worker and the database, and Pydantic AI already provides a durable-execution
integration with DBOS. Versions are the ones pinned in `uv.lock`.

| Package | Version | Used for |
| --- | --- | --- |
| Pydantic (`pydantic`, `pydantic-core`) | 2.14.3, 2.50.4 | Every data contract (`eap_schemas` models), request validation in the API (including `TypeAdapter`), validation errors returned as 422. |
| Pydantic AI (`pydantic-ai-slim`) | 2.67.0 | The agent loop (`Agent`, `RunContext`). Durability through `pydantic_ai.durable_exec.dbos.DBOSDurability`, so every model request and tool call is a DBOS step. Toolsets (`FunctionToolset`, `AbstractToolset`, MCP tools resolved per run), tool approvals (`ApprovalRequired`, `DeferredToolRequests`), `FallbackModel`, `OpenAIChatModel` against an OpenAI-compatible gateway, embeddings, and `TestModel` / `FunctionModel` for the scripted `test:*` models. |
| Pydantic Evals (`pydantic-evals`) | 2.67.0 | Eval datasets and evaluators. |
| genai-prices | 0.2.3 | Cost per model request (`calc_price` in `eap_pricing`). |
| DBOS (`dbos`) | 3.5.0 | Durable execution, queues and recovery, stored in Postgres. |

Also: FastAPI 0.162.4, Alembic 1.20.1, Next.js 15 (Studio), Postgres 17 with pgvector, OpenTelemetry
with the Grafana LGTM stack, and any LiteLLM-compatible (OpenAI-compatible) model gateway.

Deliberately not used:

- **The Pydantic AI AG-UI adapter.** Durable converts to AG-UI from its own durable event log, so a client can replay a run after it reconnects.
- **fasta2a.** The A2A server applies the platform's own rules for external runs.
- **The Logfire service.** Traces go through OpenTelemetry to the Grafana LGTM stack instead.

## Development setup

For contributors: the API, the worker and Studio run from your checkout, and Docker runs only Postgres and
the Grafana LGTM stack. You need Python 3.13, [uv](https://docs.astral.sh/uv/), Node with pnpm 9, Docker,
`curl` and `jq`. If the quick-start stack is running, stop it first (`docker compose down`): both use port 3000.

```sh
uv sync --all-packages --frozen
pnpm install --frozen-lockfile
cp .dev.env.example .dev.env    # model gateway settings and optional overrides
```

To sign in to Studio with the built-in admin account, add these two lines to `.dev.env`:

```sh
EAP_IAM_IDENTITY=localadmin,apikey,devloopback
EAP_IAM_MEMBERSHIP=localadmin
```

Then start the stack:

```sh
bash scripts/dev-up.sh          # postgres, lgtm, worker, api, studio; migrations run after postgres
bash scripts/dev-status.sh      # one line per service
```

The first start prints a setup link: open it and create the account. Studio is at <http://localhost:3000>,
the API on `127.0.0.1:8000`, Grafana on `127.0.0.1:3002` and Postgres on `127.0.0.1:54329`. In dev mode the
API also accepts `Authorization: Bearer dev:<tenant>:<user>` from this machine.

Without a model gateway you can still run agents with the scripted test models (`test:ticket`,
`test:hash`, `test:retrieve`, `test:moderation` and others in `apps/worker/src/eap_worker/models.py`).
The default model allowlist of the development stack is `test:*`, and the test suites use these models,
so no test calls a real LLM.

Stop the app services with `bash scripts/dev-down.sh`; `--all` also stops the Docker services.

### A first agent in five minutes

With the development stack running, create an agent that uses the scripted `test:ticket` model and the
built-in `tickets.create` tool, publish it, start a run and stream it. These calls follow the API
end-to-end test `apps/api/api_tests/test_run_e2e.py`.

```bash
API=http://127.0.0.1:8000/api/v1
AUTH='Authorization: Bearer dev:studio:alice'   # the built-in admin's tenant

# 1. Create the agent (its draft).
AGENT_ID=$(curl -s -X POST "$API/agents" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "spec": {
    "name": "Support triage",
    "instructions": "Open a ticket for the user'"'"'s problem, then confirm it.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create"]
  }
}' | jq -r .id)

# 2. Publish the draft as version 1.
curl -s -X POST "$API/agents/$AGENT_ID/versions" -H "$AUTH" | jq '{version}'

# 3. Start a run. The client chooses run_id; sending the same body again is idempotent.
RUN_ID=$(uuidgen | tr 'A-Z' 'a-z')
curl -s -X POST "$API/agents/$AGENT_ID/runs" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\", \"input\": \"The printer is on fire\", \"version\": null}"

# 4. Stream it over SSE. Each durable event arrives as `event: run_event` with `id: <seq>`,
#    from run_started through tool_call and tool_result to run_completed.
curl -N "$API/runs/$RUN_ID/stream" -H "$AUTH"

# Resume after event 1, as a reconnecting client would.
curl -N "$API/runs/$RUN_ID/stream" -H "$AUTH" -H 'Last-Event-ID: 1'
```

The run then appears in Studio under Runs, with its events and its trace. To use a real model,
add it to `EAP_ALLOWED_MODELS` and change `model` in the spec.

## Workflow example

[`examples/workflows/`](./examples/workflows/) holds runnable workflow definitions. Each file has a
`description`, the `agents` it needs, a sample `input` and the workflow `spec`.

[`foreach-triage.json`](./examples/workflows/foreach-triage.json) triages a batch of tickets with an
agent, four at a time, and opens an escalation ticket for urgent ones; a failed item becomes an
error entry instead of failing the run. It uses the scripted `test:structured` model, so it runs
without an LLM.

To load it: create and publish each agent in `agents` (as above) and put its id in place of the
`${agents.<name>}` placeholder; then `POST /api/v1/workflows` with `{"spec": ...}`, publish with
`POST /api/v1/workflows/{id}/versions`, and start a run with `POST /api/v1/workflows/{id}/runs` and
`{"input": ...}`. [docs/workflows/loops.md](./docs/workflows/loops.md) walks through this example
and the other loop examples, and `apps/api/api_tests/test_workflow_loops_examples.py` runs them all.

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
  iam/             Identity, policy and membership interfaces (eap_iam)
  inbound-auth/    Verification of inbound webhook signatures (eap_inbound)
plugins/           IAM implementations, one distribution each (iam-*)
config/            Model price tables
examples/          copilotkit-agui, workflow definitions
compose.yaml       The quick start: the whole platform with Docker Compose
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
request runs only the tests its change affects; [docs/ci.md](./docs/ci.md) has the selection rules.
Security scans and the CLA check have their own workflows in `.github/workflows/`.

## Configuration

Settings are `EAP_*` environment variables. The Compose quick start reads `.env`
([.env.example](./.env.example)); the development stack also reads `.dev.env`, and
[.dev.env.example](./.dev.env.example) documents the settings with their defaults.

### Sign-in

`EAP_STUDIO_AUTH` says how Studio signs people in. Nothing falls back to another mode.

| Mode | Who it is for | What it needs |
| --- | --- | --- |
| `local` | One administrator, with the built-in account. The default of the Compose file and the Kubernetes manifests. | Nothing external. |
| `zitadel` | Several users and roles. | A ZITADEL instance ([plug-in settings](./plugins/iam-zitadel/README.md)). |
| `access` | Optional: a single tenant with Cloudflare Access in front. | Cloudflare Access in front of Studio and the API. |

Each mode goes with an API identity provider; [docs/admin-account.md](./docs/admin-account.md) compares
them and describes how to switch.

### Access control and exposure

The shipped Compose file and Kubernetes ConfigMap select the `openaccess` policy: every signed-in user is an admin, which suits a trial only. To enforce roles and per-resource grants, run your own OpenFGA server: see [Switching to OpenFGA](./docs/iam.md#switching-to-openfga).

To expose Studio and the API, see [docs/expose.md](./docs/expose.md) (any Ingress or reverse proxy with TLS).

### Common settings

| Variable | Purpose |
| --- | --- |
| `EAP_DATABASE_URL`, `DBOS_SYSTEM_DATABASE_URL` | Application and DBOS system databases (set in `.env`). |
| `EAP_AUTH_MODE` | `dev` (adds a loopback-only development bearer) or `production`; `api_key` is a deprecated alias. Unset refuses to start. |
| `EAP_ALLOWED_MODELS`, `EAP_ALLOWED_EMBEDDING_MODELS` | Model allowlists, checked at publish, at run start and before every call. Unset means `test:*` only. |
| `EAP_LITELLM_BASE_URL`, `LITELLM_API_KEY` (or `EAP_LITELLM_API_KEY_CMD`) | OpenAI-compatible model gateway. |
| `EAP_APP_VERSION`, `EAP_EXECUTOR_ID` | DBOS application version and worker identity; recovery is bound to both. |

## Design documents

The index is [docs/design/README.md](./docs/design/README.md).

| Document | English | 繁體中文 | Question it answers |
| --- | --- | --- | --- |
| System specification | [EN](./docs/design/system-spec.md) | [繁中](./docs/design/system-spec.zh-TW.md) | What must the system do: requirements, interfaces and acceptance criteria. |
| Architecture design | [EN](./docs/design/architecture.md) | [繁中](./docs/design/architecture.zh-TW.md) | How it is built: components, run lifecycle, the durable-execution contract and security boundaries. |
| AI-assisted development method | [EN](./docs/design/ai-assisted-development.md) | [繁中](./docs/design/ai-assisted-development.zh-TW.md) | How it was built: the workflow, quality gates and agent harness used to develop it. |

## Feature guides

| Guide | What it covers |
| --- | --- |
| [Quick start](./docs/quickstart-compose.md) | The whole platform with Docker Compose: providers, upgrade, backup and reset. |
| [Admin account](./docs/admin-account.md) | The built-in account, the Studio sign-in modes and switching to ZITADEL. |
| [Identity and access](./docs/iam.md) | Identity providers, policy engines, roles, grants and API keys. |
| [Expose](./docs/expose.md) | Publishing Studio and the API behind an Ingress or reverse proxy. |
| [Webhooks](./docs/webhooks.md) | Starting a workflow run with a signed HTTP request. |
| [External access](./docs/external-access.md) | Letting external agents run exported workflows over MCP or A2A. |
| [AG-UI](./docs/ag-ui.md) | Serving published agents and workflows over AG-UI 1.0 to CopilotKit or any AG-UI client, including resume and reconnect. |
| [Architect](./docs/architect.md) | The built-in agent that drafts agents and workflows from a plain-language request and what it can operate. |
| [CI](./docs/ci.md) | The CI jobs and how a pull request selects only the tests its change affects. |
| [Concurrency](./docs/concurrency.md) | ETag and `If-Match` revisions that stop two editors from silently overwriting each other. |
| [Evals](./docs/evals.md) | The deterministic `regex` evaluator for "must not contain" rules. |
| [Knowledge-base sources](./docs/kb-sources.md) | Connecting an external catalogue to a knowledge base and keeping both in step. |
| [Knowledge import](./docs/knowledge-import.md) | `scripts/kb_import.py`: uploading a documentation tree into a knowledge base through the API. |
| [MCP control](./docs/mcp-control.md) | Managing a tenant's agents and workflows from an external MCP client. |
| [MCP servers](./docs/mcp-servers.md) | Managed MCP servers: idle stop, the Test action and keep-running. |
| [Observability](./docs/observability/) | Sending external OpenTelemetry traces to the platform with a tenant API key. |
| [Providers](./docs/providers/) | Model providers (Anthropic discovery, Cloudflare Workers AI) and knowledge-source provider plug-ins (GitHub). |
| [Tools](./docs/tools/) | The built-in tools: `clock.now`, `feed.parse`, `html.extract`, `http.request`, `json.query`, `text.check`, `workflow.history` and AT Protocol rich text. |
| [Workflows](./docs/workflows/) | Loops, step output size, durability across a worker restart, AI explanations and worked examples. |

## Status and roadmap

**Shipped** (on `main`): everything in the Features table, including versioned agents and
workflows, model fallback chains, conversations, built-in and MCP tools, knowledge bases with
source sync, memory, guardrails, evals, observability with OTLP ingest, built-in and ZITADEL sign-in,
roles and resource grants (OpenFGA), API keys, inbound webhooks, external agents over MCP and A2A,
AG-UI, the Architect, and the crash-injection suite.

**Planned, not implemented:**

- Workflow authoring and verification: per-node workflow editing, an engine-level dry run, re-run from step N, tool mocks, publish gates.
- Connect and embed: workflows that wait for an inbound event, notification connectors, OAuth connections, a client SDK.
- Operations: alerts, bundles, and promotion between environments.

## FAQ

**Why not Temporal?**
Temporal runs as a separate service cluster that the workers connect to. Durable uses DBOS, a
library that keeps workflow state in the same Postgres as the application data, so there is no
extra service to operate, and a run's record and its execution state are committed together.

**Why not n8n?**
n8n is licensed under the Sustainable Use License, a fair-code licence that is not OSI-approved.
Retrying a failed execution re-runs nodes, so an external call can be repeated unless the flow
guards against it, and one instance serves one organisation. Durable is Apache-2.0, multi-tenant,
and records every external side effect so that it is performed at most once.

**Why a declarative workflow interpreter instead of Python code?**
A workflow is data: it can be validated before it is published, versioned, compared, shown as a
graph in Studio, drafted by the Architect and edited by people who do not write code. Branch
conditions are expressions, not code, so a published workflow cannot run arbitrary programs, and
the interpreter's steps are recorded like any other run.

**Can I use my own model provider?**
Yes. Any OpenAI-compatible gateway works through the `litellm` provider (`EAP_LITELLM_BASE_URL`);
Cloudflare Workers AI and Anthropic models have their own guides in
[docs/providers/](./docs/providers/). A model must be in `EAP_ALLOWED_MODELS` before an agent can
be published with it.

**Does it need Redis or Kafka?**
No. Queues, events, live deltas (`LISTEN`/`NOTIFY`) and workflow state are all in Postgres.

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
