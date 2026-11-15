# Workflow loops

Workflows can repeat work with two block steps, `foreach` and `loop`, and can score a value with an
`evaluate` step. Put a `loop`, an agent step and an `evaluate` step together and you get a refine
loop: write, judge, and try again with the judge's feedback.

This guide uses three runnable examples from [`examples/workflows/`](../../examples/workflows/):

| Example | Shows |
|---|---|
| [`foreach-triage.json`](../../examples/workflows/foreach-triage.json) | `foreach` with concurrency, `on_item_error: "collect"`, a branch in the body, the Kleene `any` |
| [`until-polling.json`](../../examples/workflows/until-polling.json) | `loop` with `until`, `state` and `carry`, `on_max_iterations: "exit"`, a branch on why the loop stopped |
| [`refine.json`](../../examples/workflows/refine.json) | refine: `loop` + agent + `evaluate`, with a cost cap |

Each example file holds four fields:

- `description`: what the example does;
- `agents`: the agent specs it needs;
- `input`: a sample run input;
- `spec`: the workflow spec.

In `spec`, `"agent_id": "${agents.<name>}"` stands for the id of the agent created from
`agents.<name>`. Create and publish each agent first (`POST /api/v1/agents`, then
`POST /api/v1/agents/{id}/versions`), and put its id in place of the placeholder. Then create the
workflow (`POST /api/v1/workflows` with `{"spec": ...}`), publish it
(`POST /api/v1/workflows/{id}/versions`), and start a run
(`POST /api/v1/workflows/{id}/runs` with `{"input": ...}`).

The examples use the scripted `test:*` models, so they run without a real LLM. Swap in your real
models when you adapt them. `apps/api/api_tests/test_workflow_loops_examples.py` publishes and runs
every example through the API. It also checks that every JSON snippet in this guide is identical
to the part of the example it quotes.

## Block steps and their bodies

A `foreach` or `loop` step has a `body: {start, steps}`. The body is a small graph, validated like
a top-level one.

- **Step ids** are unique across the whole spec, bodies included.
- **Allowed in a body:** `agent`, `tool`, `branch` and `evaluate` steps, and nested loops up to
  2 deep.
- **Not allowed in a body:** `approval` and `parallel` steps (422 `loop_body_step_not_allowed`).
- **Not allowed in a parallel branch:** a loop step (422 `loop_in_parallel_not_allowed`).
- **Tool approvals:** an agent step inside a body runs with tool approvals denied. A gated tool
  call is denied at once, and the model reads the denial. Publish warns
  `loop_agent_approvals_denied` for a pinned body agent whose version has an approval policy.

**What a body step can read:**

- `input`;
- every step that finished before the loop step;
- the body steps that finished before it in the same iteration;
- `loop.<L>.index`, the 0-based iteration number;
- `loop.<L>.item`, in a `foreach` only;
- `loop.<L>.state`, in a `loop` only.

Outside the loop, only the loop step's own output `steps.<L>.output` is readable. The previous
iteration is readable only through `state`.

## foreach

`foreach` runs its body once per item of a list, `items` (a `$ref`), and outputs the list of each
item's result in item order.

<!-- example: foreach-triage.json /spec/steps/0 -->
```json
{
  "id": "each",
  "type": "foreach",
  "items": {"$ref": "input.tickets"},
  "max_items": 20,
  "concurrency": 4,
  "on_item_error": "collect",
  "body": {
    "start": "triage",
    "steps": [
      {
        "id": "triage",
        "type": "agent",
        "agent_id": "${agents.triage}",
        "version": 1,
        "input": {"title": {"$ref": "loop.each.item.title"}, "body": {"$ref": "loop.each.item.body"}},
        "next": "route"
      },
      {
        "id": "route",
        "type": "branch",
        "cases": [
          {
            "when": {
              "any": [
                {"op": "eq", "left": {"$ref": "steps.triage.output.urgency"}, "right": "high"},
                {"op": "eq", "left": {"$ref": "loop.each.item.priority"}, "right": "p1"}
              ]
            },
            "next": "escalate"
          }
        ],
        "default": "file"
      },
      {
        "id": "escalate",
        "type": "tool",
        "tool_id": "tickets.create",
        "input": {"title": {"$ref": "loop.each.item.title"}, "body": {"$ref": "steps.triage.output.summary"}}
      },
      {"id": "file", "type": "tool", "tool_id": "clock.now", "input": {}}
    ]
  }
}
```

The fields:

- **`max_items`** (required, no default) is the bound. A list longer than it fails the step with
  `loop_items_exceeded`, and a value that is not a list fails it with `loop_items_not_list`.
