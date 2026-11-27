# External access: external agents run exported workflows

An external agent (Claude Code, Codex, a LangGraph graph, any A2A client) may run the workflows a tenant admin exported,
follow them, read their results and cancel them. Nothing else. General-purpose agents live outside the platform; the
platform owns repeatable workflows.

Agents connect over **MCP** at `POST /api/v1/external/mcp`, a server of its own beside the operator's control
endpoint (`/api/v1/mcp`, [MCP control](mcp-control.md)), or over **A2A** at `/api/v1/external/a2a/{endpoint_id}`, one
endpoint per tenant. Both are **off** unless `EAP_EXTERNAL_ACCESS_ENABLED=true`. Design:
an internal design note.

## Objects

| Object | What it is |
|---|---|
| External client | One external agent. It owns the runs it starts, so its keys can be rotated. Disabling it stops its keys at the next request and cancels nothing |
| `run` key | A tenant API key with the scope `run` alone: it belongs to a client, has a non-empty workflow allowlist, reaches no agent and expires (default 90 days, at most 365). Every other route refuses it, and the external surface refuses every other key |
| Workflow export | The public contract of one published workflow: public name (`^[a-z][a-z0-9_]{0,47}$`), title, description, tags, examples, version (a number or `latest`), protocols, result mode (`output` or `status_only`), longest run, whether an idempotency key is required |

| A2A endpoint | One per tenant: a random 128-bit id in its URL (rotatable), the name and description of its public Agent Card, enabled or not |

Agents are not exported. To make one callable, wrap it in a one-step workflow and export that.

## Who may do what

| Action | Permission |
|---|---|
| See clients and exports (Studio > External access) | `tenant.read_external_access` (editor) |
| Create or change clients, keys, exports and the A2A endpoint; enable; cancel a client's runs | `tenant.manage_external_access` (admin); making a key also needs `create` on API keys |
| Start, follow and cancel a run | a `run` key: client enabled, key active, export enabled for the protocol and its workflow in the key's allowlist |

A call goes through `eap_api.services.authz` (`run.external`, scope `run`). The OpenFGA engine abstains for keys: the
scope and the allowlist decide.

## Enabling an export

An export is created **disabled**. Only a signed-in admin enables it, on a card showing the workflow, the version, the
input schema, the result mode, the warnings and the keys that can reach it; the request echoes the card's
`approval_hash`, which covers every field above and the version's contract (input schema and side-effect set). Any change
to a field disables the export again (`export_changed`). For `latest`, publishing a version whose input schema or side
effects differ disables it (`export_contract_changed`), and publishing an exported workflow warns
(`external_side_effects_without_approval`, `external_input_into_agent_prompt`).

Enabling is refused:

- without `EAP_EXTERNAL_BASE_URL` (`external_base_url_unset`);
- unless agent and workflow grants are enforced: `EAP_AGENT_GRANTS_ENFORCE=1` and a policy engine that keeps grants
  (`grants_not_enforced`). An exported workflow lends its grants to whoever holds an allowlisted key. A start is refused
  for the same reason if enforcement is switched off later.

The workflow's return value is what leaves the platform (`output` mode): check it carries no secret or personal data.

## What a run does

- It starts with the key as its actor (`principal = api_key:<id>`, on behalf of nobody); its tools and secrets come from
  the workflow's grants. The caller adds only the input, checked against the version's schema (32 KiB at most).
  `run_started.source` names the client, the key and the export.
- **Idempotency.** The key is 1 to 128 characters, scoped to (client, export). The claim is written first, with the
  resolved version and the input's hash; the run id derives from client, export and key. A repeat answers the same run; the
  same key with another input is `idempotency_conflict`; a publish between a send and its retry changes nothing. A crash
  between the claim, the run row and the hand-off is repaired by the retry, or by the worker's sweep once the run row
  exists.
- **States** a caller sees: `queued`, `running`, `input_required` (nothing is answerable from outside in this release;
  an approval never is), `completed` (a skipped run too), `cancelled`, `failed`. A run that needs a person is `failed`
  with `outcome_unknown` and `retry_safe: false`. Results over 256 KiB are withheld (`truncated`). Errors carry a stable
  code and a fixed message: never step ids, logs, host or tool names.
