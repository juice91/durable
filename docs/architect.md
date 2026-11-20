# The Architect

The Architect is the built-in agent that turns a plain-language request into an agent or a workflow draft, and, when
the person approves, puts it live. This page covers what it can operate. For the tools it writes definitions with, see
the Architect design in `docs/superpowers/specs/2026-11-17-architect-design.md`.

## What the Architect can operate

The Architect has three groups of tools.

| Group | Tool names | Approval |
| --- | --- | --- |
| Definitions it writes | `architect__*` (list, save a draft, publish, restore, archive, schedule) | Publishing, restoring, archiving and schedule changes ask the person. Drafts do not. |
| What exists | `platform__*` (knowledge bases, sources, syncs, every workflow and agent, schedules, recent runs) | None: read-only. |
| The platform's control plane | `platform__list_capabilities`, then `control__<name>` | Reads run at once. Anything that changes something asks the person. |

### It discovers the control plane

The control plane is the same one external agents drive over MCP ([Controlling PydanticOS through MCP](mcp-control.md)):
test suites, cases, scorers and eval runs, schedules, knowledge base sources and syncs, MCP servers, connections and
more. The Architect is not given a hand-picked subset. When a request is not covered by its other tools it calls
`platform__list_capabilities` (optionally with a `query`, for example `eval` or `schedule`), which lists every control
tool: its name, a one-line description, whether it is a `read` or a `manage` tool and whether a call asks for approval.
It then calls the one that fits as `control__` plus the tool's name with dots as underscores, so `eval.suite.create` is
`control__eval_suite_create`. The arguments are the control tool's own, with the same types. Only when nothing in the list
covers a request does it say that it needs an engineer, and it names the missing capability.

A new control tool therefore reaches the Architect with no change to it: the list is read from the control plane's own
registry each time.

### Actions that change something ask for approval

A `manage` tool is shown to the person as an approval card before it runs, exactly like publishing a workflow. The card
leads with a sentence saying what the call does (the tool's description and the arguments, in plain words), followed by
the arguments as a block you can copy. Nothing is sent until the person approves; a denial reaches the Architect as the
tool result. Read tools run without a card.

Two control tools are never available to the Architect, because they would let it approve its own cards or drive
itself: `approvals.decide` and `agent.chat`.

### How it is wired (for operators)

The control tools run in the API process, the Architect's tools in the worker. The worker calls the API's MCP endpoint
(`EAP_API_INTERNAL_URL`, `http://api:8000` in the manifests) with a short-lived run token: signed with a key derived from
`EAP_SECRETS_KEY`, naming the tenant and the run, valid for two minutes and minted for each call. The API accepts it only
while that run is a live run of the built-in Architect. The audit record of every call names the run
(`architect-run:<run id>`), and its scope is the tenant of the run; no API key is stored anywhere. See
[Limits and audit](mcp-control.md#limits-and-audit).
