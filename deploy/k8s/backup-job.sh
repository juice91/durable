#!/bin/sh
# Nightly custom-format dumps for eap_app and eap_dbos, plus zitadel and openfga when their passwords are in the
# job's environment (the identity services are optional).
# A new pod often sees "connection refused" because kube-router REJECTs it
# until that pod's NetworkPolicy is programmed. Postgres can also still be
# starting. Wait before creating a file, and publish only after pg_restore --list.
set -eu

backup_dir=${BACKUP_DIR:-/backups}
# Names this run's partial files. Two jobs can run at once (the nightly one and one created by hand), and each must
# only ever remove its own: deleting every *.partial made both jobs fail on 2026-11-26. The pod name is the hostname.
run_id=${BACKUP_RUN_ID:-$(hostname)-$$}

# This run's partials, and those a killed run left behind (not written to for a day).
cleanup() {
  for path in "$backup_dir"/*.dump."$run_id".partial; do
    [ -f "$path" ] || continue
    rm -f -- "$path"
  done
  find "$backup_dir" -maxdepth 1 -type f -name '*.partial' -mmin +1440 -exec rm -f -- {} +
}

# up.sh mounts the ConfigMap deploy-in-progress here while it deploys. A dump holds ACCESS SHARE on every table, which
# fails the deploy's migration, so wait for the marker to go. It carries its expiry (epoch seconds): a deploy that died
# without removing it cannot stop backups for good.
wait_for_deploy() {
  marker="${BACKUP_DEPLOY_MARKER_DIR:-/deploy-marker}/expires-at"
  wait_timeout=${BACKUP_DEPLOY_WAIT:-5400}
  wait_deadline=$(( $(date +%s) + wait_timeout ))
  reported=0
  while [ -f "$marker" ]; do
    expires=$(cat "$marker" 2>/dev/null || true)
    case "$expires" in
      ''|*[!0-9]*)
        printf 'The deploy marker holds no expiry time; ignoring it.\n' >&2
        return 0
        ;;
    esac
    now=$(date +%s)
    if [ "$now" -ge "$expires" ]; then
      printf 'The deploy marker has expired (its deploy never finished); backing up.\n' >&2
      return 0
    fi
    if [ "$now" -ge "$wait_deadline" ]; then
      printf 'A deploy is still in progress after %ss; not backing up now. The Job retries.\n' "$wait_timeout" >&2
      return 1
    fi
    if [ "$reported" = 0 ]; then
      printf 'A deploy is in progress; waiting for it to finish before the dump.\n' >&2
      reported=1
    fi
    sleep "${BACKUP_DEPLOY_POLL:-10}"
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

# One job dumps at a time (the nightly one, one created by hand, a retry): two dumps at once double the load and hold
# their table locks for longer. The lock is on a file in the shared backup directory, and the kernel drops it with the
# process, however that ends, so there is no stale lock to clean up.
lock_backups() {
  lock_wait=${BACKUP_LOCK_WAIT:-3600}
  exec 9>"${backup_dir}/.backup.lock"
  if flock -n 9; then
    return 0
  fi
  printf 'Another backup job is dumping; waiting up to %ss for it.\n' "$lock_wait" >&2
  flock -w "$lock_wait" 9 || {
    printf 'Another backup job still holds the lock after %ss; giving up.\n' "$lock_wait" >&2
    return 1
  }
}

publish_dump() {
  db=$1
  user=$2
  app_password=$3
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  final="${backup_dir}/${db}-${stamp}.dump"
  partial="${final}.${run_id}.partial"
  rc=0
  PGPASSWORD="$app_password" pg_dump --format=custom --host=postgres --username="$user" --dbname="$db" --file="$partial" || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf 'pg_dump of %s failed (exit %s); its own message is above. Exit 137 means the container was killed (memory).\n' "$db" "$rc" >&2
    return "$rc"
  fi
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

# Drop dumps older than BACKUP_RETENTION_DAYS (default 30; backup.yaml sets it). Never delete the
# newest dump that pg_restore can list. Partials are not backups.
retain() {
  days=${BACKUP_RETENTION_DAYS:-30}
  case "$days" in
    ''|*[!0-9]*)
      printf 'BACKUP_RETENTION_DAYS must be a non-negative integer.\n' >&2
      return 1
      ;;
  esac
  # `find -mtime +N` truncates the age to whole days, so +1 means older than 48 h and the previous night's dump
  # would survive. Use minutes: N days minus a 2 h tolerance for run-time jitter, so with N=1 the dump of the
  # night before (about 24 h old) goes at the next run and only the newest remains.
  cutoff=$((days * 1440 - 120))
  [ "$cutoff" -ge 0 ] || cutoff=0
  cleanup
  for db in eap_app eap_dbos zitadel openfga; do
    keep=$(newest_valid "$db")
    find "$backup_dir" -maxdepth 1 -type f -name "${db}-*.dump" -mmin "+${cutoff}" | while IFS= read -r old; do
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
  # In this order: a job waiting for a deploy must not hold the lock that an older job, which the deploy waits for,
  # is queued on.
  wait_for_deploy
  lock_backups
  wait_for_postgres
  publish_dump eap_app eap_app "$app_password"
  publish_dump eap_dbos eap_dbos "$dbos_password"
  # Optional: present only when the deploy Secret carries the identity services' database passwords.
  if [ -n "${ZITADEL_DB_PASSWORD-}" ]; then
    publish_dump zitadel zitadel "$ZITADEL_DB_PASSWORD"
  fi
  if [ -n "${OPENFGA_DB_PASSWORD-}" ]; then
    publish_dump openfga openfga "$OPENFGA_DB_PASSWORD"
  fi
  retain
}

if [ "${BACKUP_SKIP_MAIN:-}" != 1 ]; then
  main
fi
