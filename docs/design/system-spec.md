# System Specification

> Version: 2026-11-18 · 繁體中文版：[system-spec.zh-TW.md](./system-spec.zh-TW.md)

## Purpose, scope and audience

This document specifies the functional and non-functional requirements of Durable. It is the basis for development, testing and acceptance. Durable is a self-hostable platform for building, running and observing AI agents and their workflows. Every run is a durable record in the database. If a process stops, no run is lost and no external action is repeated.

The scope covers the capabilities merged into main as of 2026-11-17, when the second Studio parity round was deployed: agent and version management, tools and MCP, workflows and human approval, knowledge bases and memory, conversations, observability, the Studio interface, multi-tenancy and security. Planned items that are not yet merged (attachment storage round two, the MCP local runtime and catalog, the Architect Agent, datasets and evaluation) are listed at the end. They are not part of the acceptance basis of this version.

Audience: platform engineers (implementation and acceptance), product and project owners (scope confirmation), and adopting teams (what the system can and cannot do). Architecture decisions and their reasons are in the separate Architecture Design. The development method is in the AI-Assisted Development Method.

## Terms and roles

| Term | Definition |
| --- | --- |
| Tenant | The outermost isolation unit for data and permissions. Every query is filtered by the caller's tenant. An id from another tenant always returns 404. |
| Agent | A declarative specification: system prompt, model, tool list, processors, knowledge base and memory settings. It has a draft and immutable published versions. |
| Version | An immutable snapshot of an agent or workflow after publishing. Versions can be compared and restored. A run is always bound to one version or to a draft snapshot. |
| Run | The execution record of one agent, tool, conversation turn or workflow. One run corresponds to one durable workflow with the same id. |
| Event | A record written to a run in sequence order (seq): model request, tool call, text fragment, terminal state and so on. Each event has a unique event\_key. A replay returns the existing row. |
| Step | The smallest replayable unit recorded by the durable execution engine. Once its output is written, it is not executed again on replay. |
| Side effect | A call that has an irreversible effect on an external system. It is recorded in three stages, "reserve, call, complete", and is never repeated. |
| Tool | A function an agent can call: a built-in platform tool or a tool provided by an MCP server. |
| MCP server | An external tool server registered by a tenant (Streamable HTTP), reached through a client that enforces policy checks. Header secrets are stored encrypted. |
| Workflow | A declarative flow graph: agent step, tool call, conditional branch, parallel branch, human approval. Branch conditions are data comparisons, not code. |
| Approval | A workflow node that waits for a person to decide. Of approve, reject and timeout, only one takes effect. |
| Knowledge base | A collection of documents uploaded by a tenant. The documents are extracted, chunked and embedded for retrieval. Retrieval results carry citations. |
| Memory | Information kept across turns: working memory (structured fields), observational memory (conversation summaries) and semantic recall (vector search over past messages). |
| Conversation | A series of agent runs with history. A conversation allows only one active turn at a time. |
| Processor | A declarative guardrail attached to an agent: PII redaction, regular-expression blocklist, length limit, content moderation. |
| Studio | The operator interface (Next.js): edit and publish agents, Playground, workflow graph, traces, logs, metrics. |
| Control plane (API) | The FastAPI service: authentication, tenant authorization, run recording, queueing, event streaming. It runs no agent logic. |
| Worker | The process that hosts the durable execution engine and the agent runtime. It takes runs from the queue and persists them step by step. |

Roles: the **platform administrator** manages the allow lists of models and embedding models, tenants and deployments. The **tenant administrator** registers MCP servers and manages knowledge bases and members. The **agent designer** edits, tests and publishes agents and workflows in Studio. The **approver** decides on waiting nodes in Studio. The **end user** interacts with agents through conversations or an external front end. The **external system** starts runs and subscribes to event streams through the API with a tenant key.

## Functional requirements

The following list is taken from the code (branch head 8afde89, 2026-11-14). Each requirement names its API or module. All APIs are mounted under `/api/v1`, except `/healthz`.

