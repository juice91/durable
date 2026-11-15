# CopilotKit example (AG-UI)

A Next.js app with a CopilotKit chat for a published PydanticOS agent or workflow. It talks AG-UI
through CopilotKit's runtime, and the API key stays on the server. The protocol, with every rule
this example relies on, is in [`docs/ag-ui.md`](../../docs/ag-ui.md).

It shows:

| Feature | Where |
|---|---|
| Chat with a published agent, through `HttpAgent` and a tenant API key | `app/api/copilotkit/route.ts` |
| History restored from PydanticOS after a reload or a restart | `lib/historyRunner.ts` (the runtime's `runner`) |
| Server tool calls (MCP, built-in) with CopilotKit's tool card | `useDefaultRenderTool()` in `app/chat.tsx` |
| A frontend tool that runs in the browser | `useFrontendTool` (`set_theme_color`) |
| A human-in-the-loop tool: the agent asks, the user answers | `useHumanInTheLoop` (`confirm_with_user`) |
| Approvals of gated tools and workflow approval steps (AG-UI interrupts) | `useInterrupt` |
| A workflow's read-only progress (`STATE_SNAPSHOT` / `STATE_DELTA`) | `WorkflowState` |

Neither client tool ever runs on the server. PydanticOS ends the turn with the call pending, and
CopilotKit sends the result in the next AG-UI run, which continues the turn.

## Versions

Exact pins: `@copilotkit/runtime@1.83.0`, `@copilotkit/react-core@1.83.0`, `@ag-ui/client@1.0.2`,
`next@15.5.29`, `react@19.3.1`. `@ag-ui/client` must be **1.0.2**, the copy the runtime depends
on. Another copy makes two `AbstractAgent` classes, and the runtime's `agents` map fails to
typecheck. The example has its own pnpm workspace root and lockfile; it is not part of the repo's
workspace.

## Try it on a throwaway stack (no LLM)

```sh
bash examples/copilotkit-agui/scripts/demo-stack.sh
```

This brings up Postgres, the API, a worker and an MCP server on their own ports
(`DEMO_*_PORT`: 54359, 8210, 8219, 3110). It never uses the dev stack. It seeds a demo tenant with
the scripted `test:*` models (`scripts/seed.mjs --scripted`), writes `.env.local`, and serves the
example at http://127.0.0.1:3110. Ctrl-C removes everything, including the database and
`.env.local`.

The scripted models take JSON prompts. Pick the agent in the header, then type:

| Agent | Prompt | What happens |
|---|---|---|
| `pydanticosAgent` | `{"tool": "lookup_order", "args": {"order_id": "42"}}` | The MCP tool runs on the server; its card shows the result |
| `browserTools` | `{"tool": "set_theme_color", "args": {"color": "teal"}}` | The page's accent turns teal; the agent answers `tool said: …` |
| `browserTools` | `{"tool": "confirm_with_user", "args": {"question": "Ship it?"}}` | Yes/No buttons; the answer continues the turn |
| `gatedTickets` | `{"tool": "tickets.create", "args": {"title": "Printer", "body": "On fire"}}` | An approval card; Approve runs the tool once on the server |
| `pydanticosWorkflow` | `{"q": "refund order 42"}` | The agent step, then an approval card; the side panel shows the steps |
| `pydanticosAgent` | `{"tool": "slow_lookup", "args": {"order_id": "7", "seconds": 8}}`, then reload during the call and send another message | The missed answer appears, then the new turn runs (previous-turn healing) |

The scripted `test:mcp` model answers from the first tool result in the conversation, so use a new
thread per MCP prompt.

## Use it with your own agent

1. Publish an agent in Studio, and create an API key in Settings → API keys. Keys can only be
   created through a dev-mode API until enterprise auth exists (see the guide).
2. `cp .env.example .env.local` and fill in `PYDANTICOS_API_URL`, `PYDANTICOS_API_KEY` and
   `PYDANTICOS_AGENT_ID` (optionally `PYDANTICOS_WORKFLOW_ID`, `PYDANTICOS_AGENTS`).
3. `SCARF_ANALYTICS=false pnpm install --frozen-lockfile`, then `pnpm dev` (port 3110).

`scripts/seed.mjs <api-origin> <mcp-url>` with `PYDANTICOS_SEED_MODEL=<provider:name>` seeds the
same agents on a dev-mode API with a real model.

## Behaviour worth knowing

- **One workflow run per thread.** A workflow thread is one run; use "New thread" for the next
  one. Client tools are scoped to agents (`agentId` on the hooks), because CopilotKit sends every
  unscoped tool to every agent, and a workflow run refuses client tools.
- **Resume.** CopilotKit's `HttpAgent` cannot resume a cut stream. After a lost connection, the
  next message heals the thread: the server waits for the previous turn, sends the missed answer
  in a `MESSAGES_SNAPSHOT`, then runs the new turn.
- **Snapshot order.** `@ag-ui/client` 1.0.2 merges a snapshot by message id. The missed answer is
  appended after your new message, and messages the snapshot lacks are dropped. PydanticOS's
  snapshots carry the whole thread, a workflow's input message included, so nothing you sent
  disappears.
- **Reload.** The thread id stays in the URL (`?thread=`). CopilotKit by itself restores a thread
  only from its runtime's in-memory runner, which loses it on a restart and never holds threads
  started elsewhere. `lib/historyRunner.ts` falls back to PydanticOS's thread GET, so a reload,
  a restart or a thread started by another client opens with its history (see the guide's
  "CopilotKit limitation: history after a reload"). The fallback keeps CopilotKit's thread
  owners: it never loads a thread this process holds for another owner, and with
  `identifyUser` it loads only when the runner's `canRead` callback confirms the caller owns the
  thread (this example sets no `identifyUser`, so a thread id is the capability).
- **Approvals.** `useInterrupt` resolves a tool-call interrupt with `resume[]` and also adds a
  `tool` message holding the payload. PydanticOS drops that echo, and the resume entry alone
  decides.
