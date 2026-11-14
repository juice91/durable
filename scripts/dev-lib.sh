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
    # Loki in the LGTM container (published on loopback by compose.yaml), for GET /logs.
    export EAP_LOKI_URL="${EAP_LOKI_URL:-http://127.0.0.1:3100}"
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

    if [[ "$service" == "postgres" ]] && ! postgres_volume_guard "${compose_dir}/compose.yaml"; then
        return 1
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

# psql as the Postgres container's admin role, in database eap_app. TCP on the container's loopback
# (trusted by the image's pg_hba.conf): while the init scripts run, the image's temporary server
# listens only on the socket, so this cannot see a half-initialised volume.
_pg_admin_psql() {
    local container="$1"
    shift
    # The single-quoted program is expanded by the container's shell, where POSTGRES_USER is set.
    # shellcheck disable=SC2016
    docker exec "$container" sh -c \
        'exec psql -X -q -t -A -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "${POSTGRES_USER:-postgres}" -d eap_app "$@"' \
        psql "$@"
}

# The compose project and the data volume of an existing Postgres container. Project names differ
# between machines (the compose directory name, or an explicit -p), so recreating the container
# must reuse ITS project: under another project, compose mounts <other>_postgres_data, a different
# (often empty) volume.
postgres_compose_project() {
    docker inspect "$1" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true
}

postgres_data_volume() {
    docker inspect "$1" \
        --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}' \
        2>/dev/null || true
}

# Before `compose up` creates a NEW Postgres container: refuse when the volume compose would mount
# (<default project>_postgres_data) is not the only *_postgres_data volume on this machine. The
# default project differs between machines, so another such volume may hold the real dev data, and
# compose would silently start on an empty one. EAP_POSTGRES_ALLOW_DEFAULT_VOLUME=1 accepts the
# default (e.g. when the others belong to unrelated projects). Usage: postgres_volume_guard compose_file
postgres_volume_guard() {
    local compose_file="$1" project expected volumes others
    [[ "${EAP_POSTGRES_ALLOW_DEFAULT_VOLUME:-}" == 1 ]] && return 0
    project=$(docker compose -f "$compose_file" config 2>/dev/null | sed -n 's/^name: //p' | head -1)
    if [[ -z "$project" ]]; then
        echo "[postgres] cannot read the compose project of ${compose_file}; refusing to create a container" >&2
        return 1
    fi
    expected="${project}_postgres_data"
    volumes=$(docker volume ls -q | grep -E '_postgres_data$' || true)
    others=$(grep -vx -- "$expected" <<<"$volumes" || true)
    [[ -z "$others" ]] && return 0
    {
        echo "[postgres] REFUSED: no eap_postgres container exists, and compose project '${project}' would start a"
        echo "[postgres] new one on volume ${expected}$(grep -qx -- "$expected" <<<"$volumes" && echo "" || echo " (new and empty)"), but these Postgres data volumes also exist:"
        local v
        while IFS= read -r v; do
            echo "[postgres]   ${v}: docker compose -p ${v%_postgres_data} -f ${compose_file} up -d postgres"
        done <<<"$others"
        echo "[postgres] Start the one that holds your data with its command above (then rerun), or set"
        echo "[postgres] EAP_POSTGRES_ALLOW_DEFAULT_VOLUME=1 to use ${expected} deliberately. See packages/db/README.md."
    } >&2
    return 1
}

# Print the exact steps that move an existing Postgres container onto the image pinned in
# compose.yaml, keeping its data volume. Usage: postgres_swap_steps container image backup_hint
postgres_swap_steps() {
    local container="$1" image="$2" backup_hint="$3"
    local compose="infrastructure/docker/compose.yaml"
    local project volume rm_step=""
    project=$(postgres_compose_project "$container")
    volume=$(postgres_data_volume "$container")
    if [[ -z "$project" && "$volume" == *_postgres_data ]]; then
        # Not created by compose: derive the project from the volume, and remove the container
        # first (compose would refuse its name). The named volume survives `docker rm`.
        project="${volume%_postgres_data}"
        rm_step="docker rm -f ${container}   (removes the container only; volume ${volume} is kept)"
    fi
    echo "[postgres] container ${container} runs image ${image}, which has no pgvector extension."
    echo "[postgres] The stack now needs the image pinned in ${compose}. To switch in place"
    echo "[postgres] (same Postgres 17; volume ${volume:-unknown} and all its data are kept):"
    echo "[postgres]   1. scripts/dev-down.sh   (the app processes; Postgres keeps running for the backup)"
    echo "[postgres]   2. optional backup: ${backup_hint}"
    if [[ -z "$project" || "$volume" != "${project}_postgres_data" ]]; then
        echo "[postgres]   3. STOP: cannot tell which compose project owns volume '${volume:-unknown}' (project label"
        echo "[postgres]      '${project}'). Recreating under the wrong project mounts another, empty volume."
        echo "[postgres]      Compose mounts only <project>_postgres_data, so if '${volume:-unknown}' is not named"
        echo "[postgres]      that way, do not recreate with compose: dump and restore instead (packages/db/README.md)."
    else
        if [[ -n "$rm_step" ]]; then
            echo "[postgres]   2b. not created by compose, so remove it first: ${rm_step}"
        fi
        echo "[postgres]   3. docker compose -p ${project} -f ${compose} pull postgres && \\"
        echo "[postgres]        docker compose -p ${project} -f ${compose} up -d postgres"
        echo "[postgres]      Always pass -p ${project}: without it compose may use another project and mount"
        echo "[postgres]      another project's <project>_postgres_data volume instead of ${volume}."
        echo "[postgres]   4. check the volume; this must print ${volume}:"
        echo "[postgres]        docker inspect ${container} --format '{{range .Mounts}}{{if eq .Destination \"/var/lib/postgresql/data\"}}{{.Name}}{{end}}{{end}}'"
    fi
    echo "[postgres]   5. scripts/dev-up.sh   (creates the extension, then migrates)"
}