- **A client sees only its own runs.** Anything else reads as not found.
- **Deadline.** The shorter of the caller's `deadline_seconds` and the export's longest run. The worker's
  `eap.external_run_sweep` cancels overdue runs (parked ones included) with the platform's durable cancel; the caller sees
  `failed` with `deadline_exceeded`.
- Every start, cancel and refusal writes an audit row (`external.*`) naming the key and the client, never the input.

Disabling an export or a client does not cancel running runs. "Cancel this client's runs" in Studio is the explicit
action.

## The MCP run surface

Streamable HTTP, stateless, `Authorization: Bearer <run key>`. Both protocol revisions of the official Python client
work: 2026-07-28 and the older initialize handshake.

| Tool | Arguments | Answers |
|---|---|---|
| `run_<export>`, one per enabled MCP export in the key's allowlist | `input` (the version's input schema), `idempotency_key?`, `deadline_seconds?`, `wait_seconds?` (0 to 25, default 10) | the run view |
| `runs.get` | `run_id` | run view |
| `runs.wait` | `run_id`, `timeout_seconds?` (0 to 60, default 30) | run view, as soon as the run ends or needs a person |
| `runs.list` | `status?`, `cursor?`, `limit?` (1 to 50, default 20) | `{items, next_cursor}`: this client's runs, without results |
| `runs.cancel` | `run_id` | run view (repeating it is harmless) |
| `exports.list` | | name, title, description, input schema, result mode and version of each export |

Run view: `{run_id, export, version, state, done, waiting?: {kind, answerable}, result?, truncated?, outcome?,
error?: {code, message}, retry_safe?, deadline, poll_after_seconds}`.

- No tool approves, rejects or answers anything. A run waiting for a person shows `input_required` with
  `answerable: false`; a person decides in the platform.
- A start never holds the connection longer than 25 s, a wait never longer than 60 s. Follow with `runs.wait`.
- `tools/list` is computed from the key on every request, in a fixed order, cacheable privately for 30 s. A tool list
  therefore changes when an admin enables or disables an export; a client that keeps its list may need to reconnect.
- Errors are tool results `{error: {code, message, details?, retry_after_seconds?}}` with `isError`: for example
  `export_not_found`, `run_not_found`, `input_invalid` (locations and keywords only), `invalid_arguments`,
  `idempotency_key_required`, `idempotency_conflict`, `quota_exceeded`, `rate_limited`, `start_unavailable` and
  `cancel_unavailable` (repeat the same call), `grants_not_enforced`, `client_disabled`, `key_inactive`.
- Before the protocol: switch off 404; no `run` key 401; another scope, a foreign `Origin` or a disabled client 403;
  a request over 64 KiB 413; over a rate limit 429 with `Retry-After` (a `tools/call` over the per-key rate answers the
  `rate_limited` tool error instead). Unauthenticated requests are limited per address and write nothing to the database.
- Audit rows (`external.run.start`, `external.run.cancel`, refusals) name the key and the external client, plus the name
  the caller declared for itself (MCP `clientInfo`, else its user agent) as an untrusted label. Reads that succeed write
  none.

## The A2A server

A2A protocol **1.0** over **JSON-RPC**, no other version and no other binding. Base:
`<EAP_EXTERNAL_BASE_URL>/api/v1/external/a2a/{endpoint_id}`.

| Path | Credential | What |
|---|---|---|
| `{base}/.well-known/agent-card.json` | none (rate limited per address; `ETag`, `max-age=300`) | the public card: the endpoint's name and description, the interface, the bearer scheme and **one generic skill**. It lists no workflow |
| `{base}/rpc` | `Authorization: Bearer <run key>` and `A2A-Version: 1.0` | JSON-RPC |
| `{base}/skills/{name}/.well-known/agent-card.json`, `{base}/skills/{name}/rpc` | as above | the same for one skill, for a client that cannot name a skill in a message. Its public card does not say whether the skill exists |

