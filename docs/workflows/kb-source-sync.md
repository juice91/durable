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
without `part_numbers` (`sync_all`, `wait_all`, `report_all`); the walk below shows the targeted one.

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
updated, unchanged, documents_queued, failed, checkpoint, errors (at most 50)}`, validated by the spec's
`output_schema` and always far under the 32 KiB output cap. `GET /api/v1/knowledge-bases/{kb}/sources/{source}/syncs/{run_id}`
reads the same report from the sync's own run. `complete: false` in it means the run stopped on a budget and
the next run resumes from the stored checkpoint.

## Failures end the run with a stable code

| What happened | The parent run |
|---|---|
| connection check not `ok` | fails `output_invalid`; step `connection_failed` holds the check's `error_code` |
| the sync failed (`provider_error`, ...) | fails `output_invalid`; step `report_*` says `sync_failed` |
| the sync needs attention (`secret_missing`, `connection_auth_failed`) | fails `output_invalid`; step `report_*` says `sync_needs_attention` |
| the sync outlasts the wait loop | fails `loop_max_iterations` |
| the parent is cancelled | ends `run_cancelled`; the sync is cancelled with it |

(`output_invalid` is the run's output refused by `output_schema`: the report tool answered a stable error
object instead of a report.)

## Schedule it

An incremental sync on a schedule is this workflow on a workflow schedule, with no schedule on the source:

```
POST /api/v1/workflows/{id}/schedules
{"name": "nightly", "cron": "0 3 * * *", "timezone": "UTC", "input": {"source_id": "<source>", "mode": "incremental"}}
```

Each fire starts a run of the published workflow, which starts one incremental sync (a run id derived from
the fire time makes a repeated tick harmless).

## Cancel it

`POST /api/v1/runs/{parent_run_id}/cancel` cancels the parent run and also the sync it started: the sync gets
`cancel_requested`, its workflow is cancelled and its run ends `run_cancelled` at its checkpoint.
