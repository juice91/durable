"""FastAPI application factory for the control-plane API."""

from contextlib import AsyncExitStack, asynccontextmanager

from eap_observability import install_access_logging, instrument_fastapi, setup_logging, setup_telemetry
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager - the database schema was already checked in create_app."""
    # Each MCP endpoint's streamable-HTTP session manager (control and external) runs for the life of the app.
    _reconcile_architect_seeds(app)
    async with AsyncExitStack() as running:
        for name in ("mcp_control_app", "external_mcp_app"):
            mcp_app = getattr(app.state, name, None)
            if mcp_app is not None:
                await running.enter_async_context(mcp_app.router.lifespan_context(mcp_app))
        yield
    app.state.run_launcher.close()


def _reconcile_architect_seeds(app: FastAPI) -> None:
    """Bring every tenant's existing Architect up to the current seed's built-in tool ids (best effort)."""
    import logging

    try:
        from eap_api.services.architect import reconcile_all_seeds

        reconcile_all_seeds(app.state.database_engine)
    except Exception:
        logging.getLogger(__name__).warning("architect seed: startup reconcile skipped", exc_info=True)


def create_app(database_url: str | None = None) -> FastAPI:
    """
    Create and configure the FastAPI application.
    
    Args:
        database_url: Optional Postgres URL. If None, uses EAP_DATABASE_URL (required).

    Returns:
        Configured FastAPI app instance.

    Raises:
        RuntimeError: no EAP_DATABASE_URL, or it is not Postgres.
        eap_db.SchemaNotAtHead: the database is not at the latest Alembic migration. The API never
            creates or alters tables; `uv run alembic -c packages/db/alembic.ini upgrade head` does.
    """
    from eap_api.auth import AUTH_MODE_API_KEY
    from eap_api.cors import PathScopedCORSMiddleware, agui_cors_origins_from_env, loopback_cors_origins_from_env
    from eap_iam import check_iam

    # EAP_AUTH_MODE is `dev` (the loopback dev bearer plus keys), `production` or the deprecated `api_key` (tenant
    # API keys on /api/v1/ag-ui/* only). Unset, unknown, no identity provider or no single policy engine refuses to
    # start (eap_iam.require_iam_configured); the plug-ins are built once the database engine exists, below.
    mode = check_iam().mode
    # A malformed EAP_WEBHOOK_BASE_URL, or a public hook path (EAP_WEBHOOK_PUBLIC=true) under the forgeable dev bearer, fails startup.
    from eap_api.inbound_guard import check_webhook_startup

    check_webhook_startup(auth_mode=mode)
    # Likewise a malformed EAP_EXTERNAL_BASE_URL, or external access (EAP_EXTERNAL_ACCESS_ENABLED) under the dev bearer.
    from eap_api.external_settings import check_external_startup

    check_external_startup(auth_mode=mode)
    # A malformed EAP_AGUI_CORS_ORIGINS fails startup here (AGUI.9, D17).
    agui_cors_origins = agui_cors_origins_from_env()
    loopback_cors_origins = loopback_cors_origins_from_env()  # EAP_CORS_LOOPBACK_ORIGINS: loopback hosts only

    # Traces and logs over OTLP when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise a no-op. The
    # request span's traceparent is stored on each run so the worker joins the same trace.
    tracer_provider = setup_telemetry("eap-api")
    if tracer_provider is not None:
        setup_logging("eap-api")

    app = FastAPI(
        title="EAP Control Plane API",
        description="Control-plane API for the Enterprise Agent Platform",
        version="1.0.0",
        lifespan=lifespan,
        # No 307 to the slash-less path: no route ends in '/', and a client that normalizes a '..'
        # path segment (DELETE …/documents/.. becomes DELETE /knowledge-bases/{id}/) must get a 404,
        # not a redirect that fetch follows with the same method onto the knowledge base itself
        # (RAG milestone checkpoint review, 2026-11-14).
        redirect_slashes=False,
        # In api_key mode nothing but /healthz and /api/v1/ag-ui/* answers without the dev bearer,
        # so the schema and docs pages are off (AGUI.9 review); dev mode keeps them.
        **({"openapi_url": None, "docs_url": None, "redoc_url": None} if mode == AUTH_MODE_API_KEY else {}),
        # FastAPI's native OpenTelemetry follows any global provider; pin it to ours, or off.
        telemetry=(
            {"tracer_provider": tracer_provider}
            if tracer_provider is not None
            else {"tracing": False, "metrics": False, "logs": False}
        ),
    )
    
    # One audit_log row per mutating call (eap_api.audit); inside CORS, so a preflight is never audited.
    from eap_api.audit import AuditMiddleware

    app.add_middleware(AuditMiddleware)

    # CORS: the local Studio dev server, by either loopback name, on every route; on
    # /api/v1/ag-ui/* only, also the EAP_AGUI_CORS_ORIGINS allowlist (eap_api.cors, AGUI.9).
    app.add_middleware(PathScopedCORSMiddleware, agui_origins=agui_cors_origins, loopback_origins=loopback_cors_origins)
    
    # Refusals of the shared definition operations (eap_run_store.definitions: publish, restore and
    # archive, also used by the Architect's tools) answer with the status and body the routes always had.
    from eap_run_store.definition_errors import DefinitionError

    @app.exception_handler(DefinitionError)
    async def _definition_error(_request, exc: DefinitionError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.body)

    from eap_api import concurrency

    concurrency.register(app)  # 412 / 428 / 400 of ETag + If-Match (eap_api.concurrency)

    from eap_api import service_errors

    service_errors.register(app)  # a service's ServiceError -> the API's JSON error body

    # Import and register routes
    from eap_api.routes import router as agents_router
    
    from eap_api.routes.runs import router as runs_router

    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")

    from eap_api.routes.conversations import router as conversations_router

    app.include_router(conversations_router, prefix="/api/v1")

    # MCP server registry; a malformed EAP_SECRETS_KEY fails startup here (an unset one is a warning).
    from eap_api.routes.mcp_servers import check_secrets_configuration, router as mcp_servers_router

    check_secrets_configuration()
    app.include_router(mcp_servers_router, prefix="/api/v1")
    from eap_api.routes.secrets import router as secrets_router

    app.include_router(secrets_router, prefix="/api/v1")
    from eap_api.routes.connections import router as connections_router
    from eap_api.routes.kb_sources import router as kb_sources_router

    app.include_router(connections_router, prefix="/api/v1")
    app.include_router(kb_sources_router, prefix="/api/v1")
    from eap_api.routes.mcp_catalog import router as mcp_catalog_router

    app.include_router(mcp_catalog_router, prefix="/api/v1")

    from eap_api.routes.capabilities import router as capabilities_router

    app.include_router(capabilities_router, prefix="/api/v1")
    from eap_api.routes.provenance import router as provenance_router  # system-aware Architect, phase C
    from eap_api.routes.system_context import router as system_context_router

    app.include_router(system_context_router, prefix="/api/v1")
    app.include_router(provenance_router, prefix="/api/v1")
    from eap_api.routes.tools import router as tools_router

    app.include_router(tools_router, prefix="/api/v1")

    # A malformed workflow loop cap (EAP_WORKFLOW_MAX_*, LP.5) fails startup here.
    # Schedules first: `/workflows/schedules` must not be read as a workflow id.
    from eap_api.routes.schedules import router as schedules_router

    app.include_router(schedules_router, prefix="/api/v1")
    from eap_api.routes.workflows import check_workflow_configuration, router as workflows_router

    check_workflow_configuration()
    app.include_router(workflows_router, prefix="/api/v1")
    from eap_api.routes.agent_grants import router as agent_grants_router  # the Access section: grants of an agent / workflow

    app.include_router(agent_grants_router, prefix="/api/v1")
    from eap_api.routes.resource_access import router as resource_access_router  # Access tabs and "why can't I?"

    app.include_router(resource_access_router, prefix="/api/v1")
    # Workflow runs and approval decisions (task 3.8): POST /workflows/{id}/runs, .../approve|reject.
    from eap_api.routes.workflow_runs import cancel_router as run_cancel_router, router as workflow_runs_router

    app.include_router(workflow_runs_router, prefix="/api/v1")
    # POST /runs/{id}/cancel (loops D12, LP.8): a workflow run and its whole tree.
    app.include_router(run_cancel_router, prefix="/api/v1")
    # Approvals inbox and tool-approval decisions (AC.8): GET /approvals, .../tool-approvals/....
    from eap_api.routes.approvals import router as approvals_router

    app.include_router(approvals_router, prefix="/api/v1")
    # Test suites and eval runs (AC.12): /agents/{id}/eval-suites, /eval-suites/..., /agents/{id}/evals/....
    from eap_api.routes.evals import router as evals_router

    app.include_router(evals_router, prefix="/api/v1")
    # Reusable scorers (W2.3): /scorers CRUD and /agents/{id}/scorers attachments.
    from eap_api.routes.scorer_suggestions import router as scorer_suggestions_router
    from eap_api.routes.scorers import router as scorers_router

    app.include_router(scorers_router, prefix="/api/v1")
    app.include_router(scorer_suggestions_router, prefix="/api/v1")  # AI-suggested rules for an agent or workflow
    from eap_api.routes.workflow_explanations import router as workflow_explanations_router

    app.include_router(workflow_explanations_router, prefix="/api/v1")  # AI explanation of a published workflow version
    # Log search over Loki (EAP_LOKI_URL), tenant-scoped: GET /logs.
    from eap_api.routes.logs import router as logs_router

    app.include_router(logs_router, prefix="/api/v1")
    # Processors (P0.9): GET /processors, POST /processors/test, GET /processor-results.
    from eap_api.routes.processors import router as processors_router

    app.include_router(processors_router, prefix="/api/v1")
    # Run options (P0.8): GET /models; an agent's request context fields, schema and validation.
    from eap_api.routes.models import router as models_router
    from eap_api.routes.request_context import router as request_context_router

    app.include_router(models_router, prefix="/api/v1")
    app.include_router(request_context_router, prefix="/api/v1")
    # Tenant-scoped run, token and cost metrics (P0.6): GET /metrics/summary|timeseries|breakdown.
    from eap_api.routes.metrics import router as metrics_router

    app.include_router(metrics_router, prefix="/api/v1")
    # Built-in trace view (P0.7): GET /runs/{run_id}/trace, derived from durable events.
    from eap_api.routes.traces import router as traces_router

    app.include_router(traces_router, prefix="/api/v1")
    from eap_api.routes.spans import router as spans_router  # parity round 2: GET /spans

    app.include_router(spans_router, prefix="/api/v1")
    # External OpenTelemetry traces: POST /api/v1/otlp/v1/traces (tenant API key, scope `otlp`).
    from eap_api.routes.otlp import router as otlp_router

    app.include_router(otlp_router, prefix="/api/v1")
    # Inbound webhooks: the signed hook endpoint (authenticated by its signature alone: docs/webhooks.md) and
    # the management API behind the dev bearer. Nothing but the hook route may live under /api/v1/hooks/.
    from eap_api.routes.webhook_hooks import router as webhook_hooks_router
    from eap_api.routes.webhook_triggers import router as webhook_triggers_router

    app.include_router(webhook_hooks_router, prefix="/api/v1")
    app.include_router(webhook_triggers_router, prefix="/api/v1")
    # External access management (clients, run keys, workflow exports), for people.
    from eap_api.routes.external_access import router as external_access_router

    app.include_router(external_access_router, prefix="/api/v1")
    # Knowledge bases (RM.7): CRUD, document upload, ingest and reindex runs, GET /embedding-models.
    # A malformed embedding allowlist or knowledge base limit fails startup here.
    from eap_api.routes.knowledge import check_knowledge_configuration, router as knowledge_router

    check_knowledge_configuration()
    app.include_router(knowledge_router, prefix="/api/v1")
    # Agent memory (RM.9): working memory, observations, forget-resource.
    from eap_api.routes.memory import router as memory_router

    app.include_router(memory_router, prefix="/api/v1")
    from eap_api.routes.iam import router as iam_router  # IAM P0a: GET /iam/capabilities
    from eap_api.routes.audit import router as audit_router  # IAM P0b: GET /audit

    app.include_router(iam_router, prefix="/api/v1")
    from eap_api.routes.local_auth import router as local_auth_router  # the built-in admin account (404 without it)

    app.include_router(local_auth_router, prefix="/api/v1")
    from eap_api.routes.personal_api_keys import router as personal_api_keys_router  # personal API keys

    app.include_router(personal_api_keys_router, prefix="/api/v1")
    app.include_router(audit_router, prefix="/api/v1")
    from eap_api.routes.api_keys import router as api_keys_router  # AGUI.3

    app.include_router(api_keys_router, prefix="/api/v1")
    from eap_api.routes.ag_ui import router as ag_ui_router  # AGUI.6: /ag-ui/agents/...

    app.include_router(ag_ui_router, prefix="/api/v1")
    from eap_api.routes.ag_ui_workflows import router as ag_ui_workflows_router  # AGUI.8: /ag-ui/workflows/...

    app.include_router(ag_ui_workflows_router, prefix="/api/v1")
    from eap_api.routes.attachments import router as attachments_router  # W2.4: attachments, message feedback

    app.include_router(attachments_router, prefix="/api/v1")
    from eap_api.routes.architect import router as architect_router  # Architect: seed, options, draft, page save

    app.include_router(architect_router, prefix="/api/v1")

    # PydanticOS controlled through MCP: POST /api/v1/mcp (Streamable HTTP; tenant API keys, scope `manage`
    # for mutations). See eap_api.mcp_control and docs/mcp-control.md.
    from eap_api.mcp_control.server import mount_mcp_control

    app.state.mcp_control_app = mount_mcp_control(app)

    # The run surface for external agents: POST /api/v1/external/mcp (`run` keys only; 404 unless
    # EAP_EXTERNAL_ACCESS_ENABLED). See eap_api.external_mcp and docs/external-access.md.
    from eap_api.external_mcp.server import mount_external_mcp

    app.state.external_mcp_app = mount_external_mcp(app)

    # The A2A server for external agents under /api/v1/external/a2a/{endpoint_id}: Agent Cards and JSON-RPC (A2A 1.0),
    # under the same switch and door. See eap_api.external_a2a.
    from eap_api.external_a2a.server import mount_external_a2a

    mount_external_a2a(app)

    # Add healthz at root level (no prefix)
    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        """Health check endpoint (no auth required)."""
        return {"status": "ok"}
    
    # Fail fast unless Alembic has migrated the database to head (packages/db owns the schema).
    from eap_api.database import get_engine
    from eap_db import require_schema_at_head

    engine = get_engine(database_url)
    try:
        require_schema_at_head(engine)
    except Exception:
        engine.dispose()
        raise

    # Store engine in app state for later use
    app.state.database_engine = engine
    app.state.database_url = database_url

    # The IAM plug-ins (identity providers, the policy engine, the membership source), built on the engine. A
    # plug-in that cannot start (transitional policy in production, half-configured provider) fails startup here.
    from eap_iam import load_iam

    try:
        app.state.iam = load_iam(engine)
    except Exception:
        engine.dispose()
        raise

    # Attachment bytes live in object storage (eap_blob_store; EAP_BLOB_*), never in Postgres. A bad
    # EAP_BLOB_* setting fails startup here.
    from eap_blob_store import blob_store_from_env

    try:
        app.state.blob_store = blob_store_from_env()
    except Exception:
        engine.dispose()
        raise

    # Runs and events (eap_run_store). The DBOS client and LISTEN connections open lazily.
    from eap_api.launcher import DBOSRunLauncher, LauncherSettings
    from eap_api.sse import PostgresDeltaSource
    from eap_run_store import EventLog, RunStore

    app.state.run_launcher = DBOSRunLauncher(LauncherSettings.from_env())
    app.state.run_stores = (RunStore(engine), EventLog(engine))
    from eap_run_store.knowledge import KnowledgeStore

    app.state.knowledge_store = KnowledgeStore(engine)
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app.state.delta_source = PostgresDeltaSource(conninfo)

    instrument_fastapi(app)  # the request server span; no-op when telemetry is off
    # One access line per request with the route template, never a raw path parameter (resource ids
    # may be emails) nor a query string (P1.C2). uvicorn's own access log is off (--no-access-log).
    install_access_logging(app)

    return app
