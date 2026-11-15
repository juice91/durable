#!/usr/bin/env bash
set -euo pipefail

# These constants are consumed by scripts that source this library.
# shellcheck disable=SC2034
PROFILE=pydanticos
# shellcheck disable=SC2034
EXPECTED_CONTEXT=colima-pydanticos
# shellcheck disable=SC2034
NAMESPACE=pydanticos
# shellcheck disable=SC2034
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC2034
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." && pwd)

require_command() {
  command -v "$1" >/dev/null 2>&1 || { printf 'Required command not found: %s\n' "$1" >&2; exit 127; }
}

# Absolute path with . and .. removed, without resolving symlinks.
logical_abspath() {
  local input=$1
  local path="" part rest
  # Match a literal tilde in the variable. The shell does not expand it inside a value.
  # shellcheck disable=SC2088
  case "$input" in
    "~") input=$HOME ;;
    "~/"*) input="${HOME}/${input#~/}" ;;
  esac
  if [[ "$input" != /* ]]; then
    input="${PWD%/}/$input"
  fi
  while [[ "$input" == */ && "$input" != "/" ]]; do
    input="${input%/}"
  done
  rest="${input#/}"
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    if [[ "$rest" == */* ]]; then
      rest="${rest#*/}"
    else
      rest=""
    fi
    case "$part" in
      ""|.) ;;
      ..)
        if [[ -z "$path" || "$path" == "/" ]]; then
          path="/"
        else
          path="${path%/*}"
          [[ -n "$path" ]] || path="/"
        fi
        ;;
      *)
        if [[ -z "$path" || "$path" == "/" ]]; then
          path="/${part}"
        else
          path="${path}/${part}"
        fi
        ;;
    esac
  done
  [[ -n "$path" ]] || path="/"
  printf '%s\n' "$path"
}

# realpath, including a final component that does not exist yet.
canonical_path() {
  local path=$1 parent base
  if [[ -e "$path" || -L "$path" ]]; then
    realpath "$path"
    return
  fi
  parent=$(dirname -- "$path")
  base=$(basename -- "$path")
  if [[ "$parent" == "$path" ]]; then
    printf '%s\n' "$path"
    return
  fi
  printf '%s/%s\n' "$(canonical_path "$parent")" "$base"
}

# Create PYDANTICOS_DATA_DIR (default ~/pydanticos-data) mode 700, or refuse.
# On success, PYDANTICOS_DATA_DIR is the canonical directory and
# PYDANTICOS_BACKUP_DIR is ${PYDANTICOS_DATA_DIR}/backups.
validate_and_prepare_data_dir() {
  local input logical canonical home_real parent current target parent_real
  local owner mode reason
  input=${PYDANTICOS_DATA_DIR:-${HOME}/pydanticos-data}
  logical=$(logical_abspath "$input")
  home_real=$(realpath "$HOME")

  current="/"
  local rest="${logical#/}" part
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    if [[ "$rest" == */* ]]; then
      rest="${rest#*/}"
    else
      rest=""
    fi
    [[ -n "$part" ]] || continue
    if [[ "$current" == "/" ]]; then
      current="/${part}"
    else
      current="${current}/${part}"
    fi
    if [[ -L "$current" ]]; then
      target=$(realpath "$current")
      if [[ "$current" == "$logical" ]]; then
        printf 'Refusing to start: data directory %s is a symlink (resolves to %s).\n' "$logical" "$target" >&2
        exit 1
      fi
      parent=$(dirname -- "$current")
      parent_real=$(realpath "$parent")
      if [[ "$parent_real" != "/" && "$target" != "$parent_real" && "$target" != "$parent_real"/* ]]; then
        printf 'Refusing to start: data directory path contains symlink %s pointing outside itself (%s).\n' "$current" "$target" >&2
        exit 1
      fi
    fi
  done

  canonical=$(canonical_path "$logical")
  reason=""
  if [[ "$canonical" == "/" ]]; then
    reason="the filesystem root"
  elif [[ "$canonical" == "/Users" ]]; then
    reason="/Users"
  elif [[ "$canonical" == "/Users/Shared" ]]; then
    reason="/Users/Shared"
  elif [[ "$canonical" == "$home_real" ]]; then
    reason="the home directory"
  else
    parent=$(dirname -- "$home_real")
    while [[ "$parent" != "/" ]]; do
      if [[ "$canonical" == "$parent" ]]; then
        reason="a parent of the home directory"
        break
      fi
      parent=$(dirname -- "$parent")
    done
  fi
  if [[ -n "$reason" ]]; then
    printf 'Refusing to start: data directory %s resolves to %s, which is not allowed (%s).\n' "$input" "$canonical" "$reason" >&2
    exit 1
  fi

  if [[ -e "$logical" || -L "$logical" ]]; then
    if [[ ! -d "$logical" || -L "$logical" ]]; then
      printf 'Refusing to start: data directory %s is not a real directory.\n' "$logical" >&2
      exit 1
    fi
  else
    mkdir -p -- "$(dirname -- "$logical")"
    mkdir -m 700 -- "$logical"
  fi

  # BSD stat (macOS, the deployment host) uses -f; GNU stat (Linux CI runners) uses -c.
  if stat -f '%u' "$logical" >/dev/null 2>&1; then
    owner=$(stat -f '%u' "$logical")
    mode=$(stat -f '%OLp' "$logical")
  else
    owner=$(stat -c '%u' "$logical")
    mode=$(stat -c '%a' "$logical")
  fi
  if [[ "$owner" != "$(id -u)" ]]; then
    printf 'Refusing to start: data directory %s is not owned by the current user (uid %s).\n' "$canonical" "$owner" >&2
    exit 1
  fi
  if (( (8#$mode & 8#022) != 0 )); then
    printf 'Refusing to start: data directory %s is group- or world-writable (mode %s).\n' "$canonical" "$mode" >&2
    exit 1
  fi

  if [[ -L "${logical}/backups" ]]; then
    printf 'Refusing to start: %s/backups is a symlink.\n' "$canonical" >&2
    exit 1
  fi
  if [[ ! -d "${logical}/backups" ]]; then
    mkdir -m 700 -- "${logical}/backups"
  fi
  if [[ ! -d "${logical}/xdg-cache" ]]; then
    mkdir -m 700 -- "${logical}/xdg-cache"
  fi

  PYDANTICOS_DATA_DIR=$(realpath "$logical")
  PYDANTICOS_BACKUP_DIR="${PYDANTICOS_DATA_DIR}/backups"
  export PYDANTICOS_DATA_DIR PYDANTICOS_BACKUP_DIR
}

guard_contexts() {
  local docker_host expected_context expected_host
  expected_context="colima-${PROFILE}"
  expected_host="unix://${HOME}/.colima/${PROFILE}/docker.sock"
  if [[ "$EXPECTED_CONTEXT" != "$expected_context" ]]; then
    printf 'Refusing to continue: explicit context is %s, expected %s\n' "$EXPECTED_CONTEXT" "$expected_context" >&2
    exit 1
  fi
  docker_host=$(docker --context "$EXPECTED_CONTEXT" context inspect "$EXPECTED_CONTEXT" --format '{{.Endpoints.docker.Host}}')
  if [[ "$docker_host" != "$expected_host" ]]; then
    printf 'Refusing to continue: Docker endpoint is %s, expected %s\n' "$docker_host" "$expected_host" >&2
    exit 1
  fi
}

docker_for_profile() {
  docker --context "$EXPECTED_CONTEXT" "$@"
}

kubectl_for_profile() {
  kubectl --context "$EXPECTED_CONTEXT" "$@"
}
