#!/usr/bin/env bash
# Run every check in the repo: layer rules, all Python suites from the repo root (the plan's
# `uv run pytest` acceptance command), the API suite again against Postgres, and the Studio tests.
# Postgres-backed suites need the dev database (bash scripts/dev-db.sh up) and skip with a reason
# when it is unreachable. The crash-injection matrix (tests/recovery) is part of the root run.
set -euo pipefail
cd "$(dirname "$0")/.."

status=0
run() { echo "== $*"; "$@" || status=1; }

export EAP_AUTH_MODE="${EAP_AUTH_MODE:-dev}"

run uv run lint-imports
# The whole tree is ~8,600 tests; the worker suite alone takes ~31 min since the loops milestone
# (LP.17 verification, 2026-11-16), so 30 min could never pass. Override with EAP_TEST_TIMEOUT.
run timeout "${EAP_TEST_TIMEOUT:-5400}" uv run pytest -q
run env EAP_TEST_DATABASE_URL="${EAP_TEST_DATABASE_URL:-postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app}" uv run pytest apps/api -q
run pnpm -C apps/studio test
run timeout 300 bash tests/scripts/dev-lifecycle-test.sh
# pgvector upgrade path on throwaway containers and volumes (never eap_postgres); needs Docker.
if docker info >/dev/null 2>&1; then
    run timeout 180 bash tests/scripts/pgvector-upgrade-test.sh
else
    echo "== SKIP tests/scripts/pgvector-upgrade-test.sh (Docker is not running)"
fi
# AG-UI smoke (AGUI.14): @ag-ui/client 1.0.3 against a throwaway Postgres, API and worker
# (ports 54349, 8200, 8209; never the dev stack), removed afterwards; needs Docker.
if docker info >/dev/null 2>&1; then
    run timeout 600 bash tests/ag-ui-smoke/run.sh
else
    echo "== SKIP tests/ag-ui-smoke/run.sh (Docker is not running)"
fi

if [ "$status" -ne 0 ]; then echo "SOME SUITES FAILED"; else echo "ALL SUITES PASSED"; fi
exit "$status"
