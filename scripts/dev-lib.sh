#!/usr/bin/env bash
# Shared helper functions for dev-up.sh, dev-down.sh, dev-status.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_DIR="${REPO_ROOT}/.dev"
RUN_DIR="${DEV_DIR}/run"
LOG_DIR="${DEV_DIR}/logs"

# Export defaults shared by the API, worker, migrations, and docker-compose.
# Callers may load local environment files first; explicit values are preserved.
ensure_env() {
    export POSTGRES_USER="${POSTGRES_USER:-eap_admin}"
    export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-eap_admin_password}"
    export EAP_DATABASE_URL="${EAP_DATABASE_URL:-postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app}"
    export EAP_DBOS_URL="${EAP_DBOS_URL:-${DBOS_SYSTEM_DATABASE_URL:-postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos}}"
    export DBOS_SYSTEM_DATABASE_URL="${DBOS_SYSTEM_DATABASE_URL:-$EAP_DBOS_URL}"
    export EAP_APP_VERSION="${EAP_APP_VERSION:-m1}"
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

# Get process group pid.
get_pgrp() {
    local pid="$1"
    ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' '
}

process_start_time() {
    local pid="$1"
    ps -o lstart= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

process_command() {
    local pid="$1"
    ps -o command= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//;s/[[:space:]]*$//'
}

pid_file_field() {
    local pid_file="$1"
    local field="$2"
    sed -n "s/^${field}=//p" "$pid_file" | head -n 1
}

# A pid is owned only when it is still the recorded session leader and has the
# same start time. This prevents a recycled pid from receiving our signals.
pid_identity_matches() {
    local pid_file="$1"
    local pid pgid recorded_start current_start current_pgid

    [[ -f "$pid_file" ]] || return 1
    pid=$(pid_file_field "$pid_file" pid)
    pgid=$(pid_file_field "$pid_file" pgid)
    recorded_start=$(pid_file_field "$pid_file" start)
    [[ "$pid" =~ ^[0-9]+$ && "$pgid" =~ ^[0-9]+$ && "$pid" == "$pgid" && -n "$recorded_start" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    current_pgid=$(get_pgrp "$pid")
    current_start=$(process_start_time "$pid")
    [[ "$current_pgid" == "$pgid" && "$current_start" == "$recorded_start" ]]
}

# Check if a service is running and still matches its recorded identity.
is_running_by_pid() {
    local service="$1"
    pid_identity_matches "${RUN_DIR}/${service}.pid"
}

# Launch in a new session. macOS does not ship a setsid executable, so Python's
# os.setsid() is used before exec; the recorded pid is therefore also the pgid.
start_process_group() {
    local service="$1"
    local log_file="$2"
    local working_dir="$3"
    shift 3
    local pid_file="${RUN_DIR}/${service}.pid"
    local pid pgid start command attempts

    (
        cd "$working_dir" || exit 1
        exec nohup python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@"
    ) > "$log_file" 2>&1 &
    pid=$!

    attempts=0
    pgid=""
    while [[ $attempts -lt 50 ]]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[$service] Process exited during startup; see $log_file" >&2
            return 1
        fi
        pgid=$(get_pgrp "$pid")
        [[ "$pgid" == "$pid" ]] && break
        sleep 0.1
        attempts=$((attempts + 1))
    done
    if [[ "$pgid" != "$pid" ]]; then
        echo "[$service] Failed to create a dedicated process group" >&2
        kill -TERM "$pid" 2>/dev/null || true
        return 1
    fi

    start=$(process_start_time "$pid")
    command=$(process_command "$pid")
    {
        printf 'pid=%s\n' "$pid"
        printf 'pgid=%s\n' "$pgid"
        printf 'start=%s\n' "$start"
        printf 'command=%s\n' "$command"
    } > "${pid_file}.tmp"
    mv "${pid_file}.tmp" "$pid_file"
}

# Start a standalone service (worker, api, studio)
start_standalone_service() {
    local service="$1"
    local pid_file="${RUN_DIR}/${service}.pid"
    local log_file="${LOG_DIR}/${service}.log"

    if [[ -f "$pid_file" ]]; then
        local pid
        pid=$(pid_file_field "$pid_file" pid)
        if pid_identity_matches "$pid_file"; then
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
            start_process_group "$service" "$log_file" "$REPO_ROOT" bash "$SCRIPT_DIR/dev-worker.sh"
            ;;
        api)
            start_process_group "$service" "$log_file" "$REPO_ROOT/apps/api" \
                uv run --with uvicorn uvicorn eap_api.app:create_app --factory --host 127.0.0.1 --port 8000
            ;;
        studio)
            start_process_group "$service" "$log_file" "$REPO_ROOT/apps/studio" pnpm dev
            ;;
    esac

    echo "[$service] Started (pid $(pid_file_field "$pid_file" pid))"
}

# Stop a service by pid
stop_service_by_pid() {
    local service="$1"
    local pid_file="${RUN_DIR}/${service}.pid"

    if [[ ! -f "$pid_file" ]]; then
        echo "[$service] No pid file, skipping"
        return 0
    fi

    local pid pgid
    pid=$(pid_file_field "$pid_file" pid)
    pgid=$(pid_file_field "$pid_file" pgid)

    if ! pid_identity_matches "$pid_file"; then
        echo "[$service] Stale or mismatched pid file, removing without signalling"
        rm -f "$pid_file"
        return 0
    fi

    echo "[$service] Stopping process group $pgid (leader pid $pid)..."
    kill -TERM -- "-$pgid" 2>/dev/null || true

    # Wait up to 10s for graceful shutdown
    local waited=0
    while [[ $waited -lt 10 ]]; do
        if ! kill -0 -- "-$pgid" 2>/dev/null; then
            echo "[$service] Stopped gracefully"
            rm -f "$pid_file"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Force kill
    echo "[$service] Force killing process group $pgid..."
    kill -KILL -- "-$pgid" 2>/dev/null || true
    sleep 1
    rm -f "$pid_file"
}

# Stop docker services
stop_docker_services() {
    local compose_dir="${REPO_ROOT}/infrastructure/docker"
    docker compose -f "${compose_dir}/compose.yaml" down
}
