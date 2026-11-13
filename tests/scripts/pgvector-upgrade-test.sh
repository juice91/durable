#!/usr/bin/env bash
# RM.1 upgrade path on THROWAWAY containers and volumes (never eap_postgres / eap_postgres_data):
# postgres:17 with data in eap_app and eap_dbos and a PENDING DBOS workflow, swapped to the pinned
# pgvector image on the same volume, then ensure_vector_extension from scripts/dev-lib.sh.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="$(openssl rand -hex 4)"
PROJ="eaptest${TAG}"
C="eap_pgv_upg_${TAG}"; V="${PROJ}_postgres_data"
CL="eap_pgv_lbl_${TAG}"; VL="eaptestlbl${TAG}_postgres_data"
CF="eap_pgv_fresh_${TAG}"; VF="eaptestfresh${TAG}_postgres_data"
GUARD_DIR=""
mkdir -p "${SOURCE_ROOT}/.dev"  # inside the repo (gitignored): Docker shares it, not always $TMPDIR
INIT_DIR="$(mktemp -d "${SOURCE_ROOT}/.dev/pgv-init.XXXXXX")"
NEW_INIT="${SOURCE_ROOT}/infrastructure/docker/init-scripts"
NEW_IMAGE=$(sed -n 's/^ *image: \(pgvector\/pgvector:[^ ]*\)$/\1/p' "${SOURCE_ROOT}/infrastructure/docker/compose.yaml")
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
WF="wf_pgv_${TAG}"

