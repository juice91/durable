#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

SECRETS_ENV=""
OP_REFS=""
while (($#)); do
  case "$1" in
    --secrets-env) SECRETS_ENV=${2:?missing path after --secrets-env}; shift 2 ;;
    --op-refs) OP_REFS=${2:?missing path after --op-refs}; shift 2 ;;
    *) printf 'Usage: %s [--secrets-env FILE | --op-refs FILE]\n' "$0" >&2; exit 64 ;;
  esac
done
if [[ -n "$SECRETS_ENV" && -n "$OP_REFS" ]]; then
  printf 'Choose only one secrets input.\n' >&2
  exit 64
fi

# Refuse a broad, symlinked, or writable-by-others data directory before touching Colima.
validate_and_prepare_data_dir

tmp_files=()
secret_file="" rendered_file="" migrate_status_file="" migrate_err_file="" runs_err_file=""
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
# A signal must end the script (running the EXIT trap) even while a foreground child such as
# `docker build` is still going; bash otherwise carries on once that child exits normally.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# make_tmp NAME VAR: create a mode-600 temp file and assign its path to VAR. It must run in this
# shell, not in $(...): a subshell would drop the registration and the trap would never remove it.
make_tmp() {
  local path
  path=$(mktemp "${TMPDIR:-/tmp}/pydanticos-$1.XXXXXX")
  chmod 0600 "$path"
  tmp_files+=("$path")
  printf -v "$2" '%s' "$path"
}

# Keys the Secret must carry. The two URLs are derived from the passwords unless supplied.
base_keys=(POSTGRES_PASSWORD EAP_APP_DATABASE_PASSWORD EAP_DBOS_DATABASE_PASSWORD EAP_SECRETS_KEY EAP_LITELLM_API_KEY EAP_LITELLM_EMBEDDING_API_KEY EAP_AUDIT_HASH_SALT EAP_BLOB_S3_ACCESS_KEY EAP_BLOB_S3_SECRET_KEY)
postgres_host=postgres.pydanticos.svc.cluster.local

# Value of KEY in a dotenv file (first match, everything after the first "="). Used only to fill
# shell variables; callers never print it.
dotenv_get() {
  awk -v k="$2" 'index($0, k "=") == 1 {print substr($0, length(k) + 2); exit}' "$1"
}

for command_name in colima docker kubectl git mktemp sed awk realpath stat; do
  require_command "$command_name"
done

