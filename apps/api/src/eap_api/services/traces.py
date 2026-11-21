"""The built-in trace view of a run and the feedback on a trace (a run)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from eap_api.models import AgentVersionModel
from eap_api.services.run_filters import plain_refusals
from eap_api.services.errors import InvalidInput, NotFound, Unavailable
from eap_api.services.runs import run_json
from eap_api.workflows.models import WorkflowVersionModel
from eap_run_store import RunStore
from eap_run_store.trace import TraceQueryTimeout, TraceReader
from eap_run_store.trace_feedback import (  # noqa: F401 - re-exported for the route's request model
    FEEDBACK_COMMENT_MAX_CHARS,
    FEEDBACK_SCORE_LIMIT,
    FEEDBACK_SPAN_ID_MAX_CHARS,
    TraceFeedbackStore,
)

TIMEOUT_DETAIL = "Trace query timed out; retry later"
TRACE_NOT_FOUND = "Trace not found"


@plain_refusals
class TraceService:
    def __init__(self, db: Session, run_store: RunStore, feedback: TraceFeedbackStore) -> None:
        self.db = db
        self.run_store = run_store
        self.feedback = feedback

    def read_trace(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
        """The run's span tree with tokens and cost; NotFound for an unknown run or another tenant's."""
        db = self.db

        def workflow_spec(workflow_id: str, version: int) -> dict[str, Any] | None:
            row = (
                db.query(WorkflowVersionModel.spec)
                .filter(
                    WorkflowVersionModel.tenant_id == tenant_id,
                    WorkflowVersionModel.workflow_id == str(workflow_id),
                    WorkflowVersionModel.version == version,
                )
                .first()
            )
            return dict(row.spec) if row is not None else None

        def agent_spec(agent_id: str, version: int) -> dict[str, Any] | None:
            # RM.19: a retrieve span's knowledge base refs (top_k, min_score); read only for runs that
            # called retrieve, under the caller's tenant.
            row = (
                db.query(AgentVersionModel.spec)
                .filter(
                    AgentVersionModel.tenant_id == tenant_id,
                    AgentVersionModel.agent_id == str(agent_id),
                    AgentVersionModel.version == version,
                )
                .first()
            )
            return dict(row.spec) if row is not None else None

        try:
            trace = TraceReader(self.run_store.engine).read(
                tenant_id, run_id, workflow_spec=workflow_spec, agent_spec=agent_spec
            )
        except TraceQueryTimeout:
            raise Unavailable(TIMEOUT_DETAIL) from None
        if trace is None:
            raise NotFound("Run not found")
        chat = self.run_store.chat_usage(tenant_id, [trace["run"]["id"]])
        trace["run"] = run_json(trace["run"], chat.get(str(trace["run"]["id"])))
        return trace

    def add_feedback(
        self,
        tenant_id: str,
        user_id: str,
        trace_id: uuid.UUID,
        *,
        rating: str,
        comment: str | None,
        score: float | None,
        span_id: str | None,
    ) -> dict[str, Any]:
        """Record feedback on a trace (a run) or, with `span_id`, on one of its spans."""
        self._require_trace(tenant_id, trace_id)
        if span_id is not None:
            trace = self.read_trace(tenant_id, trace_id)
            if span_id not in {span["id"] for span in trace["spans"]}:
                raise InvalidInput("span_id is not a span of this trace")
        record = self.feedback.add(
            tenant_id, trace_id, rating=rating, created_by=user_id,
            span_id=span_id, score=score, comment=(comment or "").strip() or None,
        )
        return record.as_json()

    def list_feedback(self, tenant_id: str, trace_id: uuid.UUID, span_id: str | None) -> dict[str, Any]:
        self._require_trace(tenant_id, trace_id)
        items = self.feedback.list(tenant_id, trace_id, span_id=span_id)
        return {"items": [r.as_json() for r in items]}

    def _require_trace(self, tenant_id: str, trace_id: uuid.UUID) -> None:
        if self.run_store.get_run(tenant_id, trace_id) is None:
            raise NotFound(TRACE_NOT_FOUND)
