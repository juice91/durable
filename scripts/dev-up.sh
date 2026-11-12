#!/usr/bin/env bash
# Start the development stack: postgres, lgtm, worker, api, studio
# Usage: dev-up.sh [service...] - starts specified services or all
# Each service is started in order and waited for health
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=dev-lib.sh
# shellcheck source-path=SCRIPTDIR source=dev-lib.sh
source "${SCRIPT_DIR}/dev-lib.sh"

# Ensure environment is set up for docker-compose
ensure_env

# Load optional .dev.env file
# shellcheck disable=SC1090
DEV_ENV="${REPO_ROOT}/.dev.env"
if [[ -f "$DEV_ENV" ]]; then
    echo "[dev-up] Loading .dev.env"
    set -a
    # shellcheck source=/dev/null
    source "$DEV_ENV"
    set +a
fi

# Export POSTGRES_* vars for docker-compose to pick up
export POSTGRES_USER="${POSTGRES_USER:-eap_admin}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-eap_admin_password}"

# If EAP_LITELLM_API_KEY_CMD is set, execute it to get the key
if [[ -n "${EAP_LITELLM_API_KEY_CMD:-}" ]]; then
    echo "[dev-up] Fetching LITELLM_API_KEY from command"
    LITELLM_API_KEY=$(eval "$EAP_LITELLM_API_KEY_CMD")
    export LITELLM_API_KEY
    unset EAP_LITELLM_API_KEY_CMD
fi

# Ensure .dev directories exist
mkdir -p "${REPO_ROOT}/.dev/run" "${REPO_ROOT}/.dev/logs"

# Parse services to start
SERVICES=()
if [[ $# -eq 0 ]]; then
    SERVICES=(postgres lgtm worker api studio)
else
    for arg in "$@"; do
        case "$arg" in
            postgres|lgtm|worker|api|studio)
                SERVICES+=("$arg")
                ;;
            *)
                echo "Unknown service: $arg" >&2
                exit 1
                ;;
        esac
    done
fi

echo "[dev-up] Starting services: ${SERVICES[*]}"

for service in "${SERVICES[@]}"; do
    case "$service" in
        postgres|lgtm)
            echo "[dev-up] Starting Docker service: $service"
            if ! start_docker_service "$service"; then
                echo "ERROR: Failed to start $service" >&2
                exit 1
            fi
            if [[ "$service" == "postgres" ]]; then
                # The API and worker refuse to start unless the schema is at the latest migration.
                echo "[dev-up] Applying database migrations"
                if ! EAP_DATABASE_URL="${EAP_DATABASE_URL:-postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app}" \
                    uv run alembic -c "${REPO_ROOT}/packages/db/alembic.ini" upgrade head; then
                    echo "ERROR: database migrations failed" >&2
                    exit 1
                fi
            fi
            ;;
        worker|api|studio)
            echo "[dev-up] Starting standalone service: $service"
            if ! start_standalone_service "$service"; then
                echo "ERROR: Failed to start $service" >&2
                exit 1
            fi
            ;;
    esac
done

# Wait for health checks on standalone services
echo "[dev-up] Waiting for health checks..."

for service in "${SERVICES[@]}"; do
    case "$service" in
        worker)
            # Worker doesn't have a health endpoint; check if process is running
            if ! is_running_by_pid worker; then
                echo "ERROR: worker process not running" >&2
                tail -50 "${LOG_DIR}/worker.log" >&2
                exit 1
            fi
            echo "[dev-up] worker: running"
            ;;
        api)
            if ! wait_for_service "api" "http://127.0.0.1:8000/healthz" 60; then
                echo "ERROR: api failed health check" >&2
                tail -50 "${LOG_DIR}/api.log" >&2
                exit 1
            fi
            echo "[dev-up] api: healthy"
            ;;
        studio)
            if ! wait_for_service "studio" "http://127.0.0.1:3000" 60; then
                echo "ERROR: studio failed health check" >&2
                tail -50 "${LOG_DIR}/studio.log" >&2
                exit 1
            fi
            echo "[dev-up] studio: healthy"
            ;;
        # Docker services already checked by start_docker_service
    esac
done

echo "[dev-up] All services started successfully"
