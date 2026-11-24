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
  parent=$(canonical_path "$parent")
  # Avoid "//name" when the parent is the root: "//Users" would not match the "/Users" refusal.
  if [[ "$parent" == "/" ]]; then
    printf '/%s\n' "$base"
  else
    printf '%s/%s\n' "$parent" "$base"
  fi
}

# Validates the backup directory the operator asked for with PYDANTICOS_BACKUP_DIR (for example a folder on an external
# SSD) and sets PYDANTICOS_BACKUP_DIR to it, or exits. Symlinks are never followed: the path must already be its own real
# path. Same rules as the data directory (not broad, owned by the user, not group/world-writable), plus: when the path is
# on a volume under /Volumes (PYDANTICOS_VOLUMES_DIR in tests), that volume must be mounted, so an unplugged disk fails here
# instead of the job writing into an empty folder on the internal disk.
validate_backup_dir() { # REQUESTED DATA_DIR_REAL
  local input=$1 data_real=$2 logical canonical home_real parent volumes vol rest owner mode reason
  if [[ "$input" != /* ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s must be an absolute path.\n' "$input" >&2
    exit 1
  fi
  logical=$(logical_abspath "$input")
  if [[ "$input" != "$logical" && "$input" != "${logical}/" ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s must be a plain absolute path (no ., .. or doubled slashes); use %s.\n' "$input" "$logical" >&2
    exit 1
  fi
  if [[ ! "$logical" =~ ^[A-Za-z0-9._/@+-]+$ ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s may contain only letters, digits and ._/@+- (it is substituted into the CronJob manifest).\n' "$logical" >&2
    exit 1
  fi
  home_real=$(realpath "$HOME")
  volumes=${PYDANTICOS_VOLUMES_DIR:-/Volumes}
  if [[ "$logical" == "$volumes"/* ]]; then
    rest=${logical#"$volumes"/}
    vol="${volumes}/${rest%%/*}"
    if [[ "$logical" == "$vol" ]]; then
      printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is the root of a volume; use a folder on it, for example %s/pydanticos-backup-dir.\n' "$logical" "$vol" >&2
      exit 1
    fi
    if [[ ! -d "$vol" ]] || ! mount | awk -v p="$vol" 'index($0, " on " p " (") {found = 1} END {exit !found}'; then
      printf 'Refusing to start: the backup volume %s is not mounted, so PYDANTICOS_BACKUP_DIR=%s is unavailable. Plug in and mount the disk, then rerun. (Nothing was started.)\n' "$vol" "$logical" >&2
      exit 1
    fi
  fi
  reason=""
  case "$logical" in
    /|"$volumes"|/Users|/Users/Shared|"$home_real"|/System|/usr|/bin|/sbin|/etc|/var|/private|/Library|/Applications|/tmp) reason="a broad or system directory" ;;
    /System/*|/usr/*|/bin/*|/sbin/*|/etc/*) reason="a system directory" ;;
  esac
  parent=$(dirname -- "$home_real")
  while [[ -z "$reason" && "$parent" != "/" ]]; do
    [[ "$logical" == "$parent" ]] && reason="a parent of the home directory"
    parent=$(dirname -- "$parent")
  done
  if [[ -z "$reason" && ( "$logical" == "$data_real" || "$data_real" == "$logical"/* ) ]]; then
    reason="the data directory or a parent of it"
  fi
  if [[ -n "$reason" ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is not allowed (%s).\n' "$logical" "$reason" >&2
    exit 1
  fi
  if [[ -e "$logical" || -L "$logical" ]]; then
    canonical=$(realpath "$logical")
    if [[ "$canonical" != "$logical" ]]; then
      printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is, or passes through, a symlink (real path %s). Set it to the real directory.\n' "$logical" "$canonical" >&2
      exit 1
    fi
    if [[ ! -d "$logical" ]]; then
      printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is not a directory.\n' "$logical" >&2
      exit 1
    fi
  else
    parent=$(dirname -- "$logical")
    if [[ ! -d "$parent" || "$(realpath "$parent")" != "$parent" ]]; then
      printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s does not exist and its parent %s is not an existing real directory; create it yourself first.\n' "$logical" "$parent" >&2
      exit 1
    fi
    mkdir -m 700 -- "$logical" 2>/dev/null || {
      printf 'Refusing to start: could not create PYDANTICOS_BACKUP_DIR %s (is the volume read-only?).\n' "$logical" >&2
      exit 1
    }
  fi
  if stat -f '%u' "$logical" >/dev/null 2>&1; then
    owner=$(stat -f '%u' "$logical")
    mode=$(stat -f '%OLp' "$logical")
  else
    owner=$(stat -c '%u' "$logical")
    mode=$(stat -c '%a' "$logical")
  fi
  if [[ "$owner" != "$(id -u)" ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is not owned by the current user (uid %s).\n' "$logical" "$owner" >&2
    exit 1
  fi
  if (( (8#$mode & 8#022) != 0 )); then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is group- or world-writable (mode %s).\n' "$logical" "$mode" >&2
    exit 1
  fi
  if [[ ! -w "$logical" ]]; then
    printf 'Refusing to start: PYDANTICOS_BACKUP_DIR %s is not writable by the current user.\n' "$logical" >&2
    exit 1
  fi
  PYDANTICOS_BACKUP_DIR=$logical
}

# Create PYDANTICOS_DATA_DIR (default ~/pydanticos-data) mode 700, or refuse.
# On success, PYDANTICOS_DATA_DIR is the canonical directory and PYDANTICOS_BACKUP_DIR is the validated backup directory:
# the one the operator set in PYDANTICOS_BACKUP_DIR (validate_backup_dir), else ${PYDANTICOS_DATA_DIR}/backups.
validate_and_prepare_data_dir() {
  local input logical canonical home_real parent current target parent_real
  local owner mode reason backup_request backup_abs
  backup_request=${PYDANTICOS_BACKUP_DIR:-}
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

  # An explicit backup directory equal to the default location is just the default.
  if [[ -n "$backup_request" ]]; then
    backup_abs=$(logical_abspath "$backup_request")
    if [[ "$backup_abs" == "${logical}/backups" || "$backup_abs" == "${canonical}/backups" ]]; then
      backup_request=""
    fi
  fi
  if [[ -z "$backup_request" ]]; then
    if [[ -L "${logical}/backups" ]]; then
      printf 'Refusing to start: %s/backups is a symlink. Set PYDANTICOS_BACKUP_DIR to the real backup directory instead (deploy/README.md, "Backup directory on another volume").\n' "$canonical" >&2
      exit 1
    fi
    if [[ ! -d "${logical}/backups" ]]; then
      mkdir -m 700 -- "${logical}/backups"
    fi
  elif [[ -L "${logical}/backups" ]]; then
    printf 'Warning: %s/backups is a symlink; it is ignored because PYDANTICOS_BACKUP_DIR is set. Remove it when convenient.\n' "$canonical" >&2
  fi
  if [[ ! -d "${logical}/xdg-cache" ]]; then
    mkdir -m 700 -- "${logical}/xdg-cache"
  fi

  PYDANTICOS_DATA_DIR=$(realpath "$logical")
  if [[ -n "$backup_request" ]]; then
    validate_backup_dir "$backup_request" "$PYDANTICOS_DATA_DIR"
  else
    PYDANTICOS_BACKUP_DIR="${PYDANTICOS_DATA_DIR}/backups"
  fi
  export PYDANTICOS_DATA_DIR PYDANTICOS_BACKUP_DIR
}

# Host disk preflight. Colima's disk image (and Docker's build cache inside it) lives under ~/.colima; when that volume
# fills up the VM gets I/O errors and everything on it stops (outage of 2026-11-23). Warns under
# PYDANTICOS_WARN_FREE_PERCENT (default 15) free, and fails under PYDANTICOS_MIN_FREE_GB (default 30) free.
host_disk_preflight() { # [DIR]
  local dir=${1:-${COLIMA_HOME:-${HOME}/.colima}} line total avail min_gb warn_pct free_gb free_pct
  min_gb=${PYDANTICOS_MIN_FREE_GB:-30}
  warn_pct=${PYDANTICOS_WARN_FREE_PERCENT:-15}
  [[ "$min_gb" =~ ^[0-9]+$ && "$warn_pct" =~ ^[0-9]+$ ]] || {
    printf 'PYDANTICOS_MIN_FREE_GB and PYDANTICOS_WARN_FREE_PERCENT must be non-negative integers.\n' >&2
    return 1
  }
  # The volume that holds the directory itself; fall back to $HOME when ~/.colima does not exist yet.
  [[ -d "$dir" ]] || dir=$HOME
  line=$(df -Pk "$dir" 2>/dev/null | awk 'NR == 2 {print $2, $4}') || true
  read -r total avail <<<"$line"
  if [[ ! "$total" =~ ^[0-9]+$ || ! "$avail" =~ ^[0-9]+$ || "$total" -eq 0 ]]; then
    printf 'Warning: could not read the free space of the volume that holds %s (df); skipping the disk check.\n' "$dir" >&2
    return 0
  fi
  free_gb=$((avail / 1024 / 1024))
  free_pct=$((avail * 100 / total))
  if (( free_gb < min_gb )); then
    printf 'Refusing to deploy: only %s GB are free on the volume that holds %s (%s%% free); at least %s GB are needed. A full disk gives the Colima VM I/O errors and stops production. Free space first (prune the build cache in the VM: docker --context colima-%s builder prune --all; move or delete old dumps in %s), then rerun.\n' \
      "$free_gb" "$dir" "$free_pct" "$min_gb" "${PROFILE:-pydanticos}" "${PYDANTICOS_BACKUP_DIR:-~/pydanticos-data/backups}" >&2
    return 1
  fi
  if (( free_pct < warn_pct )); then
    printf 'Warning: only %s%% (%s GB) are free on the volume that holds %s. Below %s GB the deploy refuses to run; plan to free space.\n' \
      "$free_pct" "$free_gb" "$dir" "$min_gb" >&2
  fi
}

# `kubernetes.port: 0` in colima.yaml makes Colima pick a new random API port whenever the VM is rebuilt, which breaks the
# kube-API NetworkPolicies until the next deploy rewrites them. Pin it (deploy/README.md, "Pin the Kubernetes API port").
warn_unpinned_kube_port() { # COLIMA_YAML
  local config=$1 port
  [[ -f "$config" ]] || return 0
  port=$(awk '
    /^[[:space:]]*#/ {next}
    /^kubernetes:/ {k=1; next}
    k && /^[^[:space:]#]/ {k=0}
    k && /^[[:space:]]+port:/ {sub(/^[[:space:]]+port:[[:space:]]*/, ""); sub(/[[:space:]]*(#.*)?$/, ""); print; exit}
  ' "$config")
  if [[ "$port" == 0 ]]; then
    printf 'Warning: %s has kubernetes.port: 0, so Colima picks a new random API port each time the VM is rebuilt and the kube-API NetworkPolicies stop matching until the next deploy. Pin a fixed port there (deploy/README.md, "Pin the Kubernetes API port").\n' "$config" >&2
  fi
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
