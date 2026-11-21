"""Provider of the workflow explanation service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import SessionDep
from eap_api.services.workflow_explanations import WorkflowExplanationService


def get_workflow_explanation_service(session: SessionDep) -> WorkflowExplanationService:
    return WorkflowExplanationService(session)


WorkflowExplanationServiceDep = Annotated[WorkflowExplanationService, Depends(get_workflow_explanation_service)]
