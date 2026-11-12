#!/usr/bin/env bash
# Show status of all development services
# One line per service: running/stopped, pid, health check result, URL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=dev-lib.sh
# shellcheck source-path=SCRIPTDIR source=dev-lib.sh
source "${SCRIPT_DIR}/dev-lib.sh"

ensure_dirs

# Function to check if a process is running
check_process() {
    local pid="$1"
    kill -0 "$pid" 2>/dev/null
}

# Function to check docker service health
check_docker_health() {
    local service="$1"
    local compose_dir="${REPO_ROOT}/infrastructure/docker"
    local container_name="eap_${service}"
    
    # Check if container exists (either via docker compose or directly)
    if ! docker compose -f "${compose_dir}/compose.yaml" ps -q "$service" 2>/dev/null | grep -q .; then
        if ! docker inspect "$container_name" >/dev/null 2>&1; then
            echo "not running"
            return
        fi
    fi
    
    local status
    status=$(docker inspect --format='{{.State.Health.Status}}' "$container_name" 2>/dev/null || echo "unknown")
    # If status is empty or none, check if container is running
    if [[ -z "$status" || "$status" == "none" ]]; then
        local container_status
        container_status=$(docker inspect --format='{{.State.Status}}' "$container_name" 2>/dev/null || echo "unknown")
        if [[ "$container_status" != "running" ]]; then
            echo "stopped"
            return
        fi
    fi
    echo "$status"
}

# Function to get service URL
get_service_url() {
    local service="$1"
    case "$service" in
        postgres) echo "127.0.0.1:54329" ;;
        lgtm) echo "http://127.0.0.1:3002" ;;
        worker) echo "n/a" ;;
        api) echo "http://127.0.0.1:8000" ;;
        studio) echo "http://127.0.0.1:3000" ;;
    esac
}

echo "Service      Status        PID           Health        URL"
echo "-------------------------------------------------------------"

for service in postgres lgtm worker api studio; do
    case "$service" in
        postgres|lgtm)
            # Docker service
            compose_dir="${REPO_ROOT}/infrastructure/docker"
            container_name="eap_${service}"
            docker_status="stopped"
            health="n/a"
            
            # Check if container exists (either via docker compose or directly)
            if docker compose -f "${compose_dir}/compose.yaml" ps -q "$service" 2>/dev/null | grep -q .; then
                docker_status="running"
                if [[ "$service" == "postgres" || "$service" == "lgtm" ]]; then
                    health=$(check_docker_health "$service")
                fi
            elif docker inspect "$container_name" >/dev/null 2>&1; then
                docker_status="running"
                if [[ "$service" == "postgres" || "$service" == "lgtm" ]]; then
                    health=$(check_docker_health "$service")
                fi
            fi
            
            printf "%-12s %-13s %-13s %-13s %s\n" "$service" "$docker_status" "-" "$health" "$(get_service_url $service)"
            ;;
        worker|api|studio)
            # Standalone service
            pid_file="${RUN_DIR}/${service}.pid"
            status="stopped"
            pid="-"
            health="n/a"
            url=$(get_service_url "$service")
            
            if [[ -f "$pid_file" ]]; then
                pid=$(cat "$pid_file")
                if check_process "$pid"; then
                    status="running"
                    
                    # Check health if applicable
                    if [[ "$service" == "api" ]]; then
                        if curl -sf "http://127.0.0.1:8000/healthz" > /dev/null 2>&1; then
                            health="ok"
                        else
                            health="error"
                        fi
                    elif [[ "$service" == "studio" ]]; then
                        if curl -sf "http://127.0.0.1:3000" > /dev/null 2>&1; then
                            health="ok"
                        else
                            health="error"
                        fi
                    elif [[ "$service" == "worker" ]]; then
                        health="n/a"
                    fi
                else
                    status="dead (stale pid)"
                fi
            fi
            
            printf "%-12s %-13s %-13s %-13s %s\n" "$service" "$status" "$pid" "$health" "$url"
            ;;
    esac
done
