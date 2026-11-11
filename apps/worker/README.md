# eap-worker

Durable agent runs for the Enterprise Agent Platform: one generic Pydantic AI agent (`eap-agent`,
`DBOSDurability`) driven by the DBOS workflow `eap.agent_run` on queue `eap-agent-runs`, per the
"Run hand-off contract" in `docs/plans/2026-11-11-milestone-1-vertical-slice.md`.

Run it: `uv run python -m eap_worker` (blocks), or `bash scripts/dev-worker.sh` for a restart loop
that recovers the previous process's pending runs (fixed `EAP_EXECUTOR_ID`). Configuration is in
`.env.example`.

## Models

A spec's `model: {provider, name}` becomes the model id `provider:name`, resolved inside each
model step:

- `test:ticket`, `test:first-tool`, `test:<anything>` — deterministic scripted models used by tests.
- `litellm:<name>` — OpenAI-compatible gateway (SDK retries off; the DBOS model step retries).
  Base URL from `EAP_LITELLM_BASE_URL` (fallback `OPENAI_BASE_URL`), key from
  `EAP_LITELLM_API_KEY` (fallback `LITELLM_API_KEY`). The platform key is scoped to
  `qwen3.8-thinking` (a reasoning model): `{provider: "litellm", name: "qwen3.8-thinking"}`.
  Load the key into your shell, never into a file:
  `export LITELLM_API_KEY="$(sops -d ~/.config/sops/secrets.json | jq -r .LITELLM_QWEN38_THINKING_API_KEY)"`.

  The public URL `https://llm-gateway.example.com/v1` sits behind Cloudflare, which cuts any single
  request at ~300 s. For local development use the SSH tunnel `http://127.0.0.1:14001/v1`, which
  has no such limit.

## Tests

`uv run pytest apps/worker/tests -q` needs the dev Postgres (`bash scripts/dev-db.sh up`). Each
session uses a private DBOS system schema and drops it. `test_live_litellm.py` runs only when
`EAP_LITELLM_API_KEY` or `LITELLM_API_KEY` is set.

## Crash hook

`EAP_CRASH_AT` = `model_request` | `after_tool_call` | `after_event_write` calls `os._exit(137)` at
that point; see `src/eap_worker/crash.py` for exactly where.
