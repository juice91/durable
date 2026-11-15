# eap-worker

Durable agent runs for the Enterprise Agent Platform: one generic Pydantic AI agent (`eap-agent`,
`DBOSDurability`) driven by the DBOS workflow `eap.agent_run` on queue `eap-agent-runs`, per the
"Run hand-off contract" in `docs/plans/2026-11-11-milestone-1-vertical-slice.md`.

Run it: `uv run python -m eap_worker` (blocks), or `bash scripts/dev-worker.sh` for a restart loop
that recovers the previous process's pending runs (fixed `EAP_EXECUTOR_ID`). Configuration is in
`.env.example`.

The worker never creates tables: at startup it checks that `eap_app` is at the latest Alembic
migration and exits with `database schema is not at the latest migration; run: uv run alembic -c
packages/db/alembic.ini upgrade head` if not (see `packages/db/README.md`).

## Models

A spec's `model: {provider, name}` becomes the model id `provider:name`, resolved inside each
model step:

- `test:ticket`, `test:first-tool`, `test:history`, `test:<anything>` — deterministic scripted models
  used by tests. `test:history` answers with every user prompt it was given, which shows the
  conversation history a turn received.
- `litellm:<name>` — OpenAI-compatible gateway (SDK retries off; the DBOS model step retries).
  Base URL from `EAP_LITELLM_BASE_URL` (fallback `OPENAI_BASE_URL`), key from
  `EAP_LITELLM_API_KEY` (fallback `LITELLM_API_KEY`). The platform key is scoped to
  `qwen3.8-thinking` (a reasoning model): `{provider: "litellm", name: "qwen3.8-thinking"}`.
  Load the key into your shell, never into a file:
  `export LITELLM_API_KEY="$(sops -d ~/.config/sops/secrets.json | jq -r .LITELLM_QWEN38_THINKING_API_KEY)"`.

  The public URL `https://llm-gateway.example.com/v1` sits behind Cloudflare, which cuts any single
  request at ~300 s. For local development use the SSH tunnel `http://127.0.0.1:14001/v1`, which
  has no such limit.

## Conversations

A run that is a conversation turn is enqueued with the same six arguments as any run; its row carries
`conversation_id` and `turn`. `eap.start_run` returns that link (checkpointed), and for a turn the
workflow then runs `eap.load_history`: the conversation's earlier **completed** turns' stored
messages (`runs.messages`, written once by `eap.finish_run`), oldest first, capped at
`EAP_MAX_HISTORY_BYTES` (default 1 000 000; the oldest whole turns are dropped first). The step's
output is the `message_history` passed to `agent.iter`, so a recovered turn replays exactly the
history its first attempt had. A run without a conversation skips the step and keeps the milestone 1
step layout.

## Upgrading

The API enqueues runs on `eap-agent-runs`; workers run them. Order a deploy so no run is picked up
by a worker that does not understand it:

1. **Run the migrations** first: `uv run alembic -c packages/db/alembic.ini upgrade head` (this
   release adds `0008_run_usage`, `0009_version_restored_from` and `0010_run_options`). The API and
   the worker each start only at the exact revision they were built for, so after the migration
   old processes keep running but cannot restart.
2. **Deploy the workers before the API**, or drain the queue first (no queued or running runs).
   Workers from before this release ignore a run's model override and request context, and fail
   runs of specs with processors.
3. **Drain the queue before deploying if any in-flight run's spec has processors.** Processors add
   DBOS steps (`eap.input_processors`, `eap.output_processors`, `eap-moderator`), so such a run
   started by an older worker fails recovery on a step-name mismatch. Runs of specs without
   processors keep their step layout and recover across the deploy.
4. **Drain the queue before downgrading `0010_run_options`.** The downgrade deletes draft test runs,
   and an older worker's `eap.start_run` safety net would re-create an in-flight one as a
   published-version run with `agent_version = 0`.

`EAP_APP_VERSION` (default `m1`) is the DBOS application version. The API enqueues with it and a
worker dequeues and recovers only workflows recorded with its own value, so the API and its workers
must always share it.

**Recommended default: drain, keep `m1`.** Stop the API (or stop new runs), let the queue empty,
migrate, deploy the workers, then the API.

Bumping it (e.g. to `p0`) avoids the drain only if the old pool keeps running untouched. Start the
new workers and API with the new value and leave the old `m1` workers up until their runs finish
(including workflow runs waiting on an approval), then retire them. This breaks as soon as an old
worker restarts or is rescheduled: a worker starts only when the database is at exactly the revision
it was built for (`require_schema_at_head`), so after the migration an old worker cannot come back,
and a run recorded under `m1` with no `m1` worker left stays queued or running. If your platform
may restart or reschedule workers during the window, drain instead.

## Settings (RAG and memory)

All optional; the commented defaults are in `.dev.env.example`. The worker parses every one of them
at start (`eap_worker.app.validate_startup`, before DBOS launches or recovers anything), so a bad
value stops the worker instead of failing each run.

