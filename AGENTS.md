# AGENTS.md

Instructions for coding agents (Claude Code, Codex, Cursor, orchestrated workers) and for people. Read it once, then work.
Every command below was run against this tree; if one stops working, fix the doc in the same change.

## What this is

A durable agent and workflow platform: a FastAPI control-plane API, a DBOS worker that runs agents and workflows,
Postgres (with pgvector), and Studio, a Next.js web UI. Identity, policy and membership backends are plugin
distributions under `plugins/`.

## Layout

| Path | What is there |
|---|---|
| `apps/api` | `eap_api`: FastAPI control plane (`routes/`, `services/`, `service_deps/`, `deps.py`, `mcp_control/`, `ag_ui/`). Tests in `api_tests/` |
| `apps/worker` | `eap_worker`: DBOS worker (agent runs, workflows, schedules, ingest). Tests in `worker_tests/` |
| `apps/studio` | Studio, Next.js 15 / React 19, tested with vitest (pnpm workspace) |
| `packages/shared-schemas` | `eap_schemas`: Pydantic contracts, depends on nothing else |
| `packages/agent-runtime` | `eap_runtime`: agent execution (no persistence, web or DBOS) |
| `packages/run-store` | `eap_run_store`: all SQL, tenant-scoped stores, workflow/agent definition checks |
| `packages/db` | `eap_db`: Alembic migrations and the pytest plugin that gives each session a throwaway Postgres schema |
| `packages/iam` | `eap_iam`: IAM interfaces only (identity chain, policy, membership) |
| `packages/secrets`, `knowledge`, `kb-sources`, `processors`, `pricing`, `observability`, `mcp-runtime`, `blob-store` | Secrets and outbound policy; RAG; knowledge-base source adapters; processors; model prices; the only OpenTelemetry importer; MCP runtime adapter; blob storage |
| `plugins/iam-*` | IAM implementations: `apikey`, `cfaccess`, `devloopback`, `openaccess`, `zitadel` |
| `deploy/`, `infrastructure/docker/` | Deploy manifests and scripts; dev Postgres compose file (`compose.yaml`) |
| `scripts/` | Dev stack (`dev-*.sh`), `test.sh`, CI helpers (`ci/`), release tooling (`release/`) |
| `tests/` | Cross-cutting tests: `deploy/` (workflow shapes, selection logic), `ci/`, `recovery/` (crash matrix), `scripts/`, `ag-ui-smoke/` |
| `docs/` | Operator and design docs; `docs/platform/system-manifest.yaml` is the machine-checked system manifest |
| `config/` | `model-prices.json` and its README |

## Setup and commands

Needs `uv` (Python 3.13, `.python-version`), `pnpm` 9 and Node. Run from the repo root.

```sh
uv sync --all-packages --frozen        # every workspace member, plugins included
pnpm install --frozen-lockfile         # Studio
```

| Task | Command |
|---|---|
| Import contracts | `uv run lint-imports` (30 contracts, all must be kept) |
| Undefined names (CI gate) | `uvx pyflakes==3.2.0 apps/*/src packages/*/src plugins/*/src \| grep 'undefined name'` must print nothing (other pyflakes warnings are tolerated) |
| Python, DB-free (what PR CI runs) | `uv run pytest -p no:eap_db -q packages/pricing` (any paths) |
| Python, one plugin | `uv run pytest -q -p no:eap_db plugins/iam-zitadel` (tests live in `plugins/<name>/<pkg>_tests/`) |
| Python, with Postgres | start the dev database (below), then `uv run pytest -q apps/api`, or `uv run pytest -q` for the whole tree |
| Studio tests | `pnpm -C apps/studio exec vitest run [file]`; `pnpm -C apps/studio test` runs all |
| Studio lint / types | `pnpm -C apps/studio lint` (warnings exist, exit 0); `pnpm -C apps/studio exec tsc --noEmit` |
| Studio accessibility | `pnpm -C apps/studio exec vitest run src/test/a11y.pages.test.tsx`; the page list is `PAGES` in that file and must cover every route under `src/app` |
| System manifest drift | `uv run python scripts/ci/system_manifest.py --check`; after changing a source constant run it with `--write` |
| Version agreement | `python3 scripts/release/bump.py --check` |
| What a PR would run | `python3 scripts/ci/affected.py --format table` |
| Everything | `bash scripts/test.sh` (over an hour; Docker needed for part of it; run it in parts, see "Do not") |

