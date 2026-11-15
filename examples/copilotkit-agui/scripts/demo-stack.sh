#!/usr/bin/env bash
# A THROWAWAY PydanticOS stack for trying (and checking) the CopilotKit example, then teardown.
#
# Brings up, on its own ports, and removes on exit (Ctrl-C):
#   - Postgres (pgvector image, the repo's init scripts, migrated to head) in container
#     $DEMO_PG_CONTAINER on 127.0.0.1:$DEMO_PG_PORT, volume $DEMO_PG_VOLUME;
#   - the API on 127.0.0.1:$DEMO_API_PORT and one worker on that database, EAP_AUTH_MODE=dev,
#     the scripted test:* models only, no OTLP, no LiteLLM;
#   - the AG-UI smoke test's MCP server (tests/ag-ui-smoke/mcp_server.py) on 127.0.0.1:$DEMO_MCP_PORT;
#   - the seed (scripts/seed.mjs --scripted), which writes .env.local;
#   - the example (`next dev`) on http://127.0.0.1:$DEMO_WEB_PORT.
# It never reads the repo's .env files and never touches the dev stack (:8000, eap_postgres).
#
# Env: DEMO_PG_PORT (54359), DEMO_API_PORT (8210), DEMO_MCP_PORT (8219), DEMO_WEB_PORT (3110),
# DEMO_KEEP=1 (keep the database container and volume), DEMO_LOG_DIR (default .dev/copilotkit-demo).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"

PG_PORT="${DEMO_PG_PORT:-54359}"
API_PORT="${DEMO_API_PORT:-8210}"
MCP_PORT="${DEMO_MCP_PORT:-8219}"
WEB_PORT="${DEMO_WEB_PORT:-3110}"
PG_CONTAINER="${DEMO_PG_CONTAINER:-eap_copilotkit_demo_pg}"
PG_VOLUME="${DEMO_PG_VOLUME:-eap_copilotkit_demo_pgdata}"
PG_IMAGE="pgvector/pgvector:0.8.2-pg17-trixie"
LOG_DIR="${DEMO_LOG_DIR:-$REPO/.dev/copilotkit-demo}"
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
    if [[ "$started_pg" -eq 1 && "${DEMO_KEEP:-0}" != "1" ]]; then
        docker rm -f "$PG_CONTAINER" >/dev/null 2>&1 || true
        docker volume rm "$PG_VOLUME" >/dev/null 2>&1 || true
    fi
    # The seeded key belongs to the database just removed.
    [[ "${DEMO_KEEP:-0}" != "1" ]] && rm -f "$HERE/.env.local"
    exit "$code"
}
trap cleanup EXIT INT TERM

start() {
    local name="$1"; shift
    set -m
    "$@" >"$LOG_DIR/$name.log" 2>&1 &
    pids+=("$!")
    set +m
}

wait_http() {
    local url="$1" what="$2" log="$3"
    for _ in $(seq 1 240); do
        if curl -s -o /dev/null "$url"; then return 0; fi
        sleep 0.5
    done
    echo "ERROR: $what did not come up ($url)" >&2
    tail -40 "$log" >&2 || true
    return 1
}

for port in "$PG_PORT" "$API_PORT" "$MCP_PORT" "$WEB_PORT"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "ERROR: port $port is in use (set DEMO_*_PORT)" >&2
        exit 1
    fi
done
if docker ps -a --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
    echo "ERROR: container $PG_CONTAINER already exists; remove it first" >&2
    exit 1
fi

SCARF_ANALYTICS=false pnpm -C "$HERE" install --frozen-lockfile --silent

unset OTEL_EXPORTER_OTLP_ENDPOINT LITELLM_API_KEY EAP_LITELLM_API_KEY EAP_LITELLM_BASE_URL OPENAI_BASE_URL \
      ANTHROPIC_API_KEY EAP_LOKI_URL EAP_CRASH_AT
start mcp uv run --no-sync python "$REPO/tests/ag-ui-smoke/mcp_server.py" "$MCP_PORT"
wait_http "http://127.0.0.1:$MCP_PORT/calls" "MCP server" "$LOG_DIR/mcp.log"

started_pg=1
docker run -d --name "$PG_CONTAINER" -p "127.0.0.1:$PG_PORT:5432" \
    -e POSTGRES_USER=eap_admin -e POSTGRES_PASSWORD=eap_admin_password -e POSTGRES_DB=postgres \
    -v "$PG_VOLUME:/var/lib/postgresql/data" \
    -v "$REPO/infrastructure/docker/init-scripts:/docker-entrypoint-initdb.d:ro" \
    "$PG_IMAGE" -c max_connections=200 >/dev/null
for _ in $(seq 1 120); do
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
export EAP_APP_VERSION=copilotkit-demo
export EAP_EXECUTOR_ID=copilotkit-demo-worker-1
export EAP_ALLOWED_MODELS='test:*'
export EAP_MODEL_PRICES_FILE="$REPO/config/model-prices.test.json"
export EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1
export EAP_QUEUE_POLL_SECONDS=0.2
export EAP_APPROVAL_POLL_SECONDS=1

uv run --no-sync alembic -c packages/db/alembic.ini upgrade head >"$LOG_DIR/migrate.log" 2>&1 \
    || { tail -40 "$LOG_DIR/migrate.log" >&2; exit 1; }
start worker uv run --no-sync python -m eap_worker
start api uv run --no-sync uvicorn eap_api.app:create_app --factory --host 127.0.0.1 --port "$API_PORT"
wait_http "http://127.0.0.1:$API_PORT/healthz" "API" "$LOG_DIR/api.log"

node "$HERE/scripts/seed.mjs" "http://127.0.0.1:$API_PORT" "http://127.0.0.1:$MCP_PORT/mcp" --scripted

# The example's own server: no EAP_* variables (it only needs .env.local).
start web env -u EAP_DATABASE_URL -u EAP_DBOS_URL -u DBOS_SYSTEM_DATABASE_URL \
    COPILOTKIT_TELEMETRY_DISABLED=true PORT="$WEB_PORT" pnpm -C "$HERE" exec next dev --hostname 127.0.0.1 --port "$WEB_PORT"
wait_http "http://127.0.0.1:$WEB_PORT/" "Next.js" "$LOG_DIR/web.log"

cat <<EOF
== The CopilotKit example is up: http://127.0.0.1:$WEB_PORT   (logs: $LOG_DIR)
   ?agent=pydanticosAgent     MCP tool:            type  {"tool": "lookup_order", "args": {"order_id": "42"}}
   ?agent=browserTools        useFrontendTool:     type  {"tool": "set_theme_color", "args": {"color": "teal"}}
                              useHumanInTheLoop:   type  {"tool": "confirm_with_user", "args": {"question": "Ship it?"}}
   ?agent=gatedTickets        tool approval:       type  {"tool": "tickets.create", "args": {"title": "Printer", "body": "On fire"}}
   ?agent=pydanticosWorkflow  workflow approval:   type  {"q": "refund order 42"}
   Ctrl-C stops everything and removes the database.
EOF
wait "${pids[@]}"