### FR-1 Agents and versions

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-1.1 | Create, list, read and update an agent draft: system prompt, model, tool list, processors, knowledge base, memory, request-context fields | `POST/GET/PATCH /agents` |
| FR-1.2 | Publish as an immutable version. Publishing checks the model allow list, that MCP servers exist, that RE2 patterns are valid, and the characters of prompt placeholders | `POST /agents/{id}/versions` |
| FR-1.3 | Compare the differences between any two versions. Restore an old version as a new version and record its source | `GET /versions/compare`, `POST /versions/{n}/restore`, `restored_from` column |
| FR-1.4 | List the allowed models. A run can specify a model override, a version or a draft snapshot | `GET /models`, `runs.draft_spec` |
| FR-1.5 | An agent declares request-context fields. A run validates the context sent by the client. `tenant_id`, `user_id` and `run_id` are reserved and are not accepted from the client | `GET /agents/{id}/request-context`, `POST …/validate` |

### FR-2 Runs and event streaming

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-2.1 | Start an agent run. A `run_id` can be supplied for an idempotent retry. A repeated start returns 409 | `POST /agents/{id}/runs` |
| FR-2.2 | List and read runs. The statuses are queued / running / recovering / completed / failed / needs\_attention | `GET /runs`, `GET /runs/{id}` |
| FR-2.3 | Replay the durable event log. Event `seq` is continuous with no gaps, and `event_key` is unique | `GET /runs/{id}/events?after_seq=` |
| FR-2.4 | Live SSE streaming: first replay the events after `Last-Event-ID` or `after_seq`, then send live events and token deltas. A heartbeat every 10 seconds. A non-integer `Last-Event-ID` returns 400 | `GET /runs/{id}/stream` |
| FR-2.5 | Record the tokens and priced cost of each model request, written in the same transaction as the event. The run row carries the totals | `run_model_requests`, `config/model-prices.json` |
| FR-2.6 | Each run provides a span tree (with tokens and cost), rebuilt from the durable events | `GET /runs/{id}/trace` |
| FR-2.7 | A tool can be test-run on its own, using the same allow list and side-effect rules | `POST /tools/{id}/runs` → `eap.tool_run` |

### FR-3 Conversations

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-3.1 | Create, list, read, update and delete conversations. A conversation is bound to one agent and one resource (user) | `/conversations` |
| FR-3.2 | Start a turn in a conversation. A conversation has only one active turn at a time, enforced under the conversation row lock | `POST /conversations/{id}/runs` |
| FR-3.3 | History trimming: the last 20 turns by default (1–200), with an optional token limit. The total history is capped at 1,000,000 bytes | `HistoryConfig`, `EAP_MAX_HISTORY_BYTES` |

### FR-4 Tools and MCP

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-4.1 | List the tool catalog: built-in tools (`clock.now`, the demo side-effect tool `tickets.create`) and the MCP tools in use by agents | `GET /tools` |
| FR-4.2 | Platform tools are injected by the Worker and are not listed in the agent specification: `retrieve` (when there is a knowledge base) and `update_working_memory` (when there is working memory). Both names are reserved | `platform_tools.py`, `memory.py` |
| FR-4.3 | A tenant registers MCP servers (Streamable HTTP): create, list, read, update, delete, connection test. Header secrets are AES-GCM encrypted, support key rotation and are never returned | `/mcp-servers`, `mcp_server_secrets`, `EAP_SECRETS_KEY` / `EAP_SECRETS_OLD_KEYS` |
| FR-4.4 | An agent run can call the tools of registered servers. A call times out after 60 seconds. Tool allow list `EAP_ALLOWED_TOOLS` | `mcp.py`, `EAP_MCP_TIMEOUT_SECONDS` |

### FR-5 Workflows and approvals

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-5.1 | Create, list, read and update a workflow draft. Publish as a version | `/workflows`, `/workflows/{id}/versions` |
| FR-5.2 | Node kinds: agent step, tool call, conditional branch, parallel branch, human approval. Branch conditions are data comparisons, not code | `eap_schemas` workflow specification |
| FR-5.3 | Start a workflow run. An agent step runs as a child run. A parallel branch runs as a child workflow | `POST /workflows/{id}/runs` → `eap.workflow_run`, `eap.workflow_branch` |
| FR-5.4 | Approval node: approve, reject and timeout compete for the same key, and exactly one takes effect. The decision event is written under the run row lock | `POST …/steps/{step}/approve` / `reject` |
| FR-5.5 | Limits: 32 KiB per step output, 1 MiB flow context, 14 days maximum per run. An approval wait is re-checked once per hour | `EAP_WORKFLOW_MAX_*`, `EAP_APPROVAL_POLL_SECONDS` |