- **`concurrency`** (1 to 8, default 1) runs up to that many items at once. The window slides in
  item order: when item *i* finishes, item *i* + `concurrency` starts. The output order is always
  item order.
- **`on_item_error`** sets what happens when an item fails:
  - `"fail"` (the default) starts no more items, waits for the started ones, and fails the step
    with the first failure;
  - `"collect"` records `{error_code, error}` in that item's place and goes on.

  A cancelled run is never collected: it always ends the run.
- **Publish checks `loop.<L>.item.<field>`** against the schema of the agent output that `items`
  lists. In the example, `items` is the run input, so the fields are not checked.

In the example run, the triage agent answers `"normal"` for every ticket. Only the first ticket
(`"priority": "p1"`) is escalated. The next section explains why the second ticket, which has no
priority, is not.

## Conditions and three-valued (Kleene) logic

Branch cases, `while` and `until` take a comparison:

- `{"op": "eq" | "ne" | "gt" | "lt" | "contains", "left", "right"}`;
- `{"op": "exists", "left"}`.

You can combine comparisons with `{"all": [...]}`, `{"any": [...]}` and `{"not": c}`: up to 8
operands, nested up to 4 deep. Conditions are data, not code. There is no arithmetic, and there are
no functions.

A comparison whose `$ref` resolves to nothing (MISSING) is **unknown**, not false. The combinators
follow Kleene logic:

| Expression | Result |
|---|---|
| `not` unknown | unknown |
| `all` | false if any operand is false, else unknown if any is unknown, else true |
| `any` | true if any operand is true, else unknown if any is unknown, else false |

`exists` is never unknown. A type mismatch, such as `gt` between a string and a number, is false.

**At the top of a condition, unknown is not satisfied:**

- a branch case that is unknown is not taken;
- a `while` that is unknown stops the loop;
- an `until` that is unknown does not end the loop. The loop runs on to `max_iterations`, or until
  the condition resolves.

Each unknown adds a warning `{code: "condition_unknown", condition: "cases.<i>" | "while" |
"until", ...}` to the step's `step_completed` event. A bare comparison over a MISSING value is
plain false, exactly as before loops existed, so loop-free specs behave as they always did.

In the triage example, ticket 2 has no `priority`, so `loop.each.item.priority` is MISSING:

- the triage agent answered `"normal"`, so the urgency comparison is false;
- the priority comparison is unknown, because the value is missing;
- `any` is therefore unknown, the case is not taken, and the ticket goes to `file`.

The branch records a `condition_unknown` warning. Without Kleene logic, a renamed or missing field
could make a loop exit "by condition" without anyone noticing.

## loop: while and until

`loop` repeats its body. It takes exactly one of two conditions:

- **`while`** is checked by the loop before each iteration, the first one included. It may read
  `input`, the outer scope, `loop.<L>.index` and `loop.<L>.state`, but no body step.
- **`until`** is checked inside each iteration, after its body. It may also read every body step
  that runs on every path through the body. A body step that some path skips is 422
  `loop_ref_not_on_every_path`.

Other fields:

- **`max_iterations`** (required, no default) is the bound. `on_max_iterations` says what happens
  when the loop reaches it:
  - `"fail"` (the default) fails the step with `loop_max_iterations`;
  - `"exit"` ends the loop normally.
- **`state`** is a template evaluated once at loop start, over the outer scope.
- **`carry`** is a template evaluated after each iteration's body. Its result becomes the next
  `state`. `carry` reads the state before the update, and `until` reads the state after it. The
  state is capped at 16 KiB (`loop_state_too_large`).

The output is `{iterations, exited: "condition" | "max_iterations", last, state}`. `last` is the
output of the body step that ended the last iteration.

<!-- example: until-polling.json /spec/steps/0 -->
```json
{
  "id": "poll",
  "type": "loop",
  "max_iterations": 3,
  "on_max_iterations": "exit",
  "until": {
    "any": [
      {"op": "eq", "left": {"$ref": "steps.check.output.status"}, "right": "done"},
      {"op": "eq", "left": {"$ref": "steps.check.output.status"}, "right": "failed"}
    ]
  },
  "state": {"last_status": "unknown"},
  "carry": {"last_status": {"$ref": "steps.check.output.status"}},
  "body": {
    "start": "now",
    "steps": [
      {"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "check"},
      {
        "id": "check",
        "type": "agent",
        "agent_id": "${agents.status}",
        "version": 1,
        "input": {
          "job_id": {"$ref": "input.job_id"},
          "attempt": {"$ref": "loop.poll.index"},
          "previous": {"$ref": "loop.poll.state.last_status"},
          "checked_at": {"$ref": "steps.now.output"}
        }
      }
    ]
  },
  "next": "outcome"
}
```