- **Lint and types:** there is no ruff or mypy configuration in the repo, no CI step for either, and neither tool
  is in the dev dependencies (`uv run python -m mypy` fails: not installed). Do not claim a ruff or mypy pass;
  match the style of the surrounding code. Studio is checked by `next lint` and `tsc`.
- **Dev Postgres:** `bash scripts/dev-db.sh up|down|reset|status|check` (container `eap_postgres`, port 54329,
  compose file `infrastructure/docker/compose.yaml`). Without it, tests that need Postgres skip with a reason
  ("eap_db: no migrated test schema"); a skipped suite is not a pass. Stop what you start.
- **Full dev stack:** `scripts/dev-up.sh`, `dev-status.sh`, `dev-down.sh` (Postgres, worker, API, Studio).
  Env templates are `.env.example` and `.dev.env.example`; do not open real `.env*` files.
- Migrations after pulling: `uv run alembic -c packages/db/alembic.ini upgrade head` (reads `EAP_DATABASE_URL`).

## Architecture rules (must follow)

**API layering is strict.** Controller (`eap_api/routes`, `eap_api/mcp_control`, `eap_api/ag_ui`: protocol only)
-> Service (`eap_api/services`: business logic, collaborators injected through the constructor, raises a
`ServiceError` subclass, never imports `fastapi` or `starlette`) -> Store (`eap_run_store`: all SQL, every query
filtered by tenant). Entry points never call a store or import `sqlalchemy`. `eap_api/deps.py` and `app.py` are the
only readers of `app.state`; services are built in `eap_api/service_deps/<domain>.py`. Enforced by import-linter
contracts and `apps/api/api_tests/test_architecture_app_state.py`. The old exception lists are gone: the contracts
are absolute, so do not add `ignore_imports`. New business logic and every store call go into a service. Details:
[docs/api-layering.md](docs/api-layering.md).

**IAM.** Core `eap_iam` ships interfaces only. Every implementation is a plugin distribution under `plugins/`; the
core never imports an `eap_iam_*` package (import-linter), and plugins never import `eap_api`, `eap_worker`, a web
framework or each other. Authorisation goes through `services/authz.py`, never a direct policy call. See
[docs/iam.md](docs/iam.md).

**Plugins register through entry points.** Groups in use: `eap.iam.identity`, `eap.iam.policy`,
`eap.iam.membership`, `eap.kb_sources.adapters` (knowledge-base source providers), `eap.db.migrations` (a plugin's
own Alembic branch, version table `alembic_version_<name>`), and `pytest11` (test plugins in `packages/db` and
`packages/iam`). A new plugin is a new workspace member under `plugins/*` and must be added to the import-linter
`root_packages` and to the "core never imports" contract in the root `pyproject.toml`.

**Other import contracts** (root `pyproject.toml`, `[tool.importlinter]`): `eap_schemas` depends on nothing;
`eap_runtime` has no persistence, web or DBOS; `eap_run_store` has no runtime, web or agent code; `eap_api` never
imports `eap_worker` or `eap_runtime`; only `eap_observability` imports OpenTelemetry; `eap_secrets` and
`eap_mcp_runtime` import no platform code. Read the contract before working around it.

**Migrations** live in `packages/db/src/eap_db/migrations/versions/`, named `NNNN_short_name.py` (latest is
`0039_api_key_owner.py`; take the next number). Never edit a migration that is on `main`: add a new one.
Only `eap_db` creates or alters tables; no `create_all`. Plugin tables go in the plugin's own branch, never with a
foreign key into a core table. See `packages/db/README.md`.

**Workflow step ids.** A `branch` step is `<subject>_gate` (`check_gate`), a `workflow.return` step is
`ret_<reason>` (`ret_posted`); other steps are a snake_case noun or verb; at most 40 characters, at every depth.
Publishing warns with `step_id_convention`; examples and the system template are guarded by tests.
See [docs/workflows/authoring.md](docs/workflows/authoring.md).

**Secrets.** Never in code, tests, fixtures, logs, commit messages or error text. Credentials enter only through
Studio's masked inputs or the `secrets.request` tool (an approval card with a masked input); data-source
credentials are never scripted into the secrets store. Error messages name the call and the status, never a
credential or a response body that may carry one. Use obviously fake values in tests.

## Tests and CI

