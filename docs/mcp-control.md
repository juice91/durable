# Controlling PydanticOS through MCP

An external agent or client (Claude Code, Codex, another tenant's agent, an operator's script) can
manage a tenant's platform through the [Model Context Protocol](https://modelcontextprotocol.io)
instead of hand-written HTTP calls: list and draft agents and workflows, publish them with the same
guards as Studio, run workflows and agents, wait for runs and read bounded results, sync knowledge
source data, check connections, list MCP servers and decide approvals.

The design and its reasons are in
[`docs/superpowers/specs/2026-11-18-mcp-control-design.md`](superpowers/specs/2026-11-18-mcp-control-design.md).

## Endpoint and key

- **Endpoint:** `POST /api/v1/mcp` (Streamable HTTP, stateless). Studio shows it, with the connect
  snippets, under Settings → API keys.
- **Credential:** `Authorization: Bearer <key>`, a tenant API key (`pos_<id>_<secret>`). Every call
  authenticates again, so a revoked key stops at once. The dev bearer (`dev:<tenant>:<user>`)
  works in `EAP_AUTH_MODE=dev` only. The tenant always comes from the key.
- **Scopes:** a key needs the `mcp` scope to connect at all: without it (an AG-UI frontend key with
  only `agui:run`) the endpoint answers 403 `forbidden` before any tool runs. `mcp` allows the
  **read** tools. `manage` implies `mcp` and also allows the **mutation** tools; with `mcp` alone a
  mutation answers `{error: {code: "scope_required"}}`. A key keeps its `agent_ids` / `workflow_ids`
  allowlists (other agents and workflows look like unknown ones, and the key cannot create new ones)
  and its `allowed_origins`.
- Keys are created in Studio (Settings → API keys) or with `POST /api/v1/api-keys`
  (`{"name": "...", "scopes": ["mcp"]}` to read, `["manage"]` to also change). A key can also carry
  `otlp`, which only sends OpenTelemetry traces (see [external traces](observability/external-traces.md)).

## Connect a client

Claude Code:

```sh
export PYDANTICOS_API_KEY=pos_...
claude mcp add pydanticos --transport http https://<host>/api/v1/mcp \
  --header "Authorization: Bearer $PYDANTICOS_API_KEY"
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.pydanticos]
url = "https://<host>/api/v1/mcp"
bearer_token_env_var = "PYDANTICOS_API_KEY"
```

Any other client: Streamable HTTP, the URL above, the `Authorization` header. Then ask the client
to call `agent.list`, or `agent.chat` with the Architect (below).

### Manual smoke test (Claude Code)

1. Create a key with Connect over MCP (read), or Allow changes for mutations, in Settings → API keys and export it as `PYDANTICOS_API_KEY`.
2. Register the server as above, then `claude mcp list` shows `pydanticos` connected.
3. In Claude Code: "list my PydanticOS agents" calls `agent.list`.
4. "Ask the Architect to draft a support-triage agent" calls `agent.list` to find the Architect and
   `agent.chat`; approvals it asks for come back in `pending_approvals` and are decided with
   `approvals.decide` (or in Studio).

## Tools

Names are the platform's functional namespace (`agent.*`, `workflow.*`, `kb.*`, `settings.*`,
`mcp.*`, `approvals.*`). Results are objects; lists are paged (`limit` at most 100, `offset`,
`next_offset`). A refusal is the platform's stable error object `{error: {code, message}}` with MCP
`isError`, never a traceback.

### Read (`mcp` or `manage`)

