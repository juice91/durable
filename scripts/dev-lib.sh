#!/usr/bin/env bash
# Shared helper functions for dev-up.sh, dev-down.sh, dev-status.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_DIR="${REPO_ROOT}/.dev"
RUN_DIR="${DEV_DIR}/run"
LOG_DIR="${DEV_DIR}/logs"

# Source environment files and export variables for docker-compose
ensure_env() {
    local env_file="${REPO_ROOT}/.env"
    if [[ -f "$env_file" ]]; then
        set -a
        # shellcheck disable=SC1091,SC1090
        source "$env_file"
        set +a
    fi
    export POSTGRES_USER="${POSTGRES_USER:-eap_admin}"
    export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-eap_admin_password}"
    export EAP_AUTH_MODE="${EAP_AUTH_MODE:-dev}"
}

# Ensure directories exist
ensure_dirs() {
    mkdir -p "$RUN_DIR" "$LOG_DIR"
}

# Get service list from arguments or default to all
get_services() {
    local services=()
    if [[ $# -eq 0 ]]; then
        services=(postgres lgtm worker api studio)
    else
        for arg in "$@"; do
            case "$arg" in
                postgres|lgtm|worker|api|studio)
                    services+=("$arg")
                    ;;
                --all)
                    # Handled by caller
                    ;;
                *)
                    echo "Unknown service: $arg" >&2
                    return 1
                    ;;
            esac
        done
    fi
    printf '%s\n' "${services[@]}"
}

# Wait for a service to become healthy with timeout
wait_for_service() {
    local service="$1"
    local url="$2"
    local max_wait="${3:-60}"
    local waited=0
    local interval=2
    local last_response=""

    while [[ $waited -lt $max_wait ]]; do
        last_response=$(curl -sf "$url" 2>/dev/null || true)
        if [[ -n "$last_response" ]]; then
            return 0
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done

    echo "Health check failed after ${max_wait}s" >&2
    return 1
}

# Start a docker service
start_docker_service() {
    local service="$1"
    local compose_dir="${REPO_ROOT}/infrastructure/docker"
    local container_name="eap_${service}"
    
    # Check if container exists (either via docker compose or directly)
    if docker compose -f "${compose_dir}/compose.yaml" ps -q "$service" 2>/dev/null | grep -q .; then
        echo "[$service] Docker service already running"
        return 0
    fi
    
    if docker inspect "$container_name" >/dev/null 2>&1; then
        echo "[$service] Docker container already exists, starting it..."
        docker start "$container_name"
        # Wait for health if service has healthcheck
        case "$service" in
            postgres|lgtm)
                wait_for_docker_health "$service" 120
                ;;
        esac
        return 0
    fi

    echo "[$service] Starting Docker service..."
    docker compose -f "${compose_dir}/compose.yaml" up -d "$service"

    # Wait for health if service has healthcheck
    case "$service" in
        postgres|lgtm)
            wait_for_docker_health "$service" 120
            ;;
    esac
}

# Wait for docker service health with timeout
wait_for_docker_health() {
    local service="$1"
    local max_wait="${2:-60}"
    local waited=0
    local interval=2
    local container_name="eap_${service}"

    while [[ $waited -lt $max_wait ]]; do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container_name" 2>/dev/null || echo "")
        if [[ -n "$status" && "$status" != "none" && "$status" != "unhealthy" ]]; then
            if [[ "$status" == "healthy" ]]; then
                echo "[$service] Docker service is healthy"
                return 0
            fi
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done

    echo "[$service] Docker service did not become healthy within ${max_wait}s (status: $status)" >&2
    return 1
}

# Check if a service is running by pid file
is_running_by_pid() {
    local service="$1"
    local pid_file="${RUN_DIR}/${service}.pid"

    if [[ ! -f "$pid_file" ]]; then
        return 1
    fi

    local pid
    pid=$(cat "$pid_file")
    kill -0 "$pid" 2>/dev/null
}

# Get process group pid (first line of pgrp)
get_pgrp() {
    local pid="$1"
    ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

# Start a standalone service (worker, api, studio)
start_standalone_service() {
    local service="$1"
    local pid_file="${RUN_DIR}/${service}.pid"
    local log_file="${LOG_DIR}/${service}.log"

    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(cat "$pid_file")
        if kill -0 "$pid" 2>/dev/null; then
            echo "[$service] Already running (pid $pid)"
            return 0
        else
            echo "[$service] Stale pid file, removing"
            rm -f "$pid_file"
        fi
    fi

    echo "[$service] Starting service..."

    case "$service" in
        worker)
            cd "$REPO_ROOT"
            # Worker needs to be in process group for signals
            nohup bash "$SCRIPT_DIR/dev-worker.sh" > "$log_file" 2>&1 &
            local pid=$!
            disown "$pid"
            echo "$pid" > "$pid_file"
            ;;
        api)
            cd "$REPO_ROOT/apps/api"
            nohup uv run --with uvicorn uvicorn eap_api.app:create_app --factory --host 127.0.0.1 --port 8000 > "$log_file" 2>&1 &
            local pid=$!
            disown "$pid"
            echo "$pid" > "$pid_file"
            ;;
        studio)
            cd "$REPO_ROOT/apps/studio"
            nohup pnpm dev > "$log_file" 2>&1 &
            local pid=$!
            disown "$pid"
            echo "$pid" > "$pid_file"
            ;;
    esac

    echo "[$service] Started (pid $(cat "$pid_file"))"
}

# Stop a service by pid
stop_service_by_pid() {
    local service="$1"
    local pid_file="${RUN_DIR}/${service}.pid"

    if [[ ! -f "$pid_file" ]]; then
        echo "[$service] No pid file, skipping"
        return 0
    fi

    local pid
    pid=$(cat "$pid_file")

    if ! kill -0 "$pid" 2>/dev/null; then
        echo "[$service] Process not running, removing stale pid file"
        rm -f "$pid_file"
        return 0
    fi

    echo "[$service] Stopping (pid $pid)..."
    kill -TERM "$pid" 2>/dev/null || true

    # Wait up to 10s for graceful shutdown
    local waited=0
    while [[ $waited -lt 10 ]]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[$service] Stopped gracefully"
            rm -f "$pid_file"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Force kill
    echo "[$service] Force killing (pid $pid)..."
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
    rm -f "$pid_file"
}

# Stop docker services
stop_docker_services() {
    local compose_dir="${REPO_ROOT}/infrastructure/docker"
    docker compose -f "${compose_dir}/compose.yaml" down
}
