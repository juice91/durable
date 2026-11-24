# Agents as tools (supervisor pattern)

An agent can list other **published agents** of its tenant in `agent_tools`. The model then sees one
tool per entry and can hand a task to that agent, the way a supervisor hands work to a specialist.

```json
{
  "name": "Support lead",
  "model": {"provider": "litellm", "name": "qwen3.8"},
  "instructions": "Answer billing questions yourself. Ask the researcher for anything that needs sources.",
  "agent_tools": [
    {"agent_id": "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab"},
    {"agent_id": "7a1d6f6f-7e1b-4f1f-8e2f-1234567890bc", "name": "ask_billing", "version": 4,
     "timeout_seconds": 60, "max_calls_per_run": 2, "description": "Billing rules and refund policy."}
  ]
}
```

| Field | Default | Meaning |
|---|---|---|
| `agent_id` | required | A published, non-archived agent of the same tenant. One entry per agent, at most 8. |
| `name` | derived | The tool name the model sees: `^[a-z][a-z0-9_]{0,40}$`. Default: the target agent's name in lower case with runs of other characters turned into `_` (`Research Helper` → `research_helper`). It may not equal a built-in, MCP or platform tool name. |
| `description` | derived | What the model is told the tool is for. Default: "Delegate a task to the agent '<name>'", plus the first 300 characters of the target's instructions. Agents have no description field of their own, so set one when the instructions do not say what the agent is good for. |
| `version` | `latest` | `latest` is resolved **once per call**, inside a recorded step, to the newest published version; an integer pins that version. |
| `timeout_seconds` | 300 | 1 to 3600. After this long the child is cancelled and the model reads an error. |
| `max_calls_per_run` | 5 | 1 to 20 calls of this tool in one run. |

Defaults are left out of the stored spec, so a spec that never used the field keeps its exact JSON and
`spec_hash`.

## What the model sees

`<name>(input: str) -> {"output": ..., "run_id": str}`. `output` is the child's final answer: its text, or, for
an agent with a structured output (`output.json_schema`), the validated **object itself** as JSON (never a Python
repr). The answer is bounded at 32 KiB: a longer text, or an object whose JSON is longer, is cut (an object becomes
its JSON text) with a `[output truncated: N bytes omitted]` marker. `run_id` is the child run. Nothing else crosses: the child gets the `input` string and none of the
caller's request context, conversation, attachments or model override.

A call that cannot give an answer is a **tool error** the model can read, never a run failure:

| Code | When |
|---|---|
| `agent_tool_max_calls` | the run already made `max_calls_per_run` calls of this tool |
| `agent_tool_depth_exceeded` | the call would nest deeper than `EAP_AGENT_TOOL_MAX_DEPTH` (default 3) |
| `agent_tool_timeout` | the child did not finish in `timeout_seconds`; it was cancelled |
| `agent_tool_failed` | the child run failed (its own error code is in the message) |
| `agent_tool_unknown_agent`, `agent_tool_agent_archived`, `agent_tool_not_published`, `agent_tool_version_not_found` | the target changed after publish |

## How it runs, and why it is durable

A call is **not** run in-process. It starts a **child run** exactly like a workflow's agent step does: a
`runs` row with `parent_run_id` = the caller's run and `parent_step_id` = the tool call id, and a child
DBOS workflow `eap.agent_run` whose workflow id is the child run id,
`uuid5("{run_id}:agent_tool:{tool_call_id}")`. The caller waits for the result inside its own workflow.

- The child is its own run: its own events, trace, usage, cost, approvals and recovery. It runs **its own
  published spec** (its tools, processors, approvals, model allowlist).
- A worker kill while the child runs resumes both workflows. The child id is deterministic and DBOS records
  the start, so the child is never started twice, and the child's side effects keep their own once-only guards.
  A kill after the child finished and before the caller recorded the result replays the recorded result: one
  child run, one `agent_tool_completed`, one `tool_result`.
- `latest` is resolved once, in the `eap.agent_tool_resolve` step that also creates the child row; a replay
  reads the recorded version, and a re-executed step reloads the version the child row names.
- An approval inside the child makes the child wait, and so the caller waits, bounded by `timeout_seconds`.
  The timeout cancels the child (its run ends `failed` with `run_cancelled`, its open approvals are closed).
  A child of an eval run, or of a workflow loop body, never waits for a person, as for any agent run there.

## Guards

- **Publish** (and restore) refuses, with a stable `code`: `agent_tool_unknown_agent` (also another tenant's
  agent), `agent_tool_agent_archived`, `agent_tool_not_published`, `agent_tool_version_not_found`,
  `agent_tool_name_conflict` and `agent_tool_cycle`.
- **Cycles.** Publish walks the published graph: a `latest` ref follows the target's newest version, a pinned ref
  that version. Reaching the agent being published through a `latest` ref is a cycle (`A → B → A`). Because `latest`
  moves after publish, a cycle can still appear later (a target publishes a version that calls back), so the
  **depth cap** (`EAP_AGENT_TOOL_MAX_DEPTH`, default 3) bounds every chain at run time. A run's depth is the number
  of agent runs above it linked by agent-tool calls.
- **Calls.** `max_calls_per_run` counts the run's child rows for that agent; `timeout_seconds` is a DBOS workflow
  timeout on the child plus a bound on the caller's own wait.
- Archiving a target does not check who calls it: its callers get `agent_tool_agent_archived` at call time.

## Observability

The caller's event log gets `agent_tool_started` (`child_run_id`, version, depth) and `agent_tool_completed`
(`child_run_id`, status `completed | failed | timeout | refused`, error code; `truncated` when the output was cut),
keys `agent_tool:{call}:started|completed`. Neither carries the input or output text (those are in `tool_call` and
`tool_result`).

In the trace the child run nests under the tool call that started it (`tool_call` span, `agent_tool: true`,
`child_run_id`); a child that calls agents nests further. The trace totals sum every descendant, so the parent's run
detail shows the cost and tokens of the whole tree; usage and cost stay on each child run. Studio's Events tab shows
each call with an "Open child run trace" link.

## Studio

Agent editor → **Tools** step → **Agents as tools** section: pick published agents (the agent being edited is not offered), set the tool name and description, choose `latest` or a
pinned version, the timeout and the call limit. Publish refusals (cycle, archived target, name conflict) open the Tools step.

## When to use a workflow instead

Use agent tools when the **model should decide** whether, when and how often to ask a specialist. Use a
[workflow](../workflows/) when the steps are known: it is cheaper (no supervisor tokens), its path is visible and
testable, it has loops, branches, parallel steps and approval steps of its own, and every run takes the same path.
Agent tools add a model decision at each hop and a nesting depth; keep chains short.

## Not (yet) covered

- The Architect's restricted agent format has no `agent_tools` field: refs to other agents need row ids, which the
  Architect never sees. Add them in the editor or through `agent.save_draft`.
- AG-UI clients see an agent call as an ordinary tool call; the two `agent_tool_*` events are silent there.
