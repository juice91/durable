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
DELTA_CHANNEL = "eap_run_deltas"

_DEFAULT_APP_DB = "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
_DEFAULT_DBOS_DB = "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos"

# Embedding requests (RM.6): caps on one provider request; a larger call is split into several.
DEFAULT_EMBEDDING_MAX_BATCH_INPUTS = 32
DEFAULT_EMBEDDING_MAX_BATCH_TOKENS = 16_384  # approximate tokens, ceil(len / 4)
DEFAULT_EMBEDDING_TIMEOUT_S = 120.0


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
        )
