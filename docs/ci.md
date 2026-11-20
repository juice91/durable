# Continuous integration

Three workflows live in `.github/workflows/`:

| Workflow | Runs on | What it does |
| --- | --- | --- |
| `ci.yml` (**CI**) | pull requests, pushes to main, manual | Hosted runners: `python` (import contracts, DB-free unit tests), `studio` (lint, tsc), `studio-test` (five vitest shards), `deploy-checks` (manifests, workflow lint, deploy tests). A `changes` job decides which of them apply. |
| `ci-heavy.yml` (**CI Heavy**) | same-repository pull requests, pushes to main, nightly, manual | The self-hosted Mac Studio: Postgres-backed package suites, run-store suite and API suite. The nightly adds the worker suite and the recovery suite. |
| `deploy.yml` | manual | The deploy gate needs CI and CI Heavy green on the commit (or on an identical tree). |

## What runs on a pull request

A pull request runs **the tests its change affects**; a push to `main` runs everything.

- **Python** (the `python` job in CI, the three suite steps in CI Heavy): `scripts/ci/affected.py`
  lists the changed files (`git diff` from the merge base with the base branch), maps each to a uv
  workspace member, and adds every member that depends on a changed one, transitively. The graph
  comes from each member's `pyproject.toml`: `[project].dependencies`, `[project.optional-dependencies]`,
  `[dependency-groups]`, and the `workspace = true` entries of `[tool.uv.sources]`. The output is the
  test directories of the affected members, with the reason for each (JSON, or a table with
  `--format table`). `scripts/ci/select-tests.sh` runs it per suite and writes the step summary.
- **Studio** (`studio-test`): `vitest run --changed <merge-base>` (vitest's import-graph selection),
  still split into five shards so the check names do not change. A shard with nothing to run passes.
  `lint` and `tsc` run only when a Studio path changed (the `changes` filter), as before.
- **Jobs with nothing to run still succeed.** CI Heavy skips the Postgres container, the install and
  the migration when no suite is affected and reports "no affected Python tests" in the step summary.
  Job and check names are the same as before, so branch protection and the deploy gate keep working.
- **The step summary lists what ran and why**: the mode (affected or full), the reason, the paths per
  suite, and the changed files with the member each one affects.

### Selection rules

First match wins, per changed file:

| Change | Result |
| --- | --- |
| `docs/`, `apps/docs/`, `examples/`, `spikes/`, `.planning/`, any `*.md` | No tests. (CI Heavy does not start for these at all.) |
| `apps/studio/`, `package.json`, `pnpm-*.yaml`, `.npmrc` | Studio tests only, no Python tests. |
| `tests/deploy/`, `deploy/`, `.dockerignore`, other `.github/` files | The deploy checks (their own `deploy-checks` job), no member tests. |
| root `pyproject.toml`, `uv.lock`, `.github/workflows/ci*.yml`, `scripts/ci/`, any `conftest.py`, the rest of `tests/` | **Everything.** |
| `packages/db` | Everything: its pytest plugin (`eap_db.testing`) loads in every session. |
| the model modules `eap_db` imports (`eap_api/models.py`, `eap_api/{mcp_servers,workflows}/models.py`, `eap_worker/tickets.py`) | Everything, for the same reason. |
| a path no rule knows (`config/`, `infrastructure/`, `scripts/`, new top-level paths) | Everything, to be safe. |
| a file inside a workspace member | That member's tests plus the tests of every member that depends on it. |

Two kinds of edge the pyprojects cannot express are written down in `affected.py`
(`TEST_ONLY_DEPENDENCIES`): a few tests import a package their own package does not declare
(for example the pricing tests import `eap_api` and `eap_worker`). Such an edge is one step: a change
to `eap_api` reruns the pricing tests but not everything that depends on pricing.
`tests/deploy/test_ci_affected.py` fails if a package imports another workspace package without a
declared or listed edge, so the graph cannot drift silently.

CI Heavy only runs the suites it ran before (package suites, run-store, API); a worker-only change
selects the DB-free tests of the packages it touches and no worker suite, as the worker suite stays
nightly.

## Forcing a full run

- Add the **`ci:full`** label to the pull request, then re-run the workflow. The selection step reads
  the labels through the API on every run, so a re-run sees a label added after the first run.
  (If the labels cannot be read, the run is full.)
- Run the workflow manually (**Actions → CI** or **CI Heavy → Run workflow**) with `full` set to
  `true` (the default). With `full` set to `false` a manual run selects against `origin/main`
  like a pull request.

## Why main stays full

Selection is a pull request optimisation. The deploy gate needs CI and CI Heavy green on the main
commit, and a green run on main must mean the whole suite passed on that exact tree: a missed
dependency edge, a flaky interaction between merged pull requests, or a stale base would otherwise
reach a deploy unnoticed. Pushes to `main`, the nightly CI Heavy and every manual run (by default)
therefore run every suite. A pull request that selects too little is caught on main, and the guard
test above plus `ci:full` keep that rare.

The deploy gate counts only **full** runs. It accepts a green run for the exact commit (a push to
main), or a green run of a different commit with an identical tree if that run was a push to main or
a manual run with `full=true`. A manual run says so in its title (`run-name` in `ci.yml` and
`ci-heavy.yml`: "... (manual, full=true)"), which the gate reads from the runs API; a pull request
run is selective, so it is ignored even when green.

## Running the hosted CI on the Mac Studio

When GitHub-hosted runners are unavailable (a billing or spending-limit stop), `ci.yml` can run on the
self-hosted Mac Studio runners. Set a repository variable holding a JSON label array:

```sh
gh variable set CI_RUNNER --body '["self-hosted","self-hosted-runner"]'
```

Every job in `ci.yml` reads `runs-on: ${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}`,
so with the variable set they land on the Studio runners and without it on `ubuntu-latest`.

What changes while it is set:

- Pull request jobs run only for branches of this repository (the same guard as CI Heavy); a pull
  request from a fork gets no CI jobs rather than running its code on the Studio.
- The five Studio shards queue behind each other on the two runners instead of running in parallel.
  Each job has a `timeout-minutes` so a stuck job cannot hold a runner.
- Nothing else differs: the token stays `contents: read`, and the job names the deploy gate reads are the same.

To go back to hosted runners, delete the variable: `gh variable delete CI_RUNNER`. The public export
has no such variable and always uses `ubuntu-latest`.

## Running the selection locally

```sh
python3 scripts/ci/affected.py --format table              # against origin/main
python3 scripts/ci/affected.py --base main --head HEAD     # JSON
python3 scripts/ci/affected.py --files-from - <<< 'packages/pricing/src/eap_pricing/x.py'
```

The tests for the script and the workflow shapes are `tests/deploy/test_ci_affected.py` and
`tests/deploy/test_workflows.py`.