| Setting | Default | What it does |
|---|---|---|
| `EAP_ALLOWED_EMBEDDING_MODELS` | `test:*` | Embedding model allowlist, `provider:name=dimensions` entries (e.g. `litellm:Qwen3-VL-Embedding-8B-mlx-8bit=1024`); checked before every embedding call. Must match the API's |
| `EAP_EMBEDDING_QUERY_INSTRUCTIONS` | none | JSON `{model_id: instruction}`: the instruction prefixed to search queries (never to documents) |
| `EAP_LITELLM_EMBEDDING_BASE_URL`, `EAP_LITELLM_EMBEDDING_API_KEY` | the chat gateway's URL and key | A separate gateway or virtual key for `litellm:*` embeddings. A URL other than the chat one requires its own key (the chat key is never sent to another host) |
| `EAP_OPENAI_EMBEDDING_API_KEY` | none | Key for `openai:*` embedding models (always `api.openai.com`, never `OPENAI_API_KEY`) |
| `EAP_EMBEDDING_MAX_BATCH_INPUTS`, `EAP_EMBEDDING_MAX_BATCH_TOKENS` | 32, 16384 | Caps per provider request; a larger call is split |
| `EAP_EMBEDDING_TIMEOUT_S` | 120 | Per provider request (transport retries off; the step retries) |
| `EAP_RECALL_TIMEOUT_S` | 8 | Deadline of each pre-turn recall query attempt |
| `EAP_KB_INGEST_CONCURRENCY` | 2 | `eap.kb_ingest` runs at once per worker, on the `eap-kb-ingest` queue |
| `EAP_KB_MAX_INGEST_TOKENS` | 2000000 | Per-run budget of un-cached approximate embedding tokens (`ingest_budget_exceeded` before any provider call) |
| `EAP_KB_EMBED_BATCH_CHUNKS` | 64 | Un-embedded chunks per `eap.kb_embed_batch` step |
| `EAP_MEMORY_RETENTION_DAYS` | unset | Retention in days (1 to 36500) of message embeddings and observations (by `created_at`) and conversation working memory (by `updated_at`); `off` removes the shared schedule. Unset on this worker: it schedules nothing and leaves another worker's schedule alone |
| `EAP_MEMORY_RETENTION_EMBEDDING_DAYS`, `_OBSERVATION_DAYS`, `_WORKING_MEMORY_DAYS` | `EAP_MEMORY_RETENTION_DAYS` | Per-kind override: days (1 to 36500) or `keep` |
| `EAP_MEMORY_RETENTION_RESOURCE_WORKING_MEMORY_DAYS` | unset (kept) | Opt-in retention of resource working memory, by `updated_at` |
| `EAP_MEMORY_RETENTION_SCHEDULE` | `0 3 * * *` | Cron (UTC) of `eap.memory_retention` |
| `EAP_MEMORY_RETENTION_BATCH_SIZE` | 500 (max 10000) | Rows or conversations per batch (one transaction, one step) |
| `EAP_MEMORY_RETENTION_MAX_STEPS` | 2000 | Steps per run, shared round-robin by the kinds |

Configure every worker alike: a stored retention schedule with other settings is replaced (with a
warning) by the next worker that starts.

## Tests

`uv run pytest apps/worker -q` needs the dev Postgres (`bash scripts/dev-db.sh up`). Each
session uses a private DBOS system schema and a freshly migrated `eap_app` schema (the `eap_db`
pytest plugin), and drops both. `test_live_litellm.py` runs only when
`EAP_LITELLM_API_KEY` or `LITELLM_API_KEY` is set.

## Crash hook

`EAP_CRASH_AT` = `model_request` | `after_tool_call` | `after_event_write` calls `os._exit(137)` at
that point; see `src/eap_worker/crash.py` for exactly where.

## Upgrading

### RAG and memory milestone (RM, 2026-11)

Deploy the API and the worker together, in this order:

1. **Drain in-flight runs first.** Stop new runs (or stop the API), wait until no run is `queued`
   or `running` (`GET /api/v1/runs?status=queued,running` is empty), then stop the old workers.
   DBOS recovers a workflow by replaying its recorded step sequence, and this milestone changes it:
   - a conversation turn of an agent with `memory` runs `eap.load_memory` **instead of**
     `eap.load_history` (the same slot), and `eap.start_run` now records the conversation's
     `resource_id`. A turn whose `eap.start_run` was recorded by an older worker must not be
     recovered by the new one;
   - P0.17's processor steps (`eap.input_processors`, `eap.output_processors`) are part of the
     layout the new worker replays; a run started before them cannot be recovered by it either;
   - a turn of an agent with `memory.observational` no longer runs the observer steps itself: after
     `eap.finish_run` it enqueues the child workflow `eap.memory_observe_run` (id
     `{run_id}:memory-observe`, on `eap-memory-observe`) and records `DBOS.getResult`; the old
     `eap.memory_observe` / observer request / `eap.memory_observe_write` / `eap.memory_reflect`
     steps run inside that child. A turn recorded with the inline observer steps cannot be
     recovered by the new worker, and the reverse.

   A spec with neither `knowledge_bases` nor `memory` keeps its exact step names, so only memory
   turns are at risk, but drain everything: it is the only safe rule.
