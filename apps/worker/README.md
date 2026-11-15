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
| `EAP_KB_INGEST_CONCURRENCY` | 2 | `eap.kb_ingest` runs at once per worker, on the `eap-kb-ingest-fair` queue (partitioned by knowledge base; the same cap on the legacy `eap-kb-ingest`) |
| `EAP_KB_MAX_INGEST_TOKENS` | 2000000 | Per-run budget of un-cached approximate embedding tokens (`ingest_budget_exceeded` before any provider call) |
| `EAP_KB_MAX_PDF_PAGES` | 2000 (1 to 20000) | Pages a PDF may have; more fails the ingest `extraction_failed` (`extract_limit`) |
| `EAP_KB_EXTRACT_CPU_SECONDS` | 60 (1 to 3600) | `RLIMIT_CPU` of the extraction sandbox child; its wall timeout is twice this (`extract_timeout`) |
| `EAP_KB_EXTRACT_MEMORY_MB` | 1024 (256 to 65536) | `RLIMIT_AS` of the sandbox child, **Linux only** (macOS has no working `RLIMIT_AS`: there the input caps and the wall timeout bound it) |
| `EAP_KB_EXTRACT_CONCURRENCY` | 2 (1 to 64) | Sandbox children at once per worker process |
| `EAP_KB_EMBED_BATCH_CHUNKS` | 64 (max 128) | Un-embedded chunks per batch (`eap.kb_embed_call` + `eap.kb_embed_write`, or `eap.kb_embed_batch` for runs enqueued before P1.C5); the cap bounds the call step's checkpoint of float32 vectors (128 x 1536 x 4 bytes) |
| `EAP_MEMORY_MAX_ACTIVE_OBSERVATION_TOKENS` | 32000 (min 1000) | Reflector input cap (P1.C6), never below 2 x the spec's `reflect_after_tokens` nor `reflect_after_tokens` + the largest active row, so a normal reflection is never cut. Past it (a backlog left by failing reflections), only the newest active observations within the cap are sent, and the reflection **removes the older ones from the agent's memory** (they stay in the table as superseded rows), with one `memory_observe_trimmed` event (counts only). Lowering it discards memory content sooner. A bad value stops the worker at start |
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

### RAG P1.C hardening (deploy note)

Drain in-flight runs before deploying (step 1 above still applies). What changes in the recorded
step layouts:

- **`post_turn_done` (P1.C1).** A completed conversation turn of a spec with
  `memory.semantic_recall` and/or `memory.observational` records one more `eap.append_event`
  (`memory:post_turn:{turn}`, `post_turn_done` {observe, index}) after its last post-turn step
  (`eap.memory_index`, or the observer's `DBOS.getResult`). It is appended after the recorded
  prefix, so an older run recovers, but drain anyway. Specs without those features are unchanged.
- **Reflection growth guard (P1.C6).** No new step. `eap.memory_observe` and
  `eap.memory_observe_write` now return ids and counts only (no rendered prompt); the observer and
  reflector prompts are rendered inside the model-request step. A plan recorded before the deploy
  still carries its prompt and is used as is, so a pending `eap.memory_observe_run` recovers.
  `eap.memory_reflect` may append a `memory_observe_trimmed` event
  (`EAP_MEMORY_MAX_ACTIVE_OBSERVATION_TOKENS`): only when failed reflections left a backlog past
  the cap (never below 2 x `reflect_after_tokens`), the oldest active observations are left out of
  the reflector's input AND superseded by the reflection, so they leave the agent's memory (the
  rows stay, with `superseded_by`). A render that finds its rows gone answers without a provider
  call (`memory_observe_failed` `observer_input_changed`).
- **Embed split (P1.C5).** Ingest and reindex runs created by the new API carry
  `"embed_split": true` in their input and embed each batch with `eap.kb_embed_call` then
  `eap.kb_embed_write` (instead of `eap.kb_embed_batch`), so a deadlock retry of the write never
  calls the provider again. Runs created before (no flag) keep `eap.kb_embed_batch` and recover
  unchanged. Do not mix versions: an old worker ignores the flag and records
  `eap.kb_embed_batch`, which a new worker could not replay for a flagged run, so drain and
  switch the API and workers together.
- **Access logs (P1.C2).** Run the API with `--no-access-log` (scripts/dev-lib.sh does): the API
  logs its own access line (`eap.access`) with the route template instead of the raw path, and
  never a query string.

### RAG P1.A uploads (deploy note)

- **Pre-deploy check: the reserved metadata key `eap` (P1.A1/P1.A4).** The new code rejects a
  document metadata key or an agent's knowledge base filter key named `eap` on write (upload, agent
  save and publish, 422) and also when stored rows are read back, so check that no stored row uses
  it before deploying. Every count must be 0 (`kb_chunks` only before the first P1 deploy: after it,
  worker-extracted chunks record their extractor there):

  ```sql
  SELECT count(*) FROM kb_documents WHERE metadata ? 'eap';
  SELECT count(*) FROM kb_chunks WHERE metadata ? 'eap';
  SELECT count(*) FROM agent_versions v CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(v.spec -> 'knowledge_bases') = 'array' THEN v.spec -> 'knowledge_bases' ELSE '[]'::jsonb END) r WHERE r -> 'filter' ? 'eap';
  SELECT count(*) FROM agents a CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(a.draft_spec -> 'knowledge_bases') = 'array' THEN a.draft_spec -> 'knowledge_bases' ELSE '[]'::jsonb END) r WHERE r -> 'filter' ? 'eap';
  SELECT count(*) FROM runs u CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(u.draft_spec -> 'knowledge_bases') = 'array' THEN u.draft_spec -> 'knowledge_bases' ELSE '[]'::jsonb END) r WHERE r -> 'filter' ? 'eap';
  ```

  The last one covers draft test runs: their stored target (`runs.draft_spec`) is validated as an
  `AgentSpec` whenever it is reloaded.
