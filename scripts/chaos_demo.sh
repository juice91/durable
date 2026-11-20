#!/usr/bin/env bash
# Crash-recovery demo on the "KB Source Synchronization" workflow: start a targeted sync, kill the worker pod after the
# child sync run's first segment completed, wait for the runs to finish and print the evidence that DBOS carried them
# across the crash without redoing completed work.
#
# Usage:  EAP_TOKEN=... scripts/chaos_demo.sh [--dry-run] [--help]
#
# Configuration (environment only; nothing secret is passed in argv):
#   EAP_API                  API base URL                                  (default http://localhost:18000)
#   EAP_TOKEN                bearer token, e.g. dev:studio:operator on a dev stack (required, except with --dry-run)
#   CHAOS_SOURCE_ID          kb source to sync (default: the only enabled source in the app database)
#   CHAOS_PART_NUMBERS       comma list of orderable part numbers (default: the first 30 the source already knows)
#   CHAOS_KILL_AFTER_SEGMENTS  kill once the child sync reported this many segments        (default 1)
#   KILL_AFTER_SECONDS       fallback: kill after this long even if no segment completed   (default 120)
#   MAX_WAIT                 seconds to wait for the runs to finish after the kill         (default 1800)
#   CHAOS_KILL_CMD           command that kills the worker pod
#   CHAOS_LOG_CMD            command that prints the worker's log
#   CHAOS_DB_CMD             command that runs the SQL on its stdin and prints the result as '|'-separated rows with
#                            a header line. It is run as: bash -c "$CHAOS_DB_CMD" chaos-db <database>, so $1 is the
#                            database: eap_app (application tables) or eap_dbos (DBOS tables, schema dbos)
#   EXAMPLE_FILE             workflow example to publish when none exists (default examples/workflows/kb-source-sync.json)
#
# A targeted run spends about 3 provider (TI product) calls per part against the daily budget.
# Exit status: 0 only when the parent and the child sync completed, a crash was injected while the child was running, no
# segment completed twice and the duplicate check holds.
# Do not kill the worker during the posting schedules (slots 08, 11, 14, 17, 20 and 23 Taipei time).
set -euo pipefail

EAP_API="${EAP_API:-http://localhost:18000}"
EAP_TOKEN="${EAP_TOKEN:-}"
CHAOS_SOURCE_ID="${CHAOS_SOURCE_ID:-}"
CHAOS_PART_NUMBERS="${CHAOS_PART_NUMBERS:-}"
KILL_AFTER_SEGMENTS="${CHAOS_KILL_AFTER_SEGMENTS:-1}"
KILL_AFTER_SECONDS="${KILL_AFTER_SECONDS:-120}"
MAX_WAIT="${MAX_WAIT:-1800}"
PART_LIMIT=30
WORKFLOW_NAME="KB Source Synchronization"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXAMPLE_FILE="${EXAMPLE_FILE:-$HERE/../examples/workflows/kb-source-sync.json}"

# shellcheck disable=SC2016  # $HOME and $PATH expand on the remote host
KUBECTL_PATH='export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"'
KUBECTL="kubectl --context colima-pydanticos -n pydanticos"
DEFAULT_KILL_CMD="ssh deploy-host '${KUBECTL_PATH}; ${KUBECTL} delete pod worker-0'"
DEFAULT_LOG_CMD="ssh deploy-host '${KUBECTL_PATH}; ${KUBECTL} logs worker-0 --tail=400'"
# Databases are eap_app (application tables) and eap_dbos (DBOS tables, schema dbos): deploy/k8s/configmap.yaml. The
# admin role reads both over the pod's local socket. $1 is the database; the SQL arrives on stdin.
# shellcheck disable=SC2016  # $1 and \$HOME are meant literally: bash -c expands $1, the remote shell expands $HOME
DEFAULT_DB_CMD='ssh deploy-host "export PATH=\"\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:\$PATH\"; kubectl --context colima-pydanticos -n pydanticos exec -i postgres-0 -- psql -U eap_admin -d $1 -X -q -A -F \"|\" -P footer=off -v ON_ERROR_STOP=1"'
CHAOS_KILL_CMD="${CHAOS_KILL_CMD:-$DEFAULT_KILL_CMD}"
CHAOS_LOG_CMD="${CHAOS_LOG_CMD:-$DEFAULT_LOG_CMD}"
CHAOS_DB_CMD="${CHAOS_DB_CMD:-$DEFAULT_DB_CMD}"

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
heading() { printf '\n== %s ==\n' "$1"; }
die() { echo "chaos_demo: $*" >&2; exit 1; }

