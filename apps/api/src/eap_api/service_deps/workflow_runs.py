"""Provider of the workflow runs service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import RunLauncherDep, SessionDep, StoresDep
from eap_api.service_deps.iam import AccessDep
from eap_api.services.workflow_runs import WorkflowRunService


def get_workflow_run_service(
    session: SessionDep, stores: StoresDep, launcher: RunLauncherDep, access: AccessDep
) -> WorkflowRunService:
    run_store, event_log = stores
    return WorkflowRunService(session, run_store, event_log, launcher, access)


WorkflowRunServiceDep = Annotated[WorkflowRunService, Depends(get_workflow_run_service)]
