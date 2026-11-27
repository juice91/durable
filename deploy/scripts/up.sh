#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

SECRETS_ENV=""
while (($#)); do
  case "$1" in
    --secrets-env) SECRETS_ENV=${2:?missing path after --secrets-env}; shift 2 ;;
    *) printf 'Usage: %s [--secrets-env FILE]\n' "$0" >&2; exit 64 ;;
  esac
done

# Refuse a broad, symlinked, or writable-by-others data directory before touching Colima.
validate_and_prepare_data_dir

tmp_files=()
secret_file="" rendered_file="" migrate_status_file="" migrate_err_file="" migrate_log_file="" runs_err_file="" platform_secret_file="" iam_rendered_file="" zitadel_secret_file="" openfga_secret_file=""
# Set by the deploy stages below; read by the EXIT trap, so that a failure after a scale-down puts the platform back.
platform_armed=0 iam_armed=0 marker_set=0
cleanup() {
  local status=$? path
  # A second signal (a cancelled workflow sends two) must not cut the restore short.
  trap '' INT TERM HUP
  if ((status != 0 && platform_armed)); then restore_platform || true; fi
  if ((status != 0 && iam_armed)); then restore_iam || true; fi
  if ((marker_set)); then remove_deploy_marker || true; fi
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


# Writable mounts: the data directory, plus the backup directory when it lies outside it (a folder on an external volume,
# PYDANTICOS_BACKUP_DIR). Colima 0.10 treats an empty mounts list as "mount ~" (yaml.go: Mounts != nil && len == 0).
# Passing --mount replaces the built-in home mount; it is not added beside it. Colima also injects its cache
# directory unless that path is inside a configured mount, so the cache is
# placed under the data directory via XDG_CACHE_HOME for this command only.
mount_args=(--mount "${PYDANTICOS_DATA_DIR}:w")
backup_outside_data=0
if [[ "${PYDANTICOS_BACKUP_DIR}/" != "${PYDANTICOS_DATA_DIR}/"* ]]; then
  backup_outside_data=1
  mount_args+=(--mount "${PYDANTICOS_BACKUP_DIR}:w")
fi
cache_home="${PYDANTICOS_DATA_DIR}/xdg-cache"
colima_config="${COLIMA_HOME:-${HOME}/.colima}/${PROFILE}/colima.yaml"

# Before anything touches the VM or builds an image: the host volume holding ~/.colima must have room.
host_disk_preflight
warn_unpinned_kube_port "$colima_config"

# Broad host locations that must never be mounted into the VM.
mount_is_broad() { # LOCATION
  local loc=${1%/} parent
  [[ -n "$loc" && "$loc" != "${HOME%/}" && "$loc" != /Users && "$loc" != /Users/Shared && "$loc" != "${PYDANTICOS_VOLUMES_DIR:-/Volumes}" ]] || return 0
  parent=$(dirname -- "$(realpath "$HOME")")
  while [[ "$parent" != "/" ]]; do
    [[ "$loc" != "$parent" ]] || return 0
    parent=$(dirname -- "$parent")
  done
  return 1
}

# True when LOCATION is PATH or one of its parents.
mount_covers() { # LOCATION PATH
  local loc=${1%/}
  [[ "$2" == "$loc" || "$2" == "$loc"/* ]]
}

# An existing profile's colima.yaml is only read, never rewritten. Its mounts must be exactly what the deployment needs:
# a writable mount of the data directory, and (backup directory outside it) a writable mount of the backup directory or
# of a folder or volume that contains it. Anything else, such as the home directory, is refused. A missing one is
# reported with the YAML to add.
assert_running_mounts_match() {
  local line location writable loc ok_data=0 ok_backup=0 extra=() lacking=() yaml=""
  if [[ ! -f "$colima_config" ]]; then
    printf 'Refusing to start: profile %s is already running but %s is missing, so its host mounts cannot be verified. Stop the profile and rerun.\n' "$PROFILE" "$colima_config" >&2
    exit 1
  fi
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    location=${line%%|*}
    writable=${line#*|}
    loc=${location%/}
    if [[ "$loc" == "$PYDANTICOS_DATA_DIR" ]]; then
      [[ "$writable" == true ]] && ok_data=1
    elif (( backup_outside_data )) && ! mount_is_broad "$loc" && mount_covers "$loc" "$PYDANTICOS_BACKUP_DIR"; then
      [[ "$writable" == true ]] && ok_backup=1
    else
      extra+=("$location")
    fi
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
  if ((${#extra[@]})); then
    printf 'Refusing to start: profile %s is running with host mounts other than the data directory%s (%s). Stop it and rerun so Colima replaces them with only what is needed.\n' \
      "$PROFILE" "$( ((backup_outside_data)) && printf ' and the backup directory %s' "$PYDANTICOS_BACKUP_DIR")" "${extra[*]}" >&2
    exit 1
  fi
  (( ok_data )) || { lacking+=("$PYDANTICOS_DATA_DIR"); yaml+=$'  - location: '"${PYDANTICOS_DATA_DIR}"$'\n    writable: true\n'; }
  if (( backup_outside_data && ! ok_backup )); then
    lacking+=("$PYDANTICOS_BACKUP_DIR")
    yaml+=$'  - location: '"${PYDANTICOS_BACKUP_DIR}"$'\n    writable: true\n'
  fi
  if ((${#lacking[@]})); then
    printf 'Refusing to start: profile %s is running but %s has no writable mount for: %s.\nStop the profile (colima stop %s), add the following under "mounts:" in that file (colima.yaml is not rewritten by this script), then rerun:\n\nmounts:\n%s\n' \
      "$PROFILE" "$colima_config" "${lacking[*]}" "$PROFILE" "$yaml" >&2
    exit 1
  fi
}

# The VM must outlive this script. Under a GitHub Actions self-hosted runner, every process a job
# starts is tagged with RUNNER_TRACKING_ID and killed when the job ends ("Terminate orphan
# process: colima, limactl"), which stopped the VM right after the first successful deploy.
# Starting Colima with RUNNER_TRACKING_ID cleared keeps its host processes out of that cleanup.
if colima status "$PROFILE" >/dev/null 2>&1; then
  assert_running_mounts_match
# One awk reads all of `colima list`; a `| grep -q` here could SIGPIPE the producer, turn the
# condition false under pipefail and wrongly create a new profile over an existing one.
elif colima_profiles=$(colima list 2>/dev/null) \
  && awk -v p="$PROFILE" 'NR > 1 && $1 == p {found = 1} END {exit !found}' <<<"$colima_profiles"; then
  RUNNER_TRACKING_ID="" XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false "${mount_args[@]}"
else
  # A new cluster encrypts Secrets at rest from its first start (--secrets-encryption). Passing --k3s-arg replaces
  # Colima's built-in --disable=traefik, so that one is repeated. An existing cluster is converted once with
  # deploy/scripts/k3s-secrets-encryption.sh.
  RUNNER_TRACKING_ID="" XDG_CACHE_HOME="$cache_home" colima start "$PROFILE" --activate=false "${mount_args[@]}" --kubernetes --runtime docker --cpu 8 --memory 32 --disk 100 \
    --k3s-arg=--disable=traefik --k3s-arg=--secrets-encryption
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

build_image() { # DOCKERFILE TAG [docker build args...]
  local attempt dockerfile=$1 tag=$2
  shift 2
  for attempt in 1 2; do
    if docker_for_profile build --platform linux/arm64 -f "$REPO_ROOT/deploy/docker/$dockerfile" -t "$tag" "$@" "$REPO_ROOT"; then
      return 0
    fi
    (( attempt == 2 )) || { printf 'Image build for %s failed; retrying once in 15 seconds.\n' "$dockerfile" >&2; sleep 15; }
  done
  return 1
}
# The image's platform_version is <project version>+<this sha>; the deploy workflow passes the commit it deploys.
build_sha=${EAP_BUILD_SHA:-$git_sha}
build_image python.Dockerfile "$python_image" --build-arg "EAP_BUILD_SHA=${build_sha:0:12}"
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

# Keep the VM's build cache bounded: it reached 61 GB and filled the host disk (2026-11-23). Cache used in the last
# PYDANTICOS_BUILDER_PRUNE_AGE (default 24h) stays, so the layers just built and the next rebuild are warm.
builder_prune_age=${PYDANTICOS_BUILDER_PRUNE_AGE:-24h}
if [[ "$builder_prune_age" =~ ^[0-9]+[hm]$ ]]; then
  docker_for_profile builder prune --all --force --filter "until=${builder_prune_age}" \
    || printf 'Warning: pruning the build cache in the VM failed; the deploy carries on.\n' >&2
else
  printf 'Warning: PYDANTICOS_BUILDER_PRUNE_AGE must look like 24h or 90m; not pruning the build cache.\n' >&2
fi

kubectl_for_profile apply -f "$REPO_ROOT/deploy/k8s/namespace.yaml"
kubectl_for_profile apply -f "$REPO_ROOT/deploy/k8s/iam/namespace.yaml"

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

# Substitutions shared by every manifest set rendered below (platform, iam): the Lima gateway and the API server
# addresses read above replace the committed k3s/colima defaults. Tokens first, so one value cannot be rewritten by
# another's substitution.
cluster_sed=(
  -e "s|10\.43\.0\.1/32|@@API_SERVICE_CIDR@@|g"
  -e "s|192\.168\.5\.3/32|@@API_ENDPOINT_CIDR@@|g"
  -e "s|192.168.5.2|${host_gateway}|g"
  -e "s|@@API_SERVICE_CIDR@@|${api_service_ip}/32|g"
  -e "s|@@API_ENDPOINT_CIDR@@|${api_endpoint_ip}/32|g"
  -e "s|port: 6443$|port: ${api_endpoint_port}|g"
)


# Collect the secrets into one mode-600 file (never the operator's own file, which is only read).
source_file=""
make_tmp secrets secret_file
if [[ -n "$SECRETS_ENV" ]]; then
  [[ -r "$SECRETS_ENV" ]] || { printf 'Secrets input is not readable.\n' >&2; exit 1; }
  source_file=$SECRETS_ENV
else
  for key in "${base_keys[@]}" EAP_DATABASE_URL EAP_DBOS_URL; do
    [[ -z "${!key:-}" ]] || printf '%s=%s\n' "$key" "${!key}" >>"$secret_file"
  done
  for key in EAP_LOCAL_ADMIN_USERNAME EAP_LOCAL_ADMIN_INITIAL_PASSWORD EAP_LOCAL_AUTH_SIGNING_KEY; do
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
      EAP_BLOB_*) printf 'Required secret is missing or empty: %s. Attachment bytes live in S3-compatible object storage: create the bucket and an access key and fill the two EAP_BLOB_* credential fields (deploy/k8s/secrets.example.env).\n' "$key" >&2 ;;
      *) printf 'Required secret is missing or empty: %s\n' "$key" >&2 ;;
    esac
    exit 1
  fi
  # An unreplaced placeholder must never reach the cluster.
  [[ "$value" != CHANGE_ME* ]] || { printf 'Secret %s is still a placeholder.\n' "$key" >&2; exit 1; }
done

# The built-in admin account (the ConfigMap's EAP_IAM_IDENTITY lists localadmin): the signing key is generated here
# when neither the secrets input nor the cluster's Secret has one (never overwritten, never printed). Half of the
# optional account pair stops the deploy now, before anything is drained: the API would refuse to start.
local_admin=""
if grep -Eq '^[[:space:]]*EAP_IAM_IDENTITY:.*localadmin' "${REPO_ROOT}/deploy/k8s/configmap.yaml"; then
  local_admin=1
  # shellcheck source=deploy/scripts/local-admin.sh
  source "${SCRIPT_DIR}/local-admin.sh"
  ensure_cluster_signing_key "$secret_file"
  if [[ -z "$(dotenv_get "$secret_file" EAP_LOCAL_ADMIN_USERNAME)" && -n "$(dotenv_get "$secret_file" EAP_LOCAL_ADMIN_INITIAL_PASSWORD)" ]]; then
    printf 'EAP_LOCAL_ADMIN_INITIAL_PASSWORD is set without EAP_LOCAL_ADMIN_USERNAME: set both, or empty both to create the account in Studio (docs/admin-account.md).\n' >&2
    exit 1
  fi
fi

# EAP_DATABASE_URL / EAP_DBOS_URL follow from the roles in deploy/k8s/configmap.yaml (eap_app,
# eap_dbos), their databases, and the in-cluster Postgres service. They are derived unless a
# non-empty value was supplied, so the secrets file needs only the passwords.
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

# Identity (ZITADEL) and authorization (OpenFGA), namespace iam. Each service is optional and independent: with none of
# its fields the deploy still succeeds and it stays at 0 replicas; with only some of them the deploy stops (a
# half-configured identity service must not start). Values are never printed, only field names.
zitadel_keys=(ZITADEL_MASTERKEY ZITADEL_DB_PASSWORD ZITADEL_ADMIN_USERNAME ZITADEL_ADMIN_PASSWORD)
openfga_keys=(OPENFGA_DB_PASSWORD OPENFGA_PRESHARED_KEY)
iam_service_state() { # KEY... -> prints on, off; exits 1 when only some of the fields are set
  local key set=0 missing=""
  for key in "$@"; do
    if [[ -n "$(dotenv_get "$secret_file" "$key")" ]]; then set=$((set + 1)); else missing+="${missing:+, }$key"; fi
  done
  if ((set == 0)); then printf 'off\n'; return 0; fi
  if ((set == $#)); then printf 'on\n'; return 0; fi
  printf 'Incomplete identity configuration: set all of %s or none; missing or empty: %s.\n' "$*" "$missing" >&2
  return 1
}
require_url_safe() { # KEY MIN_LENGTH
  local value
  value=$(dotenv_get "$secret_file" "$1")
  [[ "$value" =~ ^[A-Za-z0-9._~-]+$ && ${#value} -ge $2 ]] || {
    printf '%s may only contain letters, digits and ._~- and must have at least %s characters.\n' "$1" "$2" >&2
    exit 1
  }
}
zitadel_state=$(iam_service_state "${zitadel_keys[@]}") || exit 1
openfga_state=$(iam_service_state "${openfga_keys[@]}") || exit 1
if [[ "$zitadel_state" == on ]]; then
  [[ "$(dotenv_get "$secret_file" ZITADEL_MASTERKEY | tr -d '\n' | wc -c | tr -d ' ')" == 32 ]] || {
    printf 'ZITADEL_MASTERKEY must be exactly 32 characters.\n' >&2
    exit 1
  }
  require_url_safe ZITADEL_DB_PASSWORD 16
fi
if [[ "$openfga_state" == on ]]; then
  require_url_safe OPENFGA_DB_PASSWORD 16
  require_url_safe OPENFGA_PRESHARED_KEY 16
fi

# One Secret per component, the same split a secret manager would produce. The platform Secret
# never carries the identity secrets, except the two database passwords the backup job needs and the OpenFGA preshared
# key the API and the worker present.
  make_tmp platform-secrets platform_secret_file
  grep -v -E '^(ZITADEL_MASTERKEY|ZITADEL_ADMIN_USERNAME|ZITADEL_ADMIN_PASSWORD)=' "$secret_file" >"$platform_secret_file" || true
  make_tmp zitadel-secrets zitadel_secret_file
  grep -E '^ZITADEL_[A-Z_]+=.' "$secret_file" >"$zitadel_secret_file" || true
  make_tmp openfga-secrets openfga_secret_file
  grep -E '^OPENFGA_[A-Z_]+=.' "$secret_file" >"$openfga_secret_file" || true

  kubectl_for_profile -n "$NAMESPACE" create secret generic pydanticos-secrets \
    --from-env-file="$platform_secret_file" --dry-run=client -o yaml | kubectl_for_profile apply -f - >/dev/null
  kubectl_for_profile -n iam create secret generic zitadel-secrets \
    --from-env-file="$zitadel_secret_file" --dry-run=client -o yaml | kubectl_for_profile apply -f - >/dev/null
  kubectl_for_profile -n iam create secret generic openfga-secrets \
    --from-env-file="$openfga_secret_file" --dry-run=client -o yaml | kubectl_for_profile apply -f - >/dev/null
  # iam-secrets held both services' fields before the split; nothing reads it any more.
  kubectl_for_profile -n iam delete secret iam-secrets --ignore-not-found >/dev/null

# A hash of the values each workload reads, stamped on its pod template (@@CHECKSUM_*@@ in the manifests): a rotated
# value changes the pod spec, so the next deploy restarts exactly the workloads that read it, under the drain above
# them. Postgres is left out on purpose: it reads its role passwords only when its volume is first initialised, so a
# restart would only cause an outage. Only a truncated SHA-256 of the values is used; no value is printed.
secrets_checksum() { # KEY...
  local key
  for key in "$@"; do printf '%s=%s\n' "$key" "$(dotenv_get "$secret_file" "$key")"; done | shasum -a 256 | cut -c1-16
}
checksum_platform=$(secrets_checksum EAP_LOCAL_ADMIN_USERNAME EAP_LOCAL_ADMIN_INITIAL_PASSWORD EAP_LOCAL_AUTH_SIGNING_KEY EAP_DATABASE_URL EAP_DBOS_URL EAP_SECRETS_KEY EAP_AUDIT_HASH_SALT EAP_BLOB_S3_ACCESS_KEY EAP_BLOB_S3_SECRET_KEY EAP_LITELLM_API_KEY EAP_LITELLM_EMBEDDING_API_KEY ANTHROPIC_API_KEY EAP_ZITADEL_MACHINE_KEY EAP_ZITADEL_PROJECT_ID EAP_ZITADEL_ORG_TENANT_MAP EAP_ZITADEL_STUDIO_CLIENT_ID OPENFGA_PRESHARED_KEY)
# Studio reads only its two OIDC values from the Secret (the switch and the issuer come from the ConfigMap).
checksum_studio=$(secrets_checksum EAP_ZITADEL_STUDIO_CLIENT_ID EAP_ZITADEL_PROJECT_ID EAP_LOCAL_ADMIN_USERNAME)
# The platform manifests' placeholders (the identity ones are substituted where that directory is rendered).
checksum_sed=(
  -e "s|@@CHECKSUM_PLATFORM@@|${checksum_platform}|g"
  -e "s|@@CHECKSUM_STUDIO@@|${checksum_studio}|g"
)
checksum_zitadel=$(secrets_checksum "${zitadel_keys[@]}")
checksum_openfga=$(secrets_checksum "${openfga_keys[@]}")

# Drain before applying a new worker template. See apps/worker/README.md ("drain, keep m1").
# Not counted, because they do not block a deploy:
#  - A run parked on a human approval (a pending_approvals row without decided_at) is not executing and
#    may wait for days; its step layout is unchanged while it waits. Counting it blocked every deploy on
#    2026-11-19 while the API was already scaled to 0, so nobody could decide the approval either.
#  - Runs whose kind is in PYDANTICOS_DRAIN_EXCLUDE_KINDS (comma list; kb_ingest when unset). Knowledge-base
#    ingestion is idempotent per document and recovers across a worker restart, and a catalogue sync
#    queues hundreds of them (868 on 2026-11-19, two at a time), so waiting for them failed the deploy.
#    A release that changes the eap.kb_ingest* step names must set the variable to "" for that deploy.
# PYDANTICOS_SKIP_DRAIN=1 skips the wait entirely (the Deploy Studio skip_drain input): the API stays up
# until right before the migration and is scaled back up at the end.
exclude_kinds=${PYDANTICOS_DRAIN_EXCLUDE_KINDS-kb_ingest}
exclude_list=""
if [[ -n "${exclude_kinds//[[:space:],]/}" ]]; then
  IFS=',' read -r -a exclude_kind_names <<<"$exclude_kinds"
  for kind_name in "${exclude_kind_names[@]}"; do
    kind_name=${kind_name//[[:space:]]/}
    [[ -n "$kind_name" ]] || continue
    [[ "$kind_name" =~ ^[A-Za-z0-9_.-]+$ ]] || {
      printf 'PYDANTICOS_DRAIN_EXCLUDE_KINDS must be a comma list of run kinds (letters, digits, _ . -); got: %s\n' "$kind_name" >&2
      exit 1
    }
    exclude_list+="${exclude_list:+, }'${kind_name}'"
  done
fi
# Two counts in one row: runs that must drain, and runs of an excluded kind that carry on.
drain_sql="SELECT count(*) FILTER (WHERE true"
excluded_sql="false"
if [[ -n "$exclude_list" ]]; then
  drain_sql+=" AND COALESCE(r.kind, '') NOT IN (${exclude_list})"
  excluded_sql="COALESCE(r.kind, '') IN (${exclude_list})"
fi
drain_sql+="), count(*) FILTER (WHERE ${excluded_sql}) FROM public.runs r WHERE r.status IN ('queued', 'running') AND NOT EXISTS (SELECT 1 FROM public.pending_approvals a WHERE a.run_id = r.id AND a.decided_at IS NULL)"

# ---- No backup during a deploy ----------------------------------------------------------------------------------------
# pg_dump holds ACCESS SHARE on every table until it ends, so a migration that alters a table fails with
# LockNotAvailable (2026-11-26). The deploy first sets the ConfigMap
# deploy-in-progress, which the backup pod mounts (optional volume) and waits on before it dumps, then waits for any
# backup pod that is older than the marker. In that order no backup can start between the check and the migration. A pod
# newer than the marker is waiting on it and is not waited for here. The marker carries an expiry, so a deploy host that
# dies cannot stop backups for good.
deploy_marker=deploy-in-progress
marker_ttl=${PYDANTICOS_DEPLOY_MARKER_TTL:-5400}
# One backup (dumps and upload) took about 17 minutes on 2026-11-26.
backup_wait=${PYDANTICOS_BACKUP_WAIT:-1800}
for setting in marker_ttl backup_wait; do
  [[ "${!setting}" =~ ^[0-9]+$ ]] || {
    printf 'PYDANTICOS_DEPLOY_MARKER_TTL and PYDANTICOS_BACKUP_WAIT must be whole numbers of seconds.\n' >&2
    exit 1
  }
done

remove_deploy_marker() {
  kubectl_for_profile -n "$NAMESPACE" delete configmap "$deploy_marker" --ignore-not-found >/dev/null || {
    printf 'Warning: could not delete the ConfigMap %s; backups wait until it expires. Delete it: kubectl --context %s -n %s delete configmap %s\n' \
      "$deploy_marker" "$EXPECTED_CONTEXT" "$NAMESPACE" "$deploy_marker" >&2
  }
}

# Names of the unfinished postgres-backup pods (nightly or created by hand) that are not newer than the marker.
active_backups() { # MARKER_TIMESTAMP
  local pods
  pods=$(kubectl_for_profile -n "$NAMESPACE" get pods -l app.kubernetes.io/name=postgres-backup \
    -o 'jsonpath={range .items[*]}{.metadata.name} {.status.phase} {.metadata.creationTimestamp}{"\n"}{end}') || return 1
  # RFC 3339 UTC timestamps order as text.
  awk -v marker="$1" '$1 != "" && $2 != "Succeeded" && $2 != "Failed" && $3 <= marker {printf "%s%s", sep, $1; sep=" "}' <<<"$pods"
}

# Delete first: a marker left by an earlier deploy would carry an old creation time.
remove_deploy_marker
marker_set=1
kubectl_for_profile -n "$NAMESPACE" create configmap "$deploy_marker" \
  --from-literal="expires-at=$(($(date +%s) + marker_ttl))" >/dev/null
marker_created=$(kubectl_for_profile -n "$NAMESPACE" get configmap "$deploy_marker" -o 'jsonpath={.metadata.creationTimestamp}')
[[ -n "$marker_created" ]] || { printf 'Could not read the ConfigMap %s back; the deploy stops before touching anything.\n' "$deploy_marker" >&2; exit 1; }

backup_started=$SECONDS
backup_reported=0
while true; do
  backups=$(active_backups "$marker_created") || {
    printf 'Could not list the postgres-backup pods; the deploy stops before touching anything.\n' >&2
    exit 1
  }
  [[ -n "$backups" ]] || break
  if ((SECONDS - backup_started >= backup_wait)); then
    printf 'A postgres-backup job is still running after %s seconds (pods: %s). The deploy stops before touching anything: its locks would fail the migration. Wait for it, or delete the Job (kubectl --context %s -n %s get jobs), then rerun up.sh.\n' \
      "$backup_wait" "$backups" "$EXPECTED_CONTEXT" "$NAMESPACE" >&2
    exit 1
  fi
  if ((! backup_reported)); then
    printf 'Waiting up to %s seconds for the running postgres-backup job to finish (pods: %s).\n' "$backup_wait" "$backups"
    backup_reported=1
  fi
  sleep 5
done

# ---- The release that is running now ----------------------------------------------------------------------------------
# Recorded before anything is scaled or applied, so a failed deploy can put it back.

# Prints "<replicas> <image of CONTAINER>"; nothing when the object does not exist. A read error stops the deploy here.
running_state() { # NAMESPACE KIND/NAME [CONTAINER]
  local jsonpath='{.spec.replicas}'
  [[ -z "${3:-}" ]] || jsonpath+=" {.spec.template.spec.containers[?(@.name==\"$3\")].image}"
  kubectl_for_profile -n "$1" get "$2" --ignore-not-found -o "jsonpath=${jsonpath}"
}
worker_before=$(running_state "$NAMESPACE" statefulset/worker worker)
api_before=$(running_state "$NAMESPACE" deployment/api api)
studio_before=$(running_state "$NAMESPACE" deployment/studio studio)
openfga_before=$(running_state iam deployment/openfga)
zitadel_before=$(running_state iam deployment/zitadel)

api_exists=0
[[ -z "$api_before" ]] || api_exists=1
api_replicas=${api_before%% *}
[[ "$api_replicas" =~ ^[0-9]+$ ]] || api_replicas=1

# ---- Putting the running release back ---------------------------------------------------------------------------------
# How far the deploy got. restore_platform reads these.
worker_stopped=0 manifests_applied=0 migrate_started=0 migrate_succeeded_seen=0 schema_before="" restore_failed=0

# Runs SQL in eap_app and prints the result. The SQL is a positional argument of the remote sh, so it needs no quoting.
psql_app() { # SQL
  # Single quotes are deliberate: the postgres container expands its own password.
  # shellcheck disable=SC2016
  kubectl_for_profile -n "$NAMESPACE" exec postgres-0 -- \
    sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d eap_app -v ON_ERROR_STOP=1 -tAc "$1"' sh "$1"
}

# The core Alembic revision ("none" before the first migration). The API and the worker start only at exactly the
# revision their image was built for, so this decides which images a failed deploy may run.
schema_revision() {
  local output
  if output=$(psql_app "SELECT string_agg(version_num, ',' ORDER BY version_num) FROM public.alembic_version" 2>"$runs_err_file"); then
    output=$(printf '%s' "$output" | tr -d '[:space:]')
    [[ "$output" =~ ^[A-Za-z0-9_,]*$ ]] || return 1
    printf '%s\n' "${output:-none}"
  elif grep -q 'alembic_version" does not exist' "$runs_err_file"; then
    printf 'none\n'
  else
    return 1
  fi
}

restore_step() { # kubectl args...
  kubectl_for_profile "$@" >&2 || { restore_failed=1; printf 'Restore step failed: kubectl %s\n' "$*" >&2; }
}

wait_for_no_worker_pods() { # TIMEOUT_SECONDS
  local started=$SECONDS pods
  while true; do
    pods=$(kubectl_for_profile -n "$NAMESPACE" get pods -l app.kubernetes.io/name=worker --no-headers 2>/dev/null || true)
    [[ -n "${pods//[[:space:]]/}" ]] || return 0
    ((SECONDS - started < $1)) || return 1
    sleep 2
  done
}

# Scales one component back to what it ran, with its previous image when KEEP_NEW is 0 and the manifests were applied.
restore_component() { # KIND/NAME CONTAINER STATE KEEP_NEW
  local target=$1 container=$2 replicas image
  read -r replicas image <<<"$3"
  if [[ -z "$replicas" ]]; then
    printf '%s did not exist before this deploy; nothing to restore.\n' "$target" >&2
    return 0
  fi
  if ((manifests_applied && ! $4)) && [[ -n "$image" ]]; then
    # A StatefulSet does not replace a pod that never became ready when only its template changes: stop it first.
    if [[ "$target" == statefulset/worker ]]; then
      restore_step -n "$NAMESPACE" scale "$target" --replicas=0
      wait_for_no_worker_pods "${PYDANTICOS_WORKER_STOP_TIMEOUT:-180}" || printf 'Worker pods are still terminating.\n' >&2
    fi
    restore_step -n "$NAMESPACE" set image "$target" "${container}=${image}"
  fi
  restore_step -n "$NAMESPACE" scale "$target" --replicas="$replicas"
  if (($4)); then
    printf 'Restored %s to %s replicas with the NEW image.\n' "$target" "$replicas" >&2
  else
    printf 'Restored %s to %s replicas with image %s.\n' "$target" "$replicas" "${image:-unchanged}" >&2
  fi
}

restore_platform() {
  local keep_new=0 schema_after="" attempt replicas
  platform_armed=0
  if ((! worker_stopped)); then
    # Only the API has been touched (the drain).
    if ((api_exists)); then
      kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas="$api_replicas" >&2 || true
      printf 'The API was scaled back to %s.\n' "$api_replicas" >&2
    fi
    return 0
  fi
  printf 'The deploy failed after the platform was scaled down; restoring service.\n' >&2
  if ((migrate_started)); then
    # Stop the migration first: a Job still running could commit after the old images are back.
    restore_step -n "$NAMESPACE" delete job migrate --ignore-not-found --cascade=foreground --wait=true --timeout=3m
    for attempt in 1 2 3; do
      schema_after=$(schema_revision) && break
      ((attempt == 3)) || sleep 3
    done
    if [[ -n "$schema_after" && -n "$schema_before" ]]; then
      [[ "$schema_after" == "$schema_before" ]] || keep_new=1
    else
      # Unreadable: a Job that did not succeed has rolled back (one transaction); one that succeeded has not.
      keep_new=$migrate_succeeded_seen
      printf 'Warning: the database revision could not be read; judging by the migrate Job.\n' >&2
    fi
  fi
  if ((keep_new)); then
    printf 'The database was migrated (%s -> %s). The previous images cannot start on this schema, so the new images stay; fix forward.\n' \
      "${schema_before:-unknown}" "${schema_after:-unknown}" >&2
  else
    printf 'The database schema is unchanged%s: the previous release is put back.\n' "${schema_before:+ (revision ${schema_before})}" >&2
  fi
  restore_component statefulset/worker worker "$worker_before" "$keep_new"
  restore_component deployment/api api "$api_before" "$keep_new"
  restore_component deployment/studio studio "$studio_before" "$keep_new"
  # Every scale first (a cancelled workflow gives only seconds), then wait and report.
  [[ -z "$worker_before" ]] || restore_step -n "$NAMESPACE" rollout status statefulset/worker --timeout=3m
  [[ -z "$api_before" ]] || restore_step -n "$NAMESPACE" rollout status deployment/api --timeout=3m
  [[ -z "$studio_before" ]] || restore_step -n "$NAMESPACE" rollout status deployment/studio --timeout=3m
  if ((restore_failed)); then
    printf 'THE RESTORE IS INCOMPLETE: check the platform now (deploy/scripts/status.sh).\n' >&2
  else
    printf 'Service was restored. The deploy itself failed.\n' >&2
  fi
}

# The iam manifests hold replicas 0 too: put each identity service back, under the same rule as a deploy (up only when
# its secrets are complete). Their images are pinned third-party releases and stay as applied.
restore_iam() {
  local name state before replicas
  iam_armed=0
  for name in openfga zitadel; do
    if [[ "$name" == openfga ]]; then state=$openfga_state before=$openfga_before; else state=$zitadel_state before=$zitadel_before; fi
    replicas=${before%% *}
    [[ -n "$replicas" ]] || continue
    [[ "$state" == on ]] || replicas=0
    kubectl_for_profile -n iam scale "deployment/$name" --replicas="$replicas" >&2 \
      || printf 'Restore step failed: could not scale iam deployment/%s to %s.\n' "$name" "$replicas" >&2
    printf 'Restored iam deployment/%s to %s replicas.\n' "$name" "$replicas" >&2
  done
}

if [[ "${PYDANTICOS_SKIP_DRAIN:-}" == 1 ]]; then
  printf 'Drain skipped (PYDANTICOS_SKIP_DRAIN=1): queued and running runs are not waited for. The API stays up until right before the migration.\n'
elif ((api_exists)); then
  platform_armed=1
  kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=0
fi

# Registered here, in the main shell: query_active_runs runs inside $(...), where a registration
# would be lost and the file would outlive the script.
make_tmp runs-query runs_err_file
# Prints "<active>|<excluded>". The SQL is a positional argument of the remote sh, so it needs no quoting.
query_active_runs() {
  local output err rc
  err=$runs_err_file
  set +e
  output=$(psql_app "$drain_sql" 2>"$err")
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    if grep -Eqi 'does not exist|postgres-0.*not found|NotFound' "$err"; then
      printf '0|0\n'
      return 0
    fi
    printf 'Could not query queued and running runs.\n' >&2
    cat "$err" >&2
    return 1
  fi
  output=$(printf '%s' "$output" | tr -cd '0-9|')
  if [[ "$output" =~ ^[0-9]+\|[0-9]+$ ]]; then
    printf '%s\n' "$output"
  else
    printf '0|0\n'
  fi
}

if [[ "${PYDANTICOS_SKIP_DRAIN:-}" != 1 ]] && kubectl_for_profile -n "$NAMESPACE" get pod/postgres-0 >/dev/null 2>&1; then
  kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/postgres --timeout=5m
  drain_timeout=${PYDANTICOS_DRAIN_TIMEOUT:-300}
  drain_started=$SECONDS
  excluded_reported=0
  while true; do
    counts=$(query_active_runs) || exit 1
    active_runs=${counts%%|*}
    excluded_runs=${counts#*|}
    if ((! excluded_reported && excluded_runs > 0)); then
      printf '%s %s runs continue across the deploy.\n' "$excluded_runs" "${exclude_list//\'/}"
      excluded_reported=1
    fi
    if [[ "$active_runs" == "0" ]]; then
      break
    fi
    if (( SECONDS - drain_started >= drain_timeout )); then
      printf 'Timed out after %s seconds waiting for queued and running runs to finish (%s still active). Scaling the API back to %s replicas so the platform stays up; rerun up.sh once the runs have finished (or set PYDANTICOS_SKIP_DRAIN=1).\n' "$drain_timeout" "$active_runs" "$api_replicas" >&2
      exit 1
    fi
    sleep 5
  done
fi

if [[ -n "$worker_before" ]]; then
  platform_armed=1 worker_stopped=1
  kubectl_for_profile -n "$NAMESPACE" scale statefulset/worker --replicas=0
  # terminationGracePeriodSeconds is 120; wait past that grace period.
  worker_stop_timeout=${PYDANTICOS_WORKER_STOP_TIMEOUT:-180}
  wait_for_no_worker_pods "$worker_stop_timeout" || {
    printf 'Timed out after %s seconds waiting for worker pods to terminate (terminationGracePeriodSeconds is 120). A worker may still be finishing its current step. Rerun up.sh after the pods are gone.\n' "$worker_stop_timeout" >&2
    exit 1
  }
fi

make_tmp rendered rendered_file
kubectl_for_profile kustomize "$REPO_ROOT/deploy/k8s" \
  | sed "${cluster_sed[@]}" \
    -e "s|pydanticos-python:dev|${python_image}|g" \
    -e "s|pydanticos-studio:dev|${studio_image}|g" \
    -e "s|/var/lib/pydanticos-backup-dir|${PYDANTICOS_BACKUP_DIR}|g" \
    -e "s|EAP_MCP_API_SERVER_PORTS: 443,6443|EAP_MCP_API_SERVER_PORTS: 443,${api_endpoint_port}|g" \
    "${checksum_sed[@]}" \
  >"$rendered_file"
if [[ -n "${PYDANTICOS_RENDER_OUT:-}" ]]; then
  cp "$rendered_file" "$PYDANTICOS_RENDER_OUT"
fi

# From here on the whole platform is down or being changed, even when there was no worker to stop.
if [[ -n "${api_before}${worker_before}${studio_before}" ]]; then platform_armed=1 worker_stopped=1; fi
# Skipped drain: the API took new runs until now. Stop it only for the migration window; it comes back
# up at the end, after the workers.
if [[ "${PYDANTICOS_SKIP_DRAIN:-}" == 1 ]] && ((api_exists)); then
  kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=0
fi

# The migrate Job runs Alembic once, in one transaction (packages/db/src/eap_db/migrations/env.py), with a short
# lock_timeout. Only a lock that could not be taken (LockNotAvailable) is worth another attempt.
migrate_timeout=${PYDANTICOS_MIGRATE_TIMEOUT:-600}
lock_attempts=${PYDANTICOS_MIGRATE_LOCK_ATTEMPTS:-3}
lock_delay=${PYDANTICOS_MIGRATE_LOCK_DELAY:-20}
[[ "$lock_attempts" =~ ^[1-9]$ && "$lock_delay" =~ ^[0-9]+$ ]] || {
  printf 'PYDANTICOS_MIGRATE_LOCK_ATTEMPTS must be 1 to 9 and PYDANTICOS_MIGRATE_LOCK_DELAY a number of seconds.\n' >&2
  exit 1
}
make_tmp migrate-status migrate_status_file
make_tmp migrate-status-err migrate_err_file
make_tmp migrate-log migrate_log_file

# Returns 0 when the Job succeeded and 2 when it failed (its log is then in migrate_log_file). Exits on anything else.
wait_for_migrate_job() {
  local started=$SECONDS errors=0 status succeeded failed
  # One foreground poll. `kill` of a background subshell does not kill its
  # `kubectl wait` child, so the losing watch kept running after this script exited.
  while true; do
    if (( SECONDS - started >= migrate_timeout )); then
      printf 'Timed out after %s seconds waiting for the migrate Job to succeed or fail.\n' "$migrate_timeout" >&2
      exit 1
    fi
    if kubectl_for_profile -n "$NAMESPACE" get job migrate -o jsonpath='{.status.succeeded} {.status.failed}' >"$migrate_status_file" 2>"$migrate_err_file"; then
      errors=0
      status=$(<"$migrate_status_file")
      # "<succeeded> <failed>". A missing field is empty. `read` strips the
      # leading space and would treat a failed-only status (" 1") as succeeded.
      if [[ "$status" == *" "* ]]; then
        succeeded=${status%% *}
        failed=${status#* }
      else
        succeeded=
        failed=
      fi
      if [[ "$succeeded" =~ ^[0-9]+$ ]] && (( succeeded >= 1 )); then
        migrate_succeeded_seen=1
        kubectl_for_profile -n "$NAMESPACE" logs job/migrate || true
        return 0
      fi
      if [[ "$failed" =~ ^[0-9]+$ ]] && (( failed >= 1 )); then
        printf 'Migration Job failed; Alembic output follows.\n' >&2
        kubectl_for_profile -n "$NAMESPACE" logs job/migrate >"$migrate_log_file" 2>&1 || true
        cat "$migrate_log_file" >&2
        return 2
      fi
    else
      # A single blip is retried. Three consecutive get failures are the error.
      errors=$((errors + 1))
      if (( errors >= 3 )); then
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
}

migrate_attempt=1
while true; do
  kubectl_for_profile -n "$NAMESPACE" delete job migrate --ignore-not-found --wait=true
  manifests_applied=1
  kubectl_for_profile apply -f "$rendered_file"
  kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/postgres --timeout=5m
  if ((! migrate_started)); then
    schema_before=$(schema_revision) || {
      printf 'Could not read the database revision, so a failed migration could not be judged; not migrating.\n' >&2
      exit 1
    }
  fi
  migrate_started=1
  kubectl_for_profile -n "$NAMESPACE" patch job migrate --type=merge --patch '{"spec":{"suspend":false}}'
  wait_for_migrate_job && break
  ((migrate_attempt < lock_attempts)) && grep -q 'LockNotAvailable' "$migrate_log_file" || exit 1
  migrate_attempt=$((migrate_attempt + 1))
  printf 'The migration could not take a lock (LockNotAvailable); attempt %s of %s in %s seconds.\n' "$migrate_attempt" "$lock_attempts" "$lock_delay" >&2
  sleep "$lock_delay"
done

kubectl_for_profile -n "$NAMESPACE" scale statefulset/worker --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status statefulset/worker --timeout=10m
kubectl_for_profile -n "$NAMESPACE" scale deployment/api --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status deployment/api --timeout=5m
kubectl_for_profile -n "$NAMESPACE" scale deployment/studio --replicas=1
kubectl_for_profile -n "$NAMESPACE" rollout status deployment/studio --timeout=5m

# The new release serves. A failure below leaves it in place and concerns the identity services only.
platform_armed=0

# Identity (ZITADEL) and authorization (OpenFGA): deploy/k8s/iam, namespace iam. The manifests are always applied (both
# Deployments at 0 replicas, plus the policies); a service is scaled up only when its deploy secrets are complete.
# The database and role of each service are created here, idempotently and with the password from stdin (never on a
# command line), because Postgres runs its init script only on a fresh data volume. The role keeps CONNECT on its own
# database only. ZITADEL's role may create databases: `zitadel init` insists on it, and nothing else can connect to
# the databases it creates because CONNECT is revoked from PUBLIC.
create_iam_database() { # ROLE PASSWORD_KEY CREATEDB|NOCREATEDB
  local role=$1 password_key=$2 createdb=$3
  # Single quotes are deliberate: the postgres container expands its own password.
  # shellcheck disable=SC2016
  {
    dotenv_get "$secret_file" "$password_key"
    cat <<SQL
SELECT format('CREATE ROLE %I WITH LOGIN PASSWORD %L', '${role}', :'pw')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${role}') \\gexec
SELECT format('ALTER ROLE %I WITH LOGIN ${createdb} PASSWORD %L', '${role}', :'pw') \\gexec
SELECT 'CREATE DATABASE ${role} OWNER ${role}'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${role}') \\gexec
REVOKE CONNECT ON DATABASE ${role} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${role} TO ${role};
SQL
  } | kubectl_for_profile -n "$NAMESPACE" exec -i postgres-0 -- \
    sh -c 'IFS= read -r pw; PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 --set=pw="$pw" -f - >/dev/null'
}

make_tmp iam-rendered iam_rendered_file
kubectl_for_profile kustomize "$REPO_ROOT/deploy/k8s/iam" \
  | sed "${cluster_sed[@]}" -e "s|@@CHECKSUM_ZITADEL@@|${checksum_zitadel}|g" -e "s|@@CHECKSUM_OPENFGA@@|${checksum_openfga}|g" >"$iam_rendered_file"
iam_armed=1
kubectl_for_profile apply -f "$iam_rendered_file"

if [[ "$openfga_state" == on ]]; then
  create_iam_database openfga OPENFGA_DB_PASSWORD NOCREATEDB
  kubectl_for_profile -n iam scale deployment/openfga --replicas=1
  kubectl_for_profile -n iam rollout status deployment/openfga --timeout=5m
else
  kubectl_for_profile -n iam scale deployment/openfga --replicas=0
  printf 'No OpenFGA fields in the deploy secrets: OpenFGA stays at 0 replicas.\n'
fi
if [[ "$zitadel_state" == on ]]; then
  create_iam_database zitadel ZITADEL_DB_PASSWORD CREATEDB
  kubectl_for_profile -n iam scale deployment/zitadel --replicas=1
  kubectl_for_profile -n iam rollout status deployment/zitadel --timeout=10m
else
  kubectl_for_profile -n iam scale deployment/zitadel --replicas=0
  printf 'No ZITADEL fields in the deploy secrets: ZITADEL stays at 0 replicas.\n'
fi

printf 'Deployed %s and %s to %s.\n' "$python_image" "$studio_image" "$EXPECTED_CONTEXT"
# Last, so it is on screen: how to create the admin account, while there is none.
if [[ -n "$local_admin" ]]; then print_cluster_setup; fi
