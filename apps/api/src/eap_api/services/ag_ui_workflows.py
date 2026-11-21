"""AG-UI workflow threads: the data and business rules behind /ag-ui/workflows (a thread is one workflow run).

The routes keep the protocol (input mapping, the commit order and stream plan, framing). This service
reads the thread's AG-UI rows and the workflow's data, starts the run of a new thread (validated like a
workflow run: input schema, size, archived agent steps, references) and hands it to the worker.
Refusals are `ServiceError`s with the bodies the routes always answered (`plain_refusals`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from eap_api.services.ag_ui import (
    FINGERPRINT_CONFLICT_DETAIL,
    UNKNOWN_RUN_DETAIL,
    AgUiService,
    compact_error,
)
from eap_api.services.agent_archive import archived_step_problems
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.run_filters import plain_refusals
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors
from eap_api.services.workflow_runs import MAX_WORKFLOW_INPUT_BYTES
from eap_api.services.workflows import WorkflowService
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_observability.context import current_trace_context
from eap_observability.redaction import log_failure
from eap_run_store import (
    AgUiRunModel,
    AgUiRunRecord,
    AgUiScopeMismatch,
    RunIdConflict,
)
from eap_schemas.workflows import WorkflowSpec

log = logging.getLogger("eap_api")

WORKFLOW_NOT_FOUND = "Workflow not found"
WORKFLOW_INPUT_INVALID = "workflow_input_invalid"
INPUT_LOC = ["forwardedProps", "pydanticos", "input"]
NOT_STARTED = "Run recorded but could not be started; retry the same request (same runId)"


@dataclass(frozen=True)
class WorkflowRef:
    """The workflow a thread belongs to, as the routes need it."""

    id: str
    archived: bool


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_input(value: Any) -> str:
    """The canonical JSON of a workflow input (what the run stores and compares)."""
    return _canonical(value)


def _as_user_messages(stored: Any) -> list[dict[str, Any]]:
    return [
        {"id": str(m["id"]), "role": "user", "content": str(m["content"])}
        for m in (stored or ())
        if isinstance(m, dict) and m.get("id")
    ]


def history_of(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """The user messages the thread's first AG-UI run carried (`ag_ui_runs.input_messages` of its
    starter row, from_seq -1), as AG-UI user messages: the prefix of every snapshot of the thread
    and of its thread GET, so a client's merge keeps them (AGUI.16 follow-up)."""
    starter = next((r for r in rows if r.from_seq < 0), None)
    return [] if starter is None else _as_user_messages(starter.input_messages)


def input_errors(value: Any, spec: WorkflowSpec) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return [{"loc": ["body", *INPUT_LOC], "msg": "the workflow input must be an object",
                 "type": WORKFLOW_INPUT_INVALID}]
    return non_finite_errors(value) or input_schema_errors(spec.input_schema, value)