With `"exit"`, the step after the loop can tell why the loop stopped:

<!-- example: until-polling.json /spec/steps/1 -->
```json
{
  "id": "outcome",
  "type": "branch",
  "cases": [
    {"when": {"op": "eq", "left": {"$ref": "steps.poll.output.exited"}, "right": "condition"}, "next": "finished"}
  ],
  "default": "escalate"
}
```

The scripted status agent always answers `"running"`. The example run therefore makes three
checks, ends with `exited: "max_iterations"` and `state: {"last_status": "running"}`, and opens an
escalation ticket.

There is no sleep step: each iteration starts as soon as the previous one ends. Keep
`max_iterations` small for polling, and let the agent or tool you poll do the waiting.

## evaluate and refine

`evaluate` scores a value with 1 to 8 evaluators and outputs `{passed, score, reason, results}`.

- **`value`** is a `$ref`.
- **`expected`** is optional: a literal or a `$ref`.
- **`pass`** is `"all"` (the default) or `"any"`.
- **Evaluators** are the eval suite evaluators: `equals_expected`, `contains`, `regex`,
  `schema_valid`, `field_equals` and `llm_judge`. `max_duration` and `max_cost` are not available,
  because an evaluate step has no run to measure.
- **An `llm_judge` model** must be on the platform allowlist (`EAP_ALLOWED_MODELS`). Publish and
  run start check it.

A failing verdict does not fail the step. The workflow reads `passed` and decides. An evaluator
that cannot run gives a failing result with its reason, as in an eval suite.

**Refine** is a composition, not a step kind: a `loop` with `until` on the judge's verdict, and
`carry` feeding the judge's reason into the next attempt.

<!-- example: refine.json /spec/steps/0 -->
```json
{
  "id": "refine",
  "type": "loop",
  "max_iterations": 5,
  "until": {"op": "eq", "left": {"$ref": "steps.judge.output.passed"}, "right": true},
  "state": {"feedback": ""},
  "carry": {"feedback": {"$ref": "steps.judge.output.reason"}},
  "body": {
    "start": "write",
    "steps": [
      {
        "id": "write",
        "type": "agent",
        "agent_id": "${agents.writer}",
        "version": 1,
        "input": {"attempt": {"$ref": "loop.refine.index"}, "feedback": {"$ref": "loop.refine.state.feedback"}},
        "next": "judge"
      },
      {
        "id": "judge",
        "type": "evaluate",
        "value": {"$ref": "steps.write.output.draft"},
        "evaluators": [
          {
            "kind": "llm_judge",
            "rubric": "The release note must say which attempt it is. MUST_CONTAIN:\"attempt 3\"",
            "model": {"provider": "test", "name": "judge"}
          }
        ],
        "pass": "all"
      }
    ]
  }
}
```

The scripted models:

- **`test:refine`** reads `{"attempt", "feedback"}` and answers
  `{"draft": "attempt <attempt + 1>", "after": <feedback>}`.
- **`test:judge`** passes when the draft contains the rubric's `MUST_CONTAIN` text. Quote the
  text when it has spaces: unquoted, it stops at the first space.

The example run:

- **Attempts 1 and 2 fail.** Each attempt's prompt carries the previous judge reason, which is
  echoed in `after`.
- **Attempt 3 passes.** The loop ends with `{iterations: 3, exited: "condition"}`.

The judge's calls are usage rows on the workflow run itself:

- **Operation** `chat`.
- **Request key** `judge:{scope}:{step}:{k}`, for example `judge:refine.0:judge:0`, or
  `judge:<step>:<k>` at the top level.
- **Pricing and metrics.** The rows are priced like any model request. Metrics show them under the
  `judge` operation.

## Limits and the cost cap

Every loop has a bound, and there is no default. The platform adds its own caps, checked at
publish and again at run start. A draft can still be saved over them.

| Variable | Default | Refuses |
|---|---|---|
| `EAP_WORKFLOW_MAX_LOOP_ITERATIONS` | 100 | a `max_items` or `max_iterations` above it (schema ceiling 1,000) |
| `EAP_WORKFLOW_MAX_STEP_EXECUTIONS` | 2,000 | a spec that can execute more steps than this, counting every iteration of every body (`workflow_too_many_executions`) |
| `EAP_WORKFLOW_MAX_RUN_CONCURRENCY` | 8 | a product of nested foreach `concurrency` above it (`workflow_concurrency_exceeded`) |