# Resolve the 1Password references before anything else touches Colima, so a missing `op`
# or token fails fast. Only `op` ever sees the service-account token: it is unset right after.
op_file=""
if [[ -n "$OP_REFS" ]]; then
  require_command op
  [[ -r "$OP_REFS" ]] || { printf 'The --op-refs file is not readable: %s\n' "$OP_REFS" >&2; exit 1; }
  [[ -n "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]] || {
    printf 'OP_SERVICE_ACCOUNT_TOKEN is not set. Put the 1Password service-account token in runner.env, or export it.\n' >&2
    exit 1
  }
  op whoami >/dev/null || { printf '1Password rejected the service-account token (op whoami failed).\n' >&2; exit 1; }
  make_tmp op-secrets op_file
  # --force: mktemp already created the file. The values go only to the mode-600 file.
  op inject --force --file-mode 0600 -i "$OP_REFS" -o "$op_file" >/dev/null \
    || { printf 'op inject failed; check the vault, item and field names in %s.\n' "$OP_REFS" >&2; exit 1; }
  unset OP_SERVICE_ACCOUNT_TOKEN
  # inject keeps one output line per KEY=ref line. A secret value containing a newline would add
  # lines, which `--from-env-file` would read as extra variables; refuse without echoing anything.
  [[ "$(awk '!/^[[:space:]]*(#|$)/ {n++} END {print n + 0}' "$OP_REFS")" == "$(awk '!/^[[:space:]]*(#|$)/ {n++} END {print n + 0}' "$op_file")" ]] || {
    printf 'A 1Password value contains a newline, so the injected file has a different number of KEY=value lines than %s. Refusing to use it.\n' "$OP_REFS" >&2
    exit 1
  }
fi

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

# The VM must outlive this script. Under a GitHub Actions self-hosted runner, every process a job
# starts is tagged with RUNNER_TRACKING_ID and killed when the job ends ("Terminate orphan
# process: colima, limactl"), which stopped the VM right after the first successful deploy.
# Starting Colima with RUNNER_TRACKING_ID cleared keeps its host processes out of that cleanup.
if colima status "$PROFILE" >/dev/null 2>&1; then
  assert_running_mount_is_only_data_dir
# One awk reads all of `colima list`; a `| grep -q` here could SIGPIPE the producer, turn the
# condition false under pipefail and wrongly create a new profile over an existing one.
elif colima_profiles=$(colima list 2>/dev/null) \
  && awk -v p="$PROFILE" 'NR > 1 && $1 == p {found = 1} END {exit !found}' <<<"$colima_profiles"; then
  RUNNER_TRACKING_ID="" XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false --mount "$mount_arg"
else
  RUNNER_TRACKING_ID="" XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false --mount "$mount_arg" --kubernetes --runtime docker --cpu 8 --memory 32 --disk 100
fi

guard_contexts

# The ToolHive operator runs managed MCP servers; idempotent, downloads the pinned helm release into ~/.local/bin if missing.
# PYDANTICOS_SKIP_TOOLHIVE=1 skips it (the stub-based script tests, which have no helm).
if [[ "${PYDANTICOS_SKIP_TOOLHIVE:-}" != 1 ]]; then
  "${SCRIPT_DIR}/toolhive.sh" install --context "$EXPECTED_CONTEXT"
fi

git_sha=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
python_image="pydanticos-python:${git_sha}"
studio_image="pydanticos-studio:${git_sha}"
# Platform runner images for managed MCP servers (npm and pypi packages install at pod start). The
# worker finds them through EAP_MCP_RUNNER_NODE_IMAGE / EAP_MCP_RUNNER_PYTHON_IMAGE.
# Their tag is a hash of the files each image is built from (the same one
# eap_mcp_runtime.images computes), not the commit: a code commit does not rebuild or retag them.
runner_hash() { # FILE... (relative to deploy/docker)
  local f
  for f in "$@"; do cat "$REPO_ROOT/deploy/docker/$f"; done | shasum -a 256 | cut -c1-12
}
runner_node_tag=$(runner_hash mcp-runner-node.Dockerfile)
runner_python_tag=$(runner_hash mcp-runner-python.Dockerfile mcp-run-python.sh)
runner_node_image="pydanticos-mcp-runner-node:${runner_node_tag}"
runner_python_image="pydanticos-mcp-runner-python:${runner_python_tag}"

# Right after the VM (re)starts its network can lag: a deploy failed with an i/o timeout to
# registry-1.docker.io that worked seconds later. Wait (bounded) until the VM reaches the
# registries the Dockerfiles pull from, then build, retrying a failed build once.
registry_wait=${PYDANTICOS_REGISTRY_WAIT:-90}
registry_started=$SECONDS
until colima ssh -p "$PROFILE" -- curl -s -o /dev/null -m 5 https://registry-1.docker.io/v2/ >/dev/null 2>&1; do
  if (( SECONDS - registry_started >= registry_wait )); then
    printf 'Warning: the VM could not reach registry-1.docker.io within %s seconds; building anyway.\n' "$registry_wait" >&2
    break
  fi
  sleep 3
done

build_image() { # DOCKERFILE TAG
  local attempt
  for attempt in 1 2; do
    if docker_for_profile build --platform linux/arm64 -f "$REPO_ROOT/deploy/docker/$1" -t "$2" "$REPO_ROOT"; then
      return 0
    fi
    (( attempt == 2 )) || { printf 'Image build for %s failed; retrying once in 15 seconds.\n' "$1" >&2; sleep 15; }
  done
  return 1
}
build_image python.Dockerfile "$python_image"
build_image studio.Dockerfile "$studio_image"
# The runner Dockerfiles need no repo files; a small context keeps the builds fast.
build_runner_image() { # DOCKERFILE TAG
  local attempt
  for attempt in 1 2; do
    if docker_for_profile build --platform linux/arm64 -f "$REPO_ROOT/deploy/docker/$1" -t "$2" "$REPO_ROOT/deploy/docker"; then
      return 0
    fi
    (( attempt == 2 )) || { printf 'Image build for %s failed; retrying once in 15 seconds.\n' "$1" >&2; sleep 15; }
  done
  return 1
}
build_runner_image mcp-runner-node.Dockerfile "$runner_node_image"
build_runner_image mcp-runner-python.Dockerfile "$runner_python_image"

kubectl_for_profile apply -f "$REPO_ROOT/deploy/k8s/namespace.yaml"

# Collect the secrets into one mode-600 file (never the operator's own file, which is only read).
source_file=""
make_tmp secrets secret_file
if [[ -n "$op_file" ]]; then
  source_file=$op_file
elif [[ -n "$SECRETS_ENV" ]]; then
  [[ -r "$SECRETS_ENV" ]] || { printf 'Secrets input is not readable.\n' >&2; exit 1; }
  source_file=$SECRETS_ENV
else
  for key in "${base_keys[@]}" EAP_DATABASE_URL EAP_DBOS_URL; do
    [[ -z "${!key:-}" ]] || printf '%s=%s\n' "$key" "${!key}" >>"$secret_file"
  done
fi
if [[ -n "$source_file" ]]; then
  cat -- "$source_file" >>"$secret_file"
  # A trailing newline keeps the appended derived lines on their own line.
  [[ -z "$(tail -c1 "$secret_file")" ]] || printf '\n' >>"$secret_file"
fi

for key in "${base_keys[@]}"; do
  value=$(dotenv_get "$secret_file" "$key")
  if [[ -z "$value" ]]; then
    case "$key" in
      EAP_BLOB_*) printf 'Required secret is missing or empty: %s. Attachment bytes live in Cloudflare R2: create the bucket and an S3 API token and fill the two EAP_BLOB_* credential fields (deploy/README.md, "Object storage").\n' "$key" >&2 ;;
      *) printf 'Required secret is missing or empty: %s\n' "$key" >&2 ;;
    esac
    exit 1
  fi
  # An unreplaced op-bootstrap placeholder must never reach the cluster.
  [[ "$value" != CHANGE_ME* ]] || { printf 'Secret %s is still a placeholder; run litellm-deploy-key.sh.\n' "$key" >&2; exit 1; }
