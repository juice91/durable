#!/usr/bin/env bash
# Install, inspect or remove the ToolHive operator that runs managed MCP servers.
# Idempotent: install is `helm upgrade --install` of two pinned charts.
set -euo pipefail

TOOLHIVE_VERSION=0.65.0
CRDS_CHART=oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds
OPERATOR_CHART=oci://ghcr.io/stacklok/toolhive/toolhive-operator
TOOLHIVE_NAMESPACE=toolhive-system
CRDS_RELEASE=toolhive-operator-crds
OPERATOR_RELEASE=toolhive-operator
PROBE_CRD=mcpservers.toolhive.stacklok.dev
DEFAULT_CONTEXT=colima-pydanticos

usage() {
  printf 'Usage: %s {install|status|uninstall} [--context KUBE_CONTEXT]\n' "$0" >&2
  printf 'Default context: %s. ToolHive chart version: %s.\n' "$DEFAULT_CONTEXT" "$TOOLHIVE_VERSION" >&2
}

command_name=""
context=$DEFAULT_CONTEXT
while (($#)); do
  case "$1" in
    install | status | uninstall)
      [[ -z "$command_name" ]] || { usage; exit 64; }
      command_name=$1
      shift
      ;;
    --context)
      [[ $# -ge 2 && -n "$2" ]] || { usage; exit 64; }
      context=$2
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 64
      ;;
  esac
done
[[ -n "$command_name" ]] || { usage; exit 64; }

ensure_helm() {
  command -v helm >/dev/null 2>&1 && return 0
  if [[ "$(uname -s)" == "Darwin" ]] && command -v brew >/dev/null 2>&1; then
    printf 'helm not found; installing it with Homebrew.\n' >&2
    brew install helm
  else
    printf 'helm is required. Install it (https://helm.sh/docs/intro/install/) and rerun.\n' >&2
    exit 127
  fi
}

require_kubectl() {
  command -v kubectl >/dev/null 2>&1 || { printf 'Required command not found: kubectl\n' >&2; exit 127; }
}

kc() { kubectl --context "$context" "$@"; }
hm() { helm --kube-context "$context" "$@"; }

do_install() {
  require_kubectl
  ensure_helm
  kc cluster-info >/dev/null 2>&1 || { printf 'Cluster %s is not reachable. Start it first (colima start pydanticos).\n' "$context" >&2; exit 1; }
  hm upgrade --install "$CRDS_RELEASE" "$CRDS_CHART" --version "$TOOLHIVE_VERSION" \
    --namespace "$TOOLHIVE_NAMESPACE" --create-namespace --wait --timeout 5m
  hm upgrade --install "$OPERATOR_RELEASE" "$OPERATOR_CHART" --version "$TOOLHIVE_VERSION" \
    --namespace "$TOOLHIVE_NAMESPACE" --create-namespace --wait --timeout 5m
  kc wait --for=condition=Established "crd/${PROBE_CRD}" --timeout=2m
  kc -n "$TOOLHIVE_NAMESPACE" wait --for=condition=Ready pod \
    -l app.kubernetes.io/name=toolhive-operator --timeout=5m
  printf 'ToolHive operator %s is ready on %s.\n' "$TOOLHIVE_VERSION" "$context"
}

do_status() {
  require_kubectl
  ensure_helm
  hm list --namespace "$TOOLHIVE_NAMESPACE" || true
  kc get crd "$PROBE_CRD" || { printf 'CRD %s is missing.\n' "$PROBE_CRD" >&2; exit 1; }
  kc -n "$TOOLHIVE_NAMESPACE" get pods
}

do_uninstall() {
  require_kubectl
  ensure_helm
  hm uninstall "$OPERATOR_RELEASE" --namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found --wait || true
  hm uninstall "$CRDS_RELEASE" --namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found --wait || true
  # helm keeps CRDs it did not template; remove the ToolHive ones explicitly.
  local crd
  while IFS= read -r crd; do
    [[ "$crd" == *.toolhive.stacklok.dev ]] && kc delete "$crd" --ignore-not-found
  done < <(kc get crd -o name 2>/dev/null || true)
  kc delete namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found
}

case "$command_name" in
  install) do_install ;;
  status) do_status ;;
  uninstall) do_uninstall ;;
esac
