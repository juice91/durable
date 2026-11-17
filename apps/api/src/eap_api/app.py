"""FastAPI application factory for the control-plane API."""

from contextlib import asynccontextmanager

from eap_observability import install_access_logging, instrument_fastapi, setup_logging, setup_telemetry
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager - the database schema was already checked in create_app."""
    yield
    app.state.run_launcher.close()


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
    from eap_api.auth import AUTH_MODE_API_KEY, require_auth_configured
    from eap_api.cors import PathScopedCORSMiddleware, agui_cors_origins_from_env

    # EAP_AUTH_MODE must be `dev` (the local bearer everywhere) or `api_key` (tenant API keys on
    # /api/v1/ag-ui/* are the only credential; every other route is 401). See eap_api.auth (Q2).
    mode = require_auth_configured()
    # A malformed EAP_AGUI_CORS_ORIGINS fails startup here (AGUI.9, D17).
    agui_cors_origins = agui_cors_origins_from_env()

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
    
    # CORS: the local Studio dev server, by either loopback name, on every route; on
    # /api/v1/ag-ui/* only, also the EAP_AGUI_CORS_ORIGINS allowlist (eap_api.cors, AGUI.9).
    app.add_middleware(PathScopedCORSMiddleware, agui_origins=agui_cors_origins)
    
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

    from eap_api.routes.tools import router as tools_router

    app.include_router(tools_router, prefix="/api/v1")

    # A malformed workflow loop cap (EAP_WORKFLOW_MAX_*, LP.5) fails startup here.
    # Schedules first: `/workflows/schedules` must not be read as a workflow id.
    from eap_api.routes.schedules import router as schedules_router

    app.include_router(schedules_router, prefix="/api/v1")
    from eap_api.routes.workflows import check_workflow_configuration, router as workflows_router

    check_workflow_configuration()
    app.include_router(workflows_router, prefix="/api/v1")
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
    from eap_api.routes.scorers import router as scorers_router

    app.include_router(scorers_router, prefix="/api/v1")
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
    # Knowledge bases (RM.7): CRUD, document upload, ingest and reindex runs, GET /embedding-models.
    # A malformed embedding allowlist or knowledge base limit fails startup here.
    from eap_api.routes.knowledge import check_knowledge_configuration, router as knowledge_router

    check_knowledge_configuration()
    app.include_router(knowledge_router, prefix="/api/v1")
    # Agent memory (RM.9): working memory, observations, forget-resource.
    from eap_api.routes.memory import router as memory_router

    app.include_router(memory_router, prefix="/api/v1")
    from eap_api.routes.api_keys import router as api_keys_router  # AGUI.3

    app.include_router(api_keys_router, prefix="/api/v1")
    from eap_api.routes.ag_ui import router as ag_ui_router  # AGUI.6: /ag-ui/agents/...

    app.include_router(ag_ui_router, prefix="/api/v1")
    from eap_api.routes.ag_ui_workflows import router as ag_ui_workflows_router  # AGUI.8: /ag-ui/workflows/...

    app.include_router(ag_ui_workflows_router, prefix="/api/v1")
    from eap_api.routes.attachments import router as attachments_router  # W2.4: attachments, message feedback

    app.include_router(attachments_router, prefix="/api/v1")

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
