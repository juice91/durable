"""Provider of the workflow explanation service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import SessionDep
from eap_api.service_deps.iam import AccessDep
from eap_api.services.workflow_explanations import WorkflowExplanationService


def get_workflow_explanation_service(session: SessionDep, access: AccessDep) -> WorkflowExplanationService:
    return WorkflowExplanationService(session, access)


WorkflowExplanationServiceDep = Annotated[WorkflowExplanationService, Depends(get_workflow_explanation_service)]
