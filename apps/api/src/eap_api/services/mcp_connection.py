"""Reaching a registered MCP server: the connection test (`tools/list`) and one tool call recorded on the
tool-run ledger.

A test or call is bounded (EAP_MCP_TEST_TIMEOUT_SECONDS); header values are decrypted for the call only and
cleared after it; a failure is reduced to a stable code, never exception text. A managed server is started and
waited for first (eap_api.services.mcp_runtime).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import anyio
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.mcp_servers.connection import call_server_tool, connection_timeout_s, list_server_tools
from eap_api.mcp_servers.models import McpServerModel, McpServerSecretModel
from eap_api.services.mcp_runtime import McpRuntimeService, Target, get_server_or_404, target_of
from eap_api.services.mcp_servers import keyring, secret_context
from eap_run_store import EventLog, RunIdConflict, RunStore
from eap_run_store.definition_errors import DefinitionConflict, DefinitionInvalid, DefinitionNotFound
from eap_schemas import mcp_tool_name, status_for_end
from eap_secrets import DecryptionFailed
from eap_secrets.outbound import OutboundPolicy

log = logging.getLogger("eap_api.mcp_servers")

MCP_TOOL_ID_PREFIX = "mcp:"
TOOL_ERROR_MESSAGE = "The MCP server reported that this tool call failed. Details are withheld."
MAX_TOOL_INPUT_BYTES = 32 * 1024  # the same limit as POST /tools/{id}/runs


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mcp_run_tool_id(server_id: str, tool: str) -> str:
    """The ledger's `tool_id` for a server's tool: `mcp:<server id>/<tool>` (the id, not the name,
    so a rename keeps the history; the built-in tools' ids never start with `mcp:`)."""
    return f"{MCP_TOOL_ID_PREFIX}{server_id}/{tool}"


def input_errors(schema: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool's own input schema applied to `args`, as FastAPI-style loc/msg/type entries that
    never carry the rejected values. A schema that cannot be applied checks nothing (the server
    decides)."""
    from jsonschema import Draft202012Validator

    errors: list[dict[str, Any]] = []
    try:
        validator = Draft202012Validator(schema)
        found = sorted(validator.iter_errors(args), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # noqa: BLE001 - a malformed or unresolvable schema
        return []
    for err in found[:20]:
        loc: list[Any] = ["body", "input", *err.absolute_path]
        if err.validator == "required" and isinstance(err.instance, dict):
            for name in err.validator_value:
                if name not in err.instance:
                    errors.append({"loc": [*loc, name], "msg": "Field required", "type": "missing"})
        elif err.validator == "type":
            errors.append({"loc": loc, "msg": f"Input should be of type {err.validator_value}", "type": "type_error"})
        else:
            errors.append({"loc": loc, "msg": f"Input violates the schema's {err.validator} rule", "type": "schema_error"})
    return errors


def _run_body(run: dict[str, Any], status_value: str | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run["id"]),
        "kind": "tool",
        "tool_id": run["tool_id"],
        "status": status_value or run["status"],
        "display_status": status_value or run["display_status"],
        "trace_id": run["trace_id"],
    }


class McpConnectionService:
    def __init__(
        self, engine: Engine, policy: OutboundPolicy, runtime: McpRuntimeService,
        run_store: RunStore | None = None, event_log: EventLog | None = None,
    ) -> None:
        self._engine = engine
        self._policy = policy
        self._runtime = runtime
        self._run_store = run_store
        self._event_log = event_log

    def _load_for_test(self, tenant_id: str, server_id: uuid.UUID) -> tuple[Target, list[tuple[str, bytes, str]]]:
        with Session(self._engine) as db:
            server = get_server_or_404(db, tenant_id, server_id)
            secrets = db.execute(
                select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
                .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
                       McpServerSecretModel.kind == "header")
            ).all()
            return target_of(db, server), [(name, bytes(ct), key_id) for name, ct, key_id in secrets]

    def _record_result(self, tenant_id: str, server_id: uuid.UUID, outcome: str, code: str | None) -> None:
        with Session(self._engine) as db:
            server = db.scalar(
                select(McpServerModel).where(McpServerModel.tenant_id == tenant_id, McpServerModel.id == str(server_id))
            )
            if server is None:  # deleted while testing
                return
            server.last_checked_at = datetime.now(timezone.utc)
            server.last_status = outcome
            server.last_error_code = code
            db.commit()

    async def test(self, tenant_id: str, server_id: uuid.UUID) -> dict[str, Any]:
        """Connect over Streamable HTTP, initialize and list tools, within a bounded time. Answers
        {status: "ok", tools: [...]} or {status: "error", error_code, tools: []}; the outcome is stored as
        last_status/last_error_code. A managed server whose runtime is stopped is started first and waited for
        (EAP_MCP_TEST_START_TIMEOUT_SECONDS); the answer then carries `runtime: {state (before), started,
        waited_ms, idle_minutes}`; a start still going when the time is up answers `runtime_starting` (not
        stored), a `failed` runtime `runtime_not_running` with `runtime.last_error`."""
        policy = self._policy
        target, secrets = await anyio.to_thread.run_sync(self._load_for_test, tenant_id, server_id)

        headers: dict[str, str] = {}
        code: str | None = None
        if secrets:
            ring = keyring()
            try:
                for name, ciphertext, key_id in secrets:
                    headers[name] = ring.decrypt(ciphertext, key_id, context=secret_context(tenant_id, str(server_id), name))
            except DecryptionFailed as exc:
                code = "secret_unreadable"
                self._runtime.log_failure(log, "mcp server secret could not be decrypted", exc, code=code,
                            tenant_id=tenant_id, server_id=str(server_id))

        url: str | None = None
        info: dict[str, Any] = {}
        if code is None and target.managed and target.runtime_id:
            # A stopped managed server (the idle sweep scaled it down) is started and waited for.
            code = await self._runtime.start_and_wait(tenant_id, str(server_id), info)
            if code is None:
                url, managed_policy = self._runtime.endpoint_policy(target, policy)
                if managed_policy is None:
                    code = "managed_endpoint_invalid"
                else:
                    policy = managed_policy
        elif code is None:
            url, policy, code = await self._runtime.resolve_target(tenant_id, str(server_id), target, policy)
        runtime_info = (
            {"state": info.get("state"), "started": bool(info.get("started")), "waited_ms": info.get("waited_ms", 0),
             "idle_minutes": info.get("idle_minutes"), **({"last_error": info["last_error"]} if "last_error" in info else {})}
            if info else None
        )

        def body(**extra: Any) -> dict[str, Any]:
            return {**extra, **({"runtime": runtime_info} if runtime_info is not None else {})}

        if code is None:
            assert url is not None
            result = await list_server_tools(url, headers, policy, connection_timeout_s())
            headers.clear()
            if result.status == "ok":
                await anyio.to_thread.run_sync(self._record_result, tenant_id, server_id, "ok", None)
                return body(status="ok", tools=result.tools)
            code = result.error_code or "connection_failed"
            assert result.error is not None
            self._runtime.log_failure(log, "mcp server connection test failed", result.error, code=code,
                        tenant_id=tenant_id, server_id=str(server_id))

        if code != "runtime_starting":  # transient: the stored last result stays what the last real test said
            await anyio.to_thread.run_sync(self._record_result, tenant_id, server_id, "error", code)
        return body(status="error", error_code=code, tools=[])


    async def run_tool(
        self, tenant_id: str, server_id: uuid.UUID, tool: str, tool_input: dict[str, Any], run_id: uuid.UUID | None,
        trace_id: str | None, traceparent: str | None,
    ) -> dict[str, Any]:
        """Run one tool of a registered server and record it on the tool-run ledger (`tool_id` is
        `mcp:<server id>/<tool>`). Answers the finished run `{run_id, status, ...}`; a connection or call failure
        is recorded as a failed run, a call that may have reached the tool as needs_attention, and a repeated
        `run_id` returns the recorded run without calling the tool again. Refuses with 404 (no such server or
        tool), 422 (server disabled, input too large or rejected by the tool's schema; nothing recorded), 409
        (`run_id` used by a different request), 503 (header secrets without the platform key)."""
        run_store, event_log = self._run_store, self._event_log
        run_id = run_id or uuid.uuid4()
        sid = str(server_id)
        policy = self._policy
        tool_id = mcp_run_tool_id(sid, tool)

        def load() -> tuple[str, Target, bool, list[tuple[str, bytes, str]]]:
            with Session(self._engine) as db:
                server = get_server_or_404(db, tenant_id, server_id)
                secrets = db.execute(
                    select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
                    .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
                           McpServerSecretModel.kind == "header")
                ).all()
                return server.name, target_of(db, server), server.enabled, [(n, bytes(c), k) for n, c, k in secrets]

        name, target, enabled, secrets = await anyio.to_thread.run_sync(load)
        if not enabled:
            raise DefinitionInvalid("MCP server is disabled")
        input_json = canonical_json(tool_input)
        if len(input_json.encode("utf-8")) > MAX_TOOL_INPUT_BYTES:
            raise DefinitionInvalid(f"input must be at most {MAX_TOOL_INPUT_BYTES // 1024} KiB as JSON")

        existing = await anyio.to_thread.run_sync(run_store.get_run, tenant_id, run_id)
        if existing is not None:
            if existing["kind"] != "tool" or existing["tool_id"] != tool_id or existing["input"] != input_json:
                raise DefinitionConflict("run_id is already used by a different run request")
            return _run_body(existing)  # the recorded run; the tool is never called twice

        headers: dict[str, str] = {}
        code: str | None = None
        if secrets:
            ring = keyring()
            try:
                for hname, ciphertext, key_id in secrets:
                    headers[hname] = ring.decrypt(ciphertext, key_id, context=secret_context(tenant_id, sid, hname))
            except DecryptionFailed as exc:
                code = "secret_unreadable"
                self._runtime.log_failure(log, "mcp server secret could not be decrypted", exc, code=code,
                            tenant_id=tenant_id, server_id=sid)

        timeout = connection_timeout_s()
        schema: dict[str, Any] = {}
        url: str | None = None
        if code is None:
            url, policy, code = await self._runtime.resolve_target(tenant_id, sid, target, policy)
        if code is None:
            assert url is not None
            listed = await list_server_tools(url, headers, policy, timeout)
            if listed.status == "ok":
                found = next((t for t in listed.tools if t["name"] == tool), None)
                if found is None:
                    headers.clear()
                    raise DefinitionNotFound("MCP tool not found")
                schema = found["input_schema"]
                errors = input_errors(schema, tool_input)
                if errors:
                    headers.clear()
                    raise DefinitionInvalid(errors)  # type: ignore[arg-type]  # the body's detail is the list
            else:
                code = listed.error_code or "connection_failed"
                assert listed.error is not None
                self._runtime.log_failure(log, "mcp tool listing failed", listed.error, code=code, tenant_id=tenant_id, server_id=sid)

            # Shielded from the create on: a client that disconnects must not leave a recorded run unfinished.
        with anyio.CancelScope(shield=True):
            try:
                _, created = await anyio.to_thread.run_sync(
                    lambda: run_store.create_tool_run(
                        tenant_id, tool_id, input_json, run_id, trace_id=trace_id, traceparent=traceparent
                    )
                )
            except RunIdConflict:
                headers.clear()
                raise DefinitionConflict("run_id is already in use")
            if not created:  # a concurrent request with the same run_id owns this run
                run = await anyio.to_thread.run_sync(run_store.get_run, tenant_id, run_id)
                headers.clear()
                return _run_body(run or {"id": run_id, "tool_id": tool_id, "status": "running",
                                         "display_status": "running", "trace_id": trace_id})

            finished = False
            end_recorded = False
            called = False
            final_status, output, error, error_code = "failed", None, None, code
            try:
                call_data = {"tool_call_id": "tool_run", "tool_name": mcp_tool_name(name, tool), "tool_id": tool_id}

                def record(key: str, type_: str, data: dict[str, Any]) -> None:
                    event_log.append(tenant_id, run_id, key, type_, data)

                await anyio.to_thread.run_sync(run_store.mark_running, tenant_id, run_id)
                await anyio.to_thread.run_sync(record, "start", "run_started", {"kind": "tool", "tool_id": tool_id, "input": tool_input})

                content: Any = None
                if code is None:
                    await anyio.to_thread.run_sync(record, "tool:tool_run:call", "tool_call", {**call_data, "args": tool_input})
                    called = True
                    try:
                        result = await call_server_tool(url, headers, policy, timeout, tool, tool_input)
                    except Exception as exc:  # the ledger row must never stay `running`
                        # No exception text is stored: it can carry a URL or a header value. The type and
                        # code are logged. The request may have gone out before the raise, so the tool may
                        # have run: the same outcome as a sent call that got no answer.
                        self._runtime.log_failure(log, "mcp tool call raised", exc, code="mcp_call_failed",
                                    tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
                        final_status, error_code = "needs_attention", "side_effect_outcome_unknown"
                        error = "a tool's external side effect may or may not have happened; check the external system"
                        result = None
                    if result is None:
                        pass
                    elif result.status == "ok":
                        content = result.content
                        final_status = "completed"
                        output = canonical_json(content)
                    else:
                        error_code = result.error_code or "call_failed"
                        if result.error is not None:
                            self._runtime.log_failure(log, "mcp tool call failed", result.error, code=error_code,
                                        tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
                        if error_code == "tool_error":
                            error = TOOL_ERROR_MESSAGE
                        elif result.sent:
                            final_status, error_code = "needs_attention", "side_effect_outcome_unknown"
                            error = "a tool's external side effect may or may not have happened; check the external system"
                headers.clear()
                if error is None and final_status == "failed":
                    error = f"MCP tool '{name}/{tool}' is unavailable ({error_code})"

                if final_status == "completed":
                    await anyio.to_thread.run_sync(record, "tool:tool_run:result", "tool_result", {**call_data, "status": "ok", "content": content})
                    await anyio.to_thread.run_sync(record, "end", "run_completed", {"output": output, "result": content})
                    end_recorded = True
                else:
                    if called:
                        await anyio.to_thread.run_sync(record, "tool:tool_run:result", "tool_result",
                                                {**call_data, "status": "error", "error": error, "error_code": error_code})
                    await anyio.to_thread.run_sync(record, "end", "run_failed",
                                            {"status": final_status, "error": error, "error_code": error_code})
                    end_recorded = True
                await anyio.to_thread.run_sync(run_store.set_status, tenant_id, run_id, final_status, output, error)
                finished = True
                run = await anyio.to_thread.run_sync(run_store.get_run, tenant_id, run_id)
                assert run is not None
                return _run_body(run)
            finally:
                headers.clear()
                if not finished:  # a store error must not leave the row `running` (best effort)
                    if end_recorded:  # the end event fixes the status: finish with what it says
                        fb_status, fb_output, fb_error, fb_code = final_status, output, error, error_code
                    elif called:  # the call went out, so the tool may have run
                        fb_status, fb_output, fb_code = "needs_attention", None, "side_effect_outcome_unknown"
                        fb_error = "a tool's external side effect may or may not have happened; check the external system"
                    else:
                        fb_status, fb_output, fb_code = "failed", None, "mcp_call_failed"
                        fb_error = f"MCP tool '{name}/{tool}' is unavailable (mcp_call_failed)"
                    try:
                        # A terminal status needs the run's `end` event: write both in one transaction.
                        written = await anyio.to_thread.run_sync(
                            lambda: run_store.finish_with_end(
                                tenant_id, run_id, fb_status, fb_output, fb_error,
                                end_data={"error_code": fb_code},
                            )
                        )
                        if not written:
                            # An end event that committed but raised to us (connection dropped after the
                            # commit) fixes the status, and finish_with_end refused ours: finish with ITS.
                            end = await anyio.to_thread.run_sync(event_log.get_event, tenant_id, run_id, "end")
                            if end is None:
                                log.warning("mcp tool run fallback did not write the status: run_id=%s", run_id)
                            else:
                                data = end.data or {}
                                out = data.get("output") if isinstance(data.get("output"), str) else None
                                err = data.get("error") if isinstance(data.get("error"), str) else None
                                await anyio.to_thread.run_sync(
                                    lambda: run_store.finish_with_end(
                                        tenant_id, run_id, status_for_end(end.type, data), out, err,
                                    )
                                )
                    except Exception as exc:  # noqa: BLE001
                        self._runtime.log_failure(log, "mcp tool run could not be marked failed", exc, code="mcp_call_failed",
                                    tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