| Tool | Input | Returns |
| --- | --- | --- |
| `agent.list` | `archived?`, `include_system?`, `limit?`, `offset?` | agents (id, name, draft_spec, latest_version, archived) |
| `agent.get` | `agent_id` | one agent |
| `agent.versions` | `agent_id`, `limit?`, `offset?` | published versions, newest first |
| `workflow.list` | `limit?`, `offset?` | workflows (not archived) |
| `workflow.get` | `workflow_id` | one workflow |
| `workflow.versions` | `workflow_id`, `limit?`, `offset?` | published versions |
| `workflow.run.get` | `run_id` | status, display_status, error |
| `workflow.run.wait` | `run_id`, `timeout_seconds?` (≤ 300) | `{status, done}`; `done` once the run ended or waits for approval |
| `workflow.run.result` | `run_id` | bounded report: status, usage, input and output (each ≤ 32 KiB) |
| `workflow.schedule.list` | `workflow_id?`, `limit?` | schedules |
| `kb.list` / `kb.get` | `kb_id` | knowledge bases with counts |
| `kb.source.list` / `.get` / `.syncs` | `kb_id`, `source_id`, `limit?` | data sources and their sync runs |
| `settings.connection.list` | – | connections; `credential_refs` are `secret:<name>` references only |
| `settings.connection.check` | `connection_id` | `{ok, checked_at, error_code}` |
| `mcp.server.list` | `limit?`, `offset?` | registered MCP servers (header and variable names only) |
| `capability.list` | `kind?`, `status?`, `query?`, `include_schemas?` | what the platform can do now (the capability registry): entries with status `available`, `designed_not_built` or `disabled`, approval and limits, the known gaps and a snapshot hash |
| `capability.get` | `capability_id` | one capability with its schemas, limits and the gaps that concern it; an unknown id answers the nearest ids |
| `system.context` | none | the system context for the caller: capability lines, known gaps, the tenant's resource names and counts (secrets by name only), operational facts and the principal; a layer that could not be built is missing and named in `degraded` |
| `provenance.get` | `kind`, `definition_id`, `spec_hash?` | what a draft or publish card of an agent or workflow was based on (hashes, principal summary, tool-call names) and the capabilities it uses that changed since |
| `mcp.catalog.search` | `query?`, `limit?`, `after?`, `source?` | mirrored registry entries |
| `approvals.list` | `status?`, `kind?`, `agent_id?`, `limit?`, `cursor?` | gated tool calls and workflow approval steps |
| `scorer.list` | `limit?`, `offset?` | reusable scorers with the agents each is attached to |
| `eval.suite.list` | `agent_id` or `workflow_id`, `include_archived?`, `limit?`, `offset?` | the target's test suites with their evaluators and case counts |
| `eval.run.get` | `run_id`, `results_limit?` (≤ 100) | an eval run's status and aggregates, optionally its first per-case results |

### Mutations (`manage` scope)

| Tool | Input | Does |
| --- | --- | --- |
| `agent.save_draft` | `spec`, `agent_id?` | create an agent, or replace its draft |
| `agent.publish` | `agent_id` | publish the draft with Studio's checks (`warnings` returned) |
| `agent.archive` | `agent_id`, `archived?`, `force?` | archive or unarchive |
| `agent.chat` | `agent_id`, `message`, `conversation_id?`, `wait_seconds?` (≤ 300) | one turn: `{text, run_id, conversation_id, status, done, pending_approvals}` |
| `workflow.save_draft` | `spec`, `workflow_id?` | create a workflow, or replace its draft |
| `workflow.publish` | `workflow_id` | publish the draft as the next version |
| `workflow.archive` | `workflow_id`, `archived?`, `force?` | archive or unarchive any workflow; pauses its schedules, refused (`runs_active`) while runs are queued or running unless `force` |
| `workflow.run.start` | `workflow_id`, `input`, `version?`, `run_id?` | start a run (`run_id` makes a retry idempotent) |
| `workflow.run.cancel` | `run_id` | cancel the run and its tree |
| `workflow.schedule.set` | `workflow_id`, `name?`, `cron?`, `timezone?`, `input?`, `schedule_id?`, `active?` | create a schedule, or change / pause one |
| `kb.create` | `name`, `embedding_model`, `description?`, `chunking?` | create a knowledge base |
| `kb.source.create` | `kb_id`, `connection_id`, `name`, `provider`, `config?`, `kind?`, `enabled?` | add a data source |
| `kb.source.sync` | `kb_id`, `source_id`, `mode`, `part_numbers?`, `restart?`, `run_id?` | start a sync |
| `settings.connection.create` | `name`, `config`, `credential_refs?`, `base_url?`, `kind?`, `enabled?` | register a connection (references only) |
| `mcp.server.install` | `catalog_name`, `package_index?`, `remote_index?`, `name?`, `env?` | install a catalog entry |
| `mcp.server.runtime` | `server_id`, `action` (`start` / `stop` / `restart`) | control a managed server's pod |
| `approvals.decide` | `run_id`, `kind` (`tool` / `step`), `key`, `decision` (`approve` / `deny`), `comment?`, `resume_data?` | decide an approval |
| `scorer.create` | `name`, `evaluator`, `description?` | create a reusable scorer (`contains`, `regex`, `schema_valid`, `field_equals`, `max_cost`); a restricted key cannot |
| `scorer.attach` | `agent_id`, `scorer_id`, `sampling_rate?` (0..1] | attach a scorer to an agent; returns its attached scorers |
| `scorer.suggest` | `agent_id` or `workflow_id`, `max?` (1..12, default 8), `include_runs?` | AI-suggested rules `{suggestions: [{title, rationale, scope, spec, evidence}], model, dropped}`; runs the built-in suggester (a model run), cached 10 minutes, adds nothing |
| `eval.suite.create` | `name`, `agent_id` or `workflow_id`, `evaluators?`, `description?`, `suite_id?` | create a test suite |
| `eval.case.add` | `suite_id`, `name`, `input`, `expected_output?`, `metadata?`, `evaluators?` | add a case to a suite |
| `eval.run.start` | `suite_id`, `version?`, `max_concurrency?`, `run_id?` | run a suite against a published version (agents: also `"draft"`); read it with `eval.run.get` |

