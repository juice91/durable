"""Worker settings, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
        )
