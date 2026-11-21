#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"
require_command docker
require_command kubectl
guard_contexts
kubectl_for_profile -n "$NAMESPACE" scale statefulset/worker deployment/api deployment/studio deployment/cloudflared --replicas=0
printf 'Application workloads are scaled to zero; Postgres, LGTM, PVCs, and backups remain.\n'
