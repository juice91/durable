#!/bin/sh
# Nightly custom-format dumps for eap_app and eap_dbos.
# A new pod often sees "connection refused" because kube-router REJECTs it
# until that pod's NetworkPolicy is programmed. Postgres can also still be
# starting. Wait before creating a file, and publish only after pg_restore --list.
set -eu

backup_dir=${BACKUP_DIR:-/backups}

cleanup() {
  for path in "$backup_dir"/*.dump.partial; do
    [ -f "$path" ] || continue
    rm -f -- "$path"
  done
}

wait_for_postgres() {
  ready_timeout=${BACKUP_READY_TIMEOUT:-60}
  deadline=$(( $(date +%s) + ready_timeout ))
  delay=1
  attempt=0
  while :; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      printf 'Timed out after %ss waiting for Postgres at postgres:5432. The usual cause is kube-router rejecting the new pod until its NetworkPolicy is programmed; Postgres may also still be starting.\n' "$ready_timeout" >&2
      return 1
    fi
    attempt=$((attempt + 1))
    if pg_isready -h postgres -p 5432 -U eap_app -d eap_app -q -t 2; then
      return 0
    fi
    printf 'Postgres is not accepting connections yet (attempt %s); retrying.\n' "$attempt" >&2
    sleep "$delay"
    if [ "$delay" -lt 8 ]; then
      delay=$((delay * 2))
    fi
  done
}

publish_dump() {
  db=$1
  user=$2
  app_password=$3
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  final="${backup_dir}/${db}-${stamp}.dump"
  partial="${final}.partial"
  PGPASSWORD="$app_password" pg_dump --format=custom --host=postgres --username="$user" --dbname="$db" --file="$partial"
  if ! pg_restore --list "$partial" >/dev/null 2>&1; then
    printf 'pg_restore --list failed for %s; not publishing a backup.\n' "$partial" >&2
    return 1
  fi
  mv -- "$partial" "$final"
}

# Lexical order matches the UTC stamp in the file name. The last success is newest.
newest_valid() {
  db=$1
  newest=
  for file in "$backup_dir"/"$db"-*.dump; do
    [ -f "$file" ] || continue
    [ -s "$file" ] || continue
    case "$file" in
      *.partial) continue ;;
    esac
    if pg_restore --list "$file" >/dev/null 2>&1; then
      newest=$file
    fi
  done
  printf '%s\n' "$newest"
}

# Drop dumps older than BACKUP_RETENTION_DAYS (default 30). Never delete the
# newest dump that pg_restore can list. Partials are not backups.
retain() {
  days=${BACKUP_RETENTION_DAYS:-30}
  case "$days" in
    ''|*[!0-9]*)
      printf 'BACKUP_RETENTION_DAYS must be a non-negative integer.\n' >&2
      return 1
      ;;
  esac
  cleanup
  for db in eap_app eap_dbos; do
    keep=$(newest_valid "$db")
    find "$backup_dir" -maxdepth 1 -type f -name "${db}-*.dump" -mtime "+${days}" | while IFS= read -r old; do
      [ -n "$old" ] || continue
      if [ -n "$keep" ] && [ "$old" = "$keep" ]; then
        continue
      fi
      rm -f -- "$old"
    done
  done
}

main() {
  umask 077
  mkdir -p -- "$backup_dir"
  # Provided by the CronJob secret. set -u exits if either is missing.
  app_password=$EAP_APP_DATABASE_PASSWORD
  dbos_password=$EAP_DBOS_DATABASE_PASSWORD
  trap cleanup EXIT
  cleanup
  wait_for_postgres
  publish_dump eap_app eap_app "$app_password"
  publish_dump eap_dbos eap_dbos "$dbos_password"
  retain
}

if [ "${BACKUP_SKIP_MAIN:-}" != 1 ]; then
  main
fi