| Method | What it does |
|---|---|
| `GetExtendedAgentCard` | the skills of the enabled A2A exports this key reaches: id = export name, title, description, tags, an example input and the admin's examples. The extension `urn:eap:a2a:skill-input-schema:v1` maps each skill id to the JSON Schema of its input |
| `SendMessage` | starts a run and answers its task (never a bare message) |
| `GetTask` | one task of this client |
| `ListTasks` | this client's tasks (those started over MCP included), the latest status change first; `contextId`, `status`, `statusTimestampAfter`, `pageSize` (50, at most 100), `pageToken`, `includeArtifacts` (off); always `nextPageToken`, `pageSize`, `totalSize` |
| `CancelTask` | cancels a task that has not ended |

Capabilities: `streaming: false`, `pushNotifications: false` (a later release), `extendedAgentCard: true`.

**A message.** `messageId` (required, at most 128 characters), `role: ROLE_USER`, and exactly one part: a data part
(`application/json`) with the input, or a text part when the export's input is a single required string property. Files
and URLs are refused and never fetched. Which skill runs:

1. the one the message names: a data part `{"skill": "<name>", "input": {...}}`, or `message.metadata.skill` with the
   input in the data part (on a per-skill base it must be that skill);
2. on a per-skill base, that skill;
3. else the skill an earlier send of the same `messageId` started;
4. else the only skill the key reaches; with several, `InvalidParams` naming them.

