"""Providers of the workflow services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import SessionDep
from eap_api.service_deps.iam import AccessDep
from eap_api.services.workflows import WorkflowService


def get_workflow_service(session: SessionDep, access: AccessDep) -> WorkflowService:
    return WorkflowService(session, access)


WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]