- **PR CI runs only affected tests; `main` runs everything.** `scripts/ci/affected.py` maps changed files to
  workspace members plus their dependents; Studio uses `vitest run --changed`. Root `pyproject.toml`, `uv.lock`,
  `conftest.py`, `scripts/ci/`, `packages/db`, unknown paths and most of `tests/` force a full run; docs-only
  changes run no tests. Add the `ci:full` label and re-run to force a full PR run. Details: [docs/ci.md](docs/ci.md).
- Jobs: `changes` decides which of `python`, `studio`, `studio-test` (5 vitest shards) and `deploy-checks` apply. A
  skipped job is normal. A red `changes` job is the path filter failing, not your tests: read its log and rerun.
  The `labeler.yml` workflow (job `label`) only sets the release-notes label; if it fails, set the label by hand.
  `ci-heavy.yml` runs the Postgres-backed package, run-store and API suites on a self-hosted runner for
  same-repository PRs; the worker and recovery suites are nightly. The deploy gate trusts only full runs.
- **Tolerate a loaded runner.** No tight timing caps, no sleeps that race. Vitest uses 15 s test timeouts and 8 s
  for `findBy`/`waitFor`; do not lower them. Poll for a condition instead of waiting a fixed time.
- **Every behaviour change needs a test**, in the package that owns the behaviour. Fix the cause of a flaky test;
  do not widen a timeout or add a retry to hide it.
- Do not leave work that only passes with your local state (running Postgres, env vars, files outside the tree).

## Git and PR conventions

- Branch from `origin/main`, one topic per PR, small commits. Land changes by PR, not by pushing to `main`.
- Titles are conventional commits: `feat(scope): ...`, `fix(scope): ...`, `docs: ...`, `test(scope): ...`,
  `chore:`, `ci:`, `deploy:`. The prefix drives the release-notes label (`feature`, `fix`, `security`, `docs`,
  `infra`, `internal`) set by the labeler; change the label by hand if it guesses wrong.
- Version bumps are not part of a normal PR: `Release PR` does it (`scripts/release/bump.py`). Do not hand-edit
  versions; CI runs `bump.py --check`. See [docs/releasing.md](docs/releasing.md).
- Every commit ends with `Co-Authored-By: <model name> <noreply@anthropic.com>`. An orchestrated worker also adds the
  dispatch trailer its orchestrator asks for (for example `Orca-Dispatch: <id>`). PR bodies end with the generator line your tool specifies.
- A PR body says what changed, why, and lists the commands you ran. Report breakage you find but did not cause;
  do not fix unrelated things in the same PR.

## Do not

- Run `gh auth switch`; use the account that is already active.
- Run a broad `pkill -f` (it kills sibling shells and other agents' work). Stop a process by its own PID.
- Leave background processes, dev servers, tunnels or containers you started running when you finish.
- Let a long command run unbounded. Many agent shells move a command to the background when it outlives the tool
  timeout (often 2 minutes), and the agent is then never woken. Give every test or build call an explicit timeout
  (for example 10 minutes), split long suites so each call finishes inside it (vitest `--shard=i/5`, pytest by
  directory), and never end your turn while something you started is still running.
- Spawn sub-agents that wait on helpers; do your task yourself and report.
- Read or print `.env*` files, key files or secret stores; never commit secrets or generated credentials.
- Commit local scratch: screenshots, exports, `conversation-export/`, `.dev/`, `.blobs/`, build output.
- Edit generated blocks of `docs/platform/system-manifest.yaml` by hand (use `system_manifest.py --write`), edit a
  merged migration, edit `uv.lock` or `pnpm-lock.yaml` by hand, or add an `ignore_imports` exception.
- Use `git stash` bare in a shared checkout; commit work in progress instead.

## Open-core and the public export

A public core repository is produced from `main` by an operator-only export tool (`deploy/export/`, itself not
exported). It rewrites history, replaces operator-specific strings, drops the self-hosted workflows, `deploy/runner`,
planning notes (`docs/plans`, `docs/reviews`, `docs/spikes`, `docs/superpowers`), `spikes/` and customer-specific
plugins, the operator's secret store wiring and off-site backup, then runs a residual-string scan. The public repository
receives each sync as one snapshot commit, not a merge of history. Code that only the operator's deployment needs sits in
marked regions that the export removes; keep such a change inside the markers (see the export tool's README). The export carries the root `AGENTS.md` and `CLAUDE.md` through the same
replacements, so keep both free of private hostnames, account or personal names, e-mail addresses and internal
codenames, and link only to files that survive the export (not the excluded paths above). Never edit the output of
an export by hand; change the source here and re-export.
