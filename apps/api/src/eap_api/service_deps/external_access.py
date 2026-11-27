"""Providers of the external access services."""

from __future__ import annotations

from typing import Annotated, Any

from eap_iam import Principal
from fastapi import Depends
from sqlalchemy.orm import Session

from eap_api.deps import (
    ApiKeyStoreDep,
    ExternalAccessStoreDep,
    RunLauncherDep,
    SessionDep,
    StoresDep,
    api_key_store_of,
    audit_store_of,
    external_access_store_of,
    iam_of,
    run_launcher_of,
    run_stores_of,
)
from eap_api.external_settings import ExternalSettings
from eap_api.service_deps.iam import AccessDep
from eap_api.services.access import Access
from eap_api.services.authz import AuthzService
from eap_api.services.external_access import ExternalAccessService
from eap_api.services.external_runs import ExternalRunService
from eap_api.services.workflow_runs import WorkflowRunService


def get_external_access_service(
    session: SessionDep, stores: StoresDep, launcher: RunLauncherDep, store: ExternalAccessStoreDep,
    keys: ApiKeyStoreDep, access: AccessDep,
) -> ExternalAccessService:
    run_store, event_log = stores
    runs = WorkflowRunService(session, run_store, event_log, launcher)  # the service itself checks the admin right
    return ExternalAccessService(store, keys, session, runs, ExternalSettings.from_env(), access)


ExternalAccessServiceDep = Annotated[ExternalAccessService, Depends(get_external_access_service)]


def external_access_service_of(app: Any, session: Session, principal: Principal) -> ExternalAccessService:
    """For an entry point that holds an app and a principal but no Request."""
    run_store, event_log = run_stores_of(app)
    runs = WorkflowRunService(session, run_store, event_log, run_launcher_of(app))
    return ExternalAccessService(
        external_access_store_of(app), api_key_store_of(app), session, runs, ExternalSettings.from_env(),
        Access(AuthzService(iam_of(app).policy), principal),
    )


def external_run_service_of(app: Any, session: Session) -> ExternalRunService:
    """The service the external surfaces (MCP, A2A) call, one per service call. Its WorkflowRunService has
    no Access: ExternalRunService decides what the caller may do, through the authorisation service."""
    run_store, event_log = run_stores_of(app)
    runs = WorkflowRunService(session, run_store, event_log, run_launcher_of(app))
    return ExternalRunService(
        external_access_store_of(app), api_key_store_of(app), runs, run_store, AuthzService(iam_of(app).policy),
        audit_store_of(app), session, ExternalSettings.from_env(),
    )