### FR-6 Knowledge bases

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-6.1 | Create, list, read (including embedding spend), update and delete knowledge bases. List the allowed embedding models | `/knowledge-bases`, `GET /embedding-models` |
| FR-6.2 | Upload, read, delete and re-ingest a single document (Markdown, HTML, plain text). Limits: 5 MiB per document, 2,000,000 tokens per ingest | `PUT …/documents/{key}`, `EAP_KB_MAX_*` |
| FR-6.3 | Ingest is a durable workflow: extraction and chunking are pure functions, embedding batches are persisted batch by batch, and after an interruption ingest continues from the unfinished batch. Two ingests at a time per Worker | `eap.kb_ingest`, `eap_knowledge`, `EAP_KB_INGEST_CONCURRENCY` |
| FR-6.4 | Rebuild the whole index (a new generation) and abandon a pending generation | `POST / DELETE …/reindex` |
| FR-6.5 | Search: pgvector cosine distance with an HNSW index. An agent retrieves through the `retrieve` tool. Results are capped at 24 KiB, and citation numbers are continuous across the whole run | `POST …/search`, `kb_chunks` |

### FR-7 Memory

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-7.1 | Working memory: scope is resource (default) or conversation. Template mode (Markdown note ≤ 16 KiB) or schema mode (JSON, depth 3, 50 properties). It can be read-only. A write happens exactly once on replay | `working_memories`, `/memory/working` (ETag) |
| FR-7.2 | Observational memory: after a turn ends, a child workflow produces observations and reflections, serialized per conversation. Parameters: `observe_after_tokens` 12,000, `keep_recent_turns` 4, `reflect_after_tokens` 8,000 | `eap.memory_observe_run`, `memory_observations` |
| FR-7.3 | Semantic recall: before a turn, retrieve past messages (`top_k` 4, maximum 10; surrounding context 1, maximum 3). An 8-second timeout with one retry. Results ≤ 8 KiB. After a turn, new messages are embedded | `message_embeddings`, `eap.memory_recall` / `eap.memory_index` |
| FR-7.4 | Forgetting: delete the observations of a conversation, or the memory of one user in one agent or in the whole tenant | `DELETE /memory/resources/{id}` |
| FR-7.5 | Retention sweep: a scheduled workflow runs daily at 03:00 UTC, with days set per category and batches of 500. The cutoff time is computed from the schedule time, so the decision is the same after recovery | `eap.memory_retention`, `EAP_MEMORY_RETENTION_*` |

### FR-8 Processors (guardrails)

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-8.1 | An agent can attach up to 8 processors each on input and output. The actions are allow / redact / block / truncate | `eap_schemas.processors` |
| FR-8.2 | Kinds: `pii_redact` (email, phone, credit card, IBAN, IPv4, US SSN), `regex_blocklist` (≤ 20 patterns, each ≤ 200 characters, RE2), `max_length` (≤ 1,000,000 characters), `moderation` (allow-listed model, can only block) | `eap_processors` |
| FR-8.3 | List the processor catalog and configuration schema. Test a processor. Query processing results. The matched text is not stored | `GET /processors`, `POST /processors/test`, `GET /processor-results` |

### FR-9 Observability

| ID | Requirement | Basis |
| --- | --- | --- |
| FR-9.1 | Metrics: summary, time series, breakdown (run count, tokens, cost, latency, including RAG and memory) | `GET /metrics/summary` / `timeseries` / `breakdown` |
| FR-9.2 | Log search: queries Loki, restricted by the tenant label, and never exposes another tenant | `GET /logs`, `EAP_LOKI_URL` |
| FR-9.3 | One OTel trace per run, from the API request to each tool call. Redacted before output | `eap_observability` |

### FR-10 Studio

