#!/usr/bin/env bash
set -euo pipefail
tool=$(basename "$0")
printf '%s %s\n' "$tool" "$*" >>"${STUB_LOG}"
# Record whether a colima start would be tracked (and killed) by a GitHub Actions runner.
[[ "$tool" != colima || "${1:-}" != start ]] || printf 'COLIMA_START_TRACKING_ID=[%s]\n' "${RUNNER_TRACKING_ID-unset}" >>"${STUB_LOG}"
# Like the real tools, consume a manifest piped in with `-f -`. Exiting without reading it
# lets the writer (e.g. `kubectl create secret ... -o yaml | kubectl apply -f -`) die of
# SIGPIPE, and under pipefail that kills up.sh with exit 141 at random.
if [[ " $* " == *" -f - "* || " $* " == *" --filename=- "* || " $* " == *" --filename - "* ]]; then
  cat >/dev/null || true
fi
if [[ -n "${STUB_FAIL_SUBSTRING:-}" && " $* " == *" ${STUB_FAIL_SUBSTRING} "* ]]; then
  exit 1
fi
# Fails the first call whose arguments contain these words; later calls succeed.
if [[ -n "${STUB_FAIL_ONCE:-}" && " $* " == *" ${STUB_FAIL_ONCE} "* && ! -e "${STUB_STATE}/failed_once" ]]; then
  : >"${STUB_STATE}/failed_once"
  exit 1
