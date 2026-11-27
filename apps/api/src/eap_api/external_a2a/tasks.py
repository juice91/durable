"""A run view of `ExternalRunService` as an A2A 1.0 Task (design 5.2 and 6): the task id is the run id, the history is
empty, a status message carries only fixed texts, and the one artifact, `result`, is the workflow's return value."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from eap_api.external_a2a.protocol import timestamp

STATES = {
    "queued": "TASK_STATE_SUBMITTED", "running": "TASK_STATE_WORKING", "input_required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED", "cancelled": "TASK_STATE_CANCELED", "failed": "TASK_STATE_FAILED",
}
WAITING_MESSAGE = "waiting for a person in the platform to decide"
# view key -> task metadata key
_METADATA = {
    "export": "export", "version": "version", "deadline": "deadline", "outcome": "outcome",
    "retry_safe": "retrySafe", "truncated": "truncated",
}


def task_of(view: dict[str, Any]) -> dict[str, Any]:
    """The Task of a run view made with `task_fields`; no `artifacts` unless the view carries a result."""
    status: dict[str, Any] = {
        "state": STATES[view["state"]], "timestamp": timestamp(datetime.fromisoformat(view["updated_at"])),
    }
    error = view.get("error")
    text = WAITING_MESSAGE if view["state"] == "input_required" else (error or {}).get("message")
    if text:
        status["message"] = {
            "messageId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{view['run_id']}:{view['state']}")),
            "role": "ROLE_AGENT", "parts": [{"text": text}], "taskId": view["run_id"],
            "contextId": view["context_id"],
        }
    metadata = {name: view[key] for key, name in _METADATA.items() if key in view}
    if error:
        metadata["errorCode"] = error["code"]
    task: dict[str, Any] = {"id": view["run_id"], "contextId": view["context_id"], "status": status,
                            "metadata": metadata}
    if "result" in view:
        task["artifacts"] = [{
            "artifactId": "result", "name": "result",
            "parts": [{"data": view["result"], "mediaType": "application/json"}],
        }]
    return task
