"""DBOS application setup for the worker."""

from __future__ import annotations

from dbos import DBOS, DBOSConfig
from eap_observability import dbos_otel_config, otlp_endpoint_from_env, setup_logging, setup_telemetry
from eap_observability.agents import instrument_pydantic_ai

from eap_worker.config import QUEUE_NAME, WorkerSettings
from eap_worker.runtime import configure_runtime

# Importing registers the generic agent, its DBOS steps, the queue and the workflow. This must
# happen before DBOS.launch() so recovery finds every step and workflow by name.
from eap_worker import workflow as _workflow  # noqa: F401
from eap_worker import processors as _processors  # noqa: F401  (eap-moderator, eap.input/output_processors)
from eap_worker import tool_run as _tool_run  # noqa: F401  (eap.tool_run and its steps)
from eap_worker import workflow_run as _workflow_run  # noqa: F401  (eap.workflow_run, eap.workflow_branch)
from eap_worker import kb_ingest as _kb_ingest  # noqa: F401  (eap.kb_ingest and its steps, RM.10)

APP_NAME = "eap-worker"


def dbos_config(settings: WorkerSettings) -> DBOSConfig:
    config: DBOSConfig = {
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
    # DBOS workflow/step spans and DBOS logs, through the providers setup_worker_telemetry installed.
    config.update(dbos_otel_config(otlp_endpoint_from_env()))  # type: ignore[typeddict-item]
    return config


def setup_worker_telemetry() -> bool:
    """Traces (incl. Pydantic AI model requests and tool calls, without content) and logs over OTLP
    when OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise nothing. Must run before DBOS is constructed."""
    if setup_telemetry("eap-worker") is None:
        return False
    setup_logging("eap-worker")
    instrument_pydantic_ai(include_content=False)
    return True


def launch(settings: WorkerSettings | None = None) -> WorkerSettings:
    """Configure resources (failing fast unless eap_app is at the latest migration), construct DBOS
    and launch it (recovery runs here)."""
    settings = settings or WorkerSettings.from_env()
    configure_runtime(settings)
    setup_worker_telemetry()
    DBOS(config=dbos_config(settings))
    DBOS.launch()
    # DBOS 3.5 queues are database-backed: declared with register_queue once the system DB is up
    # (Queue(...) can no longer be constructed directly). The DBOSClient enqueues by name.
    DBOS.register_queue(QUEUE_NAME, polling_interval_sec=queue_polling_interval())
    return settings


def queue_polling_interval() -> float:
    import os

    return float(os.environ.get("EAP_QUEUE_POLL_SECONDS", "1.0"))