fi
# Prints 1, 2, 3, ... for each call with the same NAME.
_count() {
  local file="${STUB_STATE}/count_$1" n=0
  [[ ! -f "$file" ]] || n=$(<"$file")
  printf '%s\n' "$((n + 1))" | tee "$file"
}
_shift_flags() {
  while (($#)); do
    case "$1" in
      -n|--namespace|--context|--filename|-f|--from-env-file|--type|--patch|--timeout|--for|--replicas|-o|--output)
        shift 2 || true ;;
      --dry-run=*|--ignore-not-found|--wait|--wait=*|--address|--filename=*|-f*)
        shift ;;
      --*=*|-*)
        shift ;;
      *)
        printf '%s\n' "$1"
        return 0 ;;
    esac
  done
}
case "$tool" in
  docker)
    if [[ " $* " == *" context inspect "* ]]; then
      printf '%s\n' "${STUB_DOCKER_HOST:-unix://${HOME}/.colima/pydanticos/docker.sock}"
    elif [[ "${1:-}" == context && "${2:-}" == show ]]; then
      if [[ -n "${STUB_DOCKER_SHOW:-}" ]]; then printf '%s\n' "$STUB_DOCKER_SHOW"
      else cat "${STUB_STATE}/docker_context"; fi
    elif [[ "${1:-}" == context && "${2:-}" == use ]]; then
      printf '%s\n' "$3" >"${STUB_STATE}/docker_context"
    fi
    ;;
  kubectl)
    # pg_restore --list is fed the dump on stdin. Reject a chosen archive body.
    if [[ " $* " == *" --list "* ]]; then
      body=$(cat || true)
      if [[ -n "${STUB_LIST_REJECT:-}" && "$body" == *"${STUB_LIST_REJECT}"* ]]; then
        exit 1
      fi
    fi
    verb=$(_shift_flags "$@")
    if [[ "$verb" == config && "${2:-${1:-}}" == config ]]; then
      :
    fi
    if [[ " $* " == *" config current-context "* ]]; then
      if [[ -n "${STUB_KUBE_SHOW:-}" ]]; then printf '%s\n' "$STUB_KUBE_SHOW"
      else cat "${STUB_STATE}/kube_context"; fi
    elif [[ " $* " == *" config use-context "* ]]; then
      printf '%s\n' "${@: -1}" >"${STUB_STATE}/kube_context"
    elif [[ "$verb" == kustomize ]]; then
      "${REAL_KUBECTL}" "$@"
    elif [[ "$verb" == get && " $* " == *" get job migrate "* ]]; then
      migrate_gets_file="${STUB_STATE}/migrate_get_count"
      migrate_gets=0
      if [[ -f "$migrate_gets_file" ]]; then
        migrate_gets=$(<"$migrate_gets_file")
      fi
      migrate_gets=$((migrate_gets + 1))
      printf '%s\n' "$migrate_gets" >"$migrate_gets_file"
      if [[ -n "${STUB_MIGRATE_GET_ERRORS:-}" && "$migrate_gets" -le "${STUB_MIGRATE_GET_ERRORS}" ]]; then
        printf '%s\n' 'error: jobs.batch "migrate" not found' >&2
        exit 1
      fi
      # Match kubectl jsonpath: a missing count is empty, so failure is " 1".
      migrate_status=${STUB_MIGRATE_STATUS:-succeeded}
      # The first STUB_MIGRATE_LOCK_FAILURES attempts (counted by the unsuspend patch) fail on a lock.
      [[ ! -e "${STUB_STATE}/lock_failure" ]] || migrate_status=failed
      case "$migrate_status" in
        failed) printf '%s' ' 1' ;;
        pending) printf '%s' '0 0' ;;
        *) printf '%s' '1 ' ;;
      esac
    elif [[ "$verb" == exec && " $* " == *" -i "* && " $* " == *" psql "* ]]; then
      # up.sh pipes the role password and the CREATE ROLE/DATABASE SQL in; consume it like the real tool.
      cat >/dev/null || true
    elif [[ "$verb" == patch && " $* " == *" patch job migrate "* ]]; then
      attempt=$(_count migrate_patch)
      rm -f "${STUB_STATE}/lock_failure"
      if ((attempt <= ${STUB_MIGRATE_LOCK_FAILURES:-0})); then : >"${STUB_STATE}/lock_failure"; fi
    elif [[ "$verb" == exec && " $* " == *" psql "* && " $* " == *"alembic_version"* ]]; then
      # The core revision: it advances once the migrate Job ran, when STUB_SCHEMA_ADVANCES is set.
      if [[ -e "${STUB_STATE}/count_migrate_patch" ]]; then
        [[ -z "${STUB_SCHEMA_UNREADABLE_AFTER:-}" ]] || exit 2
        if [[ -n "${STUB_SCHEMA_ADVANCES:-}" ]]; then printf '%s\n' 0043_next; else printf '%s\n' 0041_previous; fi
      elif [[ -n "${STUB_SCHEMA_ABSENT:-}" ]]; then
        printf '%s\n' 'ERROR:  relation "public.alembic_version" does not exist' >&2
        exit 1
      else
        [[ -z "${STUB_SCHEMA_UNREADABLE:-}" ]] || exit 2
        printf '%s\n' 0041_previous
      fi
    elif [[ "$verb" == get && " $* " == *" get configmap deploy-in-progress "* ]]; then
      printf '%s' '2026-01-01T00:00:10Z'
    elif [[ "$verb" == get && " $* " == *" get pods "* && " $* " == *"name=postgres-backup"* ]]; then
      # STUB_BACKUP_PODS ("name phase created" lines) for the first STUB_BACKUP_POLLS listings (default: every one).
      polls=$(_count backup_list)
      if ((polls <= ${STUB_BACKUP_POLLS:-1000000})); then printf '%s\n' "${STUB_BACKUP_PODS:-}"; fi
    elif [[ "$verb" == get && " $* " == *" --ignore-not-found "* && " $* " == *"jsonpath={.spec.replicas}"* ]]; then
      # What runs before the deploy: "<replicas> [<image>]"; nothing for a name in STUB_ABSENT (comma list).
      workload="" prev=""
      for a in "$@"; do [[ "$prev" != get ]] || workload=${a#*/}; prev=$a; done
      if [[ ",${STUB_ABSENT:-}," != *",${workload},"* ]]; then
        case "$workload" in
          api) printf '%s' "${STUB_API_REPLICAS:-1} pydanticos-python:previous000" ;;
          worker) printf '%s' '1 pydanticos-python:previous000' ;;
          studio) printf '%s' '1 pydanticos-studio:previous000' ;;
          *) printf '%s' '1' ;;
        esac
      fi
    elif [[ "$verb" == exec && " $* " == *" psql "* ]]; then
      # The drain query: "<active>|<excluded>" (STUB_RUNS_COUNTS), 0|0 by default.
      printf '%s\n' "${STUB_RUNS_COUNTS:-0|0}"
    elif [[ "$verb" == exec && " $* " == *" eap_iam_localadmin setup-code "* ]]; then
      # The built-in admin's CLI in the API pod: STUB_SETUP_CODE is the code it prints; STUB_ADMIN_EXISTS: exit 3.
      [[ -z "${STUB_ADMIN_EXISTS:-}" ]] || exit 3
      [[ -z "${STUB_SETUP_CODE:-}" ]] || printf 'eap localadmin setup code: %s  (there is no admin account yet)\n' "$STUB_SETUP_CODE"
    elif [[ "$verb" == get && " $* " == *" get svc kubernetes "* ]]; then
      printf '%s' '10.43.0.1'
    elif [[ "$verb" == get && " $* " == *" get endpoints kubernetes "* ]]; then
      if [[ " $* " == *".ports"* ]]; then printf '%s' '57345'; else printf '%s' '192.168.5.3'; fi
    elif [[ "$verb" == logs && " $* " == *" job/migrate "* ]]; then
      if [[ -e "${STUB_STATE}/lock_failure" ]]; then
        printf '%s\n' 'psycopg.errors.LockNotAvailable: canceling statement due to lock timeout'
      elif [[ "${STUB_MIGRATE_STATUS:-}" == failed ]]; then
        printf '%s\n' 'ALEMBIC_ERROR_SENTINEL migration failed at revision'
      else
        printf '%s\n' 'alembic upgrade head ok'
      fi
    elif [[ "$verb" == create && " $* " == *" -o yaml "* ]]; then
      printf '%s\n' 'apiVersion: v1' 'kind: Secret' 'metadata: {name: pydanticos-secrets}' 'type: Opaque'
    fi
    ;;
  colima)
    if [[ "${1:-}" == status && -n "${STUB_COLIMA_STOPPED:-}${STUB_COLIMA_NEW:-}" ]]; then
      exit 1
    elif [[ "${1:-}" == list ]]; then
      if [[ -n "${STUB_COLIMA_NEW:-}" ]]; then
        printf '%s\n' 'PROFILE STATUS'
      elif [[ -n "${STUB_COLIMA_STOPPED:-}" ]]; then
        printf '%s\n' 'PROFILE STATUS' 'pydanticos Stopped'
      fi
    elif [[ "${1:-}" == ssh ]]; then
      printf '%s\n' '192.168.5.2 host.lima.internal'
    fi
    ;;
  ssh) ;;
esac
exit 0
