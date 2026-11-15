#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

SECRETS_ENV=""
SOPS_FILE=""
while (($#)); do
  case "$1" in
    --secrets-env) SECRETS_ENV=${2:?missing path after --secrets-env}; shift 2 ;;
    --sops) SOPS_FILE=${2:?missing path after --sops}; shift 2 ;;
    *) printf 'Usage: %s [--secrets-env FILE | --sops FILE]\n' "$0" >&2; exit 64 ;;
  esac
done
if [[ -n "$SECRETS_ENV" && -n "$SOPS_FILE" ]]; then
  printf 'Choose only one secrets input.\n' >&2
  exit 64
fi

# Refuse a broad, symlinked, or writable-by-others data directory before touching Colima.
validate_and_prepare_data_dir

tmp_files=()
cleanup() {
  local path
  if ((${#tmp_files[@]})); then
    for path in "${tmp_files[@]}"; do
      rm -f -- "$path"
    done
  fi
  return 0
}
trap cleanup EXIT

make_tmp() {
  local path
  path=$(mktemp "${TMPDIR:-/tmp}/pydanticos-$1.XXXXXX")
  chmod 0600 "$path"
  tmp_files+=("$path")
  printf '%s\n' "$path"
}

for command_name in colima docker kubectl git mktemp sed awk realpath stat; do
  require_command "$command_name"
done

# One writable mount. Colima 0.10 treats an empty mounts list as "mount ~"
# (yaml.go: Mounts != nil && len == 0). Passing this single --mount replaces
# the built-in home mount; it is not added beside it. Colima also injects its cache
# directory unless that path is inside a configured mount, so the cache is
# placed under the data directory via XDG_CACHE_HOME for this command only.
mount_arg="${PYDANTICOS_DATA_DIR}:w"
cache_home="${PYDANTICOS_DATA_DIR}/xdg-cache"
colima_config="${COLIMA_HOME:-${HOME}/.colima}/${PROFILE}/colima.yaml"

assert_running_mount_is_only_data_dir() {
  local found=0 location writable line loc
  if [[ ! -f "$colima_config" ]]; then
    printf 'Refusing to start: profile %s is already running but %s is missing, so its host mounts cannot be verified. Stop the profile and rerun.\n' "$PROFILE" "$colima_config" >&2
    exit 1
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    location=${line%%|*}
    writable=${line#*|}
    found=$((found + 1))
    loc=$location
  done < <(awk '
    /^[[:space:]]*#/ {next}
    /^mounts:/ {inmount=1; next}
    inmount && /^[^[:space:]#]/ {inmount=0}
    inmount && /location:/ {
      loc=$0
      sub(/.*location:[[:space:]]*/, "", loc)
      gsub(/"/, "", loc)
      sub(/[[:space:]]+$/, "", loc)
    }
    inmount && /writable:/ {print loc "|" $2}
  ' "$colima_config")
  if [[ "$found" -ne 1 || "$loc" != "$PYDANTICOS_DATA_DIR" || "$writable" != "true" ]]; then
    printf 'Refusing to start: profile %s is running with host mounts other than %s. Stop it and rerun so Colima replaces the home mount with only that directory.\n' "$PROFILE" "$PYDANTICOS_DATA_DIR" >&2
    exit 1
  fi
}

if colima status "$PROFILE" >/dev/null 2>&1; then
  assert_running_mount_is_only_data_dir
elif colima list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$PROFILE"; then
  XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false --mount "$mount_arg"
else
  XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false --mount "$mount_arg" --kubernetes --runtime docker --cpu 8 --memory 32 --disk 100
fi

guard_contexts

git_sha=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
python_image="pydanticos-python:${git_sha}"
studio_image="pydanticos-studio:${git_sha}"

docker_for_profile build --platform linux/arm64 -f "$REPO_ROOT/deploy/docker/python.Dockerfile" -t "$python_image" "$REPO_ROOT"
docker_for_profile build --platform linux/arm64 -f "$REPO_ROOT/deploy/docker/studio.Dockerfile" -t "$studio_image" "$REPO_ROOT"

kubectl_for_profile apply -f "$REPO_ROOT/deploy/k8s/namespace.yaml"

secret_file=""
if [[ -n "$SOPS_FILE" ]]; then
  require_command sops
  secret_file=$(make_tmp secrets)
  sops --decrypt --output-type dotenv "$SOPS_FILE" >"$secret_file"
elif [[ -n "$SECRETS_ENV" ]]; then
  # Never delete, move, or modify a file the operator supplied.
  secret_file=$SECRETS_ENV
else
  secret_file=$(make_tmp secrets)
  required_keys=(POSTGRES_PASSWORD EAP_APP_DATABASE_PASSWORD EAP_DBOS_DATABASE_PASSWORD EAP_DATABASE_URL EAP_DBOS_URL EAP_SECRETS_KEY EAP_LITELLM_API_KEY EAP_LITELLM_EMBEDDING_API_KEY EAP_AUDIT_HASH_SALT)
  for key in "${required_keys[@]}"; do
    [[ -n "${!key:-}" ]] || { printf 'Required secret environment variable is unset: %s\n' "$key" >&2; exit 1; }
    printf '%s=%s\n' "$key" "${!key}" >>"$secret_file"
  done
fi

[[ -r "$secret_file" ]] || { printf 'Secrets input is not readable.\n' >&2; exit 1; }
kubectl_for_profile -n "$NAMESPACE" create secret generic pydanticos-secrets \
  --from-env-file="$secret_file" --dry-run=client -o yaml | kubectl_for_profile apply -f - >/dev/null

# Drain before applying a new worker template. See apps/worker/README.md ("drain, keep m1").
if kubectl_for_profile -n "$NAMESPACE" get deployment/api >/dev/null 2>&1; then
  kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=0
fi

query_active_runs() {
  local output err rc
  # Assign the temp path in this shell. Command substitution would drop the trap registration.
  err=$(mktemp "${TMPDIR:-/tmp}/pydanticos-runs-query.XXXXXX")
  chmod 0600 "$err"
  tmp_files+=("$err")
  set +e
  # Single quotes are deliberate: the postgres container expands its own password.
  # shellcheck disable=SC2016
  output=$(kubectl_for_profile -n "$NAMESPACE" exec postgres-0 -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d eap_app -v ON_ERROR_STOP=1 -tAc "SELECT count(*) FROM public.runs WHERE status IN ('"'"'queued'"'"', '"'"'running'"'"')"' \
    2>"$err")
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    if grep -Eqi 'does not exist|postgres-0.*not found|NotFound' "$err"; then
      printf '0\n'
      return 0
    fi
    printf 'Could not query queued and running runs.\n' >&2
    cat "$err" >&2
    return 1
  fi
  output=$(printf '%s' "$output" | tr -cd '0-9')
  if [[ -z "$output" ]]; then
    printf '0\n'
  else
    printf '%s\n' "$output"
  fi
}

if kubectl_for_profile -n "$NAMESPACE" get pod/postgres-0 >/dev/null 2>&1; then
  kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/postgres --timeout=5m
  drain_timeout=${PYDANTICOS_DRAIN_TIMEOUT:-300}
  drain_started=$SECONDS
  while true; do
    active_runs=$(query_active_runs)
    if [[ "$active_runs" == "0" ]]; then
      break
    fi
    if (( SECONDS - drain_started >= drain_timeout )); then
      printf 'Timed out after %s seconds waiting for queued and running runs to finish (%s still active). The API is scaled to 0, so it accepts no new runs. Let the workers finish those runs, then rerun up.sh.\n' "$drain_timeout" "$active_runs" >&2
      exit 1
    fi
    sleep 5
  done
fi

if kubectl_for_profile -n "$NAMESPACE" get statefulset/worker >/dev/null 2>&1; then
  kubectl_for_profile -n "$NAMESPACE" scale statefulset/worker --replicas=0
  # terminationGracePeriodSeconds is 120; wait past that grace period.
  worker_stop_timeout=${PYDANTICOS_WORKER_STOP_TIMEOUT:-180}
  worker_stop_started=$SECONDS
  while true; do
    worker_pods=$(kubectl_for_profile -n "$NAMESPACE" get pods -l app.kubernetes.io/name=worker --no-headers 2>/dev/null || true)
    if [[ -z "${worker_pods//[[:space:]]/}" ]]; then
      break
    fi
    if (( SECONDS - worker_stop_started >= worker_stop_timeout )); then
      printf 'Timed out after %s seconds waiting for worker pods to terminate (terminationGracePeriodSeconds is 120). A worker may still be finishing its current step. Rerun up.sh after the pods are gone.\n' "$worker_stop_timeout" >&2
      exit 1
    fi
    sleep 2
  done
fi

gateway_line=$(colima ssh -p "$PROFILE" -- awk '/host\.lima\.internal/ {print; exit}' /etc/hosts)
IFS=$' \t' read -r host_gateway _ <<<"$gateway_line"
if [[ ! "$host_gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  printf 'Refusing to continue: could not read the Lima host gateway (host.lima.internal) from inside the VM.\n' >&2
  exit 1
fi

rendered_file=$(make_tmp rendered)
kubectl_for_profile kustomize "$REPO_ROOT/deploy/k8s" \
  | sed \
    -e "s|pydanticos-python:dev|${python_image}|g" \
    -e "s|pydanticos-studio:dev|${studio_image}|g" \
    -e "s|/var/lib/pydanticos-backups|${PYDANTICOS_BACKUP_DIR}|g" \
    -e "s|192.168.5.2|${host_gateway}|g" \
  >"$rendered_file"
if [[ -n "${PYDANTICOS_RENDER_OUT:-}" ]]; then
  cp "$rendered_file" "$PYDANTICOS_RENDER_OUT"
fi

kubectl_for_profile -n "$NAMESPACE" delete job migrate --ignore-not-found --wait=true
kubectl_for_profile apply -f "$rendered_file"
kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/postgres --timeout=5m
kubectl_for_profile -n "$NAMESPACE" patch job migrate --type=merge --patch '{"spec":{"suspend":false}}'

# One foreground poll. `kill` of a background subshell does not kill its
# `kubectl wait` child, so the losing watch kept running after this script exited.
migrate_timeout=${PYDANTICOS_MIGRATE_TIMEOUT:-600}
migrate_started=$SECONDS
migrate_errors=0
migrate_status_file=$(make_tmp migrate-status)
migrate_err_file=$(make_tmp migrate-status-err)
while true; do
  if (( SECONDS - migrate_started >= migrate_timeout )); then
    printf 'Timed out after %s seconds waiting for the migrate Job to succeed or fail.\n' "$migrate_timeout" >&2
    exit 1
  fi
  if kubectl_for_profile -n "$NAMESPACE" get job migrate -o jsonpath='{.status.succeeded} {.status.failed}' >"$migrate_status_file" 2>"$migrate_err_file"; then
    migrate_errors=0
    migrate_status=$(<"$migrate_status_file")
    # "<succeeded> <failed>". A missing field is empty. `read` strips the
    # leading space and would treat a failed-only status (" 1") as succeeded.
    if [[ "$migrate_status" == *" "* ]]; then
      migrate_succeeded=${migrate_status%% *}
      migrate_failed=${migrate_status#* }
    else
      migrate_succeeded=
      migrate_failed=
    fi
    if [[ "$migrate_succeeded" =~ ^[0-9]+$ ]] && (( migrate_succeeded >= 1 )); then
      kubectl_for_profile -n "$NAMESPACE" logs job/migrate || true
      break
    fi
    if [[ "$migrate_failed" =~ ^[0-9]+$ ]] && (( migrate_failed >= 1 )); then
      printf 'Migration Job failed; Alembic output follows.\n' >&2
      kubectl_for_profile -n "$NAMESPACE" logs job/migrate >&2 || true
      exit 1
    fi
  else
    # A single blip is retried. Three consecutive get failures are the error.
    migrate_errors=$((migrate_errors + 1))
    if (( migrate_errors >= 3 )); then
      if [[ -s "$migrate_err_file" ]]; then
        cat "$migrate_err_file" >&2
      fi
      if [[ -s "$migrate_status_file" ]]; then
        cat "$migrate_status_file" >&2
      fi
      exit 1
    fi
  fi
  sleep 2
done

kubectl_for_profile -n "$NAMESPACE" scale statefulset/worker --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/worker --timeout=10m
kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status deployment/api --timeout=5m
kubectl_for_profile -n "$NAMESPACE" scale deployment/studio --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status deployment/studio --timeout=5m

printf 'Deployed %s and %s to %s.\n' "$python_image" "$studio_image" "$EXPECTED_CONTEXT"
