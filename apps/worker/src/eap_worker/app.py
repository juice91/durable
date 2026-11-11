"""DBOS application setup for the worker."""

from __future__ import annotations

from dbos import DBOS, DBOSConfig

from eap_worker.config import QUEUE_NAME, WorkerSettings
from eap_worker.runtime import configure_runtime

# Importing registers the generic agent, its DBOS steps, the queue and the workflow. This must
# happen before DBOS.launch() so recovery finds every step and workflow by name.
from eap_worker import workflow as _workflow  # noqa: F401

APP_NAME = "eap-worker"


def dbos_config(settings: WorkerSettings) -> DBOSConfig:
    return {
        "name": APP_NAME,
        "system_database_url": settings.dbos_system_database_url,
        "dbos_system_schema": settings.dbos_system_schema,
        # Pinned: DBOS only recovers/dequeues workflows whose version matches, and the API
        # enqueues with the same EAP_APP_VERSION.
        "application_version": settings.app_version,
        # Fixed: a restarted worker with the same id recovers its own pending workflows.
        "executor_id": settings.executor_id,
        "run_admin_server": False,
    }


def launch(settings: WorkerSettings | None = None) -> WorkerSettings:
    """Configure resources, create app tables, construct DBOS and launch it (recovery runs here)."""
    settings = settings or WorkerSettings.from_env()
    configure_runtime(settings)
    DBOS(config=dbos_config(settings))
    DBOS.launch()
    # DBOS 3.5 queues are database-backed: declared with register_queue once the system DB is up
    # (Queue(...) can no longer be constructed directly). The DBOSClient enqueues by name.
    DBOS.register_queue(QUEUE_NAME, polling_interval_sec=queue_polling_interval())
    return settings


def queue_polling_interval() -> float:
    import os

    return float(os.environ.get("EAP_QUEUE_POLL_SECONDS", "1.0"))
