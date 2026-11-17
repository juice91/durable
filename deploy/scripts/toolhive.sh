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
  printf 'Usage: %s {install|status|uninstall} [--context KUBE_CONTEXT] [--yes]\n' "$0" >&2
  printf 'uninstall deletes every MCPServer resource and needs --yes.\n' >&2
  printf 'Default context: %s. ToolHive chart version: %s.\n' "$DEFAULT_CONTEXT" "$TOOLHIVE_VERSION" >&2
}

command_name=""
context=$DEFAULT_CONTEXT
assume_yes=0
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
    --yes)
      assume_yes=1
      shift
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

HELM_VERSION=${HELM_VERSION:-v3.19.0}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# Download the official release tarball, verify it against the published checksum, install the binary.
install_helm_release() {
  local os arch machine dir name tmp want got
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux) os=linux ;;
    *) printf 'helm is required and this OS has no release download here. Install it (https://helm.sh/docs/intro/install/).\n' >&2; exit 127 ;;
  esac
  machine=$(uname -m)
  case "$machine" in
    arm64 | aarch64) arch=arm64 ;;
    x86_64 | amd64) arch=amd64 ;;
    *) printf 'helm is required and architecture %s is unsupported here. Install it manually.\n' "$machine" >&2; exit 127 ;;
  esac
  dir=${HELM_INSTALL_DIR:-$HOME/.local/bin}
  name=helm-${HELM_VERSION}-${os}-${arch}.tar.gz
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/$name" "https://get.helm.sh/$name" || { rm -rf "$tmp"; printf 'Failed to download %s\n' "$name" >&2; exit 1; }
  curl -fsSL -o "$tmp/$name.sha256sum" "https://get.helm.sh/$name.sha256sum" || { rm -rf "$tmp"; printf 'Failed to download %s.sha256sum\n' "$name" >&2; exit 1; }
  want=$(awk '{print $1; exit}' "$tmp/$name.sha256sum")
  got=$(sha256_of "$tmp/$name")
  if [[ -z "$want" || "$want" != "$got" ]]; then
    rm -rf "$tmp"
    printf 'Checksum mismatch for %s (expected %s, got %s); refusing to install.\n' "$name" "$want" "$got" >&2
    exit 1
  fi
  tar -xzf "$tmp/$name" -C "$tmp" "${os}-${arch}/helm"
  mkdir -p "$dir"
  install -m 0755 "$tmp/${os}-${arch}/helm" "$dir/helm"
  rm -rf "$tmp"
  export PATH="$dir:$PATH"
  printf 'Installed helm %s to %s\n' "$HELM_VERSION" "$dir/helm" >&2
}

ensure_helm() {
  command -v helm >/dev/null 2>&1 && return 0
  # Homebrew only as an optional first attempt, and only when its prefix is writable.
  if command -v brew >/dev/null 2>&1 && [[ -w "$(brew --prefix 2>/dev/null)" ]]; then
    printf 'helm not found; trying Homebrew.\n' >&2
    brew install helm >&2 || printf 'brew install helm failed; falling back to the release tarball.\n' >&2
    command -v helm >/dev/null 2>&1 && return 0
  fi
  install_helm_release
}

require_kubectl() {
  command -v kubectl >/dev/null 2>&1 || { printf 'Required command not found: kubectl\n' >&2; exit 127; }
}

require_helm() {
  command -v helm >/dev/null 2>&1 || { printf 'Required command not found: helm (run "install", which downloads it when missing).\n' >&2; exit 127; }
}

require_reachable() {
  kc cluster-info >/dev/null 2>&1 || { printf 'Cluster %s is not reachable. Start it first (colima start pydanticos).\n' "$context" >&2; exit 1; }
}

kc() { kubectl --context "$context" "$@"; }
hm() { helm --kube-context "$context" "$@"; }

do_install() {
  require_kubectl
  ensure_helm
  require_reachable
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
  require_helm
  require_reachable
  hm list --namespace "$TOOLHIVE_NAMESPACE"
  kc get crd "$PROBE_CRD" || { printf 'CRD %s is missing.\n' "$PROBE_CRD" >&2; exit 1; }
  kc -n "$TOOLHIVE_NAMESPACE" get pods
}

do_uninstall() {
  require_kubectl
  require_helm
  require_reachable
  if ((!assume_yes)); then
    printf 'uninstall removes the operator, its CRDs and every MCPServer resource on %s. Rerun with --yes to proceed.\n' "$context" >&2
    exit 64
  fi
  hm uninstall "$OPERATOR_RELEASE" --namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found --wait
  hm uninstall "$CRDS_RELEASE" --namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found --wait
  # helm keeps CRDs it did not template; remove the ToolHive ones explicitly.
  local crd
  while IFS= read -r crd; do
    if [[ "$crd" == *.toolhive.stacklok.dev ]]; then
      kc delete "$crd" --ignore-not-found
    fi
  done < <(kc get crd -o name 2>/dev/null || true)
  kc delete namespace "$TOOLHIVE_NAMESPACE" --ignore-not-found
}

case "$command_name" in
  install) do_install ;;
  status) do_status ;;
  uninstall) do_uninstall ;;
esac