@plain_refusals
class AgUiWorkflowService(AgUiService):
    """The AG-UI workflow routes' reads and writes (scope kind `workflow`)."""

    def load_workflow(self, tenant_id: str, workflow_id: str) -> WorkflowRef:
        """404 `Workflow not found` for a workflow that is not the tenant's."""
        return self._ref(self._workflow(tenant_id, workflow_id))

    @staticmethod
    def _ref(workflow: WorkflowModel) -> WorkflowRef:
        return WorkflowRef(id=str(workflow.id), archived=bool(workflow.archived))

    def _workflow(self, tenant_id: str, workflow_id: str) -> WorkflowModel:
        with Session(self.engine) as db:
            workflow = db.execute(
                select(WorkflowModel).where(WorkflowModel.id == workflow_id, WorkflowModel.tenant_id == tenant_id)
            ).scalar_one_or_none()
            if workflow is None:
                raise NotFound(WORKFLOW_NOT_FOUND)
            db.expunge(workflow)
            return workflow

    def thread_rows(self, tenant_id: str, workflow_id: str, thread_id: str) -> list[AgUiRunModel]:
        """The thread's AG-UI runs, in order (the first one started the run)."""
        with Session(self.engine) as session:
            rows = session.execute(
                select(AgUiRunModel)
                .where(
                    AgUiRunModel.tenant_id == tenant_id,
                    AgUiRunModel.scope_kind == "workflow",
                    AgUiRunModel.scope_id == workflow_id,
                    AgUiRunModel.external_thread_id == thread_id,
                )
                .order_by(AgUiRunModel.from_seq, AgUiRunModel.created_at, AgUiRunModel.external_run_id)
            ).scalars().all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def thread_start(
        self, tenant_id: str, workflow_id: str, record: AgUiRunRecord
    ) -> tuple[str, list[dict[str, Any]]]:
        """The AG-UI runId that started the thread's run (the D13 prefix of child-run messages) and
        the thread's input user messages (`history_of`). The starter record carries both, so a
        stream of the thread's first run reads no rows; a later run reads the thread's rows once."""
        if record.from_seq < 0:
            return record.external_run_id, _as_user_messages(record.input_messages)
        rows = self.thread_rows(tenant_id, workflow_id, record.external_thread_id)
        return (rows[0].external_run_id if rows else record.external_run_id), history_of(rows)

    def record_end_order(self, tenant_id: str, record: AgUiRunRecord, end_order: int) -> int:
        """Record where an AG-UI run ended in the commit order (the first writer wins); an unrecorded
        run (the Q5 re-send) has no row, so its end is `end_order` itself."""
        try:
            return self.ag_ui.set_end_order(
                tenant_id, scope_kind="workflow", scope_id=record.scope_id,
                external_thread_id=record.external_thread_id, external_run_id=record.external_run_id,
                end_order=end_order,
            )
        except NoResultFound:
            return end_order

    def record_thread_run(
        self,
        tenant_id: str,
        key: dict[str, str],
        *,
        run_id: str,
        fingerprint: str,
        from_seq: int,
        answered: tuple[str, ...] = (),
        from_order: int | None = None,
        input_messages: list[dict[str, str]] | None = None,
    ) -> tuple[AgUiRunRecord, bool]:
        """Record an AG-UI run of a workflow thread. 409 for a threadId of another workflow, or a runId
        already used with a different input."""
        try:
            record, created = self.ag_ui.record_ag_ui_run(
                tenant_id, **key, run_id=run_id, fingerprint=fingerprint, from_seq=from_seq,
                input_message_ids=answered, from_order=from_order, input_messages=input_messages,
            )
        except AgUiScopeMismatch:
            raise Conflict("threadId belongs to another workflow") from None
        if not created and record.fingerprint != fingerprint:
            raise Conflict(FINGERPRINT_CONFLICT_DETAIL)
        return record, created

    def relaunch(self, tenant_id: str, run: dict[str, Any], *, created: bool) -> dict[str, Any]:
        """Hand the run to the worker (again: DBOS attaches a re-enqueue to the existing workflow).
        A failed enqueue marks a run this request owns failed and is 503; the same POST heals.
        Returns the run as it stands afterwards."""
        run_id = uuid.UUID(str(run["id"]))
        with Session(self.engine) as db:
            version_id = db.execute(
                select(WorkflowVersionModel.id).where(
                    WorkflowVersionModel.tenant_id == tenant_id,
                    WorkflowVersionModel.workflow_id == str(run["workflow_id"]),
                    WorkflowVersionModel.version == run["workflow_version"],
                )
            ).scalar_one_or_none()
        if version_id is None:  # the version is immutable; never expected
            raise NotFound("Version not found")
        owns_handoff = created or self.run_store.requeue_after_enqueue_failure(tenant_id, run_id)
        try:
            self.launcher.launch_workflow(
                run_id=run_id, tenant_id=tenant_id, workflow_version_id=str(version_id),
                input_json=run["input"], traceparent=run.get("traceparent"),
            )
        except Exception as exc:
            log_failure(log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                        run_id=str(run_id), tenant_id=tenant_id)
            if owns_handoff:
                try:
                    self.run_store.mark_enqueue_failed(tenant_id, run_id)
                except Exception as mark_exc:  # noqa: BLE001 - the 503 below is the answer either way
                    log_failure(log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                                level=logging.ERROR, run_id=str(run_id), tenant_id=tenant_id)
            raise Unavailable(NOT_STARTED) from None
        return self.run_store.get_run(tenant_id, run_id) or run

    def _version_row(self, db: Session, workflow_id: Any, tenant_id: str, version: int | None) -> WorkflowVersionModel:
        query = select(WorkflowVersionModel).where(
            WorkflowVersionModel.workflow_id == workflow_id, WorkflowVersionModel.tenant_id == tenant_id
        )
        if version is None:
            latest = db.execute(
                select(func.max(WorkflowVersionModel.version)).where(
                    WorkflowVersionModel.workflow_id == workflow_id, WorkflowVersionModel.tenant_id == tenant_id
                )
            ).scalar_one_or_none()
            if latest is None:
                raise Conflict("Workflow has no published version")
            version = latest
        row = db.execute(query.where(WorkflowVersionModel.version == version)).scalar_one_or_none()
        if row is None:
            raise NotFound("Version not found")
        return row

    def start_thread_run(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        version: int | None,
        value: Any,
        run_id: str,
        key: dict[str, str],
        fingerprint: str,
        input_messages: list[dict[str, str]] | None,
    ) -> tuple[dict[str, Any], AgUiRunRecord, bool]:
        """A new thread: validate the input and the version, record the run and its AG-UI row, hand off.
        Returns (the run after the hand-off, the AG-UI row, whether that row is new)."""
        with Session(self.engine) as db:
            version_row = self._version_row(db, workflow_id, tenant_id, version)
            spec = WorkflowSpec.model_validate(version_row.spec)
            errors = input_errors(value, spec)
            if errors:
                raise InvalidInput("the workflow input is not valid", detail_object=errors)
            input_json = _canonical(value)
            if len(input_json.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
                msg = f"the workflow input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON"
                raise InvalidInput(msg, detail_object=compact_error(WORKFLOW_INPUT_INVALID, INPUT_LOC, msg))
            archived = archived_step_problems(db, tenant_id, spec.steps)
            if archived:
                raise Conflict("; ".join(archived))
            problems = WorkflowService(db).reference_problems(spec, tenant_id)
            if problems:
                raise InvalidInput("; ".join(problems))
            resolved_version = version_row.version
        trace_id, traceparent = current_trace_context()
        try:
            _, created = self.run_store.create_workflow_run(
                tenant_id, workflow_id, resolved_version, input_json, uuid.UUID(run_id),
                trace_id=trace_id, traceparent=traceparent,
            )
        except RunIdConflict:
            raise Conflict("threadId is already in use") from None
        run = self.run_store.get_run(tenant_id, uuid.UUID(run_id))
        if run is None:  # pragma: no cover - deleted between insert and read
            raise NotFound(UNKNOWN_RUN_DETAIL)
        if not created and (
            run["kind"] != "workflow" or run["workflow_id"] != str(workflow_id) or run["input"] != input_json
        ):
            raise Conflict("threadId is already in use")
        record, rec_created = self.record_thread_run(
            tenant_id, key, run_id=run_id, fingerprint=fingerprint, from_seq=-1, input_messages=input_messages
        )
        run = self.relaunch(tenant_id, run, created=created)
        if created:
            log.info("ag-ui workflow run %s recorded: tenant=%s workflow=%s", run_id, tenant_id, workflow_id)
        return run, record, rec_created
