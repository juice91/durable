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

## The system context and provenance

At the start of every run the built-in Architect gets a bounded block of generated platform facts after its
instructions: the manifest's Platform layer, the capability lines (every trigger and step kind, then tools and models
until the budget), the known gaps, the tenant's resource **names** and counts (secrets by name only, never a value), and
operational facts (the always-gated tools, recent failures, runtime). It is built once by the recorded step
`eap.load_system_context`, so a run recovered after a crash renders the same bytes even if the registry changed; the block
is never stored in `runs.messages`. The step runs for the built-in Architect only; every other agent's steps and
instructions are unchanged. A layer that cannot be built is left out and the block says so ("do not claim any
capability is present"); the `system_context_loaded` event lists the degraded layers and holds hashes, never the text.
Budgets per layer are in the manifest's `context_budget`. `GET /api/v1/system-context` and the MCP tool `system.context`
return the same layers for the caller; the tenant layer is filtered through the IAM policy engine (everything today).

`architect.list_capabilities` and `architect.get_capability` answer `snapshot_stale: true` (with the new hash) when the
registry changed since the run started.

Every draft the Architect saves, and every publish card, gets an append-only row in `definition_provenance`: the platform
version, the manifest and capability-snapshot hashes, the principal summary, the names (with argument hashes, never
arguments) of the tool calls of the run, and the model. The platform records it from the run's own events
(`provenance.record`, callable only by the Architect's run and hidden from its model); it is read with
`GET /api/v1/definitions/{agent|workflow}/{id}/provenance` or `provenance.get`. Studio shows a "Based on" footer on the draft
and the publish card, with "N capabilities changed since this was drafted" when a capability the draft uses changed status.
The capability snapshots are stored content-addressed (`capability_snapshots`: ids and statuses only).

## The capability registry

What the platform can do is one registry, not prose. `GET /api/v1/capabilities` (`?kind=&status=&q=&include_schemas=`,
`ETag` / `If-None-Match`) and `GET /api/v1/capabilities/{id}` list tools, workflow step kinds, triggers, tool options
(a `text.check` rule, an `http.request` reducer), agents as tools, features, MCP control tools and, per tenant, MCP
servers, their tools and the model providers. The MCP tools `capability.list` and `capability.get` read the same
service, and the Architect's `architect.list_capabilities` and `architect.get_capability` call it through the control
bridge.

- `status`: `available`, `designed_not_built` (designed, not in the code) or `disabled` (built, off now: a provider without
  credentials, a tool outside `EAP_ALLOWED_TOOLS`, a server that does not answer; `status_reason` says why).
- `writable_by_architect: false` on an `available` entry (`step.foreach`) means the Architect's authoring format cannot
  write it: a person builds it in the editor.
- `known_gaps` is generated from `eap_schemas/capability_gaps.py`, where the PR that ships a capability flips the
  declaration. `designed_in` is a docs path that exists on `main`, or `pr:<number>` while the design is only a pull request.
- The declared inventory comes from the code (`eap_schemas.capabilities.collect_declared`); a probe can change a status
  or add a tenant's MCP server or model, never a built-in entry. Who sees which entry is a pluggable policy (today only the
  control tools a person alone may use are hidden from the Architect's own run).
- The seed's rule: availability comes only from these tools. `architect.list_approved_tools` stays the grantable subset and
  never offers a tool the registry does not mark `available`.

