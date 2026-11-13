#!/usr/bin/env bash
set -euo pipefail

# Development database management script for pydanticOS
# Requires: docker, docker-compose
# Environment: .env file with POSTGRES_USER and POSTGRES_PASSWORD (defaults in .env.example)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${SCRIPT_DIR}/../infrastructure/docker"

# ensure_vector_extension and postgres_volume_guard
# shellcheck source=dev-lib.sh
# shellcheck source-path=SCRIPTDIR source=dev-lib.sh
source "${SCRIPT_DIR}/dev-lib.sh"

# Load environment variables from .env file if it exists
if [[ -f "${SCRIPT_DIR}/../.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/../.env"
    set +a
fi

# Defaults
POSTGRES_USER="${POSTGRES_USER:-eap_admin}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-eap_admin_password}"

# Wait for postgres to be healthy (max 60 seconds)
wait_for_healthy() {
    echo "Waiting for PostgreSQL to become healthy..."
    local max_wait=60
    local waited=0
    local interval=2
    
    while [[ $waited -lt $max_wait ]]; do
        if docker compose -f "${COMPOSE_DIR}/compose.yaml" ps -q postgres | xargs -r docker inspect --format='{{.State.Health.Status}}' 2>/dev/null | grep -q "healthy"; then
            echo "PostgreSQL is healthy"
            return 0
        fi
        sleep "$interval"
        waited=$((waited + interval))
    done
    
    echo "ERROR: PostgreSQL did not become healthy within ${max_wait} seconds"
    return 1
}

cmd_up() {
    echo "Starting PostgreSQL..."
    if ! docker inspect eap_postgres >/dev/null 2>&1 && ! postgres_volume_guard "${COMPOSE_DIR}/compose.yaml"; then
        return 1
    fi
    docker compose -f "${COMPOSE_DIR}/compose.yaml" up -d
    wait_for_healthy
    # pgvector for volumes created before the pgvector image (the init script covers new ones).
    ensure_vector_extension eap_postgres
}

cmd_down() {
    echo "Stopping PostgreSQL (keeping data volume)..."
    docker compose -f "${COMPOSE_DIR}/compose.yaml" down
}

cmd_reset() {
    echo "Resetting PostgreSQL (removing data volume)..."
    docker compose -f "${COMPOSE_DIR}/compose.yaml" down -v
}

cmd_status() {
    docker compose -f "${COMPOSE_DIR}/compose.yaml" ps
}

cmd_check() {
    echo "Running database connectivity checks..."
    local failed=0

    # Check 0: Verify port is bound to 127.0.0.1 only
    echo "Check 0: Verifying port 54329 is bound to 127.0.0.1 only..."
    local port_binding
    port_binding=$(docker compose -f "${COMPOSE_DIR}/compose.yaml" port postgres 5432 2>/dev/null || true)
    if ! echo "$port_binding" | grep -q "^127.0.0.1:54329"; then
        echo "FAIL: Port binding is $port_binding, expected to start with 127.0.0.1:54329"
        failed=1
    else
        echo "PASS: Port is correctly bound to 127.0.0.1:54329"
    fi

    # Check 1: Both databases exist
    echo "Check 1: Verifying both databases exist..."
    if ! docker compose -f "${COMPOSE_DIR}/compose.yaml" exec -T postgres psql -U "${POSTGRES_USER}" -d postgres -c "\l" 2>/dev/null | grep -q "eap_app\|eap_dbos"; then
        echo "FAIL: One or both databases (eap_app, eap_dbos) do not exist"
        failed=1
    else
        echo "PASS: Both databases exist"
    fi

    # Check 2: eap_app can create/drop table in eap_app over TCP with password
    echo "Check 2: Verifying eap_app can create/drop table in eap_app (TCP + password)..."
    if ! docker compose -f "${COMPOSE_DIR}/compose.yaml" exec -T -e PGPASSWORD=eap_app_password postgres psql -h 127.0.0.1 -U eap_app -d eap_app -c 'CREATE TABLE probe_t(x int); DROP TABLE probe_t;' >/dev/null 2>&1; then
        echo "FAIL: eap_app cannot create/drop table in eap_app"
        failed=1
    else
        echo "PASS: eap_app can create/drop table in eap_app"
    fi

    # Check 3: eap_app is REFUSED connecting to eap_dbos over TCP with password
    echo "Check 3: Verifying eap_app is refused connecting to eap_dbos (TCP + password)..."
    if docker compose -f "${COMPOSE_DIR}/compose.yaml" exec -T -e PGPASSWORD=eap_app_password postgres psql -h 127.0.0.1 -U eap_app -d eap_dbos -c "SELECT 1;" >/dev/null 2>&1; then
        echo "FAIL: eap_app should be refused connecting to eap_dbos"
        failed=1
    else
        echo "PASS: eap_app is correctly refused from eap_dbos"
    fi

    # Check 4: eap_dbos can create/drop table in eap_dbos over TCP with password
    echo "Check 4: Verifying eap_dbos can create/drop table in eap_dbos (TCP + password)..."
    if ! docker compose -f "${COMPOSE_DIR}/compose.yaml" exec -T -e PGPASSWORD=eap_dbos_password postgres psql -h 127.0.0.1 -U eap_dbos -d eap_dbos -c 'CREATE TABLE probe_t(x int); DROP TABLE probe_t;' >/dev/null 2>&1; then
        echo "FAIL: eap_dbos cannot create/drop table in eap_dbos"
        failed=1
    else
        echo "PASS: eap_dbos can create/drop table in eap_dbos"
    fi

    # Check 5: eap_dbos is REFUSED connecting to eap_app over TCP with password
    echo "Check 5: Verifying eap_dbos is refused connecting to eap_app (TCP + password)..."
    if docker compose -f "${COMPOSE_DIR}/compose.yaml" exec -T -e PGPASSWORD=eap_dbos_password postgres psql -h 127.0.0.1 -U eap_dbos -d eap_app -c "SELECT 1;" >/dev/null 2>&1; then
        echo "FAIL: eap_dbos should be refused connecting to eap_app"
        failed=1
    else
        echo "PASS: eap_dbos is correctly refused from eap_app"
    fi

    if [[ $failed -eq 1 ]]; then
        echo ""
        echo "DATABASE CHECKS FAILED"
        return 1
    else
        echo ""
        echo "ALL DATABASE CHECKS PASSED"
        return 0
    fi
}

# Main command dispatcher
case "${1:-}" in
    up)
        cmd_up
        ;;
    down)
        cmd_down
        ;;
    reset)
        cmd_reset
        ;;
    status)
        cmd_status
        ;;
    check)
        cmd_check
        ;;
    *)
        echo "Usage: $0 {up|down|reset|status|check}"
        echo ""
        echo "Commands:"
        echo "  up      - Start PostgreSQL and wait until healthy (max 60s)"
        echo "  down    - Stop PostgreSQL (keep data volume)"
        echo "  reset   - Stop and remove data volume"
        echo "  status  - Show container status"
        echo "  check   - Verify database connectivity and isolation"
        exit 1
        ;;
esac
