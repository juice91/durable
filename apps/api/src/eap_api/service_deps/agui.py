"""Providers of the AG-UI services (agent threads, workflow threads)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import (
    AgUiStoreDep,
    ApiKeyStoreDep,
    ApprovalStoreDep,
    ChatAttachmentStoreDep,
    EngineDep,
    MessageFeedbackStoreDep,
    RunLauncherDep,
    StoresDep,
)
from eap_api.services.ag_ui import AgUiService
from eap_api.services.ag_ui_workflows import AgUiWorkflowService


def get_agui_service(
    engine: EngineDep,
    stores: StoresDep,
    ag_ui: AgUiStoreDep,
    approvals: ApprovalStoreDep,
    api_keys: ApiKeyStoreDep,
    attachments: ChatAttachmentStoreDep,
    feedback: MessageFeedbackStoreDep,
    launcher: RunLauncherDep,
) -> AgUiService:
    run_store, event_log = stores
    return AgUiService(engine, run_store, event_log, ag_ui, approvals, api_keys, attachments, feedback, launcher)


AgUiServiceDep = Annotated[AgUiService, Depends(get_agui_service)]


def get_agui_workflow_service(
    engine: EngineDep,
    stores: StoresDep,
    ag_ui: AgUiStoreDep,
    approvals: ApprovalStoreDep,
    api_keys: ApiKeyStoreDep,
    attachments: ChatAttachmentStoreDep,
    feedback: MessageFeedbackStoreDep,
    launcher: RunLauncherDep,
) -> AgUiWorkflowService:
    run_store, event_log = stores
    return AgUiWorkflowService(
        engine, run_store, event_log, ag_ui, approvals, api_keys, attachments, feedback, launcher
    )


AgUiWorkflowServiceDep = Annotated[AgUiWorkflowService, Depends(get_agui_workflow_service)]
