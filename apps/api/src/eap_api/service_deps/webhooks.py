"""Providers of the inbound webhook services."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import AuditStoreDep, EngineDep, RunLauncherDep, SessionDep, StoresDep, WebhookStoreDep
from eap_api.inbound_guard import webhook_base_url
from eap_api.service_deps.iam import AccessDep
from eap_api.services.secrets import sealed_secret_store
from eap_api.services.webhook_ingest import WebhookIngestService
from eap_api.services.webhook_triggers import WebhookTriggerService
from eap_api.services.workflow_runs import WorkflowRunService


def get_webhook_trigger_service(
    session: SessionDep, engine: EngineDep, store: WebhookStoreDep, access: AccessDep
) -> WebhookTriggerService:
    return WebhookTriggerService(store, session, engine, sealed_secret_store, webhook_base_url(), access)


def get_webhook_ingest_service(
    session: SessionDep, stores: StoresDep, launcher: RunLauncherDep, store: WebhookStoreDep, audit: AuditStoreDep
) -> WebhookIngestService:
    """No Access: the sender is no person. The authority is the approved trigger, proven by the signature."""
    run_store, event_log = stores
    return WebhookIngestService(store, WorkflowRunService(session, run_store, event_log, launcher), audit)


WebhookTriggerServiceDep = Annotated[WebhookTriggerService, Depends(get_webhook_trigger_service)]
WebhookIngestServiceDep = Annotated[WebhookIngestService, Depends(get_webhook_ingest_service)]
