#!/usr/bin/env bash
# dev-down.sh --all stops each docker service under ITS OWN compose project (from the container's
# com.docker.compose.project label), never with down/rm/-v, and --dry-run stops nothing.
# Uses a stub `docker` on PATH: the real daemon (and the running dev stack) is never contacted.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEST_ROOT="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/eap-dev-down.XXXXXX")" && pwd)"  # as dev-lib resolves it
trap 'rm -rf "$TEST_ROOT"' EXIT

mkdir -p "${TEST_ROOT}/scripts" "${TEST_ROOT}/bin" "${TEST_ROOT}/infrastructure/docker"
cp "${SOURCE_ROOT}/scripts/dev-lib.sh" "${SOURCE_ROOT}/scripts/dev-down.sh" "${TEST_ROOT}/scripts/"
COMPOSE="${TEST_ROOT}/infrastructure/docker/compose.yaml"
CALLS="${TEST_ROOT}/docker-calls"
: >"$CALLS"

# Stub: `docker inspect eap_postgres` -> project eap, eap_lgtm -> project docker, eap_nolabel ->
# no label; anything else is "no such container". Every call is logged; non-inspect calls fail.
cat >"${TEST_ROOT}/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"$DOCKER_STUB_CALLS"
if [[ "$1" != "inspect" ]]; then
    echo "stub docker: refusing non-inspect call: $*" >&2
    exit 97
fi
container="$2"
case "${DOCKER_STUB_MODE:-}:$container" in
    *:eap_postgres) label=eap ;;
    nolabel:eap_lgtm) label="" ;;
    absent:eap_lgtm) exit 1 ;;
    *:eap_lgtm) label=docker ;;
    *) exit 1 ;;
esac
if [[ "$*" == *com.docker.compose.project* ]]; then
    echo "$label"
fi
exit 0
STUB
chmod +x "${TEST_ROOT}/bin/docker"

run_down() {
    env PATH="${TEST_ROOT}/bin:$PATH" DOCKER_STUB_CALLS="$CALLS" DOCKER_STUB_MODE="${1}" \
        bash "${TEST_ROOT}/scripts/dev-down.sh" "${@:2}"
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# 1. --all --dry-run: the exact command per service, with that container's own project.
out=$(run_down "" --all --dry-run)
echo "$out"
grep -qxF "[dry-run] docker compose -p eap -f ${COMPOSE} stop postgres" <<<"$out" || fail "postgres not stopped under -p eap"
grep -qxF "[dry-run] docker compose -p docker -f ${COMPOSE} stop lgtm" <<<"$out" || fail "lgtm not stopped under -p docker"
grep -qE '(^| )(down|rm)( |$)|(^| )-v( |$)' <<<"$out" && fail "a destructive docker command was printed"
grep -q "No pid file" <<<"$out" || fail "app services not reported"
if grep -vE '^inspect ' "$CALLS"; then fail "dry run made a non-inspect docker call"; fi

# 2. EAP_DEV_DOWN_DRY_RUN=1 is the same as --dry-run.
out=$(EAP_DEV_DOWN_DRY_RUN=1 run_down "" --all)
grep -qxF "[dry-run] docker compose -p eap -f ${COMPOSE} stop postgres" <<<"$out" || fail "env dry run"

# 3. A container without a compose label is stopped by name; a missing one is skipped.
out=$(run_down nolabel --all --dry-run)
grep -qxF "[dry-run] docker stop eap_lgtm" <<<"$out" || fail "unlabelled container not stopped by name"
out=$(run_down absent --all --dry-run)
grep -qxF "[lgtm] No eap_lgtm container, skipping" <<<"$out" || fail "missing container not skipped"
grep -q "stop lgtm" <<<"$out" && fail "missing container was stopped"

# 4. Without --all, docker services are skipped and docker is never called.
: >"$CALLS"
out=$(run_down "" postgres --dry-run)
grep -q "Skipping docker service: postgres (use --all)" <<<"$out" || fail "postgres not skipped without --all"
[[ -s "$CALLS" ]] && fail "docker called without --all"

# 5. Without --dry-run, the real command runs (the stub rejects it, so the failure proves the
#    exact argv reached docker): no blanket project-less `compose stop` follows.
: >"$CALLS"
if run_down "" lgtm --all >/dev/null 2>&1; then fail "non-dry run did not call docker stop"; fi
grep -qxF "compose -p docker -f ${COMPOSE} stop lgtm" "$CALLS" || fail "non-dry run argv: $(cat "$CALLS")"
grep -qE '^compose -f ' "$CALLS" && fail "a project-less compose call was made"

echo "dev-down dry-run test: OK"