Routes and their purpose: `/agents` (list), `/agents/new` (creation steps), `/agents/{id}/chat` (conversation, including Playground run options), `/agents/{id}/editor` (step-by-step editor, including the knowledge and memory steps and version comparison), `/agents/{id}/memory` (memory view), `/agents/{id}/traces`; `/workflows`, `/workflows/{id}` (graph and runs), `/workflows/{id}/runs/{runId}` (live graph); `/tools`, `/mcp`, `/knowledge`, `/processors`; `/traces` (trace drawer), `/metrics` (default home page), `/logs`, `/settings`. The old routes `/playground`, `/runs/{id}` and `/traces/{runId}` redirect to the new pages.

### FR-11 Multi-tenancy and authentication

| ID | Requirement | Basis / status |
| --- | --- | --- |
| FR-11.1 | All data and queries are isolated by tenant. Child rows are bound to the tenant by composite foreign keys. An id from another tenant returns 404 | All stores, implemented |
| FR-11.2 | Authentication modes: `dev` (local) and `api_key` (tenant key, scope `mcp` / `manage` / `otlp`) | `EAP_AUTH_MODE`, implemented |
| FR-11.3 | Enterprise account sign-in: SSO, MFA, password reset. The enterprise IdP connects by SAML / OIDC. Studio and external front ends use OIDC PKCE | Planned: `EAP_AUTH_MODE=oidc`, identity provider ZITADEL |
| FR-11.4 | A user always belongs to one tenant. A tenant maps to an organization in the identity provider, and the tenant row is created automatically on first sign-in | Planned |
| FR-11.5 | Roles: administrator, designer, approver, viewer, scoped to the whole tenant. The tenant administrator assigns them in the identity provider and they enter the token. The API checks the role for writes and approvals | Planned: project roles, `require_role` |
| FR-11.6 | Machine access (external front end, MCP, OTLP) uses service accounts and PAT / JWT keys, with scopes drawn from the same roles. It is merged with the existing tenant keys in one version | Planned |
| FR-11.7 | When a user is disabled or deleted, their memory and resources go through the existing forgetting API | Planned: identity provider event → `DELETE /memory/resources/{rid}` |
| FR-11.8 | Resource-level authorization (an agent or workflow shared only with specific people) is stored in the platform database and written in the same transaction as the resource. It is not delegated to the identity provider | Planned: `resource_grants` table; OpenFGA to be evaluated when needed |

## Non-functional requirements

