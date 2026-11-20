# Studio

Studio (`apps/studio`, Next.js 15, React 19) is the platform's web UI: build and chat with agents,
browse workflows, tools, MCP servers and processors, and inspect metrics, traces and logs. It is a
client of the API and holds no data of its own beyond a few browser preferences (see
[What is stored in this browser](#what-is-stored-in-this-browser)).

The API base URL comes from `NEXT_PUBLIC_EAP_API_URL` (default `http://localhost:8000`); requests
send `NEXT_PUBLIC_EAP_DEV_TOKEN` as the `Authorization` header (`src/lib/api.ts`).

## Running and checking

```sh
pnpm -C apps/studio dev                 # dev server (next dev)
pnpm -C apps/studio exec vitest run     # unit and component tests
pnpm -C apps/studio lint                # next lint
pnpm -C apps/studio exec tsc --noEmit   # type check
```

## Studio

### Route map

Every internal URL is built by `src/lib/routes/*` (`routes.trace(id)`, `routes.agentChat(id, …)`);
components never write paths by hand. Query parameters below are part of the URL contract.

| URL | Page |
|---|---|
| `/agents` | Agents list (List or Compact view). Model and Tools show the latest published version (a draft-only agent shows its draft model, tagged "draft"); 「草稿未發佈」 marks a draft that differs from it. |
| `/agents/new` | Create an agent. |
| `/agents/:id` | Redirects to the agent's Chat tab. |
| `/agents/:id/chat` | Agent page, Chat tab, new conversation. |
| `/agents/:id/chat/:conversationId` | Agent page, Chat tab, one conversation. |
| `…/chat[/:conversationId]?version=&model=&context=` | `version`: a version number or `draft` (absent: latest). `model`: model override. `context=preset` applies the newest saved request-context preset; `context=open` opens the request-context popover. `context` is read once and removed from the URL. |
| `/agents/:id/traces` | Agent page, Traces tab (the agent's runs). Hosts the trace Sheet: `?trace=<runId>&tab=events\|logs`. |
| `/agents/:id/editor?step=&compare=` | Agent page, Editor tab. `step`: `identity`, `instructions`, `tools`, `request-context` or `processors`. `compare=a..b`: a version compare, each side a version number or `draft` (`3..draft`, `1..3`). |
| `/agents/:id/memory[?resource=<user id>]` | Agent page, Memory tab (only for an agent with memory; otherwise it opens Chat): the users with memory or conversations, one user's working memory (view, edit, clear) and Forget user. |
| `/workflows` | Workflows list. |
| `/workflows/:id[?tab=runs]` | Workflow page: Graph tab (default), or Runs with `tab=runs`. |
| `/workflows/:id/traces` | Workflow page, Traces tab. |
| `/workflows/:id/runs/:runId` | One workflow run. |
| `/tools[?tool=<name>&server=<mcp server>&tab=run]` | Tools page; `tool` opens that tool's Sheet (`server` for an MCP tool; `tab=run` opens the Run tab, Overview is the default). |
| `/mcp` | MCP servers list. |
| `/mcp/:id` | One MCP server (connection, tools, used by, delete). |
| `/processors` | Processor catalog. |
| `/processors/:type` | One processor type (overview, test console, recent outcomes, used by). |
| `/knowledge` | Knowledge bases list. |
| `/knowledge/:id[?tab=search\|runs][&doc=<document key>]` | One knowledge base: Documents tab (default), Search (`tab=search`, the search tester) or Ingest runs (`tab=runs`). `doc` opens that document's Sheet. Hosts the trace Sheet with `traceTab` as its tab parameter. |
| `/metrics` | Metrics, with the Overview at the top. Parameters: `range` (default `7d`, left out), `from`/`to` (with `range=custom`), `interval`, `agent_id`, `model`, `provider`, `kind`. |
| `/threads[?agent_id=<agent>&q=<title search>]` | Threads: the conversations of every agent, most recently active first. A row opens the thread in the agent's Chat tab. |
| `/traces` | Traces list. Filters: `status` (comma-separated), `from`, `to`, `model`, `provider`, `agent_id`, `kind`. |
| `/traces?trace=<runId>[&tab=events\|logs]` | The trace Sheet over the Traces list. `spans` is the default tab and is left out. `routes.run(id)` is the Sheet on `tab=events`. |
| `/logs` | Logs. Filters: `q`, `level`, `agent_id`, `run_id`, and `from`/`to` or `range`. Hosts the trace Sheet (`?trace=&tab=`). |
| `/settings` | Settings, in tabs chosen by `?tab=` (appearance, chat, shortcuts, providers, secrets, connections, api-keys, browser-data, about); `/settings/providers` redirects to `?tab=providers`. |

The trace Sheet opens on any page that hosts it, with `?trace=<runId>`. Its tab parameter is `tab`
everywhere except on workflow pages (`/workflows/:id`, `/workflows/:id/traces`,
`/workflows/:id/runs/:runId`) and knowledge base pages (`/knowledge/:id`), where `tab` is the page's
own, so the Sheet uses `traceTab` instead.

#### Retired URLs

Old URLs redirect on the server (`LEGACY_RULES` in `src/lib/routes/legacy.ts`, including
`TRACE_LEGACY_RULES` from `src/lib/routes/observability.ts`). First matching row wins.

| Old URL | Goes to | Since |
|---|---|---|
| `/agents/:id` (was the editor) | `/agents/:id/chat` | SL.6 |
| `/playground?agent=<id>[&conversation=&version=&model=&context=]` | `/agents/<id>/chat[/<conversation>]?version=&model=&context=` (a `version` that is not `draft` or a positive integer is dropped) | SL.6 |
| `/playground` without `agent` | Client redirect (`PlaygroundRedirect`): with `conversation`, that conversation's agent's chat; otherwise the last-used agent's chat, else the first agent's, else `/agents`. `version`, `model` and `context` are carried. | SL.6 |
| `/?range=&from=&to=` (the old dashboard) | `/metrics` with the same `range`/`from`/`to` (other parameters dropped) | SL.7 |
| `/` (no dashboard parameters) | `/agents` | SL.7 |
| `/request-context?agent=<id>[&version=]` | `/agents/<id>/chat?version=&context=open` (chat with the request-context popover open) | SL.10 |
| `/request-context` without `agent` | `/agents` | SL.10 |
| `/settings/providers` | `/settings?tab=providers` (Providers is a tab of Settings) | ST.1 |
| `/traces/:runId` | `/traces?trace=<runId>` | SL.12 |
| `/runs/:id` | `/traces?trace=<id>&tab=events` | SL.12 |

An id that cannot be a path segment (`.` or `..`) sends an agent URL to `/agents` instead. Redirect
targets are always same-origin paths.

### Keyboard shortcuts

Press `?` in Studio for the same table (also under Settings). Single-key shortcuts are ignored
while focus is in a text field and while a dialog or menu is open; `g` sequences wait one second
for the second key.

| Keys | Action |
|---|---|
| `⌘K` (macOS) / `Ctrl+K` | Open the command palette |
| `?` | Show keyboard shortcuts |
| `[` | Collapse or expand the sidebar (wide windows) |
| `/` | Focus the list search |
| `c` | Create (the page's primary action) |
| `↑` `↓` (also `Home` `End`), then `Enter` | Move through list rows, then open one |
| `g` then `r` | Go to Architect |
| `g` then `i` | Go to Approvals |
| `g` then `a` | Go to Agents |
| `g` then `w` | Go to Workflows |
| `g` then `o` | Go to Tools |
| `g` then `m` | Go to MCP Servers |
| `g` then `p` | Go to Processors |
| `g` then `k` | Go to Knowledge |
| `g` then `d` | Go to Test suites |
| `g` then `s` | Go to Scorers |
| `g` then `e` | Go to Experiments |
| `g` then `n` | Go to Metrics |
| `g` then `h` | Go to Threads |
| `g` then `t` | Go to Traces |
| `g` then `l` | Go to Logs |
| `g` then `,` | Go to Settings |
| `]` | Agent page: show or hide the Config panel |
| `{` | Agent page, Chat tab: show or hide the conversations pane |

On macOS only `⌘K` opens the palette (`Ctrl+K` keeps its text-field meaning). The g-keys come from
the nav registry (`src/lib/nav.ts`); the bindings and help table from `src/lib/shortcuts.ts`.

### What is stored in this browser

Studio keeps a few preferences in this browser's `localStorage`. Nothing is encrypted. Settings → Browser data
(`/settings?tab=browser-data`, "Data in this browser") lists them with a count and can clear them ("layout" and "presets" groups;
the theme is changed under Appearance). The list is `BROWSER_DATA` in
`src/components/settings/browserData.ts`; a new key is added there.

| What | Keys | Holds | Cleared by |
|---|---|---|---|
| Theme | `studio.theme` | System, Light or Dark. | Appearance (not cleared) |
| Sidebar | `studio.sidebar` | Whether the sidebar is collapsed to icons. | Layout |
| Side panel width | `studio.sidepanel.v1` | The width of the right-hand panel. | Layout |
| Agent page layout | `studio.agent-layout.v1.<agentId>…`, `studio.agent-config.v1.<agentId>` | Pane widths on each agent's page, and whether its Config panel was left open or closed. | Layout |
| Agents list view | `studio.agents.view` | List or Compact. | Layout |
| Agents system rows | `studio.agents.show-system` | Whether the Agents list also shows the Architect's system helpers (the drafters). | Layout |
| Architect list | `studio.architect.show-archived` | Whether the Architect page's created list shows archived agents and workflows. | Layout |
| Skipped runs | `studio.runs.show-skipped` | Whether the Traces list and a workflow's run history also list skipped runs (a "nothing to do" exit); hidden when unset. | Layout |
| Steps not reached | `studio.workflow.not-reached-open` | Whether a workflow run's step list shows the steps the run never reached (folded into one row unless opened). | Layout |
| Workflow page layout | `studio.workflow-layout.v1` | The workflow panel's width and whether it is collapsed, and whether the run Timeline is open and its height. | Layout |
| Trace list columns | `studio.traces.hiddenColumns` | Which columns the Traces list shows. | Layout |
| Memory card | `studio.memory-card.v1` | Whether the chat's memory card is expanded into the memory inspector. | Layout |
| Chat protocol | `studio.chat.protocol` | AG-UI or Native, for the agent Chat tab (Settings → Chat; AG-UI when unset). | Layout |
| Last agent | `studio.lastAgent` | The agent the playground reopens. | Layout |
| Request-context presets | `studio.requestContextPresets.v1.<credential scope>:<agentId>`, `eap.requestContext.v1:…` | Named request-context presets for each agent (and the single saved value they were migrated from). | Presets |

The agent layout prefix also covers `studio.agent-layout.v1.<agentId>:threads-px` (the conversations
pane width last dragged to).

Not listed in Settings: per-tab `sessionStorage`, gone when the tab closes — the chat's remembered
request context (`eap.playground.context.v1:<credential scope>:<agentId>`), the chat's "Act as user"
id (`eap.studio.actAs.v1:<credential scope>:<agentId>`) and the ledgers of tool
and workflow runs started in this tab (`eap.studio.toolRuns.v1`, `eap.studio.workflowRuns.v1`).
