#!/usr/bin/env bash
# Stop the development stack
# Usage: dev-down.sh [service...] [--all] [--dry-run]
# Without --all, only stops app services (worker, api, studio)
# With --all, also stops docker services (postgres, lgtm), each under the compose project its
#   container belongs to (read from its com.docker.compose.project label; eap_postgres is in `eap`).
#   Only `stop` is used, never `down`, `rm` or `-v`: no container or volume is removed.
# With --dry-run (or EAP_DEV_DOWN_DRY_RUN=1), print what would be stopped and the exact docker
#   commands, without signalling any process or stopping any container.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=dev-lib.sh
# shellcheck source-path=SCRIPTDIR source=dev-lib.sh
source "${SCRIPT_DIR}/dev-lib.sh"

# Parse arguments
STOP_DOCKER=0
DRY_RUN="${EAP_DEV_DOWN_DRY_RUN:-0}"
SERVICES=()

for arg in "$@"; do
    case "$arg" in
        --all)
            STOP_DOCKER=1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        postgres|lgtm|worker|api|studio)
            SERVICES+=("$arg")
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done
export DRY_RUN

# Default to all services if none specified
if [[ ${#SERVICES[@]} -eq 0 ]]; then
    if [[ $STOP_DOCKER -eq 1 ]]; then
        SERVICES=(postgres lgtm worker api studio)
    else
        SERVICES=(worker api studio)
    fi
fi

echo "[dev-down] Stopping services: ${SERVICES[*]}"
if [[ $DRY_RUN == "1" ]]; then
    echo "[dev-down] Dry run: nothing is stopped"
fi
if [[ $STOP_DOCKER -eq 1 ]]; then
    echo "[dev-down] Also stopping docker services"
fi

stop_app_service() {
    local service="$1"
    if [[ $DRY_RUN != "1" ]]; then
        stop_service_by_pid "$service"
        return
    fi
    local pid_file="${RUN_DIR}/${service}.pid"
    if [[ ! -f "$pid_file" ]]; then
        echo "[$service] No pid file, skipping"
    elif pid_identity_matches "$pid_file"; then
        echo "[dry-run] kill -TERM -- -$(pid_file_field "$pid_file" pgid)"
    else
        echo "[$service] Stale or mismatched pid file (would be removed without signalling)"
    fi
}

for service in "${SERVICES[@]}"; do
    case "$service" in
        worker|api|studio)
            stop_app_service "$service"
            ;;
        postgres|lgtm)
            if [[ $STOP_DOCKER -eq 1 ]]; then
                echo "[dev-down] Stopping Docker service: $service"
                stop_docker_service "$service"
            else
                echo "[dev-down] Skipping docker service: $service (use --all)"
            fi
            ;;
    esac
done

echo "[dev-down] Done"
