#!/usr/bin/env bash
# Dev supervisor for the durable agent-run worker: runs `python -m eap_worker` and restarts it
# whenever it exits (crash, kill -9, EAP_CRASH_AT hook), with a short backoff. Because the
# executor id is fixed (EAP_EXECUTOR_ID, default worker-1), each restart recovers the pending
# runs the previous process owned. Production uses systemd / Kubernetes instead.
#
# Env: EAP_DATABASE_URL, EAP_DBOS_URL, EAP_APP_VERSION (m1), EAP_EXECUTOR_ID (worker-1),
#      EAP_LITELLM_BASE_URL / EAP_LITELLM_API_KEY, EAP_WORKER_BACKOFF (seconds, default 2).
# Stop with Ctrl-C (SIGINT/SIGTERM stop the worker and the loop).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export EAP_DBOS_URL="${EAP_DBOS_URL:-${DBOS_SYSTEM_DATABASE_URL:-postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos}}"
export EAP_EXECUTOR_ID="${EAP_EXECUTOR_ID:-worker-1}"
export EAP_APP_VERSION="${EAP_APP_VERSION:-m1}"
backoff="${EAP_WORKER_BACKOFF:-2}"

child=""
stopping=0
on_signal() {
  stopping=1
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then kill -TERM "$child" 2>/dev/null; fi
}
trap on_signal INT TERM

while true; do
  echo "[dev-worker] starting eap_worker (executor_id=$EAP_EXECUTOR_ID app_version=$EAP_APP_VERSION)"
  uv run python -m eap_worker &
  child=$!
  wait "$child"
  code=$?
  # `wait` returns early when a trapped signal arrives; wait again for the child to finish.
  if kill -0 "$child" 2>/dev/null; then wait "$child"; code=$?; fi
  child=""
  if [[ "$stopping" -eq 1 ]]; then
    echo "[dev-worker] stopped"
    exit 0
  fi
  echo "[dev-worker] worker exited with code $code; restarting in ${backoff}s"
  sleep "$backoff"
done
