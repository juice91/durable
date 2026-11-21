"""Workflow runs: start a run of a published version, decide its approval and input steps, cancel a run.

The business logic behind routes/workflow_runs.py. A service method takes plain values (tenant and user ids,
the already-parsed body fields) and answers with a dict; refusals are `DefinitionError`s (eap_run_store), whose
bodies are what the API has always answered (list-valued `detail`, no `code` key, the recorded decision beside
a 409).

Cancel (loops D12, LP.8) is three steps, none of them holding a lock across another:
1. the durable request: `cancel_requested` (key `cancel`, `{requested_by}`) in its own short transaction;
2. the DBOS cancel of the run's workflow tree, with no eap_app transaction open, because a worker's event
   append takes the run-row lock;
3. the guarded terminal write (`RunStore.cancel_run_tree`).
A knowledge-base source sync a workflow run started is cancelled the same three steps after the run's own
cascade. A failure after step 1 answers 503 and leaves the durable request: the scheduled
`eap.cancel_reconcile` (worker) re-runs steps 2 and 3, and a retried request does so at once.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from eap_api.approvals import DecisionResult, decide
from eap_api.ports import RunLauncher
from eap_api.services.agent_archive import archived_step_problems
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors
from eap_api.services.workflows import WorkflowService
from eap_api.text_checks import MAX_REPORTED_ERRORS
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability.context import current_trace_context
from eap_observability.redaction import log_failure
from eap_run_store import CANCEL_EVENT_KEY, ENQUEUE_FAILED_ERROR, EventLog, RunIdConflict, RunStore
from eap_run_store.definition_errors import (
    WORKFLOW_ARCHIVED_CODE,
    WORKFLOW_ARCHIVED_NEW_WORK,
    DefinitionConflict,
    refuse,
)
from eap_run_store.store import TERMINAL_RUN_STATUSES
from eap_schemas.workflows import WorkflowSpec

log = logging.getLogger("eap_api")

# Same bound as a tool run's input and an agent run's input text: the canonical JSON of the input.
MAX_WORKFLOW_INPUT_BYTES = 32 * 1024
NOT_WAITING_DETAIL = "step is not waiting for approval"
CANCEL_NOT_SUPPORTED = "cancel_not_supported"
# Run kinds the cancel accepts: a workflow run, and a knowledge-base source sync run (a DBOS workflow
# that watches the same `cancel_requested` event between its steps).
CANCELLABLE_KINDS = ("workflow", "kb_source_sync")
CANCEL_NOT_SUPPORTED_DETAIL = "only workflow runs can be cancelled"
ALREADY_ENDED_DETAIL = "the run has already ended"
CANCEL_PENDING_DETAIL = "cancel recorded but not finished; it completes automatically, or retry the request"


def canonical_json(value: Any) -> str:
    # allow_nan=False: NaN/Infinity are not JSON (JSONB rejects them); non_finite_errors runs first.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def resume_data_errors(schema: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    """`value` checked against an input step's resume_schema, as `input_schema_errors` does (an empty
    Registry; loc/keyword only, never jsonschema's message, which quotes the rejected value), at
    loc ["body", "data", ...]. Non-finite numbers are errors too."""
    import jsonschema
    from referencing import Registry

    found = [{**e, "loc": ["body", "data", *e["loc"][2:]]} for e in non_finite_errors(value)]
    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        for err in sorted(cls(schema, registry=Registry()).iter_errors(value), key=lambda e: list(e.absolute_path))[
            :MAX_REPORTED_ERRORS
        ]:
            loc = ["body", "data", *[p if isinstance(p, int) else str(p)[:100] for p in err.absolute_path]]
            found.append({"loc": loc, "msg": f"does not satisfy the step's schema ('{err.validator}' keyword)",
                          "type": f"json_schema.{err.validator}"})
    except Exception:  # noqa: BLE001 - SchemaError, an unresolvable $ref, ...
        return [{"loc": ["body", "data"], "msg": "the step's schema could not be applied",
                 "type": "json_schema.invalid_schema"}]
    return found[:MAX_REPORTED_ERRORS]


def _cascade(launcher: RunLauncher, run_id: uuid.UUID) -> None:
    """D12 step 2 (a seam the tests replace): cancel the run's DBOS workflow tree. The run's DBOS
    workflow id is its run id."""
    launcher.cancel_workflow_tree(workflow_id=str(run_id))  # type: ignore[attr-defined]


def _finish_cancel(run_store: Any, tenant_id: str, run_id: uuid.UUID) -> Any:
    """D12 step 3 (a seam the tests replace to inject a crash after the cascade)."""
    return run_store.cancel_run_tree(tenant_id, run_id)


def _child_sync_runs(run_store: Any, tenant_id: str, parent_id: uuid.UUID) -> list[uuid.UUID]:
    """The knowledge-base source sync runs the workflow run `parent_id` started (same tenant) that have not ended,
    oldest first."""
    with run_store.Session() as session:
        rows = session.execute(
            text(
                "SELECT s.run_id FROM kb_source_syncs s JOIN runs r ON r.tenant_id = s.tenant_id AND r.id::text = s.run_id"
                " WHERE s.tenant_id = :t AND s.parent_run_id = :p AND r.status NOT IN ('completed', 'failed', 'needs_attention', 'skipped')"
                " ORDER BY s.started_at, s.id"
            ),
            {"t": tenant_id, "p": str(parent_id)},
        ).scalars().all()
    return [uuid.UUID(str(r)) for r in rows]


def _cancel_child_syncs(
    run_store: Any, event_log: Any, launcher: RunLauncher, tenant_id: str, parent_id: uuid.UUID, requested_by: str
) -> None:
    """Cancel the sync runs of a workflow run: each gets `cancel_requested` (a sync that already ended drops it),
    the DBOS cancel of its tree and the guarded terminal write. Idempotent; the caller maps a raise to 503."""
    for child in _child_sync_runs(run_store, tenant_id, parent_id):
        event_log.append(tenant_id, child, CANCEL_EVENT_KEY, "cancel_requested", {"requested_by": requested_by})
        _cascade(launcher, child)
        _finish_cancel(run_store, tenant_id, child)


class WorkflowRunService:
    def __init__(self, session: Session, run_store: RunStore, event_log: EventLog, launcher: RunLauncher) -> None:
        self._db = session
        self._runs = run_store
        self._events = event_log
        self._launcher = launcher

    # ----------------------------------------------------------------------------- start

    def _workflow(self, tenant_id: str, workflow_id: uuid.UUID) -> WorkflowModel:
        workflow = (
            self._db.query(WorkflowModel)
            .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
            .first()
        )
        if workflow is None:
            raise refuse(404, "Workflow not found")
        if workflow.archived:
            raise DefinitionConflict(WORKFLOW_ARCHIVED_NEW_WORK, code=WORKFLOW_ARCHIVED_CODE)
        return workflow

    def _version_row(self, workflow: WorkflowModel, version: int | None) -> WorkflowVersionModel:
        query = self._db.query(WorkflowVersionModel).filter(
            WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == workflow.tenant_id
        )
        if version is None:
            latest = query.with_entities(func.max(WorkflowVersionModel.version)).scalar()
            if latest is None:
                raise refuse(409, "Workflow has no published version")
            version = latest
        row = query.filter(WorkflowVersionModel.version == version).first()
        if row is None:
            raise refuse(404, "Version not found")
        return row

    def start(
        self,
        tenant_id: str,
        workflow_id: uuid.UUID,
        input: dict[str, Any],
        *,
        version: int | None = None,
        run_id: uuid.UUID | None = None,
        rerun_of: uuid.UUID | None = None,
        pause_after_each_step: bool = False,
    ) -> dict[str, Any]:
        """Run a published workflow version as a durable, tenant-scoped run; the 202 body (run id, status).

        404 unknown (or another tenant's, or archived) workflow, or unknown `version`; 409 no published
        version, or an agent step's agent is archived (naming the step); 422 input not valid for the
        version's input_schema (loc/msg/type only, never the input), a non-finite number anywhere in the
        input, input over 32 KiB as JSON, or the version points at something this tenant can no longer run;
        409 `run_id` already used by a different request. A retry with the same `run_id`, input and version
        answers again and re-enqueues. A failed hand-off is 503 and marks the run failed with
        ENQUEUE_FAILED_ERROR (if still queued); the same retry re-queues it."""
        workflow = self._workflow(tenant_id, workflow_id)
        version_row = self._version_row(workflow, version)
        spec = WorkflowSpec.model_validate(version_row.spec)
        errors = non_finite_errors(input) or input_schema_errors(spec.input_schema, input)
        if errors:
            raise refuse(422, errors)  # type: ignore[arg-type]
        input_json = canonical_json(input)
        if len(input_json.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
            raise refuse(422, f"input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON")
        run_store = self._runs
        if rerun_of is not None:
            original = run_store.get_run(tenant_id, rerun_of)
            if original is None or original["kind"] != "workflow" or original["workflow_id"] != str(workflow.id):
                raise refuse(404, "Run to repeat not found")
        # A retry of a recorded run (same run_id) gets the normal retry handling below (compared with the
        # row, then re-enqueued): the run already committed to its version, and the worker reports a step
        # whose agent was archived since as `agent_archived`. Only a new run is checked up front.
        recorded = run_id is not None and run_store.get_run(tenant_id, run_id) is not None
        if not recorded:
            archived = archived_step_problems(self._db, tenant_id, spec.steps)
            if archived:
                # Before anything runs: an archived agent step would fail the run mid-way, after the
                # earlier steps' side effects (eap_api.agent_archive).
                raise refuse(409, "; ".join(archived))
            problems = WorkflowService(self._db).reference_problems(spec, tenant_id)
            if problems:
                raise refuse(422, "; ".join(problems))

        run_id = run_id or uuid.uuid4()
        trace_id, traceparent = current_trace_context()
        try:
            _, created = run_store.create_workflow_run(
                tenant_id, workflow.id, version_row.version, input_json, run_id, trace_id=trace_id,
                traceparent=traceparent, rerun_of=rerun_of, pause_after_each_step=pause_after_each_step,
            )
        except RunIdConflict:
            raise refuse(409, "run_id is already in use") from None
        run = run_store.get_run(tenant_id, run_id)
        if run is None:  # pragma: no cover - deleted between insert and read
            raise refuse(404, "Run not found")
        if not created and (
            run["kind"] != "workflow"
            or run["workflow_id"] != str(workflow.id)
            or run["input"] != input_json
            or run["rerun_of"] != (None if rerun_of is None else str(rerun_of))
            or run["pause_after_each_step"] != pause_after_each_step
            or (version is not None and run["workflow_version"] != version)
        ):
            raise refuse(409, "run_id is already used by a different run request")
        if not created and run["workflow_version"] != version_row.version:
            # A retry without `version` after a newer publish: start exactly the recorded version.
            version_row = self._version_row(workflow, run["workflow_version"])
        owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)

        try:
            self._launcher.launch_workflow(
                run_id=run_id,
                tenant_id=tenant_id,
                workflow_version_id=str(version_row.id),
                input_json=run["input"],
                traceparent=run.get("traceparent"),
            )
        except Exception as exc:
            log_failure(
                log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                run_id=str(run_id), tenant_id=tenant_id,
            )
            if owns_handoff:
                try:
                    run_store.mark_enqueue_failed(tenant_id, run_id)
                except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                    log_failure(
                        log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                        level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id,
                    )
            raise refuse(
                503, "Run recorded but could not be started; retry the same request (same run_id)"
            ) from None
        run = run_store.get_run(tenant_id, run_id) or run
        run_status, display_status = run["status"], run["display_status"]
        if run["error"] == ENQUEUE_FAILED_ERROR:
            run_status = display_status = "queued"
        log.info("workflow run %s queued: tenant=%s workflow=%s", run_id, tenant_id, workflow.id)
        return {
            "run_id": str(run["id"]),
            "kind": "workflow",
            "workflow_id": run["workflow_id"],
            "version": run["workflow_version"],
            "status": run_status,
            "display_status": display_status,
            "trace_id": run["trace_id"],
        }

    # ----------------------------------------------------------------------------- decisions

    def _workflow_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
        run = self._runs.get_run(tenant_id, run_id)
        if run is None or run.get("kind") != "workflow":
            raise refuse(404, "Run not found")
        return run

    @staticmethod
    def _not_waiting(result: DecisionResult) -> Any:
        # Same text as always; an earlier decision (a person's or the timeout's) is added.
        return refuse(409, NOT_WAITING_DETAIL, **(result.recorded_decision() or {}))

    def decide_step(
        self, tenant_id: str, user_id: str, run_id: uuid.UUID, step_id: str, decision: str, comment: str | None
    ) -> dict[str, Any]:
        """Approve or reject a waiting approval step (decision "approved" | "rejected")."""
        self._workflow_run(tenant_id, run_id)
        # The shared decision helper (eap_api.approvals, AC.8): the decision event and its
        # pending_approvals projection update in one transaction, then the wake-up.
        result = decide(
            self._events, self._launcher, tenant_id=tenant_id, run_id=run_id, kind="step", key=step_id,
            decision=decision, decided_by=user_id, comment=comment,  # type: ignore[arg-type]
        )
        if result.outcome == "run_not_found":
            raise refuse(404, "Run not found")
        if result.outcome == "data_required":
            raise refuse(422, [{"loc": ["path", "step_id"], "msg": "this step takes data: answer it with .../resume",
                                "type": "resume_data_required"}])  # type: ignore[arg-type]
        if result.outcome != "created":
            raise self._not_waiting(result)
        return {
            "run_id": str(run_id),
            "step_id": step_id,
            "decision": decision,
            "decided_by": user_id,
            "comment": comment,
            "notified": result.notified,
        }

    def resume_step(
        self, tenant_id: str, user_id: str, run_id: uuid.UUID, step_id: str, data: dict[str, Any], comment: str | None
    ) -> dict[str, Any]:
        """Answer a waiting input step with `data` (validated against the step's resume_schema)."""
        run = self._workflow_run(tenant_id, run_id)
        waiting = self._events.get_event(tenant_id, run_id, f"step:{step_id}:waiting")
        waiting_data = waiting.data if waiting is not None and isinstance(waiting.data, dict) else {}
        schema = waiting_data.get("resume_schema")
        earlier = self._events.get_event(tenant_id, run_id, f"step:{step_id}:decision") if waiting is not None else None
        if earlier is not None:
            raise self._not_waiting(DecisionResult("exists", event=earlier))
        if waiting is None or run.get("status") in TERMINAL_RUN_STATUSES:
            raise refuse(409, NOT_WAITING_DETAIL)
        if not isinstance(schema, dict):
            raise refuse(409, "step takes no data: approve or reject it")
        errors = resume_data_errors(schema, data)
        if not errors and len(canonical_json(data).encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
            errors = [{"loc": ["body", "data"], "msg": f"data must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON",
                       "type": "too_long"}]
        if errors:
            raise refuse(422, errors)  # type: ignore[arg-type]
        result = decide(
            self._events, self._launcher, tenant_id=tenant_id, run_id=run_id, kind="step", key=step_id,
            decision="approved", decided_by=user_id, comment=comment, resume_data=data,
        )
        if result.outcome == "run_not_found":
            raise refuse(404, "Run not found")
        if result.outcome != "created":
            raise self._not_waiting(result)
        return {
            "run_id": str(run_id),
            "step_id": step_id,
            "decision": "approved",
            "decided_by": user_id,
            "decided_at": (result.recorded_decision() or {}).get("decided_at"),
            "comment": comment,
            "notified": result.notified,
        }

    # ----------------------------------------------------------------------------- cancel

    def cancel(self, tenant_id: str, user_id: str, run_id: uuid.UUID) -> dict[str, Any]:
        """Cancel a workflow run (or a knowledge-base source sync run) and its whole tree; the 202 body.

        The run ends `failed` (or `needs_attention` when a side effect was still reserved) with
        `run_cancelled`; idempotent, so a retry answers the same status (a run that ended by itself
        between the request and the terminal write keeps its own outcome, `cancelled` false)."""
        run_store, event_log, launcher = self._runs, self._events, self._launcher
        run = run_store.get_run(tenant_id, run_id)
        if run is None:
            raise refuse(404, "Run not found")
        kind = run.get("kind")
        if kind not in CANCELLABLE_KINDS:
            raise refuse(409, CANCEL_NOT_SUPPORTED_DETAIL, code=CANCEL_NOT_SUPPORTED)
        # Step 1, its own transaction (EventLog.append commits before returning): under the run-row
        # lock, an existing `cancel` is returned as it is, and after `end` a new key is dropped.
        requested = event_log.append(tenant_id, run_id, CANCEL_EVENT_KEY, "cancel_requested", {"requested_by": user_id})
        if requested.dropped:
            raise refuse(409, ALREADY_ENDED_DETAIL)
        try:
            _cascade(launcher, run_id)  # step 2: no eap_app lock or transaction is held here
            if kind == "workflow":
                _cancel_child_syncs(run_store, event_log, launcher, tenant_id, run_id, user_id)
            result = _finish_cancel(run_store, tenant_id, run_id)  # step 3
        except Exception as exc:  # noqa: BLE001 - the durable request stands; the reconciler finishes it
            log_failure(
                log, "cancel not finished", exc, code="cancel_incomplete", level=logging.ERROR,
                run_id=str(run_id), tenant_id=tenant_id,
            )
            raise refuse(503, CANCEL_PENDING_DETAIL) from None
        end = event_log.get_event(tenant_id, run_id, "end")
        error_code = (end.data or {}).get("error_code") if end is not None else None
        current = run_store.get_run(tenant_id, run_id) or run
        log.info("workflow run %s cancel: tenant=%s status=%s", run_id, tenant_id, result.status)
        return {
            "run_id": str(run_id),
            "kind": kind,
            "status": result.status or current["status"],
            "display_status": current["display_status"],
            "error_code": error_code,
            "cancelled": error_code == "run_cancelled",
            "requested_by": (requested.event.data or {}).get("requested_by"),
            "approvals_closed": result.approvals_closed,
        }
