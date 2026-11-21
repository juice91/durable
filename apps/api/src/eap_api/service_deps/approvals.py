"""Provider of the approvals service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import ApprovalStoreDep, RunLauncherDep, StoresDep
from eap_api.services.approvals import ApprovalService


def get_approval_service(store: ApprovalStoreDep, stores: StoresDep, launcher: RunLauncherDep) -> ApprovalService:
    _, event_log = stores
    return ApprovalService(store, event_log, launcher)


ApprovalServiceDep = Annotated[ApprovalService, Depends(get_approval_service)]
