# AG-UI smoke test (AGUI.14)

The official TypeScript client, `@ag-ui/client@1.0.3`, against a real PydanticOS API and worker
with the scripted `test:*` models. It has its own pnpm workspace root and lockfile, so it is not
part of the repo's workspace.

```sh
bash tests/ag-ui-smoke/run.sh                         # throwaway stack, all scenarios, then teardown
EAP_SMOKE_SCENARIOS=chat,resume bash tests/ag-ui-smoke/run.sh   # a subset
bash tests/ag-ui-smoke/run.sh --external              # against a running stack (EAP_SMOKE_API_URL, default :8200)
pnpm -C tests/ag-ui-smoke exec tsc --noEmit           # typecheck
```

`scripts/test.sh` runs it when Docker is available.

## The stack `run.sh` brings up

Everything below is removed on exit (`SMOKE_KEEP=1` keeps the database):

| Part | Where |
|---|---|
| Postgres (`pgvector/pgvector:0.8.2-pg17-trixie`, the repo's init scripts, migrated to head) | container `eap_agui_smoke_pg`, volume `eap_agui_smoke_pgdata`, `127.0.0.1:${SMOKE_PG_PORT:-54349}` |
| API (`uvicorn eap_api.app:create_app --factory`) | `127.0.0.1:${SMOKE_API_PORT:-8200}` |
| Worker (`python -m eap_worker`, executor `agui-smoke-worker-1`, app version `agui-smoke`) | its own DBOS system database in that container |
| MCP server (`mcp_server.py`: `lookup_order`, `slow_lookup`, `GET /calls`) | `127.0.0.1:${SMOKE_MCP_PORT:-8209}/mcp` |

The environment is `EAP_AUTH_MODE=dev`, `EAP_ALLOWED_MODELS='test:*'`, the test price file, and
`EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1`. It has no OTLP export and no LiteLLM. It never reads `.env`
and never touches the dev stack (`:8000`, `eap_postgres`). Logs go to `.dev/ag-ui-smoke/`
(`SMOKE_LOG_DIR`).

In external mode only the MCP server is started. The target stack's worker must allow it
(`EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1`) and run the `test:*` models. Each run uses a fresh tenant
(`smoke<hex>`, or `EAP_SMOKE_TENANT`).

## Scenarios

Setup, with the dev bearer of the run's tenant:
- register the MCP server;
- publish the agents (echo, MCP, frontend tool, structured, gated `tickets.create`) and a workflow
  (`ask` agent step, then an `approval` step);
- create a tenant-wide API key.

Every AG-UI call uses that key.

| Scenario | Client | Checks |
|---|---|---|
| `chat` | `ResumableHttpAgent` | `RUN_STARTED`, `CUSTOM pydanticos.run`, text events, `RUN_FINISHED` success; frame ids `{runId}:{seq}`; the client's messages equal `GET .../threads/{id}` |
| `attach` | `ResumableHttpAgent.connectAgent()`, raw GET | A full replay starts with `RUN_STARTED`, `pydanticos.run` and `MESSAGES_SNAPSHOT`. `?after=<seq>` gives a continuation: no `RUN_STARTED`, the snapshot first, only frames after the cursor. A cursor past the run is 400 |
| `resume` | `ResumableHttpAgent` | The connection is cut after the first `TEXT_MESSAGE_CONTENT`, and again after the first frame with an id. Each cut is followed by an identical re-POST with `Last-Event-ID`, a continuation (no `RUN_STARTED`, a snapshot) and no repeated frame id. The final messages equal the server's |
| `mcp_tool` | `ResumableHttpAgent` | `TOOL_CALL_START/ARGS/END/RESULT` for `mcp__smoke__lookup_order`; the MCP server ran it once |
| `frontend_tool` | `HttpAgent` | The run ends with success and `pendingToolCallIds`, with no server-side result. The client's tool message continues the next run (`tool said: ...`), and that result is not re-emitted |
| `previous_turn` | `HttpAgent` | The stream drops after `RUN_STARTED` while a 5 s MCP call runs. The next message gets `RUN_STARTED`, `CUSTOM pydanticos.waiting`, a `MESSAGES_SNAPSHOT` with the missed answer (in the server's order), then its own turn. The slow tool ran once |
| `structured` | `HttpAgent` | `RUN_FINISHED.result` is the object, also returned by `runAgent()`. One text message holds its JSON. There is no `final_result` tool call |
| `tool_approval` | `HttpAgent` | `RUN_FINISHED` interrupt (`reason: tool_call`, `toolCallId`, id `approval:{run}:tool:{call}`) and `CUSTOM pydanticos.approval_required`. The resume (approve) gives `pydanticos.approval_decided`, then the tool result in the same durable run. One `tool_result` event in the native log |
| `workflow_approval` | `HttpAgent` | Steps, `STATE_SNAPSHOT/DELTA`, and an interrupt for the approval step (`reason: confirmation`, `expiresAt`). The resume finishes the same workflow run |

For every scenario:
- every POST body is validated with `RunAgentInputSchema`, and every received event with
  `EventSchema` (`@ag-ui/core/schemas`);
- every stream passes the lifecycle rules in `wire.ts`, which mirror
  `eap_api.ag_ui.conformance.lifecycle_errors`;
- the stock client's own `verifyEvents` runs as well.

`ResumableHttpAgent` is imported from `apps/studio/src/lib/agui/`, so the Studio packages must be
installed (`pnpm install` at the repo root).

## Client behaviour worth knowing

`@ag-ui/client` 1.0.3 merges a `MESSAGES_SNAPSHOT` by message id. Messages it already holds keep
their position, and new ones are appended. So after previous-turn healing, a plain `HttpAgent`
lists the missed answer *after* the new user message, although the snapshot (and the server's
thread) has it before. The smoke compares that client's messages to the server's as a set.
