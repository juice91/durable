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

## Tests

`uv run pytest apps/worker -q` needs the dev Postgres (`bash scripts/dev-db.sh up`). Each
session uses a private DBOS system schema and a freshly migrated `eap_app` schema (the `eap_db`
pytest plugin), and drops both. `test_live_litellm.py` runs only when
`EAP_LITELLM_API_KEY` or `LITELLM_API_KEY` is set.

## Crash hook

`EAP_CRASH_AT` = `model_request` | `after_tool_call` | `after_event_write` calls `os._exit(137)` at
that point; see `src/eap_worker/crash.py` for exactly where.
