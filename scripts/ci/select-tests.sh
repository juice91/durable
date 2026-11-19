#!/usr/bin/env bash
# Decide what a CI job runs: the affected tests (pull requests) or everything (main, nightly, forced).
#
#   select-tests.sh python NAME="path path ..." [NAME="path ..."]...
#   select-tests.sh studio
#
# Environment: EVENT_NAME (github.event_name), PR_NUMBER, FULL_INPUT (workflow_dispatch input `full`),
# GH_TOKEN and GITHUB_REPOSITORY (to read the pull request's labels), GITHUB_BASE_REF,
# GITHUB_OUTPUT, GITHUB_STEP_SUMMARY. Outputs (to $GITHUB_OUTPUT):
#   python: mode=full|affected, reason, any=true|false, and per suite NAME_run=true|false, NAME_paths
#   studio: mode, reason, vitest_args
# The pull request selection lives in scripts/ci/affected.py; this script only decides full or
# affected and writes the step summary.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
event="${EVENT_NAME:-push}"
out="${GITHUB_OUTPUT:-/dev/null}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
full_label="ci:full"
# affected.py needs Python 3.11+ (tomllib); a runner whose python3 is older sets AFFECTED_PYTHON.
read -ra py <<<"${AFFECTED_PYTHON:-python3}"

emit() { printf '%s=%s\n' "$1" "$2" >>"$out"; }

# decide_mode: sets $mode and $reason.
decide_mode() {
  mode=affected
  reason="pull request: only the tests the change affects"
  case "$event" in
    pull_request)
      if labels="$(gh api "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/labels" --jq '.[].name' 2>/dev/null)"; then
        if grep -Fxq "$full_label" <<<"$labels"; then
          mode=full
          reason="label ${full_label} on the pull request"
        fi
      else
        # Cannot read the labels: do not guess the cheap answer.
        mode=full
        reason="could not read the pull request labels, running everything"
      fi
      ;;
    workflow_dispatch)
      if [[ "${FULL_INPUT:-true}" == "true" ]]; then
        mode=full
        reason="manual dispatch with full=true"
      else
        reason="manual dispatch with full=false: only the tests affected against the base branch"
      fi
      ;;
    *)
      mode=full
      reason="${event}: the full suites run"
      ;;
  esac
}

python_mode() {
  decide_mode
  local any=false suite name paths result
  emit mode "$mode"
  emit reason "$reason"
  {
    echo "### Python tests: ${mode}"
    echo
    echo "${reason}"
    echo
  } >>"$summary"
  for suite in "$@"; do
    name="${suite%%=*}"
    paths="${suite#*=}"
    if [[ "$mode" == full ]]; then
      result="has_tests=true
paths=${paths}"
      printf -- '- %s: full (%s)\n' "$name" "$paths" >>"$summary"
    else
      # shellcheck disable=SC2086  # word splitting of the path list is the point
      result="$("${py[@]}" "$here/affected.py" --format github --restrict-to $paths --summary /dev/null)"
      if grep -q '^has_tests=true' <<<"$result"; then
        printf -- '- %s: %s\n' "$name" "$(sed -n 's/^paths=//p' <<<"$result")" >>"$summary"
      else
        printf -- '- %s: no affected Python tests\n' "$name" >>"$summary"
      fi
    fi
    if grep -q '^has_tests=true' <<<"$result"; then
      emit "${name}_run" true
      any=true
    else
      emit "${name}_run" false
    fi
    emit "${name}_paths" "$(sed -n 's/^paths=//p' <<<"$result")"
  done
  emit any "$any"
  if [[ "$mode" == affected ]]; then
    {
      echo
      echo "Why (changed files to members):"
      echo
      "${py[@]}" "$here/affected.py" --format table
    } >>"$summary"
  fi
  if [[ "$any" == false ]]; then
    echo "no affected Python tests" | tee -a "$summary"
  fi
}

# Changes the import graph cannot see: a vitest run with --changed would miss their effect.
studio_full_triggers='^(package\.json|pnpm-lock\.yaml|pnpm-workspace\.yaml|\.npmrc|\.github/workflows/ci\.yml|apps/studio/(package\.json|vite\.config\.[^/]+|vitest\.[^/]+|tsconfig[^/]*\.json|index\.html|.*setup[^/]*\.[tj]sx?))$'

studio_mode() {
  decide_mode
  local base merge_base trigger=""
  if [[ "$mode" == affected ]]; then
    base="origin/${GITHUB_BASE_REF:-main}"
    if merge_base="$(git merge-base "$base" HEAD 2>/dev/null)"; then
      trigger="$(git diff --name-only --no-renames "$merge_base" HEAD | grep -E "$studio_full_triggers" | head -n 3 | tr '\n' ' ' || true)"
      if [[ -n "$trigger" ]]; then
        mode=full
        reason="config or dependency change (${trigger% }): the import graph cannot see its effect"
      fi
    else
      mode=full
      reason="no merge base with ${base}, running everything"
    fi
  fi
  emit mode "$mode"
  emit reason "$reason"
  if [[ "$mode" == full ]]; then
    emit vitest_args ""
    printf '### Studio tests: full\n\n%s\n' "$reason" >>"$summary"
  else
    emit vitest_args "--changed=${merge_base} --passWithNoTests"
    printf '### Studio tests: changed since %s\n\n%s (vitest import-graph selection)\n' "${merge_base:0:12}" "$reason" >>"$summary"
  fi
}

case "${1:-}" in
  python) shift; python_mode "$@" ;;
  studio) studio_mode ;;
  *) echo "usage: select-tests.sh python NAME=\"paths\"... | studio" >&2; exit 2 ;;
esac
