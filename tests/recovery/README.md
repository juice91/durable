# Crash-injection matrix (milestone 1, task 9)

Kills real processes at named points and checks that a fresh process recovers the run without
duplicating the external side effect. Binding spec: `docs/plans/2026-11-11-milestone-1-vertical-slice.md`
("Durability contract", "Run hand-off contract", "Task 9 crash-injection matrix").

## Run it

```bash
bash scripts/dev-db.sh up                    # dev Postgres (eap_app + eap_dbos)
timeout 600 uv run pytest tests/recovery -v  # ~25 s; also part of scripts/test.sh
```

Without a reachable Postgres every test is skipped with the reason. Override the databases with
`EAP_TEST_DATABASE_URL` (eap_app) and `EAP_TEST_DBOS_URL` (eap_dbos). Each test has a 240 s hard
timeout. A summary of what each scenario observed is printed at the end of the run.

Each test gets its own DBOS system schema, application version, executor id and tenant, so its
workers recover and dequeue only its own run. Worker processes are stopped and the schema and the
tenant's rows (runs, events, side effects, fake tickets, agents) are deleted afterwards.

## How a crash is made

- Worker: `python -m eap_worker` is started with `EAP_CRASH_AT=<point>` (and `EAP_CRASH_EVENT_KEY`
  for scenario 3). When it reaches that point, `apps/worker/src/eap_worker/crash.py` calls
  `os._exit(137)`: no cleanup and no checkpoint, the same as `kill -9`. `EAP_CRASH_MARKER` makes
  sure it can crash only once. A second worker with the **same executor id** and no crash settings
  then recovers the pending workflow on launch.
- API: `crashing_api.py` runs the real API app in its own process and sends one real POST.
  `DBOSRunLauncher.launch`, the call right after the route commits the `runs` row, is replaced with
  `os._exit(137)`. The test then retries the same POST against a fresh API instance.

The agent is the `test:ticket` scripted model. Request 0 calls `tickets.create`, which hits the fake
external ticket service (it honours an idempotency key and counts every call). Request 1 streams
the answer text. A healthy run stores six events:
`start, model:0:final, tool:{id}:call, tool:{id}:result, model:1:final, end`.

## What every scenario asserts

1. Exactly one external ticket for the run. The number of calls the service received is reported,
   and it may legitimately be 2.
2. `seq` is exactly `0..5`, no `event_key` repeats, the event story is the healthy one above, the
   run is `completed`, and DBOS has exactly one workflow for the `run_id`, in `SUCCESS`.
3. SSE replay: for resume points -1, 0, the last seq stored before the crash, the midpoint, last-1
   and last, both `?after_seq=` and `Last-Event-ID` return exactly the database's events after that
   seq, with `id == seq`, ending on `run_completed`.

## What each scenario proves

| # | Crash point | What it proves |
|---|-------------|----------------|
| 1 | `model_request`: inside the request-1 model step, right after its first text delta was NOTIFY'd | A model step that dies mid-stream is re-executed, not replayed half-done. The tool step that was already checkpointed is not called again (1 call). The test listens on `eap_run_deltas`: the crashed attempt sent 1 delta under attempt A, and the retried request streams the full final text under a different attempt B, both with `model_request` 1. That is what lets the UI drop the abandoned partial text. |
| 2 | `after_tool_call`: the ticket service returned, but `side_effects` is still `reserved` and the step is not checkpointed | The tool step is at-least-once: recovery calls the service again (2 calls) with the same idempotency key, and the service deduplicates it (1 ticket). The pre-crash `tool_call_id` is replayed, not regenerated. |
| 3a | `after_event_write` for `tool:*:result`: the event committed to `eap_app`, but `eap.append_event` has no checkpoint in `eap_dbos` | The re-executed append hits `ON CONFLICT (run_id, event_key)`. The stored row keeps its seq (3) and data, no seq is consumed, and nothing is duplicated. |
| 3b | `after_event_write` for `model:*:final` (first match: `model:0:final`, seq 1) | The same, for a model event written from the checkpointed response. The tool runs for the first time after recovery (1 call). |
| 4 | API: `runs` row committed, process dies before the enqueue | The orphan row stays `queued`, with no workflow and no events, while a live worker polls. Retrying the same POST returns 200 (the row already existed), enqueues `run_id` as the workflow id, and the run completes once. A further retry after completion is a no-op: one workflow, one runs row, the same events, 1 call. |

### Why scenario 4 kills a separate API process

The alternatives were a launcher that raises on its first call, or inserting the row with
`eap_run_store` and then POSTing. Raising runs the route's exception handler and returns a 503, so
the request ends normally instead of dying. Inserting directly skips the route's own insert. Killing
the process inside `launch` leaves exactly the state a real crash leaves: the route's own insert
committed and nothing else happened. The retry then goes through the real
`created=False → compare → launch` path.
