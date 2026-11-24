"""DBOS application setup for the worker."""

from __future__ import annotations

from dbos import DBOS, DBOSConfig
from eap_observability import dbos_otel_config, otlp_endpoint_from_env, setup_logging, setup_telemetry
from eap_observability.agents import instrument_pydantic_ai

from eap_worker.config import (
    KB_INGEST_QUEUE_NAME,
    LEGACY_KB_INGEST_QUEUE_NAME,
    MEMORY_OBSERVE_QUEUE_NAME,
    QUEUE_NAME,
    WorkerSettings,
    kb_ingest_concurrency,
)
from eap_worker.attachments import configure_blob_store
from eap_worker.runtime import configure_runtime

# Importing registers the generic agent, its DBOS steps, the queue and the workflow. This must
# happen before DBOS.launch() so recovery finds every step and workflow by name.
from eap_worker import workflow as _workflow  # noqa: F401
from eap_worker import processors as _processors  # noqa: F401  (eap-moderator, eap.input/output_processors)
from eap_worker import tool_run as _tool_run  # noqa: F401  (eap.tool_run and its steps)
from eap_worker import workflow_run as _workflow_run  # noqa: F401  (eap.workflow_run, eap.workflow_branch)
from eap_worker import kb_ingest as _kb_ingest  # noqa: F401  (eap.kb_ingest and its steps, RM.10)
from eap_worker import observational as _observational  # noqa: F401  (eap-observer, eap.memory_observe*, RM.13)
from eap_worker import retention as _retention  # noqa: F401  (eap.memory_retention and its steps, RM.15)
from eap_worker import attachment_retention as _attachment_retention  # noqa: F401  (eap.attachment_retention)
from eap_worker import audit_retention as _audit_retention  # noqa: F401  (eap.audit_retention, IAM P0b)
from eap_worker import mcp_catalog_mirror as _mcp_catalog_mirror  # noqa: F401  (eap.mcp_catalog_mirror and its steps)
from eap_worker import mcp_runtime_sweep as _mcp_runtime_sweep  # noqa: F401  (eap.mcp_runtime_sweep and its step)
from eap_worker import eval_run as _eval_run  # noqa: F401  (eap.eval_run, its steps and eap-judge, AC.11)
from eap_worker import kb_source_sync as _kb_source_sync  # noqa: F401  (eap.kb_source_sync, its segment child and steps)

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
    validate_startup(settings)
    configure_blob_store()  # EAP_BLOB_*: a bad setting fails the start, before DBOS launches
    configure_runtime(settings)
    setup_worker_telemetry()
    DBOS(config=dbos_config(settings))
    DBOS.launch()
    # DBOS 3.5 queues are database-backed: declared with register_queue once the system DB is up
    # (Queue(...) can no longer be constructed directly). The DBOSClient enqueues by name.
    DBOS.register_queue(QUEUE_NAME, polling_interval_sec=queue_polling_interval())
    # eap.kb_ingest on its own queue, at most EAP_KB_INGEST_CONCURRENCY (2) running per worker, so a
    # bulk upload neither starves agent runs nor floods the embedding provider. Partitioned by tenant
    # and knowledge base (the API's queue_partition_key): any partition limit makes a DBOS queue
    # partitioned, and partition_worker_concurrency = the worker cap restricts nothing by itself, so
    # one knowledge base alone still uses every slot. Each poll visits the partitions with queued
    # runs in random order, so a knowledge base with one upload gets the next free slot about half
    # the time even behind another's bulk import of hundreds.
    concurrency = kb_ingest_concurrency()
    DBOS.register_queue(
        KB_INGEST_QUEUE_NAME,
        worker_concurrency=concurrency,
        partition_worker_concurrency=concurrency,
        polling_interval_sec=queue_polling_interval(),
    )
    # The unpartitioned queue the API used before (its runs carry no partition key, which a
    # partitioned queue never dequeues): still registered, so runs enqueued before the upgrade
    # drain. Nothing new is enqueued here. Every worker polls every queue row in the system
    # database, so removing it takes DBOS.delete_queue, not just this call: apps/worker/README.md.
    DBOS.register_queue(
        LEGACY_KB_INGEST_QUEUE_NAME,
        worker_concurrency=concurrency,
        polling_interval_sec=queue_polling_interval(),
    )
    # eap.memory_observe_run (RM.13): one observer workflow at a time per conversation (the partition
    # key), across all workers, so overlapping observers never pay for a request that cannot be written.
    DBOS.register_queue(
        MEMORY_OBSERVE_QUEUE_NAME,
        partition_concurrency=1,
        polling_interval_sec=queue_polling_interval(),
    )
    # eap.memory_retention (RM.15): scheduled when EAP_MEMORY_RETENTION_* is set, removed on =off.
    _retention.apply_retention_schedule()
    # eap.attachment_retention: unsent uploads expire (EAP_ATTACHMENT_ORPHAN_HOURS, default 24 h); on by default.
    _attachment_retention.apply_attachment_retention_schedule()
    # eap.audit_retention: audit_log rows older than EAP_AUDIT_RETENTION_DAYS (default 400) are pruned daily.
    _audit_retention.apply_audit_retention_schedule()
    # eap.mcp_catalog_mirror: hourly by default, removed on EAP_MCP_CATALOG_SCHEDULE=off.
    _mcp_catalog_mirror.apply_catalog_schedule()
    # eap.mcp_runtime_sweep: every minute, removed on EAP_MCP_RUNTIME_SWEEP_SCHEDULE=off.
    _mcp_runtime_sweep.apply_runtime_sweep_schedule()
    return settings


def validate_startup(settings: WorkerSettings) -> None:
    """Fail at start (before DBOS launches or recovers anything) on settings that would otherwise
    first fail inside a step: the ingest caps, the ingest queue's concurrency, the exact-search
    fallback cap, the reflector input cap, the retention and MCP catalog mirror settings and the embedding gateway pairing. Raises ValueError (RetentionConfigError and CatalogMirrorConfigError are)."""
    from eap_run_store.knowledge import check_exact_search_max_chunks

    from eap_worker.config import embedding_endpoint_problem

    _kb_ingest.validate_settings()
    _observational.max_active_observation_tokens()
    check_exact_search_max_chunks()
    kb_ingest_concurrency()
    _retention.RetentionConfig.from_env()
    _attachment_retention.AttachmentRetentionConfig.from_env()
    _audit_retention.AuditRetentionConfig.from_env()
    _mcp_catalog_mirror.CatalogMirrorConfig.from_env()
    _mcp_runtime_sweep.RuntimeSweepConfig.from_env()
    queue_polling_interval()
    problem = embedding_endpoint_problem(settings)
    if problem is not None:
        raise ValueError(problem)


def queue_polling_interval() -> float:
    import os

    return float(os.environ.get("EAP_QUEUE_POLL_SECONDS", "1.0"))