2. **Swap the Postgres image to pgvector** if the database still runs `postgres:17`: the dev stack
   uses `pgvector/pgvector:0.8.2-pg17-trixie`, which opens the same volume. Follow
   `packages/db/README.md` ("pgvector") step by step: it checks the compose project and volume
   before recreating the container, and takes an optional backup. pgvector 0.8.0 or later is
   required (iterative index scans).
3. **Migrate:** `uv run alembic -c packages/db/alembic.ini upgrade head` applies
   `0011_knowledge_bases` (knowledge bases, documents, chunks with partial HNSW indexes, the
   `kb_ingest` run kind, `run_model_requests.operation`), `0012_memory` (working memory and
   observations) and `0013_semantic_recall` (message embeddings). 0011 fails fast with a clear
   message when the `vector` extension is missing or older than 0.8.0. Workers refuse to start
   until the schema is at head.
4. **New queue `eap-kb-ingest`.** The API enqueues `eap.kb_ingest` (document ingests and
   reindexes) there, not on `eap-agent-runs`; every worker of this version listens on both, with at
   most `EAP_KB_INGEST_CONCURRENCY` (default 2) ingest runs per worker. An old worker does not
   listen on `eap-kb-ingest`, which is one more reason not to mix versions. Size
   `EAP_KB_MAX_INGEST_TOKENS` before the first reindex of a large knowledge base (the rule is in
   `.dev.env.example`).
5. **New queue `eap-memory-observe`.** Observational memory runs as `eap.memory_observe_run`, which
   each turn's `eap.agent_run` enqueues there with the conversation id as partition key; the queue
   is registered with `partition_concurrency=1`, so at most one observer runs per conversation
   across all workers (an overlapping observer never pays for a request it cannot write). Every
   worker of this version listens on `eap-agent-runs`, `eap-kb-ingest` and `eap-memory-observe`;
   an old worker listens on none of the new ones, and the turn that enqueued an observer waits for
   it, so drain (step 1) before switching versions, and keep a queue-wide stop on this one too: no
   `eap.memory_observe_run` may be `ENQUEUED` or `PENDING` when the old workers stop.
6. **New optional settings** (all in `.dev.env.example`): `EAP_ALLOWED_EMBEDDING_MODELS`,
   `EAP_EMBEDDING_QUERY_INSTRUCTIONS`, `EAP_KB_*`, the `EAP_LITELLM_EMBEDDING_*` pair, and
   `EAP_AUDIT_HASH_SALT` (set it per deployment so the forget audit log's resource hash cannot be
   looked up offline).

### Agent capabilities milestone (AC, 2026-11)

- **Drain before deploying.** Structured output (AC.4), limits and fallback (AC.5) and tool approvals (AC.6) add durable steps to the agent loop for specs that use them, so let in-flight agent runs finish first. A run recorded under the old worker and replayed under the new one could hit DBOS nondeterminism. Agent specs that use none of these features keep their step layout, which the golden test pins. AC.4 and AC.9 ship in one deploy.
- **Migrations:** run `0014_approvals` and `0015_evals` before starting the new API or worker.
- **Approvals:** the tool approval wait defaults to a 3600 s timeout unless the agent's approval policy sets one. Decisions are woken through `tool_approval:{run_id}` and polled every `EAP_APPROVAL_POLL_SECONDS` as a backstop.

### AG-UI milestone (AGUI, 2026-11)

- **Drain workers before deploying AG-UI.** `0016_ag_ui` adds the per-root commit order:
  - every event append takes the ROOT run's row lock, then the child's;
  - it stamps `execution_events.root_order` from a counter on the root row;
  - AG-UI workflow streams and their run boundaries are built on that order.

  Old-code workers that keep appending after the migration take the child lock first. The insert
  trigger `trg_execution_events_root_order` still stamps their rows, but it takes the root lock
  after the child's, the reverse of new code. A writer of each version on one workflow tree can
  then deadlock; Postgres aborts one, and the DBOS step retries. Stop new runs, let the queue
  empty, migrate, then deploy the workers and the API. In-flight runs from before the migration
  keep their earlier events unordered (NULL `root_order`); they are read first, in
  (ts, run_id, seq) order.
- **Migrations:** run `0016_ag_ui` and `0017_api_keys` before starting the new API or worker.
- **Throughput:** one workflow tree (or eval run with its cases) now appends one transaction at a
  time: about 110-220 appends/s per tree in local measurements, against 400-475 before. Different
  roots do not contend. Never hold a child run's append transaction open across slow work.
