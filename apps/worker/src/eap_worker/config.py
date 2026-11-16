"""Worker settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from eap_schemas.embedding_policy import EmbeddingModelPolicy
from eap_schemas.models_policy import ModelPolicy

QUEUE_NAME = "eap-agent-runs"
WORKFLOW_NAME = "eap.agent_run"
# Tool test runs (POST /tools/{tool_id}/runs), on the same queue: eap_worker.tool_run.
TOOL_WORKFLOW_NAME = "eap.tool_run"
# Workflow runs (POST /workflows/{id}/runs), on the same queue: eap_worker.workflow_run. Parallel
# branches run as child workflows of WORKFLOW_BRANCH_NAME (never enqueued by the API).
WORKFLOW_RUN_NAME = "eap.workflow_run"
WORKFLOW_BRANCH_NAME = "eap.workflow_branch"
# One iteration of a loop step (loops D5/D6): a child workflow of the run (or of an enclosing
# iteration), workflow id `{run_id}:loop:{S}` (eap_schemas.workflow_scope.iteration_workflow_id).
WORKFLOW_ITERATION_NAME = "eap.workflow_iteration"
# Workflow loop knobs (loops D10): recorded key -> (environment variable, default). Read once by
# eap.start_workflow_run into the run's `limits`. A run (or branch) started before the loops
# milestone has a recorded `limits` WITHOUT these keys, so every reader uses
# `limits.get(key, WORKFLOW_LOOP_LIMIT_DEFAULTS[key][1])` and only on loop paths.
WORKFLOW_LOOP_LIMIT_DEFAULTS: dict[str, tuple[str, int]] = {
    "max_loop_iterations": ("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", 100),
    "max_step_executions": ("EAP_WORKFLOW_MAX_STEP_EXECUTIONS", 2000),
    "max_run_concurrency": ("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", 8),
    "max_loop_output_bytes": ("EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES", 256 * 1024),
    "max_loop_state_bytes": ("EAP_WORKFLOW_MAX_LOOP_STATE_BYTES", 16 * 1024),
}
DELTA_CHANNEL = "eap_run_deltas"
# Knowledge base ingest and reindex runs (eap.kb_ingest) have their own queue, so a bulk upload never
# holds agent-run slots and never sends more than EAP_KB_INGEST_CONCURRENCY embedding workloads at
# once from one worker. The API (apps/api launcher.KB_INGEST_QUEUE_NAME) enqueues on exactly this
# name. Runs enqueued on QUEUE_NAME before the split still run there.
#
# RAG P1 follow-up: the queue is partitioned by tenant and knowledge base
# (`eap_schemas.knowledge.kb_ingest_partition_key`), so one knowledge base's bulk import cannot
# starve another's uploads: the worker visits the partitions with queued runs in random order on
# every poll, still at most EAP_KB_INGEST_CONCURRENCY running per worker in total. A DBOS queue is
# partitioned or not, so it has a new name; LEGACY_KB_INGEST_QUEUE_NAME is the old unpartitioned
# queue, still registered (same concurrency; every worker polls it) so runs enqueued before the upgrade drain.
KB_INGEST_QUEUE_NAME = "eap-kb-ingest-fair"
LEGACY_KB_INGEST_QUEUE_NAME = "eap-kb-ingest"
KB_INGEST_CONCURRENCY_ENV = "EAP_KB_INGEST_CONCURRENCY"
# Post-turn observational memory (RM.13) runs as its own workflow, eap.memory_observe_run, enqueued by
# the turn's eap.agent_run on this partitioned queue with the conversation id as partition key and at
# most ONE running per partition (globally). So observer runs of one conversation never overlap: a
# later turn's observer plans from a snapshot that already holds the earlier one's observation, and
# never pays for a request whose write would lose the race (`observation_conflict`).
MEMORY_OBSERVE_QUEUE_NAME = "eap-memory-observe"
MEMORY_OBSERVE_WORKFLOW_NAME = "eap.memory_observe_run"
DEFAULT_KB_INGEST_CONCURRENCY = 2

_DEFAULT_APP_DB = "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
_DEFAULT_DBOS_DB = "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos"

# Embedding requests (RM.6): caps on one provider request; a larger call is split into several.
DEFAULT_EMBEDDING_MAX_BATCH_INPUTS = 32
DEFAULT_EMBEDDING_MAX_BATCH_TOKENS = 16_384  # approximate tokens, ceil(len / 4)
DEFAULT_EMBEDDING_TIMEOUT_S = 120.0
# The pre-turn recall query (RM.14) runs before the model: its own short per-attempt deadline.
DEFAULT_RECALL_TIMEOUT_S = 8.0


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = float(raw)
    if not value > 0:
        raise ValueError(f"{name} must be positive, got {raw!r}")
    return value


@dataclass(frozen=True)
class WorkerSettings:
    app_database_url: str
    dbos_system_database_url: str
    dbos_system_schema: str
    app_version: str
    executor_id: str
    allowed_tools: frozenset[str] | None
    litellm_base_url: str | None
    litellm_api_key: str | None = field(repr=False)  # secret: never in repr/logs
    # Platform model allowlist (EAP_ALLOWED_MODELS); default `test:*` only.
    allowed_models: ModelPolicy = field(default_factory=lambda: ModelPolicy.parse(None))
    # Embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS + EAP_EMBEDDING_QUERY_INSTRUCTIONS);
    # default `test:*` only. Checked before every embedding call.
    allowed_embedding_models: EmbeddingModelPolicy = field(default_factory=lambda: EmbeddingModelPolicy.parse(None))
    # `litellm:*` embedding models may sit behind another gateway URL or virtual key than chat;
    # each falls back to the chat value (litellm_base_url / litellm_api_key) when unset.
    litellm_embedding_base_url: str | None = None
    litellm_embedding_api_key: str | None = field(default=None, repr=False)  # secret
    embedding_max_batch_inputs: int = DEFAULT_EMBEDDING_MAX_BATCH_INPUTS
    embedding_max_batch_tokens: int = DEFAULT_EMBEDDING_MAX_BATCH_TOKENS
    embedding_timeout_s: float = DEFAULT_EMBEDDING_TIMEOUT_S
    recall_timeout_s: float = DEFAULT_RECALL_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        allowed = os.environ.get("EAP_ALLOWED_TOOLS")
        return cls(
            app_database_url=os.environ.get("EAP_DATABASE_URL", _DEFAULT_APP_DB),
            # EAP_DBOS_URL is what the API uses; DBOS_SYSTEM_DATABASE_URL is the .env.example name.
            dbos_system_database_url=os.environ.get(
                "EAP_DBOS_URL", os.environ.get("DBOS_SYSTEM_DATABASE_URL", _DEFAULT_DBOS_DB)
            ),
            dbos_system_schema=os.environ.get("EAP_DBOS_SYSTEM_SCHEMA", "dbos"),
            app_version=os.environ.get("EAP_APP_VERSION", "m1"),
            executor_id=os.environ.get("EAP_EXECUTOR_ID", "worker-1"),
            allowed_tools=(
                frozenset(t.strip() for t in allowed.split(",") if t.strip()) if allowed is not None else None
            ),
            litellm_base_url=os.environ.get("EAP_LITELLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
            # Never logged or printed; see apps/worker/.env.example for how to load it.
            litellm_api_key=os.environ.get("EAP_LITELLM_API_KEY") or os.environ.get("LITELLM_API_KEY"),
            allowed_models=ModelPolicy.from_env(),
            allowed_embedding_models=EmbeddingModelPolicy.from_env(),
            litellm_embedding_base_url=os.environ.get("EAP_LITELLM_EMBEDDING_BASE_URL") or None,
            litellm_embedding_api_key=os.environ.get("EAP_LITELLM_EMBEDDING_API_KEY") or None,
            embedding_max_batch_inputs=_positive_int(
                "EAP_EMBEDDING_MAX_BATCH_INPUTS", DEFAULT_EMBEDDING_MAX_BATCH_INPUTS
            ),
            embedding_max_batch_tokens=_positive_int(
                "EAP_EMBEDDING_MAX_BATCH_TOKENS", DEFAULT_EMBEDDING_MAX_BATCH_TOKENS
            ),
            embedding_timeout_s=_positive_float("EAP_EMBEDDING_TIMEOUT_S", DEFAULT_EMBEDDING_TIMEOUT_S),
            recall_timeout_s=_positive_float("EAP_RECALL_TIMEOUT_S", DEFAULT_RECALL_TIMEOUT_S),
        )


def embedding_endpoint_problem(settings: WorkerSettings) -> str | None:
    """Why `litellm:*` embedding calls would fail on configuration alone, or None. A separate
    EAP_LITELLM_EMBEDDING_BASE_URL needs its own key (the chat key is never sent to another host);
    checked at worker start, not first at an embedding call."""
    embed_url = settings.litellm_embedding_base_url
    chat_url = settings.litellm_base_url
    if embed_url and embed_url.rstrip("/") != (chat_url or "").rstrip("/") and not settings.litellm_embedding_api_key:
        return "EAP_LITELLM_EMBEDDING_API_KEY is required when EAP_LITELLM_EMBEDDING_BASE_URL is set to another host"
    return None


def kb_ingest_concurrency() -> int:
    """At most this many eap.kb_ingest runs at once per worker process (EAP_KB_INGEST_CONCURRENCY, default 2)."""
    return _positive_int(KB_INGEST_CONCURRENCY_ENV, DEFAULT_KB_INGEST_CONCURRENCY)


# RAG P1 (P1.A3): the extraction sandbox (eap_worker.extract_sandbox). Its limits are the shared
# EAP_KB_* extraction settings (eap_knowledge.ExtractLimits: CPU seconds, memory MB, concurrency,
# PDF pages, the YAML/JSON cap), read here once per call so tests can change them.
# The child's JSON result is read up to this many bytes; more kills it (extract_output_too_large).
EXTRACT_MAX_OUTPUT_BYTES = 64 * 1024 * 1024


def extract_limits():  # -> eap_knowledge.ExtractLimits
    """The extraction limits from the environment (ValueError naming a bad variable)."""
    from eap_knowledge import ExtractLimits

    return ExtractLimits.from_env(os.environ)