| ID | Requirement | Verification |
| --- | --- | --- |
| NFR-1 Durability | Every run is one DBOS workflow whose id equals the run id. After a Worker is hit by `kill -9` at any step, a new Worker with the same executor id must take over and finish. Recorded steps are not executed again | `tests/recovery` crash matrix, 19 named crash points |
| NFR-2 Exactly-once side effects | An external side effect is reserved under `run_id:tool_call_id`. If the process stops between the call and the completion, the run ends in `needs_attention` and is never called again automatically | A fake external service counts by idempotency key. Each scenario asserts exactly 1 |
| NFR-3 Event integrity | `seq` is assigned under the row lock, continuous with no duplicates. An append on replay returns the existing row. SSE replay matches the database | Crash matrix assertions; API suite |
| NFR-4 Deterministic replay | The clock, version resolution, environment limits and approval deadline are read once inside a recorded step. Exceptions inside a step can be pickled, and on replay their type and message are unchanged | Worker suite |
| NFR-5 Tenant isolation | A cross-tenant read always returns 404. Log search does not cross tenants. Reserved context fields are not accepted from the client | Cross-tenant scenarios in the API suite (required for every task) |
| NFR-6 Input guard | A body > 256 KiB returns 413 (document upload 5 MiB). Non-strict UTF-8 or NUL returns 415. JSON depth > 32 or an unstorable string returns 422, with at most 20 errors reported and no input echoed. 401 comes before 422 | `request_guard.py` tests |
| NFR-7 Outbound policy | MCP traffic is https only. Private, loopback, link-local and metadata addresses are rejected. DNS is re-checked at connect time. Redirects are not followed. Responses are capped at 1 MiB | `eap_secrets` suite, `test_mcp_safety.py` |
| NFR-8 Secrets | MCP header values are AES-GCM encrypted, support rotation from old keys and are never returned. Tool results, errors, logs and spans mask echoed secrets. A malformed key makes startup fail | `eap_secrets` suite |
| NFR-9 Model governance | The model and embedding-model allow lists are checked at publish, at run start and before every call. Tests do not call real models (`test:*` scripted models) | `EAP_ALLOWED_MODELS`, `EAP_ALLOWED_EMBEDDING_MODELS` |
| NFR-10 Latency and capacity | Queue poll 1 second. Heartbeat 5 seconds, and 20 seconds without a heartbeat marks the run recovering. SSE streams: 200 global / 10 per principal, lifetime 3600 seconds. Embedding batch 32 inputs / 16,384 tokens, timeout 120 seconds | Configuration values; informal performance tests |
| NFR-11 Observability | One trace per run. Logs carry the tenant and run id. Usage and cost are written in the same transaction as events. Only `eap_observability` imports OpenTelemetry | import-linter; observability suite |
| NFR-12 Schema management | Alembic is the only schema owner. The API and Worker refuse to start when not at head. Every migration has a downgrade and a test, and `alembic check` stays clean | `packages/db/db_tests` |
| NFR-13 Layer boundaries | 9 import-linter contracts (see the Architecture Design). A violation fails the build | `uv run lint-imports` |
| NFR-14 Accessibility (Studio) | Lighthouse a11y ≥ 95. No horizontal scroll at 400 px. Fully keyboard operable. `prefers-reduced-motion` is supported | SL acceptance checklist, axe tests |
| NFR-15 Licensing and branding | No code, names or logos of third-party products. Third-party packages are listed in `THIRD_PARTY_NOTICES.md` | Reviewer check; brand-word grep |

## Interface specification

All routes are mounted under `/api/v1`, with JSON requests and responses. Reading another tenant's resource returns 404. The request guard runs before the route (see NFR-6).

| Resource | Endpoints | Description |
| --- | --- | --- |
| Agent | `POST/GET /agents`, `GET/PATCH /agents/{id}` | CRUD of the draft |
| Version | `POST/GET /agents/{id}/versions`, `GET …/versions/{n}`, `GET …/versions/compare`, `POST …/versions/{n}/restore` | Publish, compare, restore |
| Run options | `GET /models`, `GET /agents/{id}/request-context`, `POST …/request-context/validate` | Allowed models, context fields |
| Run | `POST /agents/{id}/runs`, `GET /runs`, `GET /runs/{id}`, `GET /runs/{id}/events`, `GET /runs/{id}/stream`, `GET /runs/{id}/trace` | Start returns 201, repeat returns 409; no cancel endpoint |
| Conversation | `POST /agents/{id}/conversations`, `GET /conversations`, `GET/PATCH/DELETE /conversations/{id}`, `POST /conversations/{id}/runs` | A turn goes through `eap.agent_run` |
| Tool | `GET /tools`, `GET /tools/{id}`, `POST /tools/{id}/runs` | Single-tool test run |
| MCP | `POST/GET /mcp-servers`, `GET/PATCH/DELETE /mcp-servers/{id}`, `POST /mcp-servers/{id}/test` | Secrets are not returned |
| Workflow | `POST/GET /workflows`, `GET/PATCH /workflows/{id}`, `POST/GET …/versions`, `GET …/versions/{n}` |  |
| Workflow run | `POST /workflows/{id}/runs`, `POST /workflows/runs/{run}/steps/{step}/approve`, `…/reject` | Approval decision |
| Knowledge base | `GET /embedding-models`, `GET/POST /knowledge-bases`, `GET/PATCH/DELETE /knowledge-bases/{id}`, `GET …/documents`, `GET/PUT/DELETE …/documents/{key}`, `POST …/documents/{key}/reingest`, `POST/DELETE …/reindex`, `POST …/search` | PUT upload limit 5 MiB |
| Memory | `GET/PUT/DELETE /conversations/{id}/memory/working` (ETag), `GET /agents/{id}/memory/working`, `GET/PUT/DELETE /agents/{id}/memory/resources/{rid}/working`, `GET/DELETE /conversations/{id}/memory/observations`, `DELETE …/observations/{oid}`, `DELETE /agents/{id}/memory/resources/{rid}`, `DELETE /memory/resources/{rid}` | The last two are forgetting |
| Processor | `GET /processors`, `POST /processors/test`, `GET /processor-results` |  |
| Metrics and logs | `GET /metrics/summary`, `/metrics/timeseries`, `/metrics/breakdown`, `GET /logs` | Logs query Loki |
| Health | `GET /healthz` | No authentication |

