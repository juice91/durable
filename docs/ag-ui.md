# AG-UI integration guide

PydanticOS serves every published agent and workflow over [AG-UI](https://docs.ag-ui.com) 1.0, so
any AG-UI client can drive it: CopilotKit, `@ag-ui/client`'s `HttpAgent`, or your own. This guide
is for engineers who build such a frontend. A working CopilotKit app is in
[`examples/copilotkit-agui/`](../examples/copilotkit-agui/README.md).

The design, with the reasons behind each rule, is in
[`docs/plans/2026-11-13-milestone-ag-ui.md`](plans/2026-11-13-milestone-ag-ui.md) (D0–D21, the
Resume protocol and the Event mapping).

## How it works

PydanticOS runs agents and workflows durably in a worker (DBOS). The API records each run, hands it
to the worker, and streams the run's durable event log. AG-UI is one output format of that stream;
the native API (`GET /api/v1/runs/{id}/stream` and the REST routes) is the other, and Studio is
built on it. Two things follow:

- **A dropped connection never stops a run.** The run keeps going in the worker. You can reattach,
  and you see the same events again with the same ids.
- **The server's history is authoritative.** The client's `messages` array is not trusted (see
  [History](#history-what-the-server-reads-from-messages)).

## Endpoints

All paths are under `/api/v1/ag-ui`. Each POST takes an AG-UI `RunAgentInput` and answers an SSE
stream of AG-UI events (`Content-Type: text/event-stream`).

| Method and path | What it does |
|---|---|
| `POST /agents/{agentId}` | Run one chat turn on a published agent |
| `GET /agents/{agentId}/threads/{threadId}` | The thread's history: `{threadId, conversationId, messages, runs, activeRunId}` |
| `GET /agents/{agentId}/threads/{threadId}/runs/{runId}/stream` | Attach to a run, or resume it (`Last-Event-ID` or `?after=<seq>`) |
| `POST /workflows/{workflowId}` | Start a workflow run, or answer its approvals |
| `GET /workflows/{workflowId}/threads/{threadId}` | The workflow thread's run and messages |
| `GET /workflows/{workflowId}/threads/{threadId}/runs/{runId}/stream` | Attach to or resume a workflow run |

One URL per agent or workflow serves every published version: publishing a new version never
breaks a deployed frontend. The latest published version runs unless the request names another.

`threadId` and `runId` are 1–256 printable ASCII characters without `/`, `\` or `..`, because they
are also path segments of the GET routes.

### Run options: `forwardedProps.pydanticos`

Every other key of `forwardedProps` is ignored, so CopilotKit's own keys are harmless.

| Key | Meaning |
|---|---|
| `version` | A published version number. `"draft"` works with the dev bearer only (403 for an API key) |
| `model` | `{provider, name}`: a per-run model override. Dev bearer only (403 for an API key) |
| `requestContext` | Values for the agent's `context_fields`, validated like the native `RunCreate.request_context` |
| `resourceId` | The resource of a thread's conversation (memory), set once when the thread starts; a different value later is 409 `resource_mismatch` |
| `input` | A workflow's input (see [Workflows](#workflows)) |

`state` and `context` (CopilotKit readables) are accepted and ignored: they are never stored and
never shown to the model.

## Authentication: tenant API keys

AG-UI routes take a tenant API key as a bearer token: `Authorization: Bearer pos_<id>_<secret>`.
Create one in Studio (Settings → API keys) or with `POST /api/v1/api-keys`. The plaintext is shown
**once**, when the key is created; only a hash is stored.

- **Keys can only be created through a dev-mode API until enterprise auth exists.** The key
  management routes (`/api/v1/api-keys`) take Studio's dev bearer, which only an API started with
  `EAP_AUTH_MODE=dev` accepts. A production API runs with `EAP_AUTH_MODE=api_key`: there, API keys
  on `/api/v1/ag-ui/*` are the only credential, every other route answers 401, and `/healthz` is
  the only route without credentials. Create keys with a dev-mode API on the same database.
- A key works only on `/api/v1/ag-ui/*`. It can be limited to some agents and workflows (an agent
  outside the list is 404 `Agent not found`, the same answer as an unknown or another tenant's
  agent), given an expiry, and revoked. A revoked key gets 401 on its next request; an open stream
  re-checks its key about once a minute and ends with `RUN_ERROR{code: "unauthorized"}`. The run
  itself keeps going.
- A key cannot run drafts or override the model (403).
- **Keep the key on a server.** The recommended topology is a backend (the CopilotKit runtime, or
  your own server) that holds the key and talks to PydanticOS; the browser talks only to that
  backend. For a browser-direct prototype, the API's operator lists your origin in
  `EAP_AGUI_CORS_ORIGINS`, and you can bind the key to it (`allowed_origins`: another origin gets
  403). An origin binding is a browser control only: a server-side caller sends no `Origin`.

## Ids

| AG-UI | PydanticOS |
|---|---|
| `threadId` (agent) | A conversation. A UUID that names a conversation of this tenant and agent uses it (Studio's ids); any other value derives a stable conversation, created on first use |
| `threadId` (workflow) | **One workflow run** (see [Workflows](#workflows)) |
| `runId` | One chat turn. A POST with a `runId` the server already knows never starts anything new (see [Resume](#resume-and-reconnect)) |
| `TEXT_MESSAGE_*` `messageId` | `{runId}-m{n}` for the turn's n-th model request (a workflow's agent step adds the step id: `{runId}-{stepId}-m{n}`) |
| tool result `messageId` | `{runId}-t-{toolCallId}` |
| user message id | Your id, stored with the turn |
| `CUSTOM pydanticos.run` | `{run_id, conversation_id, turn, trace_id, model_id, version, draft}`: the native ids, for the native API (cost, traces) |

Ids are derived from durable data, so a replay, an attach and a correction produce the same ids
as the live stream.

A POST with a known `runId` and a **different** body is 409. The comparison covers the new input,
tool results, resume entries, client tools and run options, not ids, history or `state`.

## History: what the server reads from `messages`

The server keeps the conversation. From your `messages` it reads only the **tail after the last
`assistant` message**:

- `user` messages: the new input: text, plus `image` / `document` parts that reference a file uploaded
  with `POST /api/v1/attachments` (a `url` source `/api/v1/attachments/{id}`; see
  [Attachments and feedback](#attachments-and-feedback)). Inline data, other URLs and other part
  types are 422 `unsupported_content`;
- `tool` messages: results of the frontend tool calls the previous turn left pending.

Everything before the tail, and `assistant`, `system`, `developer` and `reasoning` messages in it,
are ignored (logged by count only). A client cannot forge an assistant turn, a tool result or a
system prompt. You may send the whole history (what `HttpAgent` does) or only the new messages.

A user message the server already recorded for this thread (a plain client re-sends it after a lost
stream) is not run twice. Re-sending the latest recorded user message alone under a new `runId` is a
**regenerate**: the turn runs again, and snapshots then show the new answer in place of the old one.

### Attachments and feedback

- **Attachments.** Upload a file (image PNG/JPEG/GIF/WebP, PDF, plain text, Markdown, CSV, JSON) as
  multipart field `file` to `POST /api/v1/attachments` (dev bearer): `{id, name, media_type, size}`.
  413 over `EAP_CHAT_ATTACHMENT_MAX_BYTES` (default 10 MiB), 415 for another type. Send it in a user
  message as `{"type": "image" | "document", "source": {"type": "url", "value": "/api/v1/attachments/{id}",
  "mimeType": ...}, "metadata": {"filename": ...}}`. Up to 5 per message; one attachment belongs to one
  message. 422 `attachment_not_found`, `attachment_in_use`, `attachment_unsupported_by_model` (images and
  PDFs need a model that takes them; text files are inlined for any model). Thread GET and snapshots
  return the same parts on the user message. `GET /api/v1/attachments/{id}` returns the bytes
  (tenant-scoped, `nosniff`).
- **Feedback.** `PUT|DELETE /api/v1/conversations/{id}/messages/{messageId}/feedback`
  (`{rating: "up"|"down", comment?}`) rates an assistant message (`{runId}-m{n}`), one row per user.
  The thread GET adds `feedback: {messageId: {rating, comment, updated_at}}` for the caller. Feedback is
  private to its author and is not a run event. An attach or replay of the run sends the viewer's own
  current feedback, read from the feedback rows (the latest state after up, down, delete), as the custom
  event `pydanticos.message_feedback` `{message_id, rating: "up"|"down", comment}` just before the
  terminal frame, with no SSE id. Another user of the tenant, and an API key, receive none. A message
  with no feedback sends no event, and `DELETE` of absent feedback is a no-op (204).

### `MESSAGES_SNAPSHOT` and how `@ag-ui/client` merges it

The server sends `MESSAGES_SNAPSHOT` with the thread's durable history when you attach, when you
resume, after previous-turn healing, and when streamed text has to be corrected. Its order is the
server's order.

**`@ag-ui/client` 1.0.2 and 1.0.3 merge a snapshot by message id:** messages the client already
holds keep their position (with the snapshot's content), messages it holds that the snapshot does
not contain are removed, and new ones are appended. So after previous-turn healing (below), a
plain `HttpAgent`, and CopilotKit, list the missed answer **after** the user message you just
sent, although the server's thread has it before. Because held messages a snapshot lacks are
removed, every snapshot is the thread's **whole** history: an agent thread's earlier turns, and a
workflow thread's input user messages (see [Workflows](#workflows)).

To show the server's order, reload the history from
`GET .../threads/{threadId}` (Studio does), or replace your messages with the snapshot's yourself.
Compare messages as a set, not as a list, if you test against the server's thread.

## Event mapping

Events are standard AG-UI 1.0. In short:

| PydanticOS | AG-UI |
|---|---|
| Streamed model text | `TEXT_MESSAGE_START/CONTENT/END` |
| A tool call (built-in, MCP, frontend) | `TOOL_CALL_START/ARGS/END`; a server tool's result is `TOOL_CALL_RESULT` |
| Turn completed | `RUN_FINISHED{outcome: {type: "success", pendingToolCallIds?}, usage?}` (`usage`: one `TokenUsage` per answering model) |
| Structured output | `RUN_FINISHED.result` holds the validated object, plus one assistant text message with its JSON |
| An approval | `RUN_FINISHED{outcome: {type: "interrupt", interrupts: [...]}}` (see [Approvals](#approvals-interrupts)) |
| Run failed, or needs an operator | `RUN_ERROR{code, message}`; `code: "needs_attention"` when a side effect's outcome is unknown |
| Workflow steps | `STEP_STARTED/FINISHED`; progress as read-only `STATE_SNAPSHOT/DELTA` of `{status, steps: {id: {status, type, output?}}}` |

PydanticOS extras are `CUSTOM` events named `pydanticos.*` (`run`, `status` with `running` /
`recovering` / `waiting_approval`, `waiting`, `approval_required`, `approval_decided`, `model`,
`output_retry`, `processor_result`). Nothing a client needs exists only in a `CUSTOM` event; you can
ignore them. Cost and metrics are never pushed: read them from the native run API by the `run_id`
in `pydanticos.run`. Reasoning is not streamed.

## Resume and reconnect

Every SSE frame derived from a durable event carries an SSE id `id: {runId}:{seq}` (the AG-UI
`runId`, the durable sequence number). The `data:` payload is always a standard AG-UI event, so
clients that ignore ids lose nothing.

| Request | Answer |
|---|---|
| A new `runId` | `RUN_STARTED`, `CUSTOM pydanticos.run`, the turn's events, the terminal event |
| A known `runId`, the same body, no `Last-Event-ID` | The whole run again (attach from the start): `RUN_STARTED`, `pydanticos.run`, `MESSAGES_SNAPSHOT`, every event, then live |
| A known `runId`, the same body, `Last-Event-ID: {runId}:{N}` | A **continuation**: no `RUN_STARTED`; `pydanticos.run`, `MESSAGES_SNAPSHOT` through seq N (drop any partial text you hold), the events after N, then live. `{runId}:-1` means "I received only `RUN_STARTED`" |
| A known `runId`, a different body | 409 |
| An unknown `runId` with `Last-Event-ID` | 404 (except `{runId}:-1`, served as the new run, opened as a continuation) |
| A malformed `Last-Event-ID` (not `<runId>:<int>`, another run, below -1, past the run) | 400 `Last-Event-ID must be <runId>:<seq> for this run` |

**To resume after a cut**, re-POST the identical body with `Last-Event-ID` set to the last `id:`
you fully received, or GET the attach route with the same header (or `?after=<seq>`) after a page
reload, when the body is gone. Before splicing the continuation in, close locally any text message
or tool call you hold open.

The official `HttpAgent` drops `id:` lines, so it cannot resume. Studio's reconnecting client does:
[`apps/studio/src/lib/agui/resumableHttpAgent.ts`](../apps/studio/src/lib/agui/resumableHttpAgent.ts)
(with `sseFrames.ts`) extends `HttpAgent`. It tees the response to read ids, re-POSTs with
`Last-Event-ID` (5 attempts, 0.5 s → 8 s backoff with ±20% jitter, honours `Retry-After` on 429),
emits `RUN_ERROR{code: "stream.resume_failed"}` on a 4xx, never retries a user abort, and implements
`connect()` as the GET attach route. Copy it; it depends only on `@ag-ui/client` and `rxjs`.

### Clients that cannot resume: previous-turn healing

A plain client (CopilotKit, `HttpAgent`) that lost a stream just sends the next message. If the
previous turn is still running, the new POST answers `RUN_STARTED`, then
`CUSTOM pydanticos.waiting`, waits up to `EAP_AGUI_PREVIOUS_TURN_WAIT_S` (default 120 s) for that
turn to end, sends a `MESSAGES_SNAPSHOT` holding the missed answer, and then runs the new turn. If
the wait times out, the stream ends with `RUN_ERROR{code: "previous_turn_running"}`. Remember the
[snapshot merge](#messages_snapshot-and-how-ag-uiclient-merges-it): the missed answer is appended.

A recovering worker shows as `CUSTOM pydanticos.status {display_status: "recovering"}`, and a
`: keep-alive` comment goes out every 10 s while nothing else does.

## Frontend tools

Declare tools in `RunAgentInput.tools` (CopilotKit: `useFrontendTool`, `useHumanInTheLoop`). They
**never run on the server**:

1. The model calls your tool. You get `TOOL_CALL_START/ARGS/END`, and the run ends with
   `RUN_FINISHED{outcome: {type: "success", pendingToolCallIds: [...]}}`.
2. Your client runs the tool and sends its result as a `tool` message in the **next** run
   (`{role: "tool", toolCallId, content}`, or `error` for a failure). That run continues the turn.
   Its result is not re-emitted live; it appears in snapshots.

Rules (422 before the stream):

- names match `^[A-Za-z0-9_-]{1,64}$` and may not shadow a server tool of the agent, anything named
  `mcp__*`, or `retrieve`, `update_working_memory`, `final_result`;
- `parameters` is a JSON Schema (Draft 2020-12, no remote `$ref`);
- at most 32 tools, 64 KiB of schemas in total, 32 KiB per result;
- a `tool` message must answer a pending call of the thread (`client_tool_result_not_pending`).
  A pending call you leave unanswered gets a synthetic "not executed by the client" error;
- **a workflow takes no frontend tools** (422 `run_option_unsupported`). CopilotKit sends every
  unscoped tool to every agent, so scope tools to your agents (`agentId` on the hook) when a
  workflow shares the page.

The model's arguments are validated against your schema on the server; invalid ones go back to the
model (at most 2 retries). Tool results are untrusted tool output, never instructions.

## Approvals (interrupts)

An agent tool that needs approval, and a workflow approval step, end the AG-UI run with an
**interrupt**; the durable run keeps waiting:

```json
{"type": "RUN_FINISHED", "threadId": "…", "runId": "…",
 "outcome": {"type": "interrupt", "interrupts": [{
   "id": "approval:<run_id>:tool:<toolCallId>", "reason": "tool_call", "toolCallId": "<toolCallId>",
   "message": "Approve tickets__create({...})?", "responseSchema": {…}, "expiresAt": "…",
   "metadata": {"pydanticos": {"run_id": "…", "tool_name": "tickets__create"}}}]}}
```

A workflow step's interrupt is `{id: "approval:<run_id>:<step_id>", reason: "confirmation",
message, responseSchema, expiresAt}`. Before a tool-call interrupt you always get the call's
`TOOL_CALL_*` events, and several gated calls of one response come in one `RUN_FINISHED`.

**Answer** with a new run (new `runId`) on the same thread:

```json
{"threadId": "…", "runId": "<new>", "messages": [...],
 "resume": [{"interruptId": "approval:<run_id>:tool:<toolCallId>", "status": "resolved",
             "payload": {"approved": true}}]}
```

- `payload` is `{approved: boolean, reason?: string}` (Pydantic AI's schema); `reason` (≤ 500
  characters) is stored as the decision's comment. `status: "cancelled"` is a denial.
  `editedArgs` is 422 `edited_args_unsupported` (not supported in v1).
- Every open interrupt of the thread must be answered (`resume_missing_interrupt`), and a run that
  carries `resume` may not carry a new user message or tool results (`resume_with_input`).
- The decision is recorded once; the run streams `CUSTOM pydanticos.approval_decided`, then the
  same durable run continues (an approved tool runs once, on the server). A decision made
  meanwhile in Studio's Approvals inbox or by REST wins, and a late resume is still accepted.
- **CopilotKit's resume echo.** CopilotKit 1.83's `useInterrupt` also appends a `tool` message
  answering the gated call with the payload (`{toolCallId: <the call>, content: "{\"approved\":true}"}`).
  The server drops a tool message whose `toolCallId` is the call of a tool-call interrupt answered
  in the same request; the resume entry alone decides, and the body's fingerprint is the same with
  or without it. A `tool` message for any other call is read as usual.
- A client without interrupt support that just sends a new message while an approval is open gets
  the interrupt again, and nothing is recorded. It can be approved in Studio or by REST
  (`POST /api/v1/runs/{id}/tool-approvals/{toolCallId}/approve|deny`,
  `POST /api/v1/runs/{id}/steps/{stepId}/approve|reject`); the attach route then shows the
  continuation. `CUSTOM pydanticos.approval_required` announces each interrupt for such clients.

## Workflows

- **One workflow run per thread.** The first POST on a `threadId` starts the run; every later POST
  on that thread only answers its interrupts (or attaches). New input on a running or finished
  thread is 409 (`workflow_thread_running`, `workflow_thread_finished`): use a new `threadId` for
  each run.
- The input is `forwardedProps.pydanticos.input`, else the last user message parsed as JSON; it is
  validated against the workflow's input schema (422).
- The user messages the thread's first run carried (whether or not one of them was the input) are
  kept with the thread: every `MESSAGES_SNAPSHOT` of the thread and its thread GET start with them,
  once each, followed by the agent steps' messages. A thread started with
  `forwardedProps.pydanticos.input` alone has none.
- Steps stream as `STEP_STARTED/FINISHED`; an agent step's text and tool calls stream inside the
  step. Progress is read-only `STATE_*`; inbound `state` is ignored.
- A gated tool call inside an agent step is an interrupt naming the step's **child** run; answer it
  the same way.

## Limits and errors

| Limit | Value |
|---|---|
| Body | 2 MiB (`EAP_AGUI_MAX_BODY_BYTES`; 413 over it), nesting depth 64 |
| New user input per run | 32 KiB |
| Frontend tools | 32, schemas 64 KiB in total; each result 32 KiB |
| Resume entries | 64 |
| Concurrent runs | The platform's run limiter: 429 with `Retry-After` |

Errors before the stream are JSON: `{"detail": [{"loc": [...], "msg": "...", "type": "<code>"}]}`
for 422, `{"detail": "..."}` otherwise. A new thread on an archived agent is 409 `AGENT_ARCHIVED`
(a known `runId` still attaches). After the stream opened, a server-side failure is
`RUN_ERROR{code: "stream_interrupted"}`; the run continues, so resume.

## What AG-UI does not cover

AG-UI is for external frontends; PydanticOS's own surfaces stay on the native API. Only Studio's
agent Chat tab uses AG-UI, as a dogfood. These are **not** served over AG-UI: run details and
traces, the workflow run graph, the Approvals inbox (its REST routes work alongside interrupts),
eval runs and tests, tool test runs, knowledge-base ingest, memory, metrics and cost, and every
list and CRUD screen. Use the native REST API for them, with the native `run_id` from
`CUSTOM pydanticos.run`.

## CopilotKit

The example is [`examples/copilotkit-agui/`](../examples/copilotkit-agui/README.md). The minimum:

```sh
pnpm add @copilotkit/runtime@1.83.0 @copilotkit/react-core@1.83.0 @ag-ui/client@1.0.2 zod
```

Pin `@ag-ui/client` to **1.0.2**, the version `@copilotkit/runtime@1.83.0` depends on. Another
copy (1.0.3 or later) makes two `AbstractAgent` classes, and `agents: {...}` fails to typecheck
(`Type 'HttpAgent' is not assignable to type 'AbstractAgent'. Types have separate declarations of
a private property '_debug'`).

```ts
// app/api/copilotkit/route.ts: the key stays on the server.
import { HttpAgent } from "@ag-ui/client";
import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNextJSAppRouterEndpoint,
} from "@copilotkit/runtime";
import type { NextRequest } from "next/server";

const runtime = new CopilotRuntime({
  agents: {
    pydanticosAgent: new HttpAgent({
      url: "https://pydanticos.example.com/api/v1/ag-ui/agents/<agentId>",
      headers: { Authorization: `Bearer ${process.env.PYDANTICOS_API_KEY}` },
    }),
  },
});

export const POST = async (req: NextRequest) => {
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    serviceAdapter: new ExperimentalEmptyAdapter(),
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
```

In the page, with the v2 hooks (`@copilotkit/react-core/v2`):

```tsx
<CopilotKitProvider runtimeUrl="/api/copilotkit" useSingleEndpoint agentId="pydanticosAgent">
  <CopilotChat agentId="pydanticosAgent" />
</CopilotKitProvider>
```

- `useSingleEndpoint` matches `copilotRuntimeNextJSAppRouterEndpoint` (one POST route); without it
  the client first probes `GET /api/copilotkit/info` (404).
- `useFrontendTool` and `useHumanInTheLoop` declare frontend tools; give them `agentId` when a
  workflow is on the same page.
- `useInterrupt({agentId, render})` renders approvals: `resolve({approved: true})`,
  `resolve({approved: false, reason})`, or `cancel()`.
- Server tool calls (MCP, built-in) render with CopilotKit's tool card (`useDefaultRenderTool()`).
- The CopilotKit runtime's `HttpAgent` cannot resume a cut stream; previous-turn healing covers it.
- CopilotKit collects telemetry by default; set `COPILOTKIT_TELEMETRY_DISABLED=true` on the runtime
  (and `SCARF_ANALYTICS=false` when installing) to turn it off.

### CopilotKit limitation: history after a reload

CopilotKit 1.83 does not load a thread's history from PydanticOS. When the chat mounts on a
thread (a reload, a thread switch), it asks the runtime's **runner** to `connect`, and the default
`InMemoryAgentRunner` replays only the events this server process saw and still holds in memory.
A thread it does not hold comes back **empty**: after a restart, on another instance behind a load
balancer, or for a thread started elsewhere (Studio, another client). The server-side thread is
intact; the next message still continues it, because the server reads only the tail and keeps the
history itself.

**Workaround: a runner that falls back to the thread GET.** CopilotRuntime takes a `runner`.
Extend `InMemoryAgentRunner` (`@copilotkit/runtime/v2`) so that, when its own `connect` replays
nothing, it reads `GET /api/v1/ag-ui/{agents|workflows}/{id}/threads/{threadId}` with the key and
replays the thread as `RUN_STARTED`, `MESSAGES_SNAPSHOT` (the GET's `messages`) and
`RUN_FINISHED`:

```ts
import { EventType, type BaseEvent, type Message } from "@ag-ui/client";
import { type AgentRunnerConnectRequest, InMemoryAgentRunner, ɵresolveThreadOwner } from "@copilotkit/runtime/v2";
import { Observable } from "rxjs";

class PydanticOSHistoryRunner extends InMemoryAgentRunner {
  constructor(private readonly threadUrl: (agentId: string, threadId: string) => string | undefined,
              private readonly apiKey: string) {
    super();
  }

  override connect(request: AgentRunnerConnectRequest): Observable<BaseEvent> {
    const inMemory = super.connect(request);
    const url = request.agentId ? this.threadUrl(request.agentId, request.threadId) : undefined;
    if (!url) return inMemory;
    return new Observable<BaseEvent>((subscriber) => {
      let replayed = false;
      let closed = false;
      const inner = inMemory.subscribe({
        next: (event) => { replayed = true; subscriber.next(event); },
        error: (error) => subscriber.error(error),
        complete: () => {
          if (replayed) return subscriber.complete();
          this.history(request, url)
            .then((messages) => {
              if (closed) return;
              if (messages.length > 0) {
                const runId = `history-${request.threadId}`;
                subscriber.next({ type: EventType.RUN_STARTED, threadId: request.threadId, runId } as BaseEvent);
                subscriber.next({ type: EventType.MESSAGES_SNAPSHOT, messages } as BaseEvent);
                subscriber.next({ type: EventType.RUN_FINISHED, threadId: request.threadId, runId } as BaseEvent);
              }
              subscriber.complete();
            })
            // API down, a non-JSON body: connect empty, never an unhandled rejection.
            .catch(() => { if (!closed) subscriber.complete(); });
        },
      });
      return () => { closed = true; inner.unsubscribe(); };
    });
  }

  private async history(request: AgentRunnerConnectRequest, url: string): Promise<Message[]> {
    const owner = ɵresolveThreadOwner(request.ownerId);
    // Another owner holds this thread here: CopilotKit answered "unknown", so must the fallback.
    if (!this.isThreadAccessible(request.threadId, { ownerId: owner })) return [];
    // With identifyUser, the key cannot tell users apart: check ownership in your own records.
    if (owner !== "") return [];
    const response = await fetch(url, { headers: { Authorization: `Bearer ${this.apiKey}` } });
    return response.ok ? ((await response.json()).messages ?? []) : [];
  }
}

const runtime = new CopilotRuntime({
  agents: { pydanticosAgent: new HttpAgent({ url: AGENT_URL, headers: { Authorization: `Bearer ${KEY}` } }) },
  runner: new PydanticOSHistoryRunner(
    (agentId, threadId) => agentId === "pydanticosAgent" ? `${AGENT_URL}/threads/${encodeURIComponent(threadId)}` : undefined,
    KEY,
  ),
});
```

**Thread owners.** The API key is the tenant's, so PydanticOS serves the thread GET to whoever
holds it: it cannot tell CopilotKit's users apart. CopilotKit scopes its threads by `ownerId`
(the anonymous owner `""` without `identifyUser`, else the identified user's id), and the
fallback must keep that scope:

- never load a thread that this process holds for another owner (`isThreadAccessible`);
- with `identifyUser`, load only after checking, in your own records, that the caller owns the
  thread id (the example takes a `canRead(ownerId, target, threadId)` callback; without one,
  identified callers get no fallback), or give each user their own thread-id namespace;
- without `identifyUser`, a thread id is a capability, as in CopilotKit itself: whoever knows it
  may open it.

The example ships this as `examples/copilotkit-agui/lib/historyRunner.ts` and keeps the thread
id in the page URL, so a reload reopens the same thread with its history. The restored history is
the server's (D13 ids), so the next run's snapshots merge cleanly. It does not reattach to a run
still in progress after a restart: the next message waits for it (previous-turn healing), or a
client can GET the attach route itself.

Studio's agent and workflow pages have a **Connect (AG-UI)** sheet with this snippet filled in
with the endpoint URL; it never shows the key.

## Plain `@ag-ui/client`

```ts
import { HttpAgent } from "@ag-ui/client";

const agent = new HttpAgent({
  url: "https://pydanticos.example.com/api/v1/ag-ui/agents/<agentId>",
  headers: { Authorization: `Bearer ${process.env.PYDANTICOS_API_KEY}` },
  threadId: "support-42",
});
agent.addMessage({ id: crypto.randomUUID(), role: "user", content: "Where is order 42?" });
const { result, newMessages } = await agent.runAgent({ runId: crypto.randomUUID() });
```

Or with curl:

```sh
curl -N https://pydanticos.example.com/api/v1/ag-ui/agents/<agentId> \
  -H "Authorization: Bearer $PYDANTICOS_API_KEY" -H "Content-Type: application/json" \
  -d '{"threadId":"t1","runId":"r1","messages":[{"id":"u1","role":"user","content":"hi"}],
       "tools":[],"context":[],"state":{},"forwardedProps":{}}'
```

`tests/ag-ui-smoke/` runs every flow in this guide with `@ag-ui/client` 1.0.3 against a real API
and worker.
