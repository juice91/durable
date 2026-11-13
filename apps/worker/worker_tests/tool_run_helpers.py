"""Start tool test runs the way the API does (eap_api.routes.tools + DBOSRunLauncher.launch_tool)."""

from __future__ import annotations

import json
import uuid
from typing import Any


def tool_input_json(args: dict[str, Any]) -> str:
    """The canonical JSON text the API stores as runs.input and enqueues."""
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def enqueue_tool_run(
    client,
    run_store,
    *,
    tenant_id: str,
    tool_id: str,
    args: dict[str, Any],
    app_version: str,
    run_id: uuid.UUID | None = None,
    create_row: bool = True,
) -> uuid.UUID:
    """Insert the kind='tool' runs row, then enqueue `eap.tool_run(run_id, tenant_id, tool_id, input_json)`."""
    run_id = run_id or uuid.uuid4()
    input_json = tool_input_json(args)
    if create_row:
        run_store.create_tool_run(tenant_id, tool_id, input_json, run_id)
    client.enqueue(
        {
            "queue_name": "eap-agent-runs",
            "workflow_name": "eap.tool_run",
            "workflow_id": str(run_id),
            "app_version": app_version,
        },
        str(run_id),
        tenant_id,
        tool_id,
        input_json,
    )
    return run_id
