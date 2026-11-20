# Proving durability: a run survives a worker restart

Runs are DBOS workflows (the run id is the DBOS workflow id), so a worker crash or a deploy does not lose a
run: DBOS resumes it on another worker and skips the steps that already completed. Studio shows it.

## Try it

1. Start a long run, for example a workflow with a few slow agent steps, from the workflow page.
2. While it is running, delete the worker pod (`kubectl delete pod <worker-pod>`) or deploy a new image.
3. Open the run page. Once the new worker has picked the run up, the header shows a **Recovered** badge
   (**Recovered ×2** after a second restart). Its tooltip reads "Resumed by DBOS after N restart(s); completed
   steps were not re-run". The same badge is in the run sheet on the Traces page.
4. Open Metrics, Overview: the **Recovered runs** tile counts the runs resumed at least once whose last event
   falls in the selected range, and links to Traces for that range.

## Where the number comes from

`recovery_attempts` in the DBOS system database (`workflow_status`, one row per workflow). The API reads it
through the DBOS client it already holds for enqueueing runs:

- `GET /runs` and `GET /runs/{id}` return `recovery_attempts` (0 when DBOS has no row yet) and
  `recovered` (`recovery_attempts > 0`). A listing page costs one DBOS query for all its runs.
- `GET /metrics/summary` returns `recovered_runs` for each period: the tenant's top-level runs with
  `recovery_attempts > 0` whose last update is in the period. DBOS cannot filter on the counter, so the API pages
  the light status rows of workflows created from 14 days before the range start and keeps the recovered ones.
- Child workflows (`{run_id}:seg:{n}` and the like) are not runs and are not counted.
- If the DBOS system database cannot be read, run pages still load, without the badge; the tile reads 0.
