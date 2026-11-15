"""FastAPI application factory for the control-plane API."""

from contextlib import asynccontextmanager

from eap_observability import install_access_logging, instrument_fastapi, setup_logging, setup_telemetry
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


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
    from eap_api.auth import require_dev_auth_mode

    require_dev_auth_mode()

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
        # FastAPI's native OpenTelemetry follows any global provider; pin it to ours, or off.
        telemetry=(
            {"tracer_provider": tracer_provider}
            if tracer_provider is not None
            else {"tracing": False, "metrics": False, "logs": False}
        ),
    )
    
    # CORS: the local Studio dev server, by either loopback name (no other origin is allowed).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
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

    from eap_api.routes.workflows import router as workflows_router

    app.include_router(workflows_router, prefix="/api/v1")
    # Workflow runs and approval decisions (task 3.8): POST /workflows/{id}/runs, .../approve|reject.
    from eap_api.routes.workflow_runs import router as workflow_runs_router

    app.include_router(workflow_runs_router, prefix="/api/v1")
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
    # Knowledge bases (RM.7): CRUD, document upload, ingest and reindex runs, GET /embedding-models.
    # A malformed embedding allowlist or knowledge base limit fails startup here.
    from eap_api.routes.knowledge import check_knowledge_configuration, router as knowledge_router

    check_knowledge_configuration()
    app.include_router(knowledge_router, prefix="/api/v1")
    # Agent memory (RM.9): working memory, observations, forget-resource.
    from eap_api.routes.memory import router as memory_router

    app.include_router(memory_router, prefix="/api/v1")

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