- **New knowledge bases use chunker v2** (`chunking.chunker_version` 2, `min_tokens` 64 unless
  given); existing ones keep their stored config until `POST /knowledge-bases/{id}/reextract
  {"chunker_version": 2}`. The ingest run input has no new key besides P1.C5's `embed_split`
  (`document_id`, `doc_version`, `embed_split`): the worker picks `eap.kb_extract_chunk` from the document's MIME type and the
  knowledge base's `chunking.extract`.
- **Uploads.** MIME types are stored canonical (`text/x-markdown` → `text/markdown`), so the first
  re-upload of an older document stored under an alias or with parameters makes one new version.
  The upload route's body guard grows to `max(EAP_KB_MAX_DOCUMENT_BYTES, ceil(EAP_KB_MAX_BINARY_BYTES
  × 4/3) + 64 KiB)` (about 33.4 MiB by default) for base64 PDFs only: a body that streams past
  `EAP_KB_MAX_DOCUMENT_BYTES` is refused there (413, the text cap's message) unless its first
  `EAP_KB_MAX_DOCUMENT_BYTES` hold the `content_base64` key, so a text upload is never buffered
  past its cap.
  `EAP_KB_BINARY_UPLOAD_CONCURRENCY` (default 4) bounds the binary uploads one API process decodes
  at once.
- **Changed 422 text for HTML that cannot be extracted.** The detail used to be
  `document extraction failed: <reason>`; it is now the fixed message plus the code and reason,
  e.g. `The document could not be extracted. (extract_failed: too_many_attributes)`. Clients
  matching the old text must switch to the new one (the status code is unchanged).

### RAG P1.A3 extraction sandbox (deploy note)

- **Image dependencies.** The worker image needs `packages/knowledge`'s parser dependencies
  (docutils, pypdf, cryptography for encrypted PDFs, PyYAML): `uv sync --all-packages` installs
  them. The sandbox child runs `sys.executable`, so it must be the worker's own venv interpreter.
- Worker-extracted documents (rst, PDF, YAML, JSON, Doxygen Markdown) are parsed in a child process,
  `python -I -B -m eap_knowledge.sandbox` (`eap_worker/extract_sandbox.py`): empty environment
  (`PATH=/usr/bin:/bin`, `LANG=C.UTF-8`); the child sets its own limits before reading the payload:
  `RLIMIT_CPU` = `EAP_KB_EXTRACT_CPU_SECONDS`, `RLIMIT_AS` = `EAP_KB_EXTRACT_MEMORY_MB` (Linux only),
  `RLIMIT_FSIZE` 0 (no file can grow), 32 open files; wall timeout 2 x CPU; at most
  `EAP_KB_EXTRACT_CONCURRENCY` children per worker process; 64 MiB of output.
- **Tests on macOS skip the `RLIMIT_AS` case.** `worker_tests/test_extract_sandbox.py`'s memory
  limit test runs only on Linux (macOS has no working `RLIMIT_AS`), so `bash scripts/test.sh` on a
  macOS dev machine reports one skip. A "green with no skips" claim needs a Linux run (CI or a
  Linux container).
- An audit hook in the child refuses sockets and every resolver call (`getaddrinfo`,
  `gethostbyname`, `gethostbyaddr`, `getnameinfo`), process starts (including
  `_posixsubprocess.fork_exec`, which has no audit event: it is replaced and cannot be reloaded),
  signals (`os.kill`), native loaders (`ctypes`, `sqlite3` and `load_extension`), opening any file
  for writing, creating or truncating, and file-system changes (remove, rename, mkdir, rmdir,
  symlink, link, chmod, truncate, utime, `shutil.*`; `mkfifo` and `mknod` are removed). This
  is in-interpreter defence in depth, NOT OS isolation: native parser code could bypass it. It is
  enough for part 1's pure-Python parsers (pypdf, docutils, PyYAML); before Pillow or pypdfium2
  land (part 2), run the worker with OS-level network isolation (see the RAG P1 plan, P1.B1).
- The step is `eap.kb_extract_chunk`, in `eap.kb_chunk`'s slot, only for runs whose input has
  `embed_split: true` and whose document is worker-extracted (recorded in `eap.start_ingest`'s
  output as `extractor`). Runs enqueued before the deploy, Markdown/HTML/text documents and reindex
  keep their exact step layout. **Drain and do not mix worker versions** (as for P1.C5 above):
  an old worker given a flagged run records `eap.kb_chunk` and the new API's PDF/rst/YAML/JSON
  documents fail there (`extraction_failed`), and a new worker could not replay that record. Stop
  the old workers, drain, then switch the API and workers together.
- A failed extraction ends the run `extraction_failed`; the document's error is
  `extraction_failed: <fixed message>` and `run_failed` carries `extraction_code`
  (`document_encrypted`, `extract_limit`, `extract_timeout`, ...).
- **Search tester writes (P1.C4 follow-up).** No new step: a `knowledge.search` tool run's
  `tool:tool_run:result` and `end` events and its `eap.finish_run` take FOR SHARE locks on the
  knowledge base and the hits' documents in their own transaction and leave out deleted documents'
  hits; a deleted knowledge base ends the run `failed` (`knowledge_base_deleted`, no content).
  Ordinary tool runs are unchanged.

### Fair ingest queue (RAG P1 follow-up, deploy note)

- **New queue `eap-kb-ingest-fair`, partitioned by tenant and knowledge base.** The API now
  enqueues every `eap.kb_ingest` (ingest, re-extract, reindex) there with the partition key
  `{tenant_id}:{knowledge_base_id}` (`eap_schemas.knowledge.kb_ingest_partition_key`). The worker
  registers it with `worker_concurrency` and `partition_worker_concurrency` both set to
  `EAP_KB_INGEST_CONCURRENCY`: one knowledge base alone still uses every slot, but each poll visits
  the partitions with queued runs in random order, so an upload into one knowledge base gets the
  next free slot about half the time even behind a bulk import of hundreds into another (before,
  one FIFO line made it wait for the whole import).
- **Why a new name.** A DBOS queue is either partitioned or not, and a partitioned queue never
  dequeues a workflow without a partition key. Runs the old API enqueued on `eap-kb-ingest` carry
  none, so the old queue stays registered, unpartitioned, with the same per-worker cap, and its runs
  still run. Nothing new is enqueued there.
- **Every worker polls every queue in the database.** DBOS 3.5 queues are rows in the system
  database (`register_queue` upserts one), and each worker's queue thread starts a poller for
  every queue row of the application, registered by it or not (the worker never calls
  `listen_queues`). So once one new worker has registered `eap-kb-ingest-fair`, an OLD worker
  (main's code) dequeues from it too, with the partition limits read from the row, as long as it
  has the same `EAP_APP_VERSION` (dequeue matches the workflow's application version). Workers keep
  polling `eap-kb-ingest` for as long as its row exists, even after the code stops registering it.
- **Mixed versions.** Because of the above, an old worker can pick up a new API's run from the fair
  queue. Its `eap.kb_ingest` lacks P1 extraction, so rst/PDF/YAML/JSON documents would fail there
  (`extraction_failed`) and its step records could not be replayed by a new worker: the same rule
  as the P1.A3 note above, **do not mix worker versions**. Deploy order: stop the old workers,
  start the new ones (they register both queues), then switch the API. While old runs drain, a
  worker can run up to `EAP_KB_INGEST_CONCURRENCY` from each of the two queues (twice the cap on
  the embedding provider); drain `eap-kb-ingest` first (no `eap.kb_ingest` ENQUEUED or PENDING on
  it) if the provider cannot take that. Nothing new is enqueued on it, so once empty it stays empty.
- **Removing the legacy queue later.** Deleting the `register_queue` call is not enough: the row
  stays and every worker keeps polling it. Remove it only when ALL of these hold: no API older
  than this release is running (none can enqueue there); no `eap.kb_ingest` workflow on
  `eap-kb-ingest` is ENQUEUED or PENDING (`DBOS.list_workflows(queue_name="eap-kb-ingest",
  status=["ENQUEUED", "PENDING"])` is empty; delete_queue makes such workflows unrecoverable);
  and every running worker is a release that no longer calls `register_queue("eap-kb-ingest")`
  (otherwise its next start re-creates the row). Then delete the row once, from any process
  against the system database: `DBOSClient(system_database_url=..., dbos_system_schema=...).delete_queue("eap-kb-ingest")`
  (or `DBOS.delete_queue("eap-kb-ingest")` inside a launched worker). Pollers stop within about a
  second (a queue thread exits when its row is gone).
- No step, input or event changes: replay of any run is unaffected.
