#!/usr/bin/env bash
# Stop the development stack
# Usage: dev-down.sh [service...] --all
# Without --all, only stops app services (worker, api, studio)
# With --all, also stops docker services (postgres, lgtm)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=dev-lib.sh
# shellcheck source-path=SCRIPTDIR source=dev-lib.sh
source "${SCRIPT_DIR}/dev-lib.sh"

# Parse arguments
STOP_DOCKER=0
SERVICES=()

for arg in "$@"; do
    case "$arg" in
        --all)
            STOP_DOCKER=1
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

# Default to all services if none specified
if [[ ${#SERVICES[@]} -eq 0 ]]; then
    if [[ $STOP_DOCKER -eq 1 ]]; then
        SERVICES=(postgres lgtm worker api studio)
    else
        SERVICES=(worker api studio)
    fi
fi

echo "[dev-down] Stopping services: ${SERVICES[*]}"
if [[ $STOP_DOCKER -eq 1 ]]; then
    echo "[dev-down] Also stopping docker services"
fi

for service in "${SERVICES[@]}"; do
    case "$service" in
        worker|api|studio)
            stop_service_by_pid "$service"
            ;;
        postgres|lgtm)
            if [[ $STOP_DOCKER -eq 1 ]]; then
                echo "[dev-down] Stopping Docker service: $service"
                docker compose -f "${REPO_ROOT}/infrastructure/docker/compose.yaml" stop "$service"
            else
                echo "[dev-down] Skipping docker service: $service (use --all)"
            fi
            ;;
    esac
done

if [[ $STOP_DOCKER -eq 1 ]]; then
    # Stop all docker services if any were specified
    docker compose -f "${REPO_ROOT}/infrastructure/docker/compose.yaml" stop
fi

echo "[dev-down] Done"