if [ "$DRY_RUN" -eq 1 ]; then
  heading "DRY RUN (nothing is executed)"
  say "api:           $EAP_API"
  say "workflow:      $WORKFLOW_NAME (published from $(basename "$EXAMPLE_FILE") if absent)"
  say "source:        ${CHAOS_SOURCE_ID:-<the only enabled kb source>}"
  say "parts:         ${CHAOS_PART_NUMBERS:-<the first $PART_LIMIT orderable part numbers the source knows>}  (about 3 provider calls per part)"
  say "kill:          after $KILL_AFTER_SEGMENTS completed child segment(s), or after ${KILL_AFTER_SECONDS}s"
  say "1. start a targeted run {source_id, mode: targeted, part_numbers}"
  say "2. poll GET /api/v1/runs/<child run id>/events (the sync run the parent started) for segment completions"
  say "3. kill:       $CHAOS_KILL_CMD"
  say "4. poll GET /api/v1/runs/<run id> until the parent and the child are terminal"
  say "5. worker log: $CHAOS_LOG_CMD | grep -i recover"
  say "6. DB:         bash -c '$CHAOS_DB_CMD' chaos-db <eap_dbos|eap_app>   (SQL on stdin)"
  say ""
  say "== 1. EVENTS: parent steps, child segments, sync counters =="
  say "== 2. WORKER LOG: lines matching 'recover' =="
  say "== 3. DBOS: workflow_status rows and duplicate check =="
  exit 0
fi

[ -n "$EAP_TOKEN" ] || die "EAP_TOKEN is not set (the dev stack accepts dev:studio:operator)"
command -v curl >/dev/null || die "curl is required"
command -v jq >/dev/null || die "jq is required"
case "$KILL_AFTER_SEGMENTS$KILL_AFTER_SECONDS$MAX_WAIT" in *[!0-9]*) die "CHAOS_KILL_AFTER_SEGMENTS, KILL_AFTER_SECONDS and MAX_WAIT are whole numbers" ;; esac

# The bearer token goes to curl through a private header file, never through argv.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
umask 077
printf 'Authorization: Bearer %s\n' "$EAP_TOKEN" >"$TMP/auth"

api() { # api METHOD PATH [JSON_BODY_FILE]
  local method="$1" path="$2" body="${3:-}"
  if [ -n "$body" ]; then
    curl -fsS -X "$method" -H "@$TMP/auth" -H 'Content-Type: application/json' --data-binary "@$body" "$EAP_API/api/v1$path"
  else
    curl -fsS -X "$method" -H "@$TMP/auth" "$EAP_API/api/v1$path"
  fi
}
db() { # db DATABASE <SQL on stdin>
  bash -c "$CHAOS_DB_CMD" chaos-db "$1"
}
rows() { db "$1" | tail -n +2; } # the result rows without the header line

heading "SETUP"
WORKFLOW_ID="$(api GET /workflows | jq -r --arg n "$WORKFLOW_NAME" '[.[] | select(.name == $n and (.archived | not))] | last | .id // empty')"
if [ -z "$WORKFLOW_ID" ]; then
  [ -f "$EXAMPLE_FILE" ] || die "no workflow named '$WORKFLOW_NAME' and no example at $EXAMPLE_FILE"
  jq '{spec: .spec}' "$EXAMPLE_FILE" >"$TMP/create.json"
  WORKFLOW_ID="$(api POST /workflows "$TMP/create.json" | jq -r '.id')"
  say "created workflow $WORKFLOW_ID from $(basename "$EXAMPLE_FILE")"
fi
if [ -z "$(api GET "/workflows/$WORKFLOW_ID" | jq -r '.latest_version // empty')" ]; then
  api POST "/workflows/$WORKFLOW_ID/versions" >/dev/null
  say "published a version of workflow $WORKFLOW_ID"
fi
say "workflow $WORKFLOW_ID ($WORKFLOW_NAME)"