At run time, a loop's output is capped at 256 KiB (`EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES`), and its
state at 16 KiB (`EAP_WORKFLOW_MAX_LOOP_STATE_BYTES`).

`limits.max_cost_usd` (0.0001 to 1000) caps what the whole run tree may spend:

- the workflow run;
- its child agent runs;
- its judge rows.

When the cap is set, the worker checks the cost so far before every loop iteration and before
every top-level agent and evaluate step. At or over the cap, the step fails with
`run_budget_exceeded` ("the run reached its cost limit", data `{limit: "max_cost_usd", value,
spent_usd}`: the cap, and what the run tree had spent when the check stopped it).

<!-- example: refine.json /spec/limits -->
```json
{"max_cost_usd": "0.05"}
```

The cap in `refine.json` only bites under a price file that prices the `test:*` models, such as
`config/model-prices.test.json` (set `EAP_MODEL_PRICES_FILE` to it). Under the repository's default
price file, `test:refine` and `test:judge` cost $0, so five attempts spend $0, the cap never stops
the loop, and publish warns `cost_cap_unpriced_model` for the writer and the judge.

A model without a price counts as $0, so the cap cannot see it. When `max_cost_usd` is set, publish
warns `cost_cap_unpriced_model` for each `llm_judge` model, and each model of an agent step's agent
(primary and fallbacks, at any depth), whose requests count as $0. A model can count as $0 because:

- it has no price;
- it has a $0 price, as the repository's default price file gives every `test:*` and `litellm:*`
  model;
- the price file cannot be used.

A judge's warning path is `steps[<i>]...evaluators[<k>]`; an agent step's is the step's own path,
and its message names the agent version and the model (`model` or `fallback_models[<i>]`). For an
unpinned agent step, the warning reflects the latest version at publish time. The warning never
blocks the publish. Agents
warn the same way for their own `limits.max_cost_usd`.

## Cancel

`POST /api/v1/runs/{run_id}/cancel` cancels a workflow run and its whole tree: iterations,
branches and child agent runs.

The responses:

| Status | When | Body |
|---|---|---|
| 202 | The cancel succeeded, or is retried | `{status, error_code, cancelled, approvals_closed, ...}` |
| 404 | The run is unknown, or belongs to another tenant | |
| 409 | The run already ended before the cancel was requested | `{detail}` |
| 409 | The run is not a workflow run | `{detail, code: "cancel_not_supported"}` |
| 503 | The cancel was recorded but not finished | |

A 202 with `cancelled: true` means the run ends `failed` with `run_cancelled`, or `needs_attention`
when a tool side effect was still in flight. A 202 with `cancelled: false` means the run ended by
itself between the request and the cancel, and it keeps its own outcome. A retry is idempotent and
answers 202 again. After a 503, a retry finishes the cancel at once. Otherwise the worker's
reconciler, which runs every minute, finishes it on the first pass after the request is
`EAP_CANCEL_RECONCILE_AFTER_SECONDS` old (default 30 s), so within about 90 seconds.

Inside a loop, the worker also checks for the cancel request before every iteration, so even a
long loop stops within one iteration. Open approval rows of the run tree are closed as denied.

## Traces and Studio

**The built-in trace** (`GET /api/v1/runs/{id}/trace`) nests:

- a `loop` span per loop step;
- a `loop_iteration` span per iteration, under its loop;
- the body's `workflow_step` spans and child runs, under each iteration.

An `evaluate` span carries:

- the verdict: passed, score, and each evaluator's name, kind, result and error code;
- the judge's usage and cost.

It never carries the judged text.

**OpenTelemetry** gets the same loop and iteration spans, with ids and counts only.

**Studio** (Workflows → a workflow):

- **The graph** draws a loop as a group around its body. The group shows the kind, the bound,
  the concurrency, a condition summary and an `×n` badge, and it collapses.
- **A run** shows an iteration pager ("Iteration 3 of 7", ← and →, "failed only"). The body
  nodes show the selected iteration's states, and the loop's detail lists every iteration.
- **The Timeline** shows one row per iteration of a loop, and the body steps' rows under the
  iteration the pager selects only.
- **An evaluate step's panel** lists each evaluator's result. A refine loop's panel lists every
  attempt with its draft, score and reason.
- **The Cancel button** asks for confirmation, then cancels the run. A cancelled run is labelled
  "Cancelled".
- **The trace Sheet** renders `loop`, `loop_iteration` and `evaluate` spans.

**AG-UI**: the mapping of loops, iterations and cancels to AG-UI events arrives with LP.13.