`message.metadata.deadlineSeconds` (or the request's `metadata`) shortens the run's deadline.

**Blocking sends.** Set `configuration.returnImmediately: true` and follow the task with `GetTask`. Without it the send
waits at most **60 s** for the task to end or need a person, then answers the task that is still working (a bounded wait
by design, which departs from the specification's unbounded blocking). Set the client's HTTP timeout above 60 s; many
default to a few seconds.

**Idempotency.** `messageId` is the idempotency key, scoped to (client, export): the same message again answers the same
task, also after the export was disabled or others were added. The same id with another input or another `contextId` is
`InvalidParams` (`idempotency_conflict`).

**Task.** `id` is the run id. `contextId` is yours (kept as a label, at most 200 characters; it shares nothing between
runs) or a generated one; a run started over MCP has its run id. `history` is always empty. A completed task has one
artifact, `result`: a data part with the workflow's return value (none for a `status_only` export; over 256 KiB none and
`metadata.truncated`). `metadata`: `export`, `version`, `deadline`, and where they apply `outcome`, `errorCode`,
`retrySafe`.

| Run | `status.state` | Notes |
|---|---|---|
| queued | `TASK_STATE_SUBMITTED` | |
| running | `TASK_STATE_WORKING` | no step names or logs |
| waiting for an approval or an input step | `TASK_STATE_INPUT_REQUIRED` | message "waiting for a person in the platform to decide" |
| completed or skipped | `TASK_STATE_COMPLETED` | skipped: `metadata.outcome` |
| cancelled | `TASK_STATE_CANCELED` | |
| failed, over its deadline included | `TASK_STATE_FAILED` | `metadata.errorCode`, a fixed message |
| needs attention | `TASK_STATE_FAILED` | `errorCode: outcome_unknown`, `retrySafe: false`: do not resubmit |

**Nothing can be decided from outside.** A message to a task (`taskId`) changes nothing in this release: approvals and
input steps are answered by people in the platform.

**Errors** are JSON-RPC errors (HTTP 200) with a `google.rpc.ErrorInfo` whose `metadata.reason` is the platform's
stable code, and for invalid parameters a `google.rpc.BadRequest` naming the fields (never the rejected value):

| Code | When |
|---|---|
| `-32001` TaskNotFound | no such task of this client (another client's, a person's run, another tenant's) |
| `-32002` TaskNotCancelable | `CancelTask` on a task that has ended |
| `-32003` PushNotificationNotSupported | a push method, or a push configuration in a send |
| `-32004` UnsupportedOperation | streaming and subscribing; a message to a task; grants no longer enforced; an archived workflow |
| `-32005` ContentTypeNotSupported | a file or URL part, another media type, text for a structured input |
| `-32007` ExtendedAgentCardNotConfigured | the key reaches no skill here |
| `-32009` VersionNotSupported | `A2A-Version` is not 1.0 (a request without it is 0.3) |
| `-32600`, `-32601`, `-32602`, `-32700` | not a request (batches and notifications included), unknown method, invalid parameters, not JSON |
| `-32603` | an internal error; with HTTP 429 (rate or quota) or 503 (not yet possible: repeat the same request) and `Retry-After` |

Before the protocol, the same answers as the MCP surface: switch off, unknown, disabled or another tenant's endpoint
404; no `run` key 401; another scope, a foreign `Origin` or a disabled client 403; a request over 64 KiB 413; over a
rate limit 429. Limits, quotas, deadlines, ownership and audit rows are those of the MCP surface (one service); the
audit label is the caller's user agent.

## Operator setup

1. Preconditions: `EAP_AUTH_MODE=production` (the API refuses to start with external access on under `dev`),
   `EAP_AGENT_GRANTS_ENFORCE=1` with the grants backfill reviewed, a policy engine that keeps grants, and
   `EAP_EXTERNAL_BASE_URL` set to the public https origin.
2. Deploy with `EAP_EXTERNAL_ACCESS_ENABLED=false`. Check: `/api/v1/external/mcp` answers 404.
3. In Studio > External access create one client, one export of a workflow with no side effects, and one key; enable the
   export. For A2A, tick the A2A protocol on the export, then fill in and enable the **A2A endpoint** card: its name and
   description are public to anyone who has the URL, so name no customer or process there. Set the switch to `true` and
   try it through a private connection first.
4. Only then make the one prefix public ([Expose Studio and the API](expose.md)). Where a sign-in gate stands in front of
   the host, let exactly `<public-host>/api/v1/external` through it, add a rate limit at the proxy for that prefix and
   change nothing else. Behind a proxy set `EAP_TRUSTED_PROXY_CIDRS`, or every caller shares one per-address bucket. Check:
   `/api/v1/external/mcp` answers 401 without a key, while `/api/v1/mcp`, `/api/v1/agents` and path tricks
   (`/api/v1/external/../agents`, encoded slashes, case variants) still meet the gate.
5. Connect an agent from outside, then export real workflows one at a time.

For A2A, also check from outside that the endpoint's card answers 200 without a key and `{base}/rpc` answers 401. Hand
the agent the endpoint's URL and its key. Rotating the endpoint id invalidates the old URL at once.

To turn it off, quickest first: disable the export, the client or the A2A endpoint (next request); set the switch to `false` (404; running
runs continue and stay visible in Studio); remove the proxy exception.

## Connect a client

The key is shown once, when it is made. Keep it in an environment variable; never write it into a file that is shared or
committed. In the examples `DURABLE_RUN_KEY` holds it and `<public-host>` is `EAP_EXTERNAL_BASE_URL`'s host.

Claude Code, as a project file (`.mcp.json`; the variable is expanded when Claude Code reads it, so the file holds no
key):

```json
{
  "mcpServers": {
    "durable": {
      "type": "http",
      "url": "https://<public-host>/api/v1/external/mcp",
      "headers": { "Authorization": "Bearer ${DURABLE_RUN_KEY}" }
    }
  }
}
```

Or for yourself only (the shell expands the variable, and Claude Code stores the value in your own configuration):

```sh
claude mcp add --transport http durable https://<public-host>/api/v1/external/mcp \
  --header "Authorization: Bearer $DURABLE_RUN_KEY"
```

`claude mcp list` then shows `durable` connected. Ask it to list the exports, run one and wait for it.

Codex (`~/.codex/config.toml`; the key is read from the environment):

```toml
[mcp_servers.durable]
url = "https://<public-host>/api/v1/external/mcp"
bearer_token_env_var = "DURABLE_RUN_KEY"
tool_timeout_sec = 90  # runs.wait may take 60 s
```

Or `codex mcp add durable --url https://<public-host>/api/v1/external/mcp --bearer-token-env-var DURABLE_RUN_KEY`.

Any other client: Streamable HTTP, the URL above and the `Authorization` header. Some clients prefix tool names with the
server name and cap their length, so keep the server name and export names short.

### An A2A client (the official SDK)

[`docs/examples/external_a2a_client.py`](examples/external_a2a_client.py) is a complete client on the official
`a2a-sdk` (tested against this server). It resolves the card, lists the skills, sends and follows a task:

```sh
pip install "a2a-sdk==1.3.1"
export DURABLE_A2A_URL="https://<public-host>/api/v1/external/a2a/<endpoint-id>"
export DURABLE_RUN_KEY=...   # from your secret store; never in a file that is shared or committed
python docs/examples/external_a2a_client.py
python docs/examples/external_a2a_client.py run_report '{"q": "x"}'
```

The core of it:

```python
async with httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=90) as http:
    card = await A2ACardResolver(http, base_url).get_agent_card()
    client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)
    part = json_format.ParseDict({"data": {"skill": "run_report", "input": {"q": "x"}}}, Part())
    request = SendMessageRequest(
        message=Message(message_id=uuid.uuid4().hex, role=Role.ROLE_USER, parts=[part]),
        configuration=SendMessageConfiguration(return_immediately=True),
    )
    async for reply in client.send_message(request):
        task = reply.task
    task = await client.get_task(GetTaskRequest(id=task.id))
```

A LangGraph graph uses it as a node: keep the `messageId` in the graph state, so a retried node finds its task instead
of starting another.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `EAP_EXTERNAL_ACCESS_ENABLED` | off | Whether external callers are served. The API refuses to start with it on under `EAP_AUTH_MODE=dev` |
| `EAP_EXTERNAL_BASE_URL` | unset | The public https origin; unset: exports can be drafted, not enabled |
| `EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT` | 5 | A client record may set its own, up to 50 |
| `EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_TENANT` | 20 | Unended external runs of a tenant |
| `EAP_EXTERNAL_MAX_RUNS_PER_DAY` | 500 | Per client, rolling 24 hours; a client record may set its own, up to 10000 |
| `EAP_EXTERNAL_SWEEP_SCHEDULE` | `0 * * * * *` | Worker: the sweep's DBOS cron (seconds first); `off` removes it |
| `EAP_EXTERNAL_REQUESTS_PER_KEY_PER_MINUTE` | 120 | Per key and API process; `0` turns it off |
| `EAP_EXTERNAL_IP_RATE_PER_MINUTE` | 300 | Per client address and API process, before the key is read; `0` turns it off |
| `EAP_EXTERNAL_CARD_REQUESTS_PER_IP_PER_MINUTE` | 60 | Unauthenticated A2A card requests per client address and API process; `0` turns it off |
| `EAP_EXTERNAL_MAX_CONCURRENT_PER_KEY` | 10 | MCP tool calls and A2A requests in flight per key (a wait counts) |
| `EAP_EXTERNAL_MAX_CONCURRENT_GLOBAL` | 200 | The same, all keys |
| `EAP_TRUSTED_PROXY_CIDRS` | unset | Peers whose `CF-Connecting-IP` names the caller's address (shared with webhooks) |

## API (people only)

`/api/v1/agent-access`: `GET` (status), `clients` (list, create, read, `PATCH` with `If-Match`, `keys`, `cancel-runs`),
`exports` (list, create, read, `PATCH` with `If-Match`, `enable`, `disable`), `a2a-endpoint` (`GET`, `PUT` with
`card_name`, `card_description` and `enabled`, `POST …/rotate`). Enabling the endpoint needs `EAP_EXTERNAL_BASE_URL`.

## Rollout

Run migration `0043_external_access`, then bootstrap the OpenFGA model (it gains
`can_manage_external_access` and `can_read_external_access`) and update the pinned model id, as in `docs/iam.md`,
"Bootstrap". Leave `EAP_EXTERNAL_ACCESS_ENABLED` off until "Operator setup" above is done.