### Event stream (SSE)

`GET /runs/{id}/stream` returns `text/event-stream`. The client states the last sequence number it has received with the `Last-Event-ID` header or `?after_seq=` (the header takes precedence; the default is -1). The server first replays the durable events after that number, then sends live events and token deltas. Every 10 seconds it sends a `: heartbeat` comment. An `end` event closes the stream. Durable events and transient deltas travel from the Worker to the API through Postgres `NOTIFY`.

Event kinds (in `event_key` form): `start`, `model:{n}:final`, `tool:{call_id}:call`, `tool:{call_id}:result`, processor results, workflow step start and end, approval decision, `end`.

### Workflows and queues (internal)

| DBOS workflow | Trigger | Queue |
| --- | --- | --- |
| `eap.agent_run` | Agent run, conversation turn, agent step of a workflow | `eap-agent-runs` |
| `eap.tool_run` | Single-tool test | `eap-agent-runs` |
| `eap.workflow_run`, `eap.workflow_branch` | Workflow run and parallel branch | `eap-agent-runs` |
| `eap.kb_ingest` | Document upload, re-ingest, reindex | `eap-kb-ingest` (2 per Worker) |
| `eap.memory_observe_run` | After a turn ends | `eap-memory-observe` (1 per conversation) |
| `eap.memory_retention` | Schedule `0 3 * * *` UTC | Scheduled workflow |

## Data model summary

There are two databases in the same Postgres: `eap_app` (product data, managed by Alembic) and `eap_dbos` (DBOS system tables: workflow state, step outputs, queues). All product tables carry `tenant_id`, and child tables are bound to the tenant by composite foreign keys.

| Migration | Tables added | Other changes |
| --- | --- | --- |
| 0001\_baseline | `agents`, `agent_versions`, `runs`, `execution_events`, `side_effects`, `fake_external_tickets`, `fake_external_ticket_calls` |  |
| 0002\_conversations | `conversations` | `runs` gains `conversation_id`, `turn`, `messages` |
| c689c50ff203 |  | `runs` gains `trace_id`, `traceparent` |
| 0003 |  | `runs` tenant and creation-time index |
| 0004\_mcp\_servers | `mcp_servers`, `mcp_server_secrets` |  |
| 0005\_workflows | `workflows`, `workflow_versions` |  |
| 0006\_tool\_runs |  | `runs` gains `kind`, `tool_id` |
| 0007\_workflow\_runs |  | `runs` gains `parent_run_id`, `parent_step_id`, `workflow_id`, `workflow_version` |
| 0008\_run\_usage | `run_model_requests` | `runs` gains token, cost, model and time columns |
| 0009 |  | `agent_versions.restored_from` |
| 0010\_run\_options |  | `runs` gains `draft_spec`, `request_context` |
| 0011\_knowledge\_bases | `knowledge_bases`, `kb_documents`, `kb_chunks` (pgvector, HNSW cosine) | `runs.knowledge_base_id` |
| 0012\_memory | `working_memories`, `memory_observations` | `conversations.resource_id` |
| 0013\_semantic\_recall | `message_embeddings` (pgvector), `memory_forget_watermarks` |  |

Core relations: `agents` 1—n `agent_versions`; `agents` 1—n `runs` (a run is bound to a version number or to `draft_spec`); `runs` 1—n `execution_events` (`seq` continuous, `event_key` unique); `runs` 1—n `run_model_requests`; `runs` 1—n `side_effects` (key = `run_id:tool_call_id`); `runs` relate to themselves (`parent_run_id`, the child runs of a workflow); `conversations` 1—n `runs` (`turn`); `knowledge_bases` 1—n `kb_documents` 1—n `kb_chunks`; `conversations` / resource 1—n `working_memories`, `memory_observations`, `message_embeddings`.