# Create the pgvector extension in eap_app if it is missing (RAG and memory need the `vector` type).
# CREATE EXTENSION needs a superuser and the eap_app role is not one, so this runs as the admin role.
# New volumes already have it from init-scripts/01-setup-databases.sh; this covers volumes created
# under the former postgres:17 image. Idempotent. Fails, naming the fix, when the container runs an
# image without pgvector. Usage: ensure_vector_extension [container] (default eap_postgres).
ensure_vector_extension() {
    local container="${1:-eap_postgres}"
    local waited=0 present="" available=""

    until present=$(_pg_admin_psql "$container" -c "SELECT extversion FROM pg_extension WHERE extname = 'vector'" 2>/dev/null); do
        if [[ $waited -ge 60 ]]; then
            echo "[postgres] cannot query database eap_app in container ${container} as the admin role" >&2
            _pg_admin_psql "$container" -c "SELECT 1" >&2 || true
            return 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    # An image on another glibc than the one that created the volume mis-sorts text indexes; Postgres
    # only logs a warning, so surface it here (see "pgvector" in packages/db/README.md).
    local drifted
    drifted=$(_pg_admin_psql "$container" -c "SELECT string_agg(datname || ' ' || datcollversion || ' -> ' || pg_database_collation_actual_version(oid), ', ') FROM pg_database WHERE datname IN ('eap_app', 'eap_dbos') AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)" 2>/dev/null || true)
    if [[ -n "$drifted" ]]; then
        echo "[postgres] WARNING: collation version mismatch (${drifted}): the container's image has another glibc than the one that created this volume. Use the image pinned in infrastructure/docker/compose.yaml." >&2
    fi
    if [[ -n "$present" ]]; then
        echo "[postgres] vector extension ${present} present in eap_app"
        return 0
    fi

    local backup_hint
    backup_hint="mkdir -p ~/.eap-backups && docker exec ${container} sh -c 'pg_dumpall -U \"\$POSTGRES_USER\"' > ~/.eap-backups/eap-\$(date +%Y%m%d-%H%M%S).sql"
    if ! available=$(_pg_admin_psql "$container" -c "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"); then
        echo "[postgres] query failed: cannot read pg_available_extensions in eap_app (container ${container})" >&2
        return 1
    fi
    if [[ -z "$available" ]]; then
        local image
        image=$(docker inspect --format='{{.Config.Image}}' "$container" 2>/dev/null || echo "unknown")
        postgres_swap_steps "$container" "$image" "$backup_hint" >&2
        return 1
    fi

    echo "[postgres] This volume predates pgvector: creating the vector extension ${available} in eap_app."
    echo "[postgres] This changes no rows. Before the upcoming RAG migrations, a backup of this volume is cheap insurance:"
    echo "[postgres]   ${backup_hint}"
    if ! _pg_admin_psql "$container" -c "CREATE EXTENSION IF NOT EXISTS vector"; then
        echo "[postgres] CREATE EXTENSION vector failed in eap_app" >&2
        return 1
    fi
    echo "[postgres] vector extension created in eap_app"
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

# The command that stops one docker service (postgres, lgtm) of the dev stack, printed one word
# per line (empty: nothing to stop). Read-only: it only inspects the eap_<service> container.
# Containers of one machine may belong to DIFFERENT compose projects (e.g. eap_postgres in `eap`,
# eap_lgtm in `docker`), so the project comes from the container's own com.docker.compose.project
# label, never from compose's default for this directory: under another project, compose would not
# find the container (or would act on another project's). A container without the label (created
# outside compose) is stopped by name. Only `stop` is ever used: never down, rm or -v, so no volume
# is touched.
docker_service_stop_command() {
    local service="$1"
    local container="eap_${service}"
    local compose="${REPO_ROOT}/infrastructure/docker/compose.yaml"
    local project
    if ! docker inspect "$container" >/dev/null 2>&1; then
        return 0
    fi
    project=$(postgres_compose_project "$container")
    if [[ -n "$project" ]]; then
        printf '%s\n' docker compose -p "$project" -f "$compose" stop "$service"
    else
        printf '%s\n' docker stop "$container"
    fi
}

# Stop one docker service with its own compose project (see docker_service_stop_command).
# With DRY_RUN=1, print the command instead of running it.
stop_docker_service() {
    local service="$1"
    local cmd=()
    while IFS= read -r word; do
        cmd+=("$word")
    done < <(docker_service_stop_command "$service")
    if [[ ${#cmd[@]} -eq 0 ]]; then
        echo "[$service] No eap_${service} container, skipping"
        return 0
    fi
    if [[ "${DRY_RUN:-0}" == "1" ]]; then
        echo "[dry-run] ${cmd[*]}"
        return 0
    fi
    echo "[$service] ${cmd[*]}"
    "${cmd[@]}"
}

# Stop the docker services (postgres, lgtm), each under its own compose project. Never `down`.
stop_docker_services() {
    local service
    for service in postgres lgtm; do
        stop_docker_service "$service"
    done
}