done

# EAP_DATABASE_URL / EAP_DBOS_URL follow from the roles in deploy/k8s/configmap.yaml (eap_app,
# eap_dbos), their databases, and the in-cluster Postgres service. They are derived unless a
# non-empty value was supplied, so 1Password stores only the passwords.
derive_url() {
  local key=$1 scheme=$2 role=$3 password_key=$4 database=$5 password
  [[ -z "$(dotenv_get "$secret_file" "$key")" ]] || return 0
  password=$(dotenv_get "$secret_file" "$password_key")
  # Passwords are put into a URL without encoding, so only URL-safe characters are allowed.
  [[ "$password" =~ ^[A-Za-z0-9._~-]+$ ]] || {
    printf '%s may only contain letters, digits and ._~- so it can be embedded in %s.\n' "$password_key" "$key" >&2
    exit 1
  }
  printf '%s=%s://%s:%s@%s:5432/%s\n' "$key" "$scheme" "$role" "$password" "$postgres_host" "$database" >>"$secret_file"
}
derive_url EAP_DATABASE_URL postgresql+psycopg eap_app EAP_APP_DATABASE_PASSWORD eap_app
derive_url EAP_DBOS_URL postgresql eap_dbos EAP_DBOS_DATABASE_PASSWORD eap_dbos

kubectl_for_profile -n "$NAMESPACE" create secret generic pydanticos-secrets \
  --from-env-file="$secret_file" --dry-run=client -o yaml | kubectl_for_profile apply -f - >/dev/null

# Drain before applying a new worker template. See apps/worker/README.md ("drain, keep m1").
# A run parked on a human approval (a pending_approvals row without decided_at) is not executing and
# may wait for days; it is left out of the count and recovers across the deploy (its step layout is
# unchanged while it waits). Counting it blocked every deploy on 2026-11-19 while the API was already
# scaled to 0, so nobody could decide the approval either.
if kubectl_for_profile -n "$NAMESPACE" get deployment/api >/dev/null 2>&1; then
  kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=0