Every mutation calls the code the HTTP route runs (the route function itself, or the shared
service under it: publish, restore and archive in `eap_run_store.definitions`, syncs in
`eap_run_store.kb_source_launch`, runs through the launcher), so a refusal is the refusal Studio
gets, with the platform's stable code.

### The Architect from Claude Code

`agent.chat` with the built-in Architect agent (find it with `agent.list`, `include_system`
unnecessary) turns an external client into a front end for it. The Architect's own publish,
archive and schedule tools are gated by approvals: they arrive in `pending_approvals` (also in
`approvals.list` and Studio) and are decided with `approvals.decide`. After the decision read the
finished run with the `pydanticos://runs/{run_id}/report` resource, or continue the conversation
with `agent.chat` and the same `conversation_id` once `done` is true.

## Resources

Read-only, tenant-scoped, at most 256 KiB:

- `pydanticos://agents/{id}`: the agent and its draft spec
- `pydanticos://workflows/{id}`: the workflow and its draft spec
- `pydanticos://runs/{id}/report`: a run's bounded report (any kind)
- `pydanticos://knowledge-bases/{id}`: a knowledge base with counts

## Concurrent edits

`agent.save_draft`, `workflow.save_draft` and `workflow.schedule.set` (with `schedule_id`) pass the
revision they read. If someone saved in between, the tool reads again, re-applies its change and retries
once; a second conflict is the error `{error: {code: "revision_mismatch", message, current_revision}}`:
read the resource again before saving. See [concurrency.md](concurrency.md).

## What is not exposed, and why

- **Secrets and connection credentials.** There is no tool to store or read a secret: the
  data-source credentials rule stands. A connection holds `secret:<name>` references; a value is
  refused (`secret_ref_required`) and never echoed. `mcp.server.install` refuses a value for a
  variable the catalog marks secret (`secret_value_refused`) and takes no headers: store the secret
  in Studio, then pass the reference.
- Raw events, traces, attachment bytes, platform settings, API key management.
- Other tenants: nothing is addressable by id across tenants (404 `not_found`, like an unknown id).

## Limits and audit

- Lists are paged at 100; a run's input and output are cut at 32 KiB; a result is cut at 256 KiB.
- Each key may have 10 calls in flight (`EAP_MCP_MAX_CONCURRENT_PER_KEY`, global
  `EAP_MCP_MAX_CONCURRENT_GLOBAL`, default 200); beyond that a call answers `rate_limited`.
- Every call writes one audit record (`eap.audit=mcp_call`) with the tool, tenant, key id, outcome
  and error code; never arguments or results.

## The Architect uses this catalog too

The built-in Architect discovers these tools itself (`platform__list_capabilities`) and calls them as
`control__<name with dots as underscores>`; the tools that change something ask the person for approval first, and
`approvals.decide` and `agent.chat` are not available to it. Its audit actor is `architect-run:<run id>`. See
[The Architect](architect.md#what-the-architect-can-operate).