if [ -z "$CHAOS_SOURCE_ID" ]; then
  found="$(printf '%s\n' "select id from kb_sources where enabled order by created_at;" | rows eap_app)"
  [ -n "$found" ] && [ "$(printf '%s\n' "$found" | wc -l)" -eq 1 ] || die "set CHAOS_SOURCE_ID: found $(printf '%s' "$found" | grep -c .) enabled kb sources"
  CHAOS_SOURCE_ID="$found"
fi
case "$CHAOS_SOURCE_ID" in *[!A-Za-z0-9_-]* | "") die "CHAOS_SOURCE_ID has unexpected characters" ;; esac
say "source   $CHAOS_SOURCE_ID"

if [ -z "$CHAOS_PART_NUMBERS" ]; then
  CHAOS_PART_NUMBERS="$(printf '%s\n' "select distinct i.value from kb_entity_identifiers i join kb_entities e on e.tenant_id = i.tenant_id and e.id = i.entity_id where e.source_id = '$CHAOS_SOURCE_ID' and i.scheme = 'orderable_part_number' order by i.value limit $PART_LIMIT;" | rows eap_app | paste -sd, -)"
  [ -n "$CHAOS_PART_NUMBERS" ] || die "the source knows no orderable part numbers: set CHAOS_PART_NUMBERS"
fi
case "$CHAOS_PART_NUMBERS" in *[!A-Za-z0-9._/,+-]*) die "CHAOS_PART_NUMBERS has unexpected characters" ;; esac
PARTS_JSON="$(printf '%s' "$CHAOS_PART_NUMBERS" | jq -R 'split(",") | map(select(length > 0))')"
PART_COUNT="$(printf '%s' "$PARTS_JSON" | jq 'length')"
say "parts    $PART_COUNT (about $((PART_COUNT * 3)) provider calls)"

jq -n --arg s "$CHAOS_SOURCE_ID" --argjson p "$PARTS_JSON" '{input: {source_id: $s, mode: "targeted", part_numbers: $p}}' >"$TMP/start.json"
RUN_ID="$(api POST "/workflows/$WORKFLOW_ID/runs" "$TMP/start.json" | jq -r '.run_id')"
[ -n "$RUN_ID" ] || die "the run did not start"
say "run      $RUN_ID"

run_status() { api GET "/runs/$1" | jq -r '.status'; }
is_terminal() { case "$1" in completed | failed | needs_attention | cancelled | skipped) return 0 ;; *) return 1 ;; esac; }
segments_done() { # distinct segments the child sync reported
  api GET "/runs/$CHILD_ID/events" | jq '[.[] | select(.type == "ingest_progress" and .data.phase == "sync_segment") | .data.segment] | unique | length'
}

# The parent's sync_targeted step answers {run_id}: the child sync run.
CHILD_ID=""
deadline=$((SECONDS + KILL_AFTER_SECONDS + 120))
while [ -z "$CHILD_ID" ]; do
  CHILD_ID="$(api GET "/runs/$RUN_ID/events" | jq -r '[.[] | select(.type == "step_completed" and .data.step_id == "sync_targeted")] | first | .data.output.run_id // empty')"
  [ -n "$CHILD_ID" ] && break
  is_terminal "$(run_status "$RUN_ID" || true)" && die "the parent ended before it started a sync"
  [ "$SECONDS" -lt "$deadline" ] || die "timed out waiting for the parent to start its sync"
  sleep 2
done
say "child    $CHILD_ID (the sync run)"

say "waiting for $KILL_AFTER_SEGMENTS completed segment(s) (fallback: kill after ${KILL_AFTER_SECONDS}s)"
CRASH_INJECTED=0
KILLED_AT="none"
SEGMENTS_AT_KILL=0
start=$SECONDS
while :; do
  SEGMENTS_AT_KILL="$(segments_done || echo 0)"
  child_status="$(run_status "$CHILD_ID" || true)"
  is_terminal "$child_status" && { say "the child sync ended ($child_status) before the kill: no crash will be injected"; break; }
  if [ "${SEGMENTS_AT_KILL:-0}" -ge "$KILL_AFTER_SEGMENTS" ] || [ $((SECONDS - start)) -ge "$KILL_AFTER_SECONDS" ]; then
    say "child segments completed: $SEGMENTS_AT_KILL; killing the worker"
    KILLED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    bash -c "$CHAOS_KILL_CMD" || die "the kill command failed"
    CRASH_INJECTED=1
    say "killed at $KILLED_AT"
    break
  fi
  sleep 1