State machine of `runs.status`: `queued` → `running` → (`recovering`, heartbeat older than 20 seconds) → `completed` | `failed` | `needs_attention`. A terminal state carries `finished_at` and the usage totals. Later migrations on main (0016 and 0017: API keys, AG-UI) are not listed in this table.

## Acceptance criteria

All of the following must pass for this version to be accepted. The first three are decided by automation. The last three are done by a person walking through them once on the development stack.

1. `bash scripts/test.sh` passes with no skips: import-linter, all pytest suites (including `tests/recovery`), the API suite against a real Postgres, Studio vitest, the dev-stack lifecycle script, the pgvector upgrade script.
2. Every scenario of the crash matrix: side effect exactly once; `seq` continuous with no duplicates; the run ends in `completed` and the DBOS workflow has exactly one SUCCESS; SSE replay matches the database; one usage row per model request.
3. CI (hosted) and CI Heavy (self-hosted) are green on main. The nightly heavy run includes the worker and recovery suites.
4. Browser check: create an agent → publish → run it in the Playground with `test:ticket` and see two model requests with tokens and cost → the trace drawer shows the span tree → log search finds the run → the metrics page count increases.
5. Workflow check: create a flow with an agent step, a branch and an approval → run it → it stops at the approval node → restart the Worker → press approve in Studio → the flow continues to the end. There is only one decision event.
6. Knowledge and memory check: upload a Markdown document → ingest completes → the agent answers with `retrieve` and citations → update working memory in a conversation → the memory view page shows the content → after the resource is deleted, the memory is gone.
7. Studio: usable at 1440 / 1024 / 768 / 400 px, in light and dark themes, and with the keyboard only. Lighthouse a11y ≥ 95. No brand words.
8. The final whole-branch review has no unresolved Critical or Important finding.

## Out of scope and planned

| Item | Status (2026-11-19) | Description |
| --- | --- | --- |
| AG-UI output adapter, API key, CopilotKit example | Merged (migrations 0016, 0017) | AG-UI is an output layer converted from the durable event log. It does not replace native streaming |
| MCP runtime and catalog (ToolHive) | Live (PR 31, 33) | 36 tools; scope `mcp` / `manage`; FR-4 covers only external registration |
| Attachments r2 (R2 object storage) | Merged |  |
| Architect Agent | Live | Designs agents and workflows through conversation; drafts need no review, publish needs approval; platform.\* read tools (PR 45) |
| Evals: test suite, scorer, suggested rules | Live (PR 43, 46) | Builds evals from run records; the core of milestone 4 is delivered |
| Secret input, http.request tool, KB source sync, OTLP ingest | Live (PR 12, 34, 19/42, 53) | Migrations up to 0032 |
| Open-core split, docs site | Merged (PR 55, 48, 50) | Public repo in preparation: audit PR 49 awaits review |
| Identity integration (AUTH) | Planned; requirements in FR-11.3–11.8 | ZITADEL; the platform changes only `current_principal` |
| Studio parity round 3 | Planned |  |
| KB native document formats (PDF, Office) | P1 follow-up | Only Markdown, HTML and plain text today |
| Run cancel endpoint | None |  |
| Workflow loop node | Branch in progress |  |

The FR chapters of this document are taken from the code of 2026-11-14 (8afde89). The capabilities marked "Live" in the table above have not yet been expanded into FRs. They will be added at the next update. The next milestone (2026-11-19) has four tracks: A Identity and Governance (maps to FR-11.3–11.8, plus an audit table, quotas and budgets), B Authoring and Verification (per-node editing, dry run, re-run from step N, tool mocks, publish gates), C Connect and Embed (webhooks, notification connectors, OAuth, SDK, local runner), D Operate (model fallback, alerts, bundles, injection detection, data governance). The acceptance conditions of each item are in that milestone's plan. After delivery they expand into chapters FR-12 and later.

Explicitly out of scope: tenant-supplied code running in the Worker; cross-database deployment (queue or vectors on external services); running an agent synchronously inside the request thread.
