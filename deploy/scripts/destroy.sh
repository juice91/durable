#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=deploy/scripts/lib.sh
source "${SCRIPT_DIR}/lib.sh"
require_command colima
printf 'Delete Colima profile %s and all of its Kubernetes data? Type %s to continue: ' "$PROFILE" "$PROFILE"
read -r confirmation
if [[ "$confirmation" != "$PROFILE" ]]; then
  printf 'Cancelled.\n'
  exit 1
fi
colima delete "$PROFILE"
printf 'Deleted Colima profile %s. Files under PYDANTICOS_DATA_DIR (default ~/pydanticos-data), including backups/, are not deleted.\n' "$PROFILE"
