#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage: eap-entrypoint {api|worker|migrate|setup-link|run} [arguments]
  api      Run the API on 0.0.0.0:8000 (no proxy headers: the dev bearer trusts the TCP peer only)
  worker   Run the durable worker
  migrate  Upgrade the application database to the Alembic head
  setup-link  Print the link that creates the built-in admin account (at EAP_STUDIO_URL)
  run      Run a command with the same settings, e.g. run python -m eap_iam_localadmin reset-password
EAP_DATABASE_URL, EAP_DBOS_URL, EAP_SECRETS_KEY, EAP_SECRETS_OLD_KEYS and EAP_AUDIT_HASH_SALT are read from the
file <NAME>_FILE names when <NAME> itself is empty.
EOF
}

# Settings that may come from a file (a mounted secret). The sign-in key is not listed: its plug-in reads
# EAP_LOCAL_AUTH_SIGNING_KEY_FILE itself.
FILE_SETTINGS="EAP_DATABASE_URL EAP_DBOS_URL EAP_SECRETS_KEY EAP_SECRETS_OLD_KEYS EAP_AUDIT_HASH_SALT"

load_file_settings() {
  for setting in $FILE_SETTINGS; do
    # The names come from the list above, never from outside, so the eval expands nothing else.
    eval "value=\${$setting:-}"
    eval "file=\${${setting}_FILE:-}"
    if [ -n "$value" ] || [ -z "$file" ]; then continue; fi
    # Name the setting, never the content.
    if ! value=$(cat -- "$file" 2>/dev/null) || [ -z "$value" ]; then
      printf '%s_FILE names a file that is missing, unreadable or empty.\n' "$setting" >&2
      exit 78
    fi
    export "$setting=$value"
  done
  unset value file setting
}

# The newest one-time setup code replaces every earlier one, the one in the API's start-up log included.
setup_link() {
  url=${EAP_STUDIO_URL:-http://localhost:3000}
  url=${url%/}
  status=0
  output=$(python -m eap_iam_localadmin setup-code) || status=$?
  if [ "$status" -eq 3 ]; then
    printf 'An admin account already exists: sign in at %s\n' "$url"
    printf 'Forgot the password? docker compose exec api eap-entrypoint run python -m eap_iam_localadmin reset-password\n'
    return 0
  fi
  if [ "$status" -ne 0 ]; then return "$status"; fi
  code=$(printf '%s\n' "$output" | sed -n 's/.*setup code: \([A-Z2-7-]\{20,\}\).*/\1/p' | head -n 1)
  if [ -z "$code" ]; then
    printf 'No setup code in the answer of eap_iam_localadmin.\n' >&2
    return 1
  fi
  printf 'Open %s/setup#code=%s\n' "$url" "$code"
}

command_name=${1:-api}
shift || true

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  usage
  exit 0
fi

case "$command_name" in
  api|worker|migrate|setup-link|run) load_file_settings ;;
esac

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
    # installed IAM plug-in that owns tables and is listed in EAP_IAM_* (`localadmin`; otherwise a no-op).
    alembic -c packages/db/alembic.ini upgrade head "$@"
    exec eap-db plugins upgrade
    ;;
  setup-link)
    setup_link
    ;;
  run)
    if [ "$#" -eq 0 ]; then
      echo "run: name a command" >&2
      exit 64
    fi
    exec "$@"
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
