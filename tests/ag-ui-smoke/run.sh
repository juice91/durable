#!/usr/bin/env bash
# AG-UI smoke test (AGUI.14): @ag-ui/client 1.0.3 against a real API and worker.
#
# Default: brings up a THROWAWAY stack and removes it afterwards, never touching the dev stack:
#   - Postgres (pgvector image, the repo's init scripts) in container $SMOKE_PG_CONTAINER on
#     127.0.0.1:$SMOKE_PG_PORT, volume $SMOKE_PG_VOLUME, migrated to head;
#   - the API (uvicorn) on 127.0.0.1:$SMOKE_API_PORT and one worker, both on that database, with
#     the test:* models only, EAP_AUTH_MODE=dev, no OTLP export, no LiteLLM;
#   - the smoke's MCP server (mcp_server.py) on 127.0.0.1:$SMOKE_MCP_PORT.
#
# External mode: `run.sh --external` (or EAP_SMOKE_API_URL set) runs the scenarios against an
# already running stack, e.g. scripts/dev-up.sh: it starts only the MCP server, and that stack's
# worker must allow it (EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1) and run the test:* models.
#
# Env: SMOKE_PG_PORT (54349), SMOKE_API_PORT (8200), SMOKE_MCP_PORT (8209), SMOKE_KEEP=1 (leave the
# stack up for debugging; remove it with `docker rm -f $SMOKE_PG_CONTAINER && docker volume rm
# $SMOKE_PG_VOLUME`), EAP_SMOKE_API_URL (external mode), EAP_SMOKE_SCENARIOS (comma-separated
# names to run a subset). Logs go to $SMOKE_LOG_DIR (default .dev/ag-ui-smoke).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

PG_PORT="${SMOKE_PG_PORT:-54349}"
API_PORT="${SMOKE_API_PORT:-8200}"
MCP_PORT="${SMOKE_MCP_PORT:-8209}"
PG_CONTAINER="${SMOKE_PG_CONTAINER:-eap_agui_smoke_pg}"
PG_VOLUME="${SMOKE_PG_VOLUME:-eap_agui_smoke_pgdata}"
PG_IMAGE="pgvector/pgvector:0.8.2-pg17-trixie"
LOG_DIR="${SMOKE_LOG_DIR:-$REPO/.dev/ag-ui-smoke}"
EXTERNAL=0
[[ "${1:-}" == "--external" || -n "${EAP_SMOKE_API_URL:-}" ]] && EXTERNAL=1
mkdir -p "$LOG_DIR"

pids=()
started_pg=0
cleanup() {
    local code=$?
    for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; done
    for pid in "${pids[@]}"; do
        for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    done
    if [[ "$started_pg" -eq 1 && "${SMOKE_KEEP:-0}" != "1" ]]; then
        docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
        docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
    fi
    exit "$code"
}
trap cleanup EXIT INT TERM

# Each service in its own process group (set -m), so cleanup stops uv's children too.
start() {
    local name="$1"; shift
    set -m
    "$@" >"$LOG_DIR/$name.log" 2>&1 &
    pids+=("$!")
    set +m
}

wait_http() {
    local url="$1" what="$2" log="$3"
    for _ in $(seq 1 120); do
        if curl -s -o /dev/null "$url"; then return 0; fi
        sleep 0.5
    done
    echo "ERROR: $what did not come up ($url)" >&2
    tail -40 "$log" >&2 || true
    return 1
}

pnpm -C "$HERE" install --frozen-lockfile --silent

for port in "$MCP_PORT" $([[ "$EXTERNAL" -eq 0 ]] && echo "$PG_PORT $API_PORT"); do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ERROR: port $port is in use (set SMOKE_*_PORT)" >&2
        exit 1
    fi
done

unset OTEL_EXPORTER_OTLP_ENDPOINT LITELLM_API_KEY EAP_LITELLM_API_KEY EAP_LITELLM_BASE_URL OPENAI_BASE_URL \
      ANTHROPIC_API_KEY EAP_LOKI_URL EAP_CRASH_AT
start mcp uv run --no-sync python "$HERE/mcp_server.py" "$MCP_PORT"
wait_http "http://127.0.0.1:$MCP_PORT/calls" "MCP server" "$LOG_DIR/mcp.log"

if [[ "$EXTERNAL" -eq 0 ]]; then
    if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
        echo "ERROR: container $PG_CONTAINER already exists; remove it first" >&2
        exit 1
    fi
    started_pg=1
    docker run -d --name "$PG_CONTAINER" -p "127.0.0.1:$PG_PORT:5432" \
        -e POSTGRES_USER=eap_admin -e POSTGRES_PASSWORD=eap_admin_password -e POSTGRES_DB=postgres \
        -v "$PG_VOLUME:/var/lib/postgresql/data" \
        -v "$REPO/infrastructure/docker/init-scripts:/docker-entrypoint-initdb.d:ro" \
        "$PG_IMAGE" -c max_connections=200 >/dev/null
    for _ in $(seq 1 120); do
        # The init scripts run against a temporary server first; wait for the eap_app role's database.
        if docker exec "$PG_CONTAINER" pg_isready -h 127.0.0.1 -U eap_admin -d eap_app >/dev/null 2>&1 \
            && docker exec "$PG_CONTAINER" psql -h 127.0.0.1 -U eap_admin -d eap_app -tAc "SELECT 1" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done

    export EAP_DATABASE_URL="postgresql+psycopg://eap_app:eap_app_password@127.0.0.1:$PG_PORT/eap_app"
    export EAP_DBOS_URL="postgresql://eap_dbos:eap_dbos_password@127.0.0.1:$PG_PORT/eap_dbos"
    export DBOS_SYSTEM_DATABASE_URL="$EAP_DBOS_URL"
    export EAP_DBOS_SYSTEM_SCHEMA=dbos
    export EAP_AUTH_MODE=dev
    export EAP_APP_VERSION=agui-smoke
    export EAP_EXECUTOR_ID=agui-smoke-worker-1
    export EAP_ALLOWED_MODELS='test:*'
    export EAP_MODEL_PRICES_FILE="$REPO/config/model-prices.test.json"
    export EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1
    export EAP_QUEUE_POLL_SECONDS=0.2
    export EAP_APPROVAL_POLL_SECONDS=1
    export EAP_AGUI_PREVIOUS_TURN_WAIT_S=60

    uv run --no-sync alembic -c packages/db/alembic.ini upgrade head >"$LOG_DIR/migrate.log" 2>&1 \
        || { tail -40 "$LOG_DIR/migrate.log" >&2; exit 1; }
    start worker uv run --no-sync python -m eap_worker
    start api uv run --no-sync uvicorn eap_api.app:create_app --factory --host 127.0.0.1 --port "$API_PORT"
    wait_http "http://127.0.0.1:$API_PORT/healthz" "API" "$LOG_DIR/api.log"
    export EAP_SMOKE_API_URL="http://127.0.0.1:$API_PORT/api/v1"
fi

export EAP_SMOKE_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp"
echo "== AG-UI smoke against $EAP_SMOKE_API_URL (logs: $LOG_DIR)"
status=0
pnpm -C "$HERE" exec tsx smoke.ts || status=$?
if [[ "$status" -ne 0 && "$EXTERNAL" -eq 0 ]]; then
    echo "--- worker.log (tail)"; tail -30 "$LOG_DIR/worker.log" || true
    echo "--- api.log (tail)"; tail -30 "$LOG_DIR/api.log" || true
fi
exit "$status"
