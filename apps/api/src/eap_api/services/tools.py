"""Platform tools: the catalog with the current allowlist view, and tool test runs.

`start_run` records a tenant-scoped run of kind 'tool' and enqueues `eap.tool_run` for it, per the run
hand-off contract (the same semantics as agent runs, see eap_api.services.runs.launch_recorded_run).
Refusals answer the bodies the routes always had (`{"detail": <message>}`; a rejected input is a list of
loc/msg/type, never the rejected values, as the `detail_object`).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from pydantic import ValidationError

from eap_api.ports import RunLauncher
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.run_filters import plain_refusals
from eap_observability.context import current_trace_context
from eap_observability.redaction import log_failure
from eap_run_store import ENQUEUE_FAILED_ERROR, RunIdConflict, RunStore
from eap_run_store.workflow_checks import allowed_tool_ids as _allowed_tool_ids
from eap_schemas.tools import TOOL_DECLARATIONS, ToolDeclaration

log = logging.getLogger("eap_api")

# Same bound as an agent run's input text (RunCreate): the canonical JSON of the tool input.
MAX_TOOL_INPUT_BYTES = 32 * 1024
TOOL_NOT_FOUND = "Tool not found"
RUN_ID_IN_USE = "run_id is already in use"
RUN_ID_DIFFERENT = "run_id is already used by a different run request"
NOT_STARTED = "Run recorded but could not be started; retry the same request (same run_id)"


def safe_errors(exc: ValidationError, prefix: list[Any]) -> list[dict[str, Any]]:
    """FastAPI's error shape (loc, msg, type) without the rejected values or ctx: tool input can be
    anything a user typed, including secrets, and must never be echoed back."""
    return [
        {"loc": [*prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validated_input(declaration: ToolDeclaration, raw: dict[str, Any]) -> dict[str, Any]:
    """The input as the tool's Pydantic input model accepts it (JSON mode), or InvalidInput with its errors."""
    try:
        validated = declaration.input_model.model_validate(raw)
    except ValidationError as exc:
        raise InvalidInput("tool input is invalid", detail_object=safe_errors(exc, ["body", "input"])) from None
    return validated.model_dump(mode="json")


def tool_response(declaration: ToolDeclaration, allowed_tools: frozenset[str] | None) -> dict:
    """Render a declaration with the current platform policy view."""
    return {
        "id": declaration.id,
        "title": declaration.title,
        "description": declaration.description,
        "side_effect": declaration.side_effect,
        "external_idempotent": declaration.external_idempotent,
        "input_schema": declaration.input_json_schema(),
        "allowed": allowed_tools is None or declaration.id in allowed_tools,
    }


@plain_refusals
class ToolService(AccessBound):
    """The platform tool registry: any member reads it (`read` on the tenant); running one directly is the tenant's
    `run` (editors)."""

    def __init__(
        self, run_store: RunStore | None = None, launcher: RunLauncher | None = None, access: Access | None = None
    ) -> None:
        self._access = access  # None: no caller to check
        self.run_store = run_store
        self.launcher = launcher

    def list(self) -> list[dict]:
        """Platform tool declarations and their current allowlist state (tenant-agnostic)."""
        acl.require(self._access, "read", "tenant", None)
        allowed_tools = _allowed_tool_ids()
        return [tool_response(declaration, allowed_tools) for declaration in TOOL_DECLARATIONS.values()]

    def get(self, tool_id: str) -> dict:
        acl.require(self._access, "read", "tenant", None)
        declaration = TOOL_DECLARATIONS.get(tool_id)
        if declaration is None:
            raise NotFound(TOOL_NOT_FOUND)
        return tool_response(declaration, _allowed_tool_ids())

    def start_run(
        self, tenant_id: str, tool_id: str, tool_input: dict[str, Any], run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        """Run one tool call as a durable, tenant-scoped run; the answer is the 202 body.

        NotFound for an unknown tool; InvalidInput for a tool not in EAP_ALLOWED_TOOLS, an input the tool
        rejects or one over 32 KiB; Conflict when `run_id` is already used by a different request. A retry
        with the same `run_id` and input answers again and re-enqueues (DBOS attaches it to the existing
        workflow).

        A failed hand-off is Unavailable. The run this request created (or re-queued) is then marked
        failed with ENQUEUE_FAILED_ERROR, if it is still queued, so it is not left queued with no
        workflow; a retry with the same run_id and input re-queues it and enqueues again. If DBOS did
        accept the enqueue that raised, the workflow runs anyway and its status replaces `failed`."""
        acl.require(self._access, "run", "tenant", None)
        assert self.run_store is not None and self.launcher is not None
        run_store, launcher = self.run_store, self.launcher
        declaration = TOOL_DECLARATIONS.get(tool_id)
        if declaration is None:
            raise NotFound(TOOL_NOT_FOUND)
        allowed_tools = _allowed_tool_ids()
        if allowed_tools is not None and tool_id not in allowed_tools:
            raise InvalidInput(
                f"tool '{tool_id}' is not allowed on this platform (not in the EAP_ALLOWED_TOOLS allowlist)"
            )
        input_json = canonical_json(validated_input(declaration, tool_input))
        if len(input_json.encode("utf-8")) > MAX_TOOL_INPUT_BYTES:
            raise InvalidInput(f"input must be at most {MAX_TOOL_INPUT_BYTES // 1024} KiB as JSON")

        new_id = run_id or uuid.uuid4()
        trace_id, traceparent = current_trace_context()
        try:
            _, created = run_store.create_tool_run(
                tenant_id, tool_id, input_json, new_id, trace_id=trace_id, traceparent=traceparent
            )
        except RunIdConflict:
            raise Conflict(RUN_ID_IN_USE) from None
        run = run_store.get_run(tenant_id, new_id)
        if run is None:  # pragma: no cover - deleted between insert and read
            raise NotFound("Run not found")
        if not created and (run["kind"] != "tool" or run["tool_id"] != tool_id or run["input"] != input_json):
            raise Conflict(RUN_ID_DIFFERENT)
        # This request owns the hand-off of a row it created, or of one an earlier failed hand-off left
        # failed and it re-queues now; it never touches a run some workflow already drives.
        owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, new_id)

        try:
            # Exactly what the stored row says, so a retry starts the recorded run.
            launcher.launch_tool(
                run_id=new_id,
                tenant_id=tenant_id,
                tool_id=run["tool_id"],
                input_json=run["input"],
                traceparent=run.get("traceparent"),
            )
        except Exception as exc:
            # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
            log_failure(
                log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
                run_id=str(new_id), tenant_id=tenant_id,
            )
            if owns_handoff:
                try:
                    run_store.mark_enqueue_failed(tenant_id, new_id)
                except Exception as mark_exc:  # noqa: BLE001 - the refusal below is the answer either way
                    log_failure(
                        log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                        level=logging.ERROR, run_id=str(new_id), tenant_id=tenant_id,
                    )
            raise Unavailable(NOT_STARTED) from None
        run = run_store.get_run(tenant_id, new_id) or run
        run_status, display_status = run["status"], run["display_status"]
        if run["error"] == ENQUEUE_FAILED_ERROR:
            # A concurrent request that owned the hand-off failed and marked the run failed, but this
            # enqueue succeeded: the workflow exists, and its start step clears that error.
            run_status = display_status = "queued"
        log.info("tool run %s queued: tenant=%s tool=%s", new_id, tenant_id, tool_id)
        return {
            "run_id": str(run["id"]),
            "kind": "tool",
            "tool_id": run["tool_id"],
            "status": run_status,
            "display_status": display_status,
            "trace_id": run["trace_id"],
        }
