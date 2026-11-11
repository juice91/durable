"""Starting runs on the orchestration side, per the run hand-off contract.

docs/plans/2026-11-11-milestone-1-vertical-slice.md, "Run hand-off contract": the API never
imports worker code; it enqueues by name through `dbos.DBOSClient` against the DBOS system
database, with workflow_id = run_id, so a repeated enqueue for the same run attaches to the
existing workflow instead of starting a second one (DBOS's default
`workflow_id_reuse_policy` is "return-existing").
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from fastapi import Request

QUEUE_NAME = "eap-agent-runs"
WORKFLOW_NAME = "eap.agent_run"

_DEFAULT_DBOS_URL = "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos"


@dataclass(frozen=True)
class LauncherSettings:
    system_database_url: str
    system_schema: str
    app_version: str

    @classmethod
    def from_env(cls) -> "LauncherSettings":
        # Same names and fallbacks as the worker (apps/worker/src/eap_worker/config.py).
        return cls(
            system_database_url=os.environ.get(
                "EAP_DBOS_URL", os.environ.get("DBOS_SYSTEM_DATABASE_URL", _DEFAULT_DBOS_URL)
            ),
            system_schema=os.environ.get("EAP_DBOS_SYSTEM_SCHEMA", "dbos"),
            app_version=os.environ.get("EAP_APP_VERSION", "m1"),
        )


class RunLauncher(Protocol):
    def launch(
        self, *, run_id: UUID, tenant_id: str, agent_id: UUID, version: int, spec_json: str, input_text: str
    ) -> None: ...

    def close(self) -> None: ...


class DBOSRunLauncher:
    """Enqueues `eap.agent_run` on `eap-agent-runs`. The DBOSClient is created on first use."""

    def __init__(self, settings: LauncherSettings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from dbos import DBOSClient

                self._client = DBOSClient(
                    system_database_url=self.settings.system_database_url,
                    dbos_system_schema=self.settings.system_schema,
                    lazy=True,
                    # Fail the request instead of blocking it while the system DB is down.
                    retry_connection_errors=False,
                )
            return self._client

    def launch(
        self, *, run_id: UUID, tenant_id: str, agent_id: UUID, version: int, spec_json: str, input_text: str
    ) -> None:
        options = {
            "queue_name": QUEUE_NAME,
            "workflow_name": WORKFLOW_NAME,
            "workflow_id": str(run_id),
            "app_version": self.settings.app_version,
        }
        self._get_client().enqueue(options, str(run_id), tenant_id, str(agent_id), version, spec_json, input_text)

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None and hasattr(client, "destroy"):
            client.destroy()


def get_run_launcher(request: Request) -> RunLauncher:
    return request.app.state.run_launcher
