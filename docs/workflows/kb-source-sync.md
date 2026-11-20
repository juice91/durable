# KB Source Synchronization

`examples/workflows/kb-source-sync.json` ([`kb-source-sync.json`](../../examples/workflows/kb-source-sync.json))
is the published workflow that keeps a knowledge base in step with an external catalogue. It is the one way
a tenant, an operator or a schedule runs a source sync: it composes the five functional tools
`kb.source.get`, `settings.connection.check`, `kb.source.sync`, `workflow.run.wait` and
`workflow.run.result`. The native sync run behind `kb.source.sync` is an implementation detail; agents read
the synced documents through `retrieve` on the knowledge base, never through provider tools.

The example needs no agent. Its file holds `description`, `agents` (empty), `input` (a sample) and `spec`.

## Publish it

Create the connection and the source first (`POST /api/v1/settings/connections`, then
`POST /api/v1/knowledge-bases/{kb}/sources`; credentials are `secret:<name>` references, never values), then:

1. `POST /api/v1/workflows` with `{"spec": <spec>}`;
2. `POST /api/v1/workflows/{id}/versions` (publish; no warnings).

## Run it

`POST /api/v1/workflows/{id}/runs` with `{"input": {"source_id", "mode", "part_numbers"}}`:

- `source_id`: the source to sync;
- `mode`: `targeted`, `incremental` or `full`;
- `part_numbers`: only for `targeted` (at most 100). An incremental or full run leaves it out (the input
  schema declares `default: []`, but the workflow engine does not apply schema defaults, so the step `route`
  branches on whether the input has it); any other mode refuses a non-empty list (`part_numbers_not_allowed`).

## The steps

<!-- example: kb-source-sync.json /spec/steps/0 -->
```json
{
  "id": "source",
  "type": "tool",
  "tool_id": "kb.source.get",
  "input": {
    "source_id": {
      "$ref": "input.source_id"
    }
  },
  "next": "check"
}
```

`source` reads the source; `check` checks its connection and `gate` stops at `connection_failed` (the check
again, whose output carries the stable `error_code`) when it is not `ok`. Nothing is started then. `route` sends a
run that has `part_numbers` down the `*_targeted` steps and any other run down `*_all`, the same three steps
without `part_numbers` (`sync_all`, `wait_all`, `report_all`), and a third with `restart: true` for a full or incremental walk that starts from the beginning instead of the stored cursor (`sync_all_restart`, `wait_all_restart`, `report_all_restart`; the engine applies no input defaults and a `$ref` to a missing input fails, so each optional input gets its own chain); the walk below shows the targeted one.

<!-- example: kb-source-sync.json /spec/steps/5 -->
```json
{
  "id": "sync_targeted",
  "type": "tool",
  "tool_id": "kb.source.sync",
  "input": {
    "source_id": {
      "$ref": "input.source_id"
    },
    "mode": {
      "$ref": "input.mode"
    },
    "part_numbers": {
      "$ref": "input.part_numbers"
    }
  },
  "next": "wait_targeted"
}
```

`sync_targeted` starts the sync exactly once per run and tool call, and answers `{run_id, status: "queued"}`.

<!-- example: kb-source-sync.json /spec/steps/6 -->
```json
{
  "id": "wait_targeted",
  "type": "loop",
  "max_iterations": 100,
  "on_max_iterations": "fail",
  "until": {
    "op": "eq",
    "left": {
      "$ref": "steps.await_targeted.output.terminal"
    },
    "right": true
  },
  "body": {
    "start": "await_targeted",
    "steps": [
      {
        "id": "await_targeted",
        "type": "tool",
        "tool_id": "workflow.run.wait",
        "input": {
          "run_id": {
            "$ref": "steps.sync_targeted.output.run_id"
          },
          "wait_seconds": 300
        }
      }
    ]
  },
  "next": "report_targeted"
}
```

`wait_targeted` sleeps cooperatively inside each `workflow.run.wait` (at most 300 s), up to 100 times (about 8 hours;
the run's own limits allow 14 days). `report_targeted` is `workflow.run.result` with `require_success`.

## Read the report

The run output is the sync's bounded report: `{run_id, status, source_id, mode, discovered, inserted,
updated, unchanged, documents_queued, failed, checkpoint, errors (at most 50)}` and, when the sync reports them, `removed`, `budget {date, used, remaining?}`, `source_revision`, `complete` and `stop_reason`, validated by the spec's
`output_schema` and always far under the 32 KiB output cap. `GET /api/v1/knowledge-bases/{kb}/sources/{source}/syncs/{run_id}`
reads the same report from the sync's own run. `complete: false` in it means the run stopped on a budget and
the next run resumes from the stored checkpoint.

## Failures end the run with a stable code

| What happened | The parent run |
|---|---|
| connection check not `ok` | fails with the check's error code (`secret_missing`, `connection_auth_failed`, `connection_disabled`, ...; `connection_failed` when it names none); step `connection_failed` is `settings.connection.check` with `require_ok` |
| the sync failed (`provider_error`, ...) | fails `sync_failed`; step `report_*` |
| the sync needs attention (`secret_missing`, `connection_auth_failed`) | fails `sync_needs_attention`; step `report_*` |
| the parent's sync run was cancelled | fails `run_cancelled`; step `report_*` |
| the sync outlasts the wait loop | fails `loop_max_iterations` |
| the parent is cancelled | ends `run_cancelled`; the sync is cancelled with it |

A tool step whose tool answers `{error: {code, message}}` (as `workflow.run.result` with `require_success` and
`settings.connection.check` with `require_ok` do) fails the step and the run with that `code`, so the run page
shows the cause instead of an output that does not match `output_schema`. A `code` that is not
`^[a-z][a-z0-9_]{0,63}$` becomes `tool_error`.

## Schedule it

An incremental sync on a schedule is this workflow on a workflow schedule, with no schedule on the source:

```
POST /api/v1/workflows/{id}/schedules
{"name": "nightly", "cron": "0 3 * * *", "timezone": "UTC", "input": {"source_id": "<source>", "mode": "incremental"}}
```

The schedule's `input` is validated against the workflow's **latest published version** input schema at creation and update time; a mismatch returns a 422 `schedule_input_invalid` error. Publishing a new workflow version while schedules exist checks each enabled schedule's input and returns warnings if any no longer match the new schema.

Each fire starts a run of the published workflow, which starts one incremental sync (a run id derived from
the fire time makes a repeated tick harmless).

## Cancel it

`POST /api/v1/runs/{parent_run_id}/cancel` cancels the parent run and also the sync it started: the sync gets
`cancel_requested`, its workflow is cancelled and its run ends `run_cancelled` at its checkpoint.
