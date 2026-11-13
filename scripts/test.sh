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
run timeout 1800 uv run pytest -q
run env EAP_TEST_DATABASE_URL="${EAP_TEST_DATABASE_URL:-postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app}" uv run pytest apps/api -q
run pnpm -C apps/studio test
run timeout 300 bash tests/scripts/dev-lifecycle-test.sh

if [ "$status" -ne 0 ]; then echo "SOME SUITES FAILED"; else echo "ALL SUITES PASSED"; fi
exit "$status"
