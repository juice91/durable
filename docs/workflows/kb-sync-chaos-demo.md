# Crash-recovery demo: kill the worker during a KB source sync

This demo shows that DBOS carries a run across a worker crash. It uses the existing
[KB Source Synchronization](kb-source-sync.md) workflow
([`kb-source-sync.json`](../../examples/workflows/kb-source-sync.json)); nothing is added to the platform for it.
`scripts/chaos_demo.sh` starts a targeted sync, kills the worker pod after the first segment of the sync has completed,
waits for the runs to finish and prints three blocks of evidence.

## What the script does

1. Finds the workflow named `KB Source Synchronization` with `GET /api/v1/workflows` (it publishes the example first if
   none exists).
2. Resolves the input: the source (`CHAOS_SOURCE_ID`, or the only enabled source) and the part numbers
   (`CHAOS_PART_NUMBERS`, a comma list, or the first 30 orderable part numbers the source already knows, read from
   `kb_entity_identifiers` with scheme `orderable_part_number`). The run input is
   `{source_id, mode: "targeted", part_numbers}`.
3. Starts the run. Its `sync_targeted` step starts the child sync run (`kb.source.sync` returns its `run_id`).
4. Polls the child's events until `CHAOS_KILL_AFTER_SEGMENTS` segments (default 1) have completed (an
   `ingest_progress` event with `phase: sync_segment`; the segment itself is the step `eap.kb_source_sync_segment` of the
   DBOS child workflow `<child run id>:seg:<n>`), then runs `CHAOS_KILL_CMD`. If no segment completes within
   `KILL_AFTER_SECONDS` (default 120) it kills anyway. If the child ends before the kill, no crash is injected and the
   script says so and exits 1.
5. Polls until the parent and the child are terminal and prints the evidence.

A targeted run spends about **3 provider (TI product) calls per part** against the daily budget: 30 parts is about 90
calls. Choose `CHAOS_PART_NUMBERS` accordingly.

## Usage

```sh
export EAP_API=http://localhost:18000      # the tunnel from deploy/scripts/tunnel.sh
export EAP_TOKEN=dev:studio:operator       # example for a dev stack
scripts/chaos_demo.sh --dry-run            # prints the plan and the commands, executes nothing
scripts/chaos_demo.sh
```

Only environment variables configure it, so no credential appears in argv (the bearer goes to `curl` through a private
header file).

| Variable | Default |
| --- | --- |
| `EAP_API` | `http://localhost:18000` |
| `EAP_TOKEN` | required (the dev value `dev:studio:operator` is an example) |
| `CHAOS_SOURCE_ID` | the only enabled `kb_sources` row |
| `CHAOS_PART_NUMBERS` | first 30 known orderable part numbers of the source |
| `CHAOS_KILL_AFTER_SEGMENTS` | `1` |
| `KILL_AFTER_SECONDS` | `120` (fallback kill) |
| `MAX_WAIT` | `1800` seconds to wait after the kill |
| `CHAOS_KILL_CMD` | `ssh deploy-host '…kubectl --context colima-pydanticos -n pydanticos delete pod worker-0'` |
| `CHAOS_LOG_CMD` | the matching `kubectl logs worker-0 --tail=400` |
| `CHAOS_DB_CMD` | `ssh deploy-host '…kubectl exec -i postgres-0 -- psql -U eap_admin -d $1 …'` |

`CHAOS_DB_CMD` is run as `bash -c "$CHAOS_DB_CMD" chaos-db <database>`: `$1` is `eap_app` (application tables) or
`eap_dbos` (the DBOS tables, schema `dbos`), the SQL arrives on stdin, and the command prints `|`-separated rows with a
header line. Both databases are created by `deploy/k8s/configmap.yaml`.

## The three blocks and what each proves

**1. EVENTS** reads the run event logs through the API. For the parent run it lists `step_completed` per step (a loop
step is counted per iteration): every step must be `x1`. For the child sync run it lists each completed segment: every
segment must be `x1`. It ends with the statuses and the final `kb_source_syncs` counters. This shows the runs finished
with each unit of work recorded once, whichever worker did it.

**2. WORKER LOG** prints the lines of the restarted worker that match `recover`. This is DBOS recovering the pending
workflows when the new process starts, which is the proof that the crash was real and that the runs were resumed by
recovery rather than started again.

**3. DBOS** prints the `workflow_status` rows of the parent run, the child sync run and every segment child
`<child run id>:seg:<n>`, with `status` and `recovery_attempts` (the workflows that were running at the crash show
`recovery_attempts` of at least 1). A **DUPLICATES** check follows, from the application database: for the source,
`count(*)` of `kb_source_records` against the count of distinct `(external_id, record_type)` (the table's key includes
`record_type`, so a bare `external_id` would repeat legitimately), and the number of `kb_entity_documents` per entity for the
synced parts, with the number of entities that link the same document twice (must be 0). Segments write with
hash-guarded upserts, so a segment that crashed after its writes and was replayed changes nothing twice.

## Exit status

`0` only when the parent and the child completed, a crash was injected while the child was running, no step or segment
completed twice and the duplicate check holds. Anything else prints the reason and exits `1`.

## When not to run it

Do not kill the worker during the posting schedules: the Taipei slots **08, 11, 14, 17, 20 and 23** (the 小天 posts). A
kill during a slot delays or repeats that slot's run. The worker pod is also the one that runs the nightly knowledge-base
sync and the other schedules. Run the demo between slots.
