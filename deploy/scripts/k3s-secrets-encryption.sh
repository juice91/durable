#!/usr/bin/env bash
# Turns on Kubernetes Secret encryption at rest in the pydanticos Colima/k3s profile, re-encrypts the Secrets that
# already exist, and verifies the result. Idempotent: on a cluster that already encrypts it only repairs the persistent
# configuration and prints the status. An operator runs it once, by hand (deploy/README.md, "Secrets encryption at
# rest"); up.sh never runs it. New clusters need nothing: up.sh starts them with --secrets-encryption.
#
#   k3s-secrets-encryption.sh [--yes]        enable, re-encrypt, verify
#   k3s-secrets-encryption.sh --status       print `k3s secrets-encrypt status` and exit (read-only)
#   k3s-secrets-encryption.sh --rollback     disable encryption and rewrite the Secrets in plain form
#
# How this cluster runs k3s: Colima starts a Lima VM (profile pydanticos) and installs k3s as the systemd service `k3s`
# inside it, with the arguments from `kubernetes.k3sArgs` in ~/.colima/pydanticos/colima.yaml. `colima ssh` reaches the
# VM. The k3s server is one node with its embedded datastore, so the single-server procedure applies
# (https://docs.k3s.io/cli/secrets-encrypt). Enabling needs k3s v1.35.3+k3s1 or newer (the March 2026 releases); older
# versions are refused with the exact reason, because the older path is not supported for an existing cluster.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"

MODE=enable
ASSUME_YES=""
while (($#)); do
  case "$1" in
    --status) MODE=status; shift ;;
    --rollback) MODE=rollback; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    *) printf 'Usage: %s [--status | --rollback] [--yes]\n' "$0" >&2; exit 64 ;;
  esac
done

require_command colima
require_command kubectl

DROPIN_DIR=/etc/rancher/k3s/config.yaml.d
DROPIN=${DROPIN_DIR}/20-secrets-encryption.yaml
MIN_VERSION="1.35.3"
colima_config="${COLIMA_HOME:-${HOME}/.colima}/${PROFILE}/colima.yaml"
rotation_timeout=${PYDANTICOS_K3S_ROTATION_TIMEOUT:-600}

vm() { colima ssh -p "$PROFILE" -- "$@"; }

colima status "$PROFILE" >/dev/null 2>&1 || {
  printf 'The Colima profile %s is not running. Start it (deploy/scripts/up.sh does) and rerun.\n' "$PROFILE" >&2
  exit 1
}

# `k3s secrets-encrypt` talks to https://127.0.0.1:6443 unless told otherwise, but Colima starts k3s with a random
# --https-listen-port (colima.yaml `kubernetes.port: 0` picks a new one on each start). Read it from the unit file
# inside the VM; the k3s installer writes the arguments quoted, one per line ('--https-listen-port' '41234').
parse_listen_port() { # unit file text on stdin -> the port, or 6443 when the unit sets none
  local port
  port=$(tr -d "'\"\\\\" | tr '\n' ' ' | grep -Eo -- '--https-listen-port[= ]+[0-9]+' | head -n1 | grep -Eo '[0-9]+$' || true)
  printf '%s\n' "${port:-6443}"
}
k3s_server_url() { printf 'https://127.0.0.1:%s' "$(vm cat /etc/systemd/system/k3s.service 2>/dev/null | parse_listen_port)"; }
SERVER_URL=$(k3s_server_url)
senc() { vm sudo k3s secrets-encrypt "$1" "${@:2}" --server "$SERVER_URL"; }

# Right after a restart the server has not yet written the node's encryption annotation (error ID 08020, "missing
# annotation"); retry for a bounded time before anyone evaluates the status.
status_wait=${PYDANTICOS_K3S_STATUS_WAIT:-60}
encryption_status() {
  local started=$SECONDS text
  while :; do
    text=$(senc status 2>&1 || true)
    [[ "$text" == *"08020"* || "$text" == *"missing annotation"* ]] || break
    ((SECONDS - started < status_wait)) || break
    sleep 2
  done
  printf '%s\n' "$text"
}
field() { # LABEL TEXT -> the value after "LABEL: "
  awk -v l="$1" 'index($0, l ": ") == 1 {print substr($0, length(l) + 3); exit}' <<<"$2"
}

confirm() {
  [[ -n "$ASSUME_YES" ]] && return 0
  [[ -t 0 ]] || { printf 'Refusing to continue without --yes (no terminal to ask on).\n' >&2; exit 1; }
  printf '%s [y/N] ' "$1" >&2
  local answer
  read -r answer
  [[ "$answer" == y || "$answer" == Y ]] || { printf 'Not confirmed.\n' >&2; exit 1; }
}

# `sort -V` puts the older of two versions first.
version_at_least() { # HAVE WANT, both "1.35.3"
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$2" ]]
}

wait_api() {
  local started=$SECONDS
  until kubectl --context "$EXPECTED_CONTEXT" get --raw /readyz >/dev/null 2>&1; do
    ((SECONDS - started < 180)) || { printf 'The Kubernetes API did not come back within 180 seconds.\n' >&2; exit 1; }
    sleep 3
  done
}

restart_k3s() {
  vm sudo systemctl restart k3s
  wait_api
}

