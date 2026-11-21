"""Providers of the connections and settings services: MCP servers and their runtime, the MCP catalog,
connections, secrets, API keys and OTLP ingest.

`build_*` make a service from plain collaborators (the MCP control tools call them with what they hold);
`get_*` are the FastAPI providers built from the providers in eap_api.deps.
"""

from __future__ import annotations

from typing import Annotated

from eap_mcp_runtime import McpRuntime
from eap_observability import log_failure
from eap_run_store import EventLog, RunStore
from eap_run_store.mcp_catalog import CatalogStore
from eap_secrets.outbound import OutboundPolicy
from fastapi import Depends
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.deps import ApiKeyStoreDep, EngineDep, SessionDep, SpanStoreDep, StoresDep
from eap_api.mcp_servers.managed import get_mcp_runtime
from eap_api.services.api_keys import ApiKeyService
from eap_api.services.connections import ConnectionService
from eap_api.services.mcp_catalog import McpCatalogService
from eap_api.services.mcp_connection import McpConnectionService
from eap_api.services.mcp_runtime import McpRuntimeService
from eap_api.services.mcp_servers import McpServerService
from eap_api.services.otlp import OtlpIngestService
from eap_api.services.secrets import SecretService, sealed_secret_store


def get_outbound_policy() -> OutboundPolicy:
    """The outbound policy from the environment (EAP_MCP_DEV_ALLOW_HOSTS); tests override it."""
    return OutboundPolicy.from_env()


PolicyDep = Annotated[OutboundPolicy, Depends(get_outbound_policy)]
RuntimeDep = Annotated[McpRuntime, Depends(get_mcp_runtime)]


def build_mcp_catalog_service(engine: Engine) -> McpCatalogService:
    return McpCatalogService(CatalogStore(engine))


def build_mcp_runtime_service(db: Session | None, engine: Engine, runtime: McpRuntime) -> McpRuntimeService:
    return McpRuntimeService(db, engine, runtime, log_failure)


def build_mcp_server_service(db: Session, engine: Engine, policy: OutboundPolicy, runtime: McpRuntime) -> McpServerService:
    return McpServerService(db, engine, policy, McpRuntimeService(db, engine, runtime, log_failure), CatalogStore(engine))


def build_mcp_connection_service(
    engine: Engine, policy: OutboundPolicy, runtime: McpRuntime,
    run_store: RunStore | None = None, event_log: EventLog | None = None,
) -> McpConnectionService:
    return McpConnectionService(engine, policy, McpRuntimeService(None, engine, runtime, log_failure), run_store, event_log)


def build_connection_service(engine: Engine) -> ConnectionService:
    return ConnectionService(engine, sealed_secret_store)


def build_secret_service(db: Session, engine: Engine, runtime: McpRuntime) -> SecretService:
    return SecretService(engine, db, McpRuntimeService(db, engine, runtime, log_failure))


def get_mcp_catalog_service(engine: EngineDep) -> McpCatalogService:
    return build_mcp_catalog_service(engine)


def get_mcp_runtime_service(db: SessionDep, engine: EngineDep, runtime: RuntimeDep) -> McpRuntimeService:
    return build_mcp_runtime_service(db, engine, runtime)


def get_mcp_server_service(db: SessionDep, engine: EngineDep, policy: PolicyDep, runtime: RuntimeDep) -> McpServerService:
    return build_mcp_server_service(db, engine, policy, runtime)


def get_mcp_connection_service(
    engine: EngineDep, policy: PolicyDep, runtime: RuntimeDep, stores: StoresDep
) -> McpConnectionService:
    return build_mcp_connection_service(engine, policy, runtime, *stores)


def get_connection_service(engine: EngineDep) -> ConnectionService:
    return build_connection_service(engine)


def get_secret_service(db: SessionDep, engine: EngineDep, runtime: RuntimeDep) -> SecretService:
    return build_secret_service(db, engine, runtime)


def get_api_key_service(store: ApiKeyStoreDep, db: SessionDep) -> ApiKeyService:
    return ApiKeyService(store, db)


def get_otlp_ingest_service(spans: SpanStoreDep) -> OtlpIngestService:
    return OtlpIngestService(spans)


McpCatalogServiceDep = Annotated[McpCatalogService, Depends(get_mcp_catalog_service)]
McpRuntimeServiceDep = Annotated[McpRuntimeService, Depends(get_mcp_runtime_service)]
McpServerServiceDep = Annotated[McpServerService, Depends(get_mcp_server_service)]
McpConnectionServiceDep = Annotated[McpConnectionService, Depends(get_mcp_connection_service)]
ConnectionServiceDep = Annotated[ConnectionService, Depends(get_connection_service)]
SecretServiceDep = Annotated[SecretService, Depends(get_secret_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
OtlpIngestServiceDep = Annotated[OtlpIngestService, Depends(get_otlp_ingest_service)]
