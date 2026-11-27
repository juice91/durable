# Workflows

A workflow is a published graph of typed steps that runs durably. Use one when each run should behave the same way:
read a feed, ask an agent to choose, check the result, ask a person, post it. A workflow is a JSON document, so it can be
reviewed, versioned, compared and drafted by the [Architect](../architect.md). Nothing in it is evaluated as code.

## Step types

| `type` | What it does |
| --- | --- |
| `tool` | Calls a platform tool with an input mapping. `on_error: "continue"` keeps an error as the step's output. |
| `agent` | Runs one of your agents (a pinned version, or the latest published one, resolved once per run). |
| `branch` | Goes to the first case whose condition holds, else to `default`. |
| `parallel` | Runs several branches at once and joins them. |
| `foreach` | Runs a body once per item of a list, with bounded concurrency. |
| `loop` | Repeats a body while or until a condition holds, up to `max_iterations`. |
| `evaluate` | Scores a value with evaluators, including a model as judge. |
| `approval` | Waits for a person to approve or reject. |
| `input` | Waits for a person to submit data that matches a schema. |

Loops are described in detail in [Workflow loops](../workflows/loops.md), and the naming convention for step ids in
[Authoring conventions](../workflows/authoring.md).

## A small example

```json
{
  "name": "Publish a release note",
  "input_schema": {
    "type": "object",
    "properties": { "change": { "type": "string" } },
    "required": ["change"],
    "additionalProperties": false
  },
  "start": "draft",
  "steps": [
    { "id": "draft", "type": "agent", "agent_id": "<your agent id>", "input": { "$ref": "input.change" }, "next": "review" },
    { "id": "review", "type": "approval", "prompt": "Publish this note?", "timeout_hours": 72, "next": "stamp" },
    { "id": "stamp", "type": "tool", "tool_id": "clock.now", "input": {} }
  ]
}
```

`{"$ref": "input.change"}` reads the run's input; `{"$ref": "steps.draft.output"}` reads an earlier step's output.
The directory [`examples/workflows`](../../examples/workflows) holds complete definitions.

## Create, publish, run

In Studio, open **Architect** and choose the workflow tab, or send the definition to the API:

```sh
curl -X POST "$API/api/v1/workflows" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"spec\": $(cat my-workflow.json)}"
curl -X POST "$API/api/v1/workflows/$WORKFLOW_ID/versions" -H "Authorization: Bearer $TOKEN"
curl -X POST "$API/api/v1/workflows/$WORKFLOW_ID/runs" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"input": {"change": "Add signed inbound webhooks"}}'
```

The run page in Studio shows the graph with each step's state, the input and output of every step, the timeline and the
events. A run can be started with a pause after each step, cancelled, or run again with the same input.

## Limits

- At most 50 steps, at most 64 KiB as JSON, and loops nested at most two deep.
- The engine applies no defaults from `input_schema`: pass every input the steps read.
- `limits` can cap a run's total cost. A model without a price counts as zero, and publishing warns about it.