fi

# Registered here, in the main shell: query_active_runs runs inside $(...), where a registration
# would be lost and the file would outlive the script.
make_tmp runs-query runs_err_file
query_active_runs() {
  local output err rc
  err=$runs_err_file
  set +e
  # Single quotes are deliberate: the postgres container expands its own password.
  # shellcheck disable=SC2016
  output=$(kubectl_for_profile -n "$NAMESPACE" exec postgres-0 -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d eap_app -v ON_ERROR_STOP=1 -tAc "SELECT count(*) FROM public.runs r WHERE r.status IN ('"'"'queued'"'"', '"'"'running'"'"') AND NOT EXISTS (SELECT 1 FROM public.pending_approvals a WHERE a.run_id = r.id AND a.decided_at IS NULL)"' \
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

# The API server addresses NetworkPolicies see (after Service NAT): the `kubernetes` Service ClusterIP
# and its endpoint, read from the cluster rather than assumed. networkpolicy.yaml and configmap.yaml
# carry the k3s/colima defaults (10.43.0.1, 192.168.5.3), replaced here via tokens so one value
# cannot be rewritten by another's substitution. Exported for anything run from this script.
api_service_ip=$(kubectl_for_profile --namespace=default get svc kubernetes -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)
api_endpoint_ips=$(kubectl_for_profile --namespace=default get endpoints kubernetes -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)
ipv4_re='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
[[ "$api_service_ip" =~ $ipv4_re ]] || {
  printf 'Refusing to continue: could not read the kubernetes Service address (kubectl get svc kubernetes).\n' >&2
  exit 1
}
read -r -a api_endpoint_list <<<"$api_endpoint_ips"
if [[ ${#api_endpoint_list[@]} -ne 1 || ! "${api_endpoint_list[0]}" =~ $ipv4_re ]]; then
  printf 'Refusing to continue: expected exactly one kubernetes endpoint address (kubectl get endpoints kubernetes), got: %s\n' "${api_endpoint_ips:-none}" >&2
  exit 1
fi
api_endpoint_ip=${api_endpoint_list[0]}
# Policies see the endpoint port after Service NAT: 6443 on stock Kubernetes, 57345 on colima's k3s.
api_endpoint_port=$(kubectl_for_profile --namespace=default get endpoints kubernetes -o jsonpath='{.subsets[0].ports[0].port}' 2>/dev/null || true)
[[ "$api_endpoint_port" =~ ^[0-9]{1,5}$ ]] || {
  printf 'Refusing to continue: could not read the kubernetes endpoint port (kubectl get endpoints kubernetes).\n' >&2
  exit 1
}
export EAP_MCP_API_SERVER_CIDRS="${api_service_ip}/32,${api_endpoint_ip}/32"
export EAP_MCP_API_SERVER_PORTS="443,${api_endpoint_port}"

make_tmp rendered rendered_file
kubectl_for_profile kustomize "$REPO_ROOT/deploy/k8s" \
  | sed \
    -e "s|pydanticos-python:dev|${python_image}|g" \
    -e "s|pydanticos-studio:dev|${studio_image}|g" \
    -e "s|/var/lib/pydanticos-backups|${PYDANTICOS_BACKUP_DIR}|g" \
    -e "s|10\.43\.0\.1/32|@@API_SERVICE_CIDR@@|g" \
    -e "s|192\.168\.5\.3/32|@@API_ENDPOINT_CIDR@@|g" \
    -e "s|192.168.5.2|${host_gateway}|g" \
    -e "s|@@API_SERVICE_CIDR@@|${api_service_ip}/32|g" \
    -e "s|@@API_ENDPOINT_CIDR@@|${api_endpoint_ip}/32|g" \
    -e "s|port: 6443$|port: ${api_endpoint_port}|g" \
    -e "s|EAP_MCP_API_SERVER_PORTS: 443,6443|EAP_MCP_API_SERVER_PORTS: 443,${api_endpoint_port}|g" \
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
make_tmp migrate-status migrate_status_file
make_tmp migrate-status-err migrate_err_file
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
