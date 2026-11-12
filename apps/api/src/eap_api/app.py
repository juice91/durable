"""FastAPI application factory for the control-plane API."""

from contextlib import asynccontextmanager

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

    app = FastAPI(
        title="EAP Control Plane API",
        description="Control-plane API for the Enterprise Agent Platform",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    # CORS middleware - allow http://localhost:3000
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Import and register routes
    from eap_api.routes import router as agents_router
    
    from eap_api.routes.runs import router as runs_router

    app.include_router(agents_router, prefix="/api/v1")
    app.include_router(runs_router, prefix="/api/v1")
    
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
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    app.state.delta_source = PostgresDeltaSource(conninfo)

    return app
