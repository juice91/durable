#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: eap-entrypoint {api|worker|migrate} [arguments]
  api      Run the API on 0.0.0.0:8000 (no proxy headers: the dev bearer trusts the TCP peer only)
  worker   Run the durable worker
  migrate  Upgrade the application database to the Alembic head
EOF
}

command_name=${1:-api}
shift || true

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

case "$command_name" in
  api)
    exec uvicorn eap_api.app:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --no-proxy-headers "$@"
    ;;
  worker)
    exec python -m eap_worker "$@"
    ;;
  migrate)
    ready_timeout=${MIGRATE_READY_TIMEOUT:-60}
    deadline=$(( $(date +%s) + ready_timeout ))
    delay=1
    attempt=0
    while :; do
      now=$(date +%s)
      if [ "$now" -ge "$deadline" ]; then
        printf 'Timed out after %ss waiting for Postgres at postgres:5432 before migration.\n' "$ready_timeout" >&2
        exit 1
      fi
      attempt=$((attempt + 1))
      if pg_isready -h postgres -p 5432 -U eap_app -d eap_app -q -t 2; then
        break
      fi
      printf 'Postgres is not accepting connections yet (attempt %s); retrying before migration.\n' "$attempt" >&2
      sleep "$delay"
      if [ "$delay" -lt 8 ]; then
        delay=$((delay * 2))
      fi
    done
    # Alembic is deliberately executed once: migration failures must fail the Job. Core first, then the branch of every
    # installed IAM plug-in that owns tables and is listed in EAP_IAM_* (none today: a no-op).
    alembic -c packages/db/alembic.ini upgrade head "$@"
    exec eap-db plugins upgrade
    ;;
  help|--help|-h)
    usage
    ;;
  *)
    echo "unknown entrypoint: $command_name" >&2
    usage >&2
    exit 64
    ;;
esac
