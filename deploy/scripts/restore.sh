#!/usr/bin/env bash
# Restore eap_app and eap_dbos from custom-format pg_dump files.
# Asks for confirmation. Does not delete or rewrite the dump files.
# Reads the database password from the postgres container environment.
# --app and --dbos take a dump path, or the word latest.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

usage() {
  printf 'Usage: %s --app APP_DUMP|latest --dbos DBOS_DUMP|latest\n' "$0" >&2
}

backup_directory() {
  if [[ -n "${PYDANTICOS_BACKUP_DIR:-}" ]]; then
    printf '%s\n' "$PYDANTICOS_BACKUP_DIR"
    return
  fi
  local data=${PYDANTICOS_DATA_DIR:-${HOME}/pydanticos-data}
  printf '%s\n' "${data%/}/backups"
}

# Missing, partial, and empty files are not backups. No cluster calls.
validate_dump_shape() {
  local dump=$1
  if [[ ! -f "$dump" ]]; then
    printf 'Dump file is missing: %s\n' "$dump" >&2
    return 1
  fi
  if [[ "$dump" == *.partial ]]; then
    printf 'Refusing a partial dump: %s\n' "$dump" >&2
    return 1
  fi
  if [[ ! -s "$dump" ]]; then
    printf 'Refusing an empty dump: %s\n' "$dump" >&2
    return 1
  fi
}

# pg_restore --list reads the archive from stdin. Custom format is seekable
# enough on this image; the previous local proof listed a dump this way.
list_dump() {
  local dump=$1
  if ! kubectl_for_profile -n "$NAMESPACE" exec -i postgres-0 -- pg_restore --list <"$dump" >/dev/null; then
    printf 'Refusing a dump pg_restore cannot list: %s\n' "$dump" >&2
    return 1
  fi
}

# Newest valid eap_app-*.dump or eap_dbos-*.dump. Skips empty files, partial
# files, and archives pg_restore cannot list.
pick_latest() {
  local db=$1 dir=$2
  local -a candidates=()
  local file chosen="" i
  if [[ ! -d "$dir" ]]; then
    printf 'Backup directory is missing: %s\n' "$dir" >&2
    return 1
  fi
  shopt -s nullglob
  candidates=("$dir"/"$db"-*.dump)
  shopt -u nullglob
  if ((${#candidates[@]} == 0)); then
    printf 'No valid %s dump in %s. Empty files, partial files, and dumps pg_restore cannot list were skipped.\n' "$db" "$dir" >&2
    return 1
  fi
  for ((i=${#candidates[@]} - 1; i >= 0; i--)); do
    file=${candidates[$i]}
    if [[ "$file" == *.partial ]]; then
      printf 'Skipping partial dump: %s\n' "$file" >&2
      continue
    fi
    if [[ ! -s "$file" ]]; then
      printf 'Skipping empty dump: %s\n' "$file" >&2
      continue
    fi
    if ! kubectl_for_profile -n "$NAMESPACE" exec -i postgres-0 -- pg_restore --list <"$file" >/dev/null 2>&1; then
      printf 'Skipping dump pg_restore cannot list: %s\n' "$file" >&2
      continue
    fi
    chosen=$file
    break
  done
  if [[ -z "$chosen" ]]; then
    printf 'No valid %s dump in %s. Empty files, partial files, and dumps pg_restore cannot list were skipped.\n' "$db" "$dir" >&2
    return 1
  fi
  printf '%s\n' "$chosen"
}

resolve_dump() {
  local db=$1 value=$2
  if [[ "$value" == latest ]]; then
    pick_latest "$db" "$(backup_directory)" || return 1
    return 0
  fi
  validate_dump_shape "$value" || return 1
  printf '%s\n' "$value"
}

APP_SPEC=""
DBOS_SPEC=""
while (($#)); do
  case "$1" in
    --app) APP_SPEC=${2:?missing path after --app}; shift 2 ;;
    --dbos) DBOS_SPEC=${2:?missing path after --dbos}; shift 2 ;;
    *) usage; exit 64 ;;
  esac
done
if [[ -z "$APP_SPEC" || -z "$DBOS_SPEC" ]]; then
  usage
  exit 64
fi

require_command kubectl
guard_contexts

if ! APP_DUMP=$(resolve_dump eap_app "$APP_SPEC"); then
  exit 1
fi
if ! DBOS_DUMP=$(resolve_dump eap_dbos "$DBOS_SPEC"); then
  exit 1
fi

printf 'Restore eap_app from %s and eap_dbos from %s? This replaces objects in both databases. Type restore to continue: ' "$APP_DUMP" "$DBOS_DUMP"
read -r confirmation
if [[ "$confirmation" != "restore" ]]; then
  printf 'Cancelled.\n'
  exit 1
fi

list_dump "$APP_DUMP"
list_dump "$DBOS_DUMP"

restore_db() {
  local db=$1 dump=$2
  # Single quotes are deliberate: the postgres container expands its own password.
  # shellcheck disable=SC2016
  kubectl_for_profile -n "$NAMESPACE" exec -i postgres-0 -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner --username="$POSTGRES_USER" --dbname='"$db" \
    <"$dump"
}

restore_db eap_app "$APP_DUMP"
restore_db eap_dbos "$DBOS_DUMP"
printf 'Restored eap_app and eap_dbos.\n'