cleanup() {
    docker rm -f "$C" "$CL" "$CF" >/dev/null 2>&1 || true
    docker volume rm -f "$V" "$VL" "$VF" >/dev/null 2>&1 || true
    rm -rf "$INIT_DIR"
    [[ -n "$GUARD_DIR" ]] && rm -rf "$GUARD_DIR"
    return 0
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
[[ -n "$NEW_IMAGE" ]] || fail "no pgvector image pinned in compose.yaml"

# The init script without its pgvector step (postgres:17 has no extension to create).
grep -vi vector "${NEW_INIT}/01-setup-databases.sh" > "${INIT_DIR}/01-setup-databases.sh"
chmod 755 "$INIT_DIR" "${INIT_DIR}/01-setup-databases.sh"

run_pg() { # container image volume initdir [port] [extra docker args...]
    local name="$1" image="$2" vol="$3" init="$4" port="${5:-}"
    shift 5 || shift 4
    local pub=()
    [[ -n "$port" ]] && pub=(-p "127.0.0.1:${port}:5432")
    # ${pub[@]+...}: an empty array under set -u is an error in bash 3.2 (macOS).
    docker run -d --name "$name" "$@" ${pub[@]+"${pub[@]}"} \
        -e POSTGRES_USER=eap_admin -e POSTGRES_PASSWORD=eap_admin_password -e POSTGRES_DB=postgres \
        -v "${vol}:/var/lib/postgresql/data" -v "${init}:/docker-entrypoint-initdb.d:ro" "$image" >/dev/null
}
wait_pg() {
    for _ in $(seq 1 90); do
        docker exec "$1" psql -h 127.0.0.1 -U eap_admin -d eap_app -tAc 'select 1' >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "postgres in $1 did not come up"
}
psql_in() { local c="$1" u="$2" d="$3"; shift 3; docker exec "$c" psql -X -q -h 127.0.0.1 -U "$u" -d "$d" -v ON_ERROR_STOP=1 -tA "$@"; }
counts() { # every base table of both databases: db.schema.table=count
    local db
    for db in eap_app eap_dbos; do
        psql_in "$1" eap_admin "$db" -c "
          SELECT '$db.' || table_schema || '.' || table_name || '=' ||
                 (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text
          FROM information_schema.tables
          WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_type = 'BASE TABLE' ORDER BY 1"
    done
}
wf_fingerprint() { psql_in "$1" eap_admin eap_dbos -c "SELECT status, md5(w::text) FROM dbos.workflow_status w WHERE workflow_uuid = '$WF'"; }
dbos_tables() { psql_in "$1" eap_admin eap_dbos -c "SELECT table_name FROM information_schema.tables WHERE table_schema = 'dbos' ORDER BY 1"; }
ensure() { (cd "$SOURCE_ROOT" && bash -c 'source scripts/dev-lib.sh; ensure_vector_extension "$1"' _ "$1"); }

echo "1. postgres:17 with data in eap_app and eap_dbos"
run_pg "$C" postgres:17 "$V" "$INIT_DIR" "$PORT"; wait_pg "$C"
psql_in "$C" eap_app eap_app -c "CREATE TABLE proof_rows (id int PRIMARY KEY, payload text);
    INSERT INTO proof_rows SELECT g, md5(g::text) FROM generate_series(1, 1000) g"
psql_in "$C" eap_dbos eap_dbos -c "CREATE TABLE proof_checkpoints (id text PRIMARY KEY, status text);
    INSERT INTO proof_checkpoints SELECT 'c_' || g, 'PENDING' FROM generate_series(1, 300) g"
DBOS_URL="postgresql://eap_dbos:eap_dbos_password@127.0.0.1:${PORT}/eap_dbos"
(cd "$SOURCE_ROOT" && uv run python tests/scripts/pgvector_dbos_probe.py start "$DBOS_URL" "$WF") >/dev/null 2>&1 || true
BEFORE_WF=$(wf_fingerprint "$C")
[[ "$BEFORE_WF" == PENDING\|* ]] || fail "expected a PENDING workflow row before the swap, got '${BEFORE_WF}'"
BEFORE_TABLES=$(dbos_tables "$C")
grep -qx workflow_status <<<"$BEFORE_TABLES" || fail "dbos.workflow_status missing before the swap"
BEFORE=$(counts "$C")
grep -q '^eap_app.public.proof_rows=1000$' <<<"$BEFORE" || fail "eap_app data not loaded"
grep -q '^eap_dbos.public.proof_checkpoints=300$' <<<"$BEFORE" || fail "eap_dbos data not loaded"
grep -q '^eap_dbos.dbos.workflow_status=1$' <<<"$BEFORE" || fail "workflow row not counted"

echo "2. ensure_vector_extension on an image without pgvector exits 1 and prints swap steps with -p ${PROJ}"
set +e; OUT=$(ensure "$C" 2>&1); RC=$?; set -e
[[ $RC -eq 1 ]] || fail "expected exit 1 on postgres:17, got $RC: $OUT"
grep -q 'has no pgvector' <<<"$OUT" || fail "no 'has no pgvector' message: $OUT"
[[ $(grep -c -- "-p ${PROJ} " <<<"$OUT") -ge 2 ]] || fail "swap steps lack '-p ${PROJ}': $OUT"
grep -q "docker rm -f ${C}" <<<"$OUT" || fail "unlabeled container: no docker rm step: $OUT"
grep -q "${V}" <<<"$OUT" || fail "swap steps do not name the volume ${V}"
[[ -z "$(psql_in "$C" eap_admin eap_app -c "select 1 from pg_extension where extname='vector'")" ]] || fail "extension appeared on postgres:17"

echo "2b. a compose-labelled container: the project comes from its label"
LBL="eaptestlbl${TAG}"
run_pg "$CL" postgres:17 "$VL" "$INIT_DIR" "" --label "com.docker.compose.project=${LBL}"; wait_pg "$CL"
set +e; OUT=$(ensure "$CL" 2>&1); RC=$?; set -e
[[ $RC -eq 1 ]] || fail "labelled: expected exit 1, got $RC"
[[ $(grep -c -- "-p ${LBL} " <<<"$OUT") -ge 2 ]] || fail "labelled: steps lack '-p ${LBL}': $OUT"
! grep -q 'docker rm -f' <<<"$OUT" || fail "labelled: compose-created container must not need docker rm"
docker rm -f "$CL" >/dev/null; docker volume rm -f "$VL" >/dev/null

echo "3. swap to ${NEW_IMAGE} on the same volume"
docker stop "$C" >/dev/null; docker rm "$C" >/dev/null
run_pg "$C" "$NEW_IMAGE" "$V" "$NEW_INIT" "$PORT"; wait_pg "$C"
[[ -z "$(psql_in "$C" eap_admin eap_app -c "select 1 from pg_extension where extname='vector'")" ]] \
    || fail "init script re-ran on an existing volume"
set +e; OUT1=$(ensure "$C" 2>&1); RC=$?; set -e
[[ $RC -eq 0 ]] || fail "ensure_vector_extension failed after the swap: $OUT1"
grep -q 'vector extension created in eap_app' <<<"$OUT1" || fail "first run did not create the extension: $OUT1"
[[ -n "$(psql_in "$C" eap_admin eap_app -c "select 1 from pg_extension where extname='vector'")" ]] || fail "extension missing"
set +e; OUT2=$(ensure "$C" 2>&1); RC=$?; set -e
[[ $RC -eq 0 ]] || fail "second run failed: $OUT2"
grep -q 'present in eap_app' <<<"$OUT2" || fail "second run is not a no-op: $OUT2"
! grep -q 'creating the vector extension' <<<"$OUT2" || fail "second run created again: $OUT2"
for o in "$OUT1" "$OUT2"; do
    ! grep -qi 'collation' <<<"$o" || fail "collation warning from ensure_vector_extension: $o"
done
! docker logs "$C" 2>&1 | grep -qi 'collation version' || fail "collation version warning in the server log"
DRIFT=$(psql_in "$C" eap_admin postgres -c "select string_agg(datname, ',') from pg_database where datname in ('eap_app','eap_dbos','postgres') and datcollversion is distinct from pg_database_collation_actual_version(oid)")
[[ -z "$DRIFT" ]] || fail "collation drift: $DRIFT"

echo "4. data unchanged"
AFTER=$(counts "$C")
[[ "$BEFORE" == "$AFTER" ]] || { diff <(echo "$BEFORE") <(echo "$AFTER") || true; fail "row counts changed"; }
[[ "$(wf_fingerprint "$C")" == "$BEFORE_WF" ]] || fail "workflow_status row changed (status/columns)"
[[ "$(dbos_tables "$C")" == "$BEFORE_TABLES" ]] || fail "dbos schema tables changed"
[[ "$(psql_in "$C" eap_app eap_app -c "SELECT '[1,0]'::vector <=> '[0,1]'::vector")" == 1 ]] || fail "app role cannot use vector"

echo "5. the pending DBOS workflow recovers on the new image (real DBOS recovery)"
# The probe polls with its own 45 s deadline; timeout 60 is the backstop for a hang in DBOS itself.
RECOVER=$(cd "$SOURCE_ROOT" && timeout 60 uv run python tests/scripts/pgvector_dbos_probe.py recover "$DBOS_URL" "$WF" 2>&1 | tail -3)
grep -qx 'result=recovered' <<<"$RECOVER" || fail "workflow did not recover: $RECOVER"
grep -qx 'status=SUCCESS' <<<"$RECOVER" || fail "workflow status not SUCCESS: $RECOVER"

echo "6. a fresh volume on ${NEW_IMAGE} gets the extension from the init script alone"
run_pg "$CF" "$NEW_IMAGE" "$VF" "$NEW_INIT" ""; wait_pg "$CF"
EXT=$(psql_in "$CF" eap_admin eap_app -c "select extversion from pg_extension where extname='vector'")
[[ -n "$EXT" ]] || fail "fresh volume: the init script did not create the vector extension"
[[ "$(psql_in "$CF" eap_app eap_app -c "SELECT '[1,0]'::vector <=> '[0,1]'::vector")" == 1 ]] || fail "fresh volume: app role cannot use vector"
docker rm -f "$CF" >/dev/null; docker volume rm -f "$VF" >/dev/null

echo "7. compose up refuses to start a new container while another *_postgres_data volume exists"
GUARD_DIR="$(mktemp -d "${SOURCE_ROOT}/.dev/pgvguard${TAG}.XXXXXX")"
GUARD_PROJECT=$(basename "$GUARD_DIR" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
cp "${SOURCE_ROOT}/infrastructure/docker/compose.yaml" "${GUARD_DIR}/compose.yaml"
docker volume create "$V" >/dev/null  # an "other" volume (the step 1-5 one, recreated if removed)
guard() { (cd "$SOURCE_ROOT" && bash -c 'source scripts/dev-lib.sh; postgres_volume_guard "$1"' _ "${GUARD_DIR}/compose.yaml"); }
set +e; OUT=$(guard 2>&1); RC=$?; set -e
[[ $RC -eq 1 ]] || fail "guard: expected refusal, got $RC: $OUT"
grep -q "REFUSED" <<<"$OUT" || fail "guard: no REFUSED message: $OUT"
grep -q "${GUARD_PROJECT}_postgres_data" <<<"$OUT" || fail "guard: default volume not named: $OUT"
grep -q -- "-p ${PROJ} " <<<"$OUT" || fail "guard: no '-p ${PROJ}' command for ${V}: $OUT"
set +e; OUT=$(EAP_POSTGRES_ALLOW_DEFAULT_VOLUME=1 guard 2>&1); RC=$?; set -e
[[ $RC -eq 0 ]] || fail "guard: EAP_POSTGRES_ALLOW_DEFAULT_VOLUME=1 should accept the default volume: $OUT"

echo "pgvector upgrade tests passed"
