# Approvals

A run can stop and wait for a person. The wait is stored in Postgres, so it survives restarts and deploys, and it can
last days. Approve, reject and timeout compete for one decision: exactly one of them wins.

## In a workflow

An `approval` step shows a prompt and continues only when someone approves. An `input` step asks for data that must match
a JSON Schema; the submitted object becomes the step's output.

```json
{ "id": "review", "type": "approval", "prompt": "Publish this note?", "timeout_hours": 72, "next": "publish" }
```

`timeout_hours` is from 1 to 168. A rejected or expired approval ends the run as failed.

## On an agent's tools

List the tools that need a decision in the agent's `approvals`:

```json
"approvals": { "tools": ["http.request"], "timeout_hours": 24 }
```

MCP tools are written as `server/tool`. The default timeout is 24 hours. The run waits before the call; if the call is
denied, the model is told, with the approver's comment when there is one.

Some actions always need a person, for example publishing something the Architect drafted.

## Deciding

- **Studio:** the **Approvals** inbox lists what is waiting, and the run page shows the same card.
- **API:** `GET /api/v1/approvals`, then
  `POST /api/v1/workflows/runs/{run_id}/steps/{step_id}/approve` or `/reject` for a workflow step, and
  `POST /api/v1/runs/{run_id}/tool-approvals/{tool_call_id}/approve` or `/deny` for a tool call.
- **MCP:** a client of the [control endpoint](../mcp-control.md) can list and decide approvals with a key that allows
  changes.
