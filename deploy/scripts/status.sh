#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"
require_command docker
require_command kubectl
guard_contexts
kubectl_for_profile -n "$NAMESPACE" get pods,svc,statefulset,deployment,job,cronjob,pvc