done

deadline=$((SECONDS + MAX_WAIT))
say "waiting for the parent and the child to finish (the worker restarts and DBOS recovers them)"
while :; do
  p="$(run_status "$RUN_ID" 2>/dev/null || true)"
  c="$(run_status "$CHILD_ID" 2>/dev/null || true)"
  is_terminal "${p:-}" && is_terminal "${c:-}" && break
  [ "$SECONDS" -lt "$deadline" ] || die "timed out: parent ${p:-unknown}, child ${c:-unknown} after ${MAX_WAIT}s"
  sleep 3
done

PARENT_EVENTS="$(api GET "/runs/$RUN_ID/events")"
CHILD_EVENTS="$(api GET "/runs/$CHILD_ID/events")"
PARENT_STATUS="$(run_status "$RUN_ID")"
CHILD_STATUS="$(run_status "$CHILD_ID")"
KB_ID="$(printf '%s' "$PARENT_EVENTS" | jq -r '[.[] | select(.type == "step_completed" and .data.step_id == "source")] | first | .data.output.knowledge_base_id // empty')"

heading "1. EVENTS: parent steps, child segments, sync counters"
say "-- parent run $RUN_ID: step_completed per step (each must be x1) --"
printf '%s' "$PARENT_EVENTS" | jq -r '
  [.[] | select(.type == "step_completed")
       | {step: .data.step_id, scope: ((.data.scope // []) | map((.loop // "") + "." + (.index | tostring)) | join("/"))}]
  | group_by([.step, .scope]) | map({step: .[0].step, scoped: (.[0].scope != ""), n: length})
  | group_by([.step, .scoped]) | map({step: .[0].step, scoped: .[0].scoped, iterations: length, max: (map(.n) | max)})
  | .[] | "\(.step | . + (" " * (22 - length)))  x\(.max)\(if .scoped then "  (per loop iteration, \(.iterations) iterations)" else "" end)\(if .max > 1 then "   <-- DUPLICATE" else "" end)"'
PARENT_DUPES="$(printf '%s' "$PARENT_EVENTS" | jq '[.[] | select(.type == "step_completed") | {s: .data.step_id, a: (.data.scope // [])}] | group_by(.) | map(select(length > 1)) | length')"
say ""
say "-- child sync run $CHILD_ID: segment completions (each must be x1) --"
printf '%s' "$CHILD_EVENTS" | jq -r '
  [.[] | select(.type == "ingest_progress" and .data.phase == "sync_segment") | .data.segment]
  | group_by(.) | map({seg: .[0], n: length})
  | if length == 0 then "(no segment completions recorded)" else .[] | "segment \(.seg)  x\(.n)\(if .n > 1 then "   <-- DUPLICATE" else "" end)" end'
SEGMENT_DUPES="$(printf '%s' "$CHILD_EVENTS" | jq '[.[] | select(.type == "ingest_progress" and .data.phase == "sync_segment") | .data.segment] | group_by(.) | map(select(length > 1)) | length')"
SEGMENTS_TOTAL="$(printf '%s' "$CHILD_EVENTS" | jq '[.[] | select(.type == "ingest_progress" and .data.phase == "sync_segment") | .data.segment] | unique | length')"
say ""
say "parent status  : $PARENT_STATUS"
say "child status   : $CHILD_STATUS"
say "segments       : $SEGMENTS_TOTAL completed ($SEGMENTS_AT_KILL before the kill)"
say "duplicates     : $PARENT_DUPES parent step(s), $SEGMENT_DUPES segment(s) completed more than once"
say "killed at      : $KILLED_AT"
if [ -n "$KB_ID" ]; then
  api GET "/knowledge-bases/$KB_ID/sources/$CHAOS_SOURCE_ID/syncs/$CHILD_ID" | jq -r '"sync status    : \(.status)", "sync counters  : \(.counters | tojson)", "sync started   : \(.started_at)", "sync completed : \(.completed_at)"'
else
  say "sync counters  : (the parent's source step did not report a knowledge base)"
fi

heading "2. WORKER LOG: lines matching 'recover'"
LOG="$(bash -c "$CHAOS_LOG_CMD" 2>&1 || true)"
printf '%s\n' "$LOG" | grep -i recover || say "(no line matching 'recover' in the log command's output)"

heading "3. DBOS: workflow_status rows and duplicate check"
say "-- workflow_status: parent, child sync and every segment child ($CHILD_ID:seg:<n>) --"
db eap_dbos <<SQL || say "(the DBOS query failed)"
select workflow_uuid, status, recovery_attempts, updated_at from dbos.workflow_status
where workflow_uuid in ('$RUN_ID', '$CHILD_ID') or workflow_uuid like '$CHILD_ID:seg:%'
order by created_at, workflow_uuid;
SQL
say ""
say "-- DUPLICATES: source $CHAOS_SOURCE_ID, $PART_COUNT synced part(s) --"
DUP_ROWS="$(db eap_app <<SQL || true
select
  (select count(*) from kb_source_records where source_id = '$CHAOS_SOURCE_ID') as records,
  (select count(*) from (select distinct external_id, record_type from kb_source_records where source_id = '$CHAOS_SOURCE_ID') d) as distinct_records,
  (select count(*) from kb_entities e where e.source_id = '$CHAOS_SOURCE_ID' and exists (select 1 from kb_entity_identifiers i where i.tenant_id = e.tenant_id and i.entity_id = e.id and i.scheme = 'orderable_part_number' and i.value = any (string_to_array('$CHAOS_PART_NUMBERS', ',')))) as entities,
  (select coalesce(max(c), 0) from (select count(*) c from kb_entity_documents d join kb_entities e on e.tenant_id = d.tenant_id and e.id = d.entity_id where e.source_id = '$CHAOS_SOURCE_ID' and exists (select 1 from kb_entity_identifiers i where i.tenant_id = e.tenant_id and i.entity_id = e.id and i.scheme = 'orderable_part_number' and i.value = any (string_to_array('$CHAOS_PART_NUMBERS', ','))) group by e.id) x) as max_documents_per_entity,
  (select count(*) from (select e.id from kb_entity_documents d join kb_entities e on e.tenant_id = d.tenant_id and e.id = d.entity_id where e.source_id = '$CHAOS_SOURCE_ID' and exists (select 1 from kb_entity_identifiers i where i.tenant_id = e.tenant_id and i.entity_id = e.id and i.scheme = 'orderable_part_number' and i.value = any (string_to_array('$CHAOS_PART_NUMBERS', ','))) group by e.id having count(*) > count(distinct d.source_url) or count(*) > count(distinct d.kb_document_id)) y) as entities_with_duplicate_documents;
SQL
)"
printf '%s\n' "${DUP_ROWS:-(the duplicate query failed)}" | column -s'|' -t 2>/dev/null || printf '%s\n' "$DUP_ROWS"
DUP_OK=0
if [ -n "$DUP_ROWS" ]; then
  IFS='|' read -r records distinct_records _ _ dup_entities <<<"$(printf '%s\n' "$DUP_ROWS" | tail -n 1)"
  [ "${records:-x}" = "${distinct_records:-y}" ] && [ "${dup_entities:-1}" = "0" ] && DUP_OK=1
fi
say "duplicate check: $([ "$DUP_OK" -eq 1 ] && echo "holds (records == distinct records, no entity has a repeated document)" || echo "FAILED or not available")"

heading "RESULT"
if [ "$PARENT_STATUS" = "completed" ] && [ "$CHILD_STATUS" = "completed" ] && [ "$CRASH_INJECTED" -eq 1 ] \
  && [ "$PARENT_DUPES" -eq 0 ] && [ "$SEGMENT_DUPES" -eq 0 ] && [ "$DUP_OK" -eq 1 ]; then
  say "PASS: both runs completed after the crash, no step or segment completed twice, no duplicate rows"
  exit 0
fi
say "FAIL: parent=$PARENT_STATUS child=$CHILD_STATUS crash_injected=$CRASH_INJECTED parent_duplicates=$PARENT_DUPES segment_duplicates=$SEGMENT_DUPES duplicate_check=$DUP_OK"
exit 1
