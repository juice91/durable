"""Env-controlled crash hook for crash-injection tests.

`EAP_CRASH_AT` names one point; when execution reaches it the process calls `os._exit(137)`
(no cleanup, no checkpoint, like `kill -9`):

- `model_request`: inside the DBOS model-request step, right after the first text delta of a
  model response has been published over NOTIFY (the stream has started, no final message is
  checkpointed yet). Only responses that stream text reach it; with the `test:ticket` model that
  is model request 1 (request 0 is a tool call).
- `after_tool_call`: inside the `tickets.create` tool step, right after the external ticket
  service returned and before `side_effects.complete` and the DBOS step checkpoint (the side
  effect row stays `reserved`). Also inside an MCP tool step (eap_worker.mcp), right after the MCP
  server answered `tools/call`, at the same point relative to `side_effects.complete`. Also inside
  the platform `retrieve` call step (eap_worker.platform_tools), after the search and before the
  checkpoint; there it is filtered by `EAP_CRASH_EVENT_KEY` against `retrieve:offset:{n}` (n: the
  sources the run's earlier retrieve calls returned), so `retrieve:offset:[1-9]*` targets a later call.
- `after_tool_run_call`: inside the `eap.tool_call` step of a tool test run (eap_worker.tool_run),
  right after the tool function returned and before its result is recorded (for a side-effecting
  tool: before `side_effects.complete` of the run's `{run_id}:tool` reservation; for any tool:
  before the step checkpoint). Recovery then ends a side-effecting tool run in `needs_attention`
  without calling the tool again, and simply re-runs a read-only one.
- `workflow_mid_step`: in a workflow run (eap_worker.workflow_run), right after a step's
  `step_started` event is recorded and before the step does its work (resolves an agent, calls a
  tool, waits, ...). Filtered like `after_event_write`: `EAP_CRASH_EVENT_KEY` is matched against
  `step:{step_id}:started`, so `step:approve*` targets one step.
- `after_workflow_tool_call`: inside a workflow tool step's `eap.workflow_tool_call` step, right
  after the tool returned and before its result is recorded (for a side-effecting tool: before
  `side_effects.complete` of the `{run_id}:{step_id}` reservation). Recovery then ends the run in
  `needs_attention` without calling the tool again; a read-only tool is simply called again.
  Filtered by `EAP_CRASH_EVENT_KEY` against `step:{step_id}:call`.
- `after_child_run_insert`: inside a workflow agent step's `eap.workflow_resolve_agent` step,
  right after the child agent run row is inserted (naming the resolved agent version) and before
  the step's result is checkpointed. Recovery re-runs the step, finds the row and reuses ITS
  version (never re-resolving 'latest'). Filtered by `EAP_CRASH_EVENT_KEY` against
  `step:{step_id}:child`.
- `workflow_waiting`: in a workflow approval step, right after its `step_waiting` event is
  recorded and before the workflow starts waiting for the decision. Filtered by
  `EAP_CRASH_EVENT_KEY` against `step:{step_id}:waiting`.
- `input_processors` / `output_processors` (P0.17): inside an `eap.input_processors` /
  `eap.output_processors` step, right after the deterministic chain segment ran (off the event
  loop) and before the step's result is checkpointed. Recovery re-runs the segment on the same
  text and gets the same processed text.
- `moderation_request` (P0.17): inside the moderation model's request step
  (`eap-moderator__model.request`), right after the moderation model answered and before the step
  checkpoint. Recovery calls the moderation model again; its usage row is still written once, with
  the `processor_result` event that follows the step.
- `after_event_write`: inside the `eap.append_event` DBOS step, right after the durable event
  committed to `eap_app` and before the step's checkpoint reaches `eap_dbos`. Only fires for
  events whose `event_key` matches the glob `EAP_CRASH_EVENT_KEY` (default `*`, i.e. the first
  event written, `start`).

- `kb_chunk` (RM.10): inside `eap.kb_chunk` (ingest) right after the chunk rows were upserted, and
  inside `eap.kb_copy` (reindex) right after the active chunks were copied, before the step
  checkpoint. Recovery re-runs the step; the upsert / copy is idempotent (no duplicate rows).
- `kb_batch_before_commit` (RM.10): inside `eap.kb_embed_batch`, after the embedding call returned
  and before the transaction that writes the vectors, the usage row and the `ingest_progress` event.
  Recovery embeds that batch again (one bounded duplicate provider call) and writes ONE usage row.
  Filtered by `EAP_CRASH_EVENT_KEY` against the batch key `embed:batch:{i}`.
- `kb_batch_after_commit` (RM.10): inside `eap.kb_embed_batch`, right after that transaction
  committed and before the step checkpoint. Recovery finds the batch's event and returns its counts
  without a provider call. Filtered like `kb_batch_before_commit`.
- `kb_before_finalize` / `kb_finalize` (RM.10): inside `eap.kb_finalize`, right before / right after
  the atomic version (or generation) switch transaction. Recovery re-runs the switch, which answers
  'already_active' when it had committed.

- `load_memory` (RM.12): inside `eap.load_memory`, right after the memory snapshot was read and
  before the step checkpoint. Recovery reads the snapshot again. (A memory turn's
  `update_working_memory` uses `after_tool_call`: right after its write, event and ledger row
  committed; recovery is answered from the ledger.)

- `observer_request` (RM.13): inside the observer's model request step
  (`eap-observer__model.request`), right after the observer model answered and before the step
  checkpoint (the run is already completed). Recovery calls the observer again; the observation,
  its event and its usage row are written once, by the write step that follows.
- `after_observation_write` (RM.13): inside `eap.memory_observe_write` / `eap.memory_reflect`,
  right after the observation (or reflection, or failure event) transaction committed and before
  the step checkpoint. Recovery re-runs the step: the slot answers `existing`, one row, one event,
  one usage row.
- `recall_embedding` (RM.14): inside `eap.memory_recall`, right after the query embedding returned
  and before the search and the `embed:recall:{turn}` event and usage transaction. Recovery
  re-runs the step (one bounded duplicate provider call): one event, one usage row, the same block.
- `after_memory_index` (RM.14): inside `eap.memory_index`, right after the index_turn transaction
  (rows, `embed:index:{turn}` event, usage row) committed and before the step checkpoint (the run
  is already completed). Recovery re-runs the step: index_turn answers `existing`, the event key
  exists, so one row per role, one event, one usage row.

`EAP_CRASH_MARKER`, if set, is a file path: the hook fires only while that file does not exist
and creates it before exiting, so a supervised worker that inherits the env crashes once and
then recovers instead of crash-looping.
"""

from __future__ import annotations

import fnmatch
import os
import sys

CRASH_POINTS = frozenset(
    {
        "model_request",
        "after_tool_call",
        "after_tool_run_call",
        "after_event_write",
        "workflow_mid_step",
        "after_workflow_tool_call",
        "workflow_waiting",
        "after_child_run_insert",
        "input_processors",
        "output_processors",
        "moderation_request",
        "kb_chunk",
        "kb_batch_before_commit",
        "kb_batch_after_commit",
        "kb_before_finalize",
        "kb_finalize",
        "load_memory",
        "observer_request",
        "after_observation_write",
        "recall_embedding",
        "after_memory_index",
    }
)


def crash_point(name: str, *, event_key: str | None = None) -> None:
    target = os.environ.get("EAP_CRASH_AT")
    if target != name:
        return
    if event_key is not None and not fnmatch.fnmatchcase(event_key, os.environ.get("EAP_CRASH_EVENT_KEY", "*")):
        return
    marker = os.environ.get("EAP_CRASH_MARKER")
    if marker:
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        os.close(fd)
    print(f"eap-worker: EAP_CRASH_AT={name} reached (event_key={event_key}); exiting 137", file=sys.stderr, flush=True)
    os._exit(137)
