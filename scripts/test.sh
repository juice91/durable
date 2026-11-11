#!/usr/bin/env bash
# Run every test suite in the repo. Each Python package is tested on its own because the
# packages each keep a `tests` package, and pytest cannot collect same-named packages together.
# Postgres-backed suites need the dev database: bash scripts/dev-db.sh up
set -euo pipefail
cd "$(dirname "$0")/.."

status=0
run() { echo "== $*"; "$@" || status=1; }

run uv run lint-imports
for pkg in packages/*/ apps/api/ apps/worker/; do
  compgen -G "${pkg}tests/test_*.py" > /dev/null || continue
  run uv run pytest "${pkg}tests" -q
done
run env EAP_TEST_DATABASE_URL="${EAP_TEST_DATABASE_URL:-postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app}" uv run pytest apps/api/tests -q
run pnpm -C apps/studio test

if [ "$status" -ne 0 ]; then echo "SOME SUITES FAILED"; else echo "ALL SUITES PASSED"; fi
exit "$status"