wait_stage() { # STAGE: wait until the rotation stage equals it, or fail on reencrypt_failed / timeout
  local started=$SECONDS text stage
  while :; do
    text=$(encryption_status)
    stage=$(field "Current Rotation Stage" "$text")
    [[ "$stage" != "$1" ]] || return 0
    [[ "$stage" != reencrypt_failed ]] || { printf 'Re-encryption failed. Server status:\n%s\n' "$text" >&2; exit 1; }
    ((SECONDS - started < rotation_timeout)) || { printf 'Timed out after %s seconds waiting for stage %s. Status:\n%s\n' "$rotation_timeout" "$1" "$text" >&2; exit 1; }
    sleep 5
  done
}

# Keep a recreated VM encrypting too: the flag belongs in colima.yaml's k3sArgs (new profiles get it from up.sh).
persist_colima_arg() {
  [[ -f "$colima_config" ]] || return 0
  grep -q -- '--secrets-encryption' "$colima_config" && return 0
  awk '{print} /^[[:space:]]*k3sArgs:[[:space:]]*$/ && !done {print "    - --secrets-encryption"; done = 1}' "$colima_config" >"${colima_config}.new"
  grep -q -- '--secrets-encryption' "${colima_config}.new" || { rm -f "${colima_config}.new"; printf 'Could not find kubernetes.k3sArgs in %s; add "--secrets-encryption" to it by hand.\n' "$colima_config" >&2; return 0; }
  mv "${colima_config}.new" "$colima_config"
  printf 'Added --secrets-encryption to kubernetes.k3sArgs in %s.\n' "$colima_config"
}

report() {
  local text
  text=$(encryption_status)
  printf '%s\n' "$text"
  [[ "$(field "Encryption Status" "$text")" == Enabled && "$(field "Current Rotation Stage" "$text")" == reencrypt_finished && "$text" == *"All hashes match"* ]]
}

if [[ "$MODE" == status ]]; then
  report || exit 1
  exit 0
fi

current=$(encryption_status)

if [[ "$MODE" == rollback ]]; then
  [[ "$(field "Encryption Status" "$current")" == Enabled ]] || { printf 'Encryption is not enabled; nothing to roll back.\n%s\n' "$current"; exit 0; }
  confirm "Disable Secret encryption and store Secrets in plain form again? k3s restarts once; running pods keep running."
  senc disable
  restart_k3s
  senc reencrypt --force --skip
  printf '%s\n' "$(encryption_status)"
  printf 'Rolled back. The --secrets-encryption flag stays in the k3s configuration on purpose: k3s needs the same arguments while it holds an encryption configuration.\n'
  exit 0
fi

version=$(vm k3s --version 2>/dev/null | awk 'NR == 1 {for (i = 1; i <= NF; i++) if ($i ~ /^v[0-9]/) {sub(/^v/, "", $i); sub(/\+.*/, "", $i); print $i; exit}}')
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'Could not read the k3s version inside the VM.\n' >&2; exit 1; }

if [[ "$(field "Encryption Status" "$current")" == Enabled ]]; then
  stage=$(field "Current Rotation Stage" "$current")
  if [[ "$stage" == reencrypt_finished ]]; then
    persist_colima_arg
    printf 'Secrets encryption is already enabled and complete.\n'
    report
    exit $?
  fi
  printf 'Encryption is enabled but re-encryption is at stage %s; waiting for it to finish.\n' "$stage"
  wait_stage reencrypt_finished
  restart_k3s
  persist_colima_arg
  report
  exit $?
fi

if ! version_at_least "$version" "$MIN_VERSION"; then
  printf 'k3s v%s cannot enable Secret encryption on an existing cluster; v%s+k3s1 or newer can (k3s docs, "Enable Secrets Encryption on an Existing Cluster").\n' "$version" "$MIN_VERSION" >&2
  printf 'Raise kubernetes.version in %s, restart the profile, then rerun this script. Nothing was changed.\n' "$colima_config" >&2
  exit 1
fi

confirm "Enable Secret encryption at rest? k3s restarts twice (the API is unreachable for about a minute each time; running pods keep running)."

# 1. Declare the intent, then make the flag part of the server's configuration so every restart keeps it.
if [[ "$current" == *"no configuration file found"* ]]; then
  senc enable
fi
vm sudo mkdir -p "$DROPIN_DIR"
printf 'secrets-encryption: true\n' | vm sudo tee "$DROPIN" >/dev/null
restart_k3s

# 2. The status must now read: disabled, at the start stage.
text=$(encryption_status)
[[ "$(field "Current Rotation Stage" "$text")" == start || "$(field "Encryption Status" "$text")" == Enabled ]] || {
  printf 'Unexpected status after restarting with --secrets-encryption; stopping before any key rotation:\n%s\n' "$text" >&2
  exit 1
}

# 3. Rotate keys: this writes the new key and re-encrypts every existing Secret (about five per second).
if [[ "$(field "Encryption Status" "$text")" != Enabled ]]; then
  senc rotate-keys
fi
wait_stage reencrypt_finished

# 4. A restart makes the server load the rotated keys, then verify.
restart_k3s
persist_colima_arg
printf 'Verifying (kubectl access, then k3s secrets-encrypt status):\n'
kubectl --context "$EXPECTED_CONTEXT" -n "$NAMESPACE" get secrets >/dev/null
report || { printf 'The status is not "Enabled / reencrypt_finished / All hashes match".\n' >&2; exit 1; }
printf 'Secret encryption at rest is on. Back up %s inside the VM with the cluster data: without it the stored Secrets cannot be read.\n' /var/lib/rancher/k3s/server/cred/encryption-config.json
