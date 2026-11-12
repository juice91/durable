#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/eap-dev-scripts.XXXXXX")"
UNRELATED_PID=""

cleanup() {
    if [[ -n "$UNRELATED_PID" ]]; then
        kill "$UNRELATED_PID" 2>/dev/null || true
        wait "$UNRELATED_PID" 2>/dev/null || true
    fi
    if [[ -f "${TEST_ROOT}/.dev/run/worker.pid" ]]; then
        bash "${TEST_ROOT}/scripts/dev-down.sh" worker >/dev/null 2>&1 || true
    fi
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

mkdir -p "${TEST_ROOT}/scripts" "${TEST_ROOT}/tests/fixtures"
cp "${SOURCE_ROOT}/scripts/dev-lib.sh" "${SOURCE_ROOT}/scripts/dev-down.sh" "${TEST_ROOT}/scripts/"

# The single-quoted program is intentionally expanded by the clean subshell.
# shellcheck disable=SC2016
env_output=$(env -i PATH="$PATH" HOME="$HOME" bash -c '
    source "$1/scripts/dev-lib.sh"
    ensure_env
    printf "%s\n" "$EAP_DATABASE_URL" "$EAP_DBOS_URL" "$DBOS_SYSTEM_DATABASE_URL" "$EAP_APP_VERSION" "$EAP_AUTH_MODE"
' _ "$TEST_ROOT")
expected_env=$(printf '%s\n' \
    'postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app' \
    'postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos' \
    'postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos' \
    'm1' \
    'dev')
if [[ "$env_output" != "$expected_env" ]]; then
    echo "fresh environment defaults did not match API/worker configuration" >&2
    exit 1
fi

cat > "${TEST_ROOT}/tests/fixtures/dummy-wrapper.sh" <<'EOF'
#!/usr/bin/env bash
trap 'exit 0' TERM INT
trap '' HUP
bash -c 'trap "" TERM INT HUP; while :; do sleep 1; done' &
echo "$!" > "$1"
while :; do sleep 1; done
EOF
chmod +x "${TEST_ROOT}/tests/fixtures/dummy-wrapper.sh"

# shellcheck source=/dev/null
source "${TEST_ROOT}/scripts/dev-lib.sh"
ensure_dirs
child_pid_file="${TEST_ROOT}/.dev/run/dummy-child.pid"
start_process_group worker "${TEST_ROOT}/.dev/logs/worker.log" "$TEST_ROOT" \
    bash "${TEST_ROOT}/tests/fixtures/dummy-wrapper.sh" "$child_pid_file"

for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -s "$child_pid_file" ]] && break
    sleep 0.1
done
child_pid=$(cat "$child_pid_file")
bash "${TEST_ROOT}/scripts/dev-down.sh" worker >/dev/null
if kill -0 "$child_pid" 2>/dev/null; then
    child_state=$(ps -o state= -p "$child_pid" 2>/dev/null | tr -d ' ')
    if [[ "$child_state" != Z* ]]; then
        echo "descendant process survived dev-down (pid $child_pid)" >&2
        exit 1
    fi
fi

sleep 60 &
UNRELATED_PID=$!
unrelated_start=$(process_start_time "$UNRELATED_PID")
cat > "${TEST_ROOT}/.dev/run/api.pid" <<EOF
pid=$UNRELATED_PID
pgid=$(get_pgrp "$UNRELATED_PID")
start=${unrelated_start}-not-the-same-process
command=sleep 60
EOF
bash "${TEST_ROOT}/scripts/dev-down.sh" api >/dev/null
if ! kill -0 "$UNRELATED_PID" 2>/dev/null; then
    echo "dev-down signalled an unrelated process from a stale pid file" >&2
    exit 1
fi
if [[ -e "${TEST_ROOT}/.dev/run/api.pid" ]]; then
    echo "dev-down did not remove the stale pid file" >&2
    exit 1
fi

echo "dev lifecycle tests passed"
