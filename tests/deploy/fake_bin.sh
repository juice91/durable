#!/usr/bin/env bash
set -euo pipefail
tool=$(basename "$0")
printf '%s %s\n' "$tool" "$*" >>"${STUB_LOG}"
if [[ -n "${STUB_FAIL_SUBSTRING:-}" && " $* " == *" ${STUB_FAIL_SUBSTRING} "* ]]; then
  exit 1
fi
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
      case "${STUB_MIGRATE_STATUS:-succeeded}" in
        failed) printf '%s' ' 1' ;;
        pending) printf '%s' '0 0' ;;
        *) printf '%s' '1 ' ;;
      esac
    elif [[ "$verb" == logs && " $* " == *" job/migrate "* ]]; then
      if [[ "${STUB_MIGRATE_STATUS:-}" == failed ]]; then
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
