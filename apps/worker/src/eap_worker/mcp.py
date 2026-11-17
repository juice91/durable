"""MCP tools in agent runs (milestone 3, task 3.5).

A spec's `mcp_tools` ({server, tool} pairs naming the tenant's registered MCP servers) are added to
the run's DynamicToolset by `mcp_toolset_for_run`. DBOSDurability re-evaluates that factory inside
each `...get_tools` and `...call_tool` step; building the toolset does no I/O. Server rows are read
and header secrets decrypted only (a) in discovery and (b) inside a call whose side-effect key was
just reserved. A replayed step never needs the database or the platform key: a recorded get_tools
step is not re-executed, and a re-executed call step finds its key reserved or completed first.

- Discovery (`get_tools` step): connect to each referenced server over Streamable HTTP with
  FastMCP's client on the outbound policy's guarded HTTP client (URL re-checked, host re-resolved
  and re-checked at connect), list its tools and expose ONLY the listed ones, named
  `mcp_tool_name(server, tool)`. The recorded tool definitions are what replays and the call step use.
- Call (`call_tool` step): `run_side_effect(key=f"{run_id}:{tool_call_id}", external_idempotent=False)`
  around one `tools/call`. The crash point `after_tool_call` sits between the server's answer and
  `side_effects.complete`. A recovered step finds the key `reserved` and raises
  SideEffectOutcomeUnknown before loading anything, so the run ends `needs_attention` (even if the
  server was deleted or the key rotated meanwhile) and the server is not called again. A failure
  after the request may have been sent (timeout, dropped connection) is also an unknown outcome; a
  failure to connect at all is McpToolUnavailable (the run fails, nothing was sent).
- Retries only before a call: the model's arguments are checked against the tool's JSON schema
  (args validator) before call_tool runs, with at most MAX_ARG_RETRIES retry prompts. call_tool
  itself never raises ModelRetry/ValidationError, so no retry (a new tool call id, hence a new key
  and a second external call) can follow an attempted call. A server-reported tool error (isError)
  or a JSON-RPC rejection (parse error, invalid request, method not found, invalid params) is
  stored without its body and returned to the model as a final `ToolFailed` with a fixed message
  (TOOL_ERROR_MESSAGE).
- A server that is gone or disabled, unreadable secrets, a refused URL or a tool the server no
  longer has fail the run with McpToolUnavailable (stable code, no exception text).

Secrets: header values live only in McpServerConn.headers (excluded from repr) and the MCP transport.
Every decrypted value and its obvious encodings (`secret_variants`) are replaced by REDACTED in tool
results, tool descriptions and schemas before they are stored or shown to the model. Failures are
logged with `log_failure` (code and type only) and raised without their cause chain.

Threat model of response redaction. It protects against ACCIDENTAL echoes of a tenant's header
secrets by an MCP server: the plain value and credential part, common encodings (URL, base64, hex,
JSON-escaped, any letter case), base64/hex runs that decode to text containing it, contiguous and
simple split forms (across strings joined in document, values-only and keys-only order, and whole
strings that are pieces of it) and the truncation edge. It cannot stop a deliberately malicious
server, which already holds the secret, from returning it in an arbitrary reversible transform
(reversed, ROT13, one character per key among decoys, ...); that is out of scope for redaction. A
malicious server is contained instead by: tenant-scoped registration (it only ever receives that
tenant's own secrets), the outbound URL policy (checked again at connect, DNS-pinned, no redirects),
response size caps and identity-only encoding, no retries after a call, server error bodies never
stored or shown, and secrets never written to logs, spans, events or stored errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR
from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema, to_jsonable_python
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from eap_mcp_runtime import McpRuntime, RuntimeHandle, RuntimeSpec
from eap_observability import log_failure, redact as redact_text
from eap_runtime import SideEffectOutcomeUnknown, run_side_effect
from eap_schemas import McpToolRef, mcp_tool_name
from eap_secrets import SecretsError, load_keyring, mcp_env_context, mcp_header_context
from eap_secrets.redaction import (  # shared with the API's connection test
    REDACTED as REDACTED,  # re-exported; worker tests import it from here
    leaves_with_kind as _leaves_with_kind,  # re-exported; same leaf order the mask uses
    mask_split_secrets as _mask_split_secrets,
    redact,
    secret_variants,
)
from eap_secrets.outbound import OutboundPolicy
from eap_secrets.outbound_errors import (  # shared with the API's connection test
    OutboundGuardError,
    RedirectRefused,
    failure_code as shared_failure_code,
    refuse_redirect,
    walk_chain as _chain,
)
from eap_run_store.secrets import SecretStore
from eap_worker.crash import crash_point
from eap_worker.secret_refs import SecretRefNotFound, resolve_string

log = logging.getLogger("eap_worker.mcp")

TIMEOUT_ENV = "EAP_MCP_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_S = 60.0
MAX_RESULT_CHARS = 100_000  # stored in side_effects and the DBOS checkpoint; keep well under 2 MB
_ARGS_VALIDATOR = SchemaValidator(core_schema.dict_schema(core_schema.str_schema(), core_schema.any_schema()))


class McpToolUnavailable(Exception):
    """A spec's MCP tool cannot be used. Carries only names from the spec and a stable code."""

    def __init__(self, server: str, tool: str, code: str) -> None:
        self.server = server[:100]
        self.tool = tool[:128]
        self.code = code
        # args are the constructor arguments, so pickle (DBOS records a failed step's exception and
        # unpickles it on replay) rebuilds exactly this exception.
        super().__init__(self.server, self.tool, self.code)

    def __str__(self) -> str:
        return f"MCP tool {self.server}/{self.tool} is unavailable: {self.code}"


def timeout_s() -> float:
    """EAP_MCP_TIMEOUT_SECONDS (connect + one operation), clamped to [1, 600]; 60 by default."""
    try:
        value = float(os.environ.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_TIMEOUT_S
    return min(max(value, 1.0), 600.0)


@dataclass(frozen=True)
class McpServerConn:
    """A registered server ready to connect to: its URL and decrypted headers (never in repr).

    A managed server (`runtime_id` set) has the in-cluster endpoint its runtime adapter reports as
    `url`; the outbound policy then allows exactly that endpoint, by identity."""

    name: str
    url: str
    headers: dict[str, str] = field(repr=False)
    runtime_id: str | None = None
    server_id: str = ""


START_TIMEOUT_ENV = "EAP_MCP_START_TIMEOUT_SECONDS"
START_POLL_ENV = "EAP_MCP_START_POLL_SECONDS"
DEFAULT_START_TIMEOUT_S = 120.0
DEFAULT_START_POLL_S = 1.0
_STARTABLE = ("pending", "stopped", "idle", "failed")

_mcp_runtime: McpRuntime | None = None


def set_mcp_runtime(runtime: McpRuntime | None) -> None:
    """Replace the process's runtime adapter (tests); None restores the lazily built operator adapter."""
    global _mcp_runtime
    _mcp_runtime = runtime


def get_mcp_runtime() -> McpRuntime:
    """The managed-server runtime adapter, built on first use (a worker without managed servers never
    needs a cluster connection)."""
    global _mcp_runtime
    if _mcp_runtime is None:
        from eap_mcp_runtime import OperatorRuntime

        _mcp_runtime = OperatorRuntime()
    return _mcp_runtime


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        value = default
    if not math.isfinite(value):
        value = default
    return min(max(value, low), high)


def start_timeout_s() -> float:
    """EAP_MCP_START_TIMEOUT_SECONDS: how long a call waits for a managed server to be running; 120 by default."""
    return _env_float(START_TIMEOUT_ENV, DEFAULT_START_TIMEOUT_S, 1.0, 900.0)


def start_poll_s() -> float:
    return _env_float(START_POLL_ENV, DEFAULT_START_POLL_S, 0.01, 10.0)


_SERVERS = text(
    "SELECT id, name, url, enabled, runtime FROM mcp_servers WHERE tenant_id = :t AND name IN :names"
).bindparams(bindparam("names", expanding=True))
_SECRETS = text(
    "SELECT server_id, header_name, ciphertext, key_id FROM mcp_server_secrets "
    "WHERE tenant_id = :t AND kind = 'header' AND server_id IN :ids"
).bindparams(bindparam("ids", expanding=True))
_RUNTIMES = text(
    "SELECT id, server_id, namespace, cr_name FROM mcp_runtimes WHERE tenant_id = :t AND server_id IN :ids"
).bindparams(bindparam("ids", expanding=True))


def load_servers(
    engine: Engine, tenant_id: str, refs: Sequence[McpToolRef], runtime: McpRuntime | None = None
) -> dict[str, McpServerConn]:
    """The tenant's servers named by `refs`, with header secrets decrypted (blocking; run in a thread).

    A managed server's endpoint comes from its `mcp_runtimes` row (same tenant) through
    `McpRuntime.endpoint`; nothing is started here (see `ensure_managed_running`).

    Raises McpToolUnavailable (for the first tool of the server) when a server is not registered in
    this tenant (`server_not_found`), is disabled (`server_disabled`), is managed without a runtime
    row of this tenant (`runtime_missing`) or its secrets cannot be decrypted (`secret_unreadable`,
    also when EAP_SECRETS_KEY is unset or wrong)."""
    first_tool = {}
    for ref in refs:
        first_tool.setdefault(ref.server, ref.tool)
    with engine.connect() as conn:
        rows = {r.name: r for r in conn.execute(_SERVERS, {"t": tenant_id, "names": list(first_tool)})}
        for name, tool in first_tool.items():
            if name not in rows:
                raise McpToolUnavailable(name, tool, "server_not_found")
            if not rows[name].enabled:
                raise McpToolUnavailable(name, tool, "server_disabled")
        secrets = conn.execute(_SECRETS, {"t": tenant_id, "ids": [r.id for r in rows.values()]}).all()
        managed_ids = [r.id for r in rows.values() if r.runtime == "managed"]
        runtimes = (
            {r.server_id: r for r in conn.execute(_RUNTIMES, {"t": tenant_id, "ids": managed_ids})} if managed_ids else {}
        )
    urls: dict[str, tuple[str, str | None]] = {}
    for r in rows.values():
        if r.runtime != "managed":
            if not r.url:
                raise McpToolUnavailable(r.name, first_tool[r.name], "server_url_missing")
            urls[r.id] = (r.url, None)
            continue
        if r.id not in runtimes:
            raise McpToolUnavailable(r.name, first_tool[r.name], "runtime_missing")
        row = runtimes[r.id]
        endpoint = (runtime or get_mcp_runtime()).endpoint(RuntimeHandle(row.namespace, row.cr_name))
        urls[r.id] = (endpoint, str(row.id))
    headers: dict[str, dict[str, str]] = {r.id: {} for r in rows.values()}
    if secrets:
        by_id = {r.id: r.name for r in rows.values()}
        name = by_id[secrets[0].server_id]
        try:
            ring = load_keyring()
            store = SecretStore(engine, ring)
            for server_id, header, ciphertext, key_id in secrets:
                name = by_id[server_id]
                context = mcp_header_context(tenant_id, server_id, header)
                value = ring.decrypt(bytes(ciphertext), key_id, context=context)
                # A `secret:<name>` value is the tenant's stored secret (eap_worker.secret_refs); the
                # resolved value then feeds the same header redaction as any other.
                headers[server_id][header] = resolve_string(value, engine, tenant_id, store)
        except SecretRefNotFound as exc:
            log.warning("mcp server header secret reference not found tenant=%s server=%s ref=%s",
                        tenant_id, name, exc.ref)
            raise McpToolUnavailable(name, first_tool[name], "secret_not_found") from None
        except SecretsError as exc:
            log_failure(log, "mcp server secret could not be decrypted", exc, code="secret_unreadable",
                        tenant_id=tenant_id, server=name)
            raise McpToolUnavailable(name, first_tool[name], "secret_unreadable") from None
    return {r.name: McpServerConn(r.name, urls[r.id][0], headers[r.id], urls[r.id][1], r.id) for r in rows.values()}


# --------------------------------------------------------------------------- start on demand

_RUNTIME_ROW = text(
    "SELECT id, state, image, package, namespace, cr_name FROM mcp_runtimes WHERE id = :r AND tenant_id = :t AND server_id = :s"
)
_CLAIM_START = text(
    "UPDATE mcp_runtimes SET state = 'starting', last_error = NULL, last_used_at = :now, updated_at = :now "
    "WHERE id = :r AND tenant_id = :t AND state IN ('pending', 'stopped', 'idle', 'failed') RETURNING id"
)
_SET_STATE = text(
    "UPDATE mcp_runtimes SET state = :state, last_error = :err, updated_at = :now "
    "WHERE id = :r AND tenant_id = :t AND state = :was"
)
_SET_RUNNING = text(
    "UPDATE mcp_runtimes SET state = 'running', endpoint = :ep, last_error = NULL, last_used_at = :now, updated_at = :now "
    "WHERE id = :r AND tenant_id = :t AND state = 'starting'"
)
_TOUCH = text(
    "UPDATE mcp_runtimes SET last_used_at = :now "
    "WHERE id = :r AND tenant_id = :t AND state IN ('running', 'idle') RETURNING id"
)
_ENV_SECRETS = text(
    "SELECT header_name, ciphertext, key_id FROM mcp_server_secrets WHERE tenant_id = :t AND server_id = :s AND kind = 'env'"
)
_SERVER_NAME = text("SELECT name FROM mcp_servers WHERE id = :s AND tenant_id = :t")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _exec(engine: Engine, stmt: Any, **params: Any) -> int:
    with engine.begin() as conn:
        return conn.execute(stmt, params).rowcount


def _build_spec(engine: Engine, tenant_id: str, conn: McpServerConn, row: Any) -> RuntimeSpec:
    """The RuntimeSpec for starting this runtime: image from its row, env from the package document
    (`args`, `env`, `transport`, `port`), secret env values decrypted here and only here."""
    package = row.package if isinstance(row.package, dict) else json.loads(row.package or "{}")
    with engine.connect() as db:
        name = db.execute(_SERVER_NAME, {"s": conn.server_id, "t": tenant_id}).scalar_one()
        sealed = db.execute(_ENV_SECRETS, {"s": conn.server_id, "t": tenant_id}).all()
    ring = load_keyring()
    store = SecretStore(engine, ring)
    secret_refs = {
        var: resolve_string(
            ring.decrypt(bytes(ciphertext), key_id, context=mcp_env_context(tenant_id, conn.server_id, var)),
            engine, tenant_id, store,
        )
        for var, ciphertext, key_id in sealed
    }
    return RuntimeSpec(
        tenant_id=tenant_id,
        server_id=conn.server_id,
        name=name,
        image=row.image,
        args=tuple(package.get("args") or ()),
        env=dict(package.get("env") or {}),
        secret_refs=secret_refs,
        transport=package.get("transport") or "stdio",
        port=package.get("port"),
    )


async def ensure_managed_running(
    engine: Engine,
    tenant_id: str,
    conn: McpServerConn,
    tool: str,
    runtime: McpRuntime | None = None,
) -> None:
    """Before connecting to a managed server: start it when it is stopped, idle, pending or failed, and
    wait (up to EAP_MCP_START_TIMEOUT_SECONDS, polling status) while it is `starting`, whoever started it.
    Records last_used_at, which the idle sweep reads. External servers return at once.

    One caller wins the `starting` claim (a conditional UPDATE) and calls `McpRuntime.start`; the others
    poll. Raises McpToolUnavailable with `runtime_start_failed`, `runtime_failed`, `runtime_start_timeout`,
    `secret_unreadable` or `runtime_missing`; never with exception text. A timed-out row stays `starting`
    (the sweep fails it after ten minutes)."""
    if conn.runtime_id is None:
        return
    rt = runtime or get_mcp_runtime()
    rid, server = conn.runtime_id, conn.name

    def read() -> Any:
        with engine.connect() as db:
            return db.execute(_RUNTIME_ROW, {"r": rid, "t": tenant_id, "s": conn.server_id}).first()

    loop = asyncio.get_running_loop()
    deadline = loop.time() + start_timeout_s()
    poll = start_poll_s()

    def remaining() -> float:
        return max(deadline - loop.time(), 0.001)

    first = True
    while True:
        if not first and loop.time() >= deadline:
            raise McpToolUnavailable(server, tool, "runtime_start_timeout")
        first = False
        row = await asyncio.to_thread(read)
        if row is None:
            raise McpToolUnavailable(server, tool, "runtime_missing")
        handle = RuntimeHandle(row.namespace, row.cr_name)
        state = row.state
        if state in _STARTABLE:
            claimed = await asyncio.to_thread(_exec_returning, engine, _CLAIM_START, now=_now(), r=rid, t=tenant_id)
            if not claimed:
                continue  # somebody else moved it; look again
            try:
                spec = await asyncio.to_thread(_build_spec, engine, tenant_id, conn, row)
                await asyncio.wait_for(rt.start(spec), remaining())
            except SecretRefNotFound as exc:
                await asyncio.to_thread(_fail, engine, tenant_id, rid, "secret_not_found")
                log.warning("mcp runtime env secret reference not found tenant=%s server=%s ref=%s",
                            tenant_id, server, exc.ref)
                raise McpToolUnavailable(server, tool, "secret_not_found") from None
            except SecretsError as exc:
                await asyncio.to_thread(_fail, engine, tenant_id, rid, "secret_unreadable")
                log_failure(log, "mcp runtime env secret could not be decrypted", exc, code="secret_unreadable",
                            tenant_id=tenant_id, server=server)
                raise McpToolUnavailable(server, tool, "secret_unreadable") from None
            except Exception as exc:  # noqa: BLE001 - reduced to a stable code
                await asyncio.to_thread(_fail, engine, tenant_id, rid, f"start_failed: {type(exc).__name__}")
                log_failure(log, "mcp runtime start failed", exc, code="runtime_start_failed", tenant_id=tenant_id,
                            server=server)
                raise McpToolUnavailable(server, tool, "runtime_start_failed") from None
            state = "starting"
        if state == "stopping":  # the idle sweep is scaling it down; wait for `stopped`, then start it
            await asyncio.sleep(min(poll, remaining()))
            continue
        if state == "starting":
            try:
                status = await asyncio.wait_for(rt.status(handle), remaining())
            except Exception as exc:  # noqa: BLE001 - a status hiccup is retried until the deadline
                log_failure(log, "mcp runtime status failed", exc, code="runtime_status_failed",
                            level=logging.INFO, tenant_id=tenant_id, server=server)
                status = None
            if status is not None and status.state == "running":
                await asyncio.to_thread(_exec, engine, _SET_RUNNING, ep=status.endpoint or None, now=_now(), r=rid, t=tenant_id)
                continue
            if status is not None and status.state == "failed":
                await asyncio.to_thread(_fail, engine, tenant_id, rid, "runtime_failed", was="starting")
                raise McpToolUnavailable(server, tool, "runtime_failed")
            if loop.time() >= deadline:
                raise McpToolUnavailable(server, tool, "runtime_start_timeout")
            await asyncio.sleep(min(poll, remaining()))
            current = await asyncio.to_thread(read)  # the starter may have failed the row meanwhile
            if current is not None and current.state == "failed":
                raise McpToolUnavailable(server, tool, "runtime_failed")
            continue
        touched = await asyncio.to_thread(_exec_returning, engine, _TOUCH, now=_now(), r=rid, t=tenant_id)
        if touched:
            return
        # the row left running/idle (the sweep claimed it): start/wait again


def _exec_returning(engine: Engine, stmt: Any, **params: Any) -> bool:
    with engine.begin() as conn:
        return conn.execute(stmt, params).first() is not None


def _fail(engine: Engine, tenant_id: str, runtime_id: str, error: str, was: str = "starting") -> None:
    _exec(engine, _SET_STATE, state="failed", err=error, now=_now(), r=runtime_id, t=tenant_id, was=was)


def failure_code(exc: BaseException, http_status: int | None = None) -> str:
    """A stable code for a failed connection or listing (never exception text): the classifier the
    API's connection test uses too (eap_secrets.outbound_errors), so both report the same code.

    `http_status` is the first non-2xx status the server answered with. The MCP SDK reports those as
    a generic MCPError, so without it a 401 and a JSON-RPC failure would both be protocol_error."""
    return shared_failure_code(exc, http_status)


def _unknown_code(exc: BaseException) -> str:
    """The log code for a failure after the request may have been sent (all: unknown outcome)."""
    for e in _chain(exc):
        if isinstance(e, OutboundGuardError):
            return e.code
        if type(e).__name__ == "InputRequiredRoundsExceededError":
            return "input_required"
    return "call_outcome_unknown"


def _definite_rejection(exc: BaseException) -> bool:
    """True when the server answered the call with a JSON-RPC rejection (nothing was executed)."""
    return any(isinstance(e, MCPError) and e.code in _REJECTION_CODES for e in _chain(exc))


def _has_non_local_ref(schema: Any) -> bool:
    """Whether any $ref / $dynamicRef in `schema` points outside the schema document."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in ("$ref", "$dynamicRef") and not (isinstance(value, str) and value.startswith("#")):
                return True
            if _has_non_local_ref(value):
                return True
    elif isinstance(schema, list):
        return any(_has_non_local_ref(v) for v in schema)
    return False


def _describe(error: Any) -> str:
    """One validation error as `argument '<path>' is invalid: <keyword>`. A missing required
    argument is named (its name is in the schema the model is shown); no other schema value is."""
    path = "/".join(map(str, error.path))[:100] or "(arguments)"
    if error.validator == "required" and isinstance(error.instance, dict):
        missing = [str(n)[:64] for n in error.validator_value or () if n not in error.instance][:5]
        return f"argument '{path}' is invalid: required (missing: {', '.join(missing)})"
    return f"argument '{path}' is invalid: {str(error.validator)[:40]}"


def _schema_check(schema: dict[str, Any]) -> Callable[..., None]:
    """An args_validator_func: the model's arguments against the tool's JSON schema, as a bounded
    ModelRetry (a retry prompt) before anything is sent.

    The schema is the server's: it is used only with an empty `referencing.Registry` (nothing is
    ever retrieved: jsonschema would otherwise GET a remote $ref with `requests`, outside the
    outbound policy), and a schema with any non-local reference, an invalid schema, or one that
    fails while validating checks nothing. Retry text names only the argument path and the failing
    JSON-schema keyword, never jsonschema's message (it quotes schema values), and is redacted."""
    import jsonschema
    from referencing import Registry

    validator: Any = None
    if not _has_non_local_ref(schema):
        try:
            cls = jsonschema.validators.validator_for(schema)
            cls.check_schema(schema)
            validator = cls(schema, registry=Registry())
        except Exception:  # noqa: BLE001 - the server's schema is not ours to enforce if it is invalid
            validator = None

    def check(ctx: RunContext[Any], **args: Any) -> None:
        if validator is None:
            return
        try:
            errors = sorted(validator.iter_errors(args), key=lambda e: [str(p) for p in e.path])
        except Exception:  # noqa: BLE001 - e.g. an unresolvable reference: treat as an invalid schema
            return
        if errors:
            raise ModelRetry(redact_text("Invalid arguments for this tool: " + "; ".join(map(_describe, errors[:5]))))

    return check


MAX_RESPONSE_BYTES = 1024 * 1024  # per HTTP response body, JSON or SSE (the SDK's SSE event cap)


class ResponseTooLarge(OutboundGuardError):
    """An MCP server's HTTP response body passed MAX_RESPONSE_BYTES; reading stopped there."""

    code = "response_too_large"


class _CappedStream(httpx2.AsyncByteStream):
    """A response body that raises ResponseTooLarge once more than `cap` bytes arrived, so neither
    `aread()` (application/json) nor the SSE reader can buffer an unbounded body."""

    def __init__(self, inner: Any, cap: int) -> None:
        self._inner = inner
        self._cap = cap

    async def __aiter__(self) -> Any:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._cap:
                raise ResponseTooLarge()
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class UnsupportedEncoding(OutboundGuardError):
    """An MCP server answered with a Content-Encoding other than identity although the client asked
    for identity only. Its body is never read (a compressed body could expand without bound)."""

    code = "unsupported_encoding"


async def _cap_response(response: httpx2.Response) -> None:
    """Response hook, before any body byte is read: refuse an encoded body, cap the rest.

    The client sends `Accept-Encoding: identity`, so raw bytes ARE decoded bytes and the cap bounds
    exactly what aread() / the SSE reader / JSON parsing see. A server that compresses anyway is
    refused here, so httpx2 never decompresses anything."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        raise UnsupportedEncoding()
    response.stream = _CappedStream(response.stream, MAX_RESPONSE_BYTES)


def _http_client_factory(
    policy: OutboundPolicy, statuses: list[int] | None = None
) -> Callable[..., httpx2.AsyncClient]:
    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        client = policy.httpx_client_factory(**kwargs)
        client.headers["Accept-Encoding"] = "identity"
        hooks = client.event_hooks.setdefault("response", [])
        if statuses is not None:

            async def record_status(response: httpx2.Response) -> None:
                # The first non-2xx answer. The SDK then raises a generic error; failure_code needs
                # the status to agree with the API's connection test (401 -> unauthorized, not
                # protocol_error).
                if response.status_code >= 300 and not statuses:
                    statuses.append(response.status_code)

            hooks.append(record_status)
        hooks.append(refuse_redirect)
        hooks.append(_cap_response)
        return client

    return factory


def _client(
    conn: McpServerConn, policy: OutboundPolicy, timeout: float, statuses: list[int] | None = None
) -> Any:
    """A FastMCP client over Streamable HTTP on the policy's guarded HTTP client (not yet connected).

    Response bodies are capped (MAX_RESPONSE_BYTES). `input_required_max_rounds=0`: an
    InputRequiredResult is never answered by re-sending tools/call (each re-send would execute the
    tool again inside one reserved side effect); it raises instead, an unknown outcome.
    `statuses`, when given, receives the first non-2xx HTTP status the server answers with."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    if conn.runtime_id is not None:
        # A managed pod: exactly its own endpoint, by identity. The dev allowlist never applies.
        policy = policy.for_managed_runtime(conn.runtime_id, conn.url)
    policy.check_url(conn.url)  # raises OutboundBlocked; the transport re-checks on every request
    transport = StreamableHttpTransport(
        conn.url, headers=dict(conn.headers), httpx_client_factory=_http_client_factory(policy, statuses)
    )
    return Client(transport, timeout=timeout, init_timeout=timeout, input_required_max_rounds=0)


@contextlib.asynccontextmanager
async def _session(
    conn: McpServerConn, policy: OutboundPolicy, timeout: float, statuses: list[int] | None = None
) -> AsyncIterator[Any]:
    """A connected client with FastMCP's own spans suppressed: they would carry a tool's error text
    (a server-controlled body) in the span status. Trace context still propagates."""
    from fastmcp.telemetry import suppress_fastmcp_telemetry

    with suppress_fastmcp_telemetry():
        async with _client(conn, policy, timeout, statuses) as client:
            yield client


TOOL_ERROR_MESSAGE = (
    "The MCP server reported that this tool call failed. Details are withheld. "
    "The call has finished; do not repeat it."
)
MAX_ARG_RETRIES = 2  # invalid-argument retries per tool; they happen before any call is attempted
# JSON-RPC errors that mean the server rejected the request without executing anything. Timeouts,
# closed connections and internal errors (-32603, possibly after a partial effect) stay unknown.
_REJECTION_CODES = frozenset({PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS})
def _cut_redacted(text: str, limit: int, variants: Sequence[str]) -> str:
    """`text` (longer than `limit`) cut to at most `limit` characters and redacted, with no part of a
    secret left at the cut. Bounded work: only `limit + W` characters are ever redacted.

    W is the longest scrubbed form + 4 (the hex form, 2x the secret, bounds a secret's base64/hex
    encoding inside a longer run too). The first `limit + W` characters are redacted, so every secret
    wholly inside them is gone; the only secret that can survive is one straddling that window's end,
    and its visible part lies within the last W characters, which are dropped before the final cut."""
    width = max((len(v) for v in variants), default=0) + 4 if variants else 0
    window = text[: limit + width]
    red = redact(window, variants)
    if len(text) > len(window) and width:
        red = red[: max(0, len(red) - width)]
    return red[:limit]


def _result_json(result: Any, variants: Sequence[str]) -> dict[str, Any]:
    """A CallToolResult as JSON for side_effects: {"is_error", "content"}, secrets scrubbed, bounded.

    An error result keeps no body at all (content None): its text is the server's and may quote
    anything, so neither the store nor the model sees it. Otherwise structured content wins; text
    blocks become strings (one block: a bare string), other blocks (images, ...) only their type.

    A secret split across strings (text blocks, structured fields, list items, a key and its value)
    is invisible string by string, so each part of it is masked where it lies (`_mask_split_secrets`);
    the structure is kept. An oversized result is cut BEFORE redaction (bounded work; see `_cut_redacted`:
    nothing that could be part of a secret straddling the cut survives) and stored as text."""
    if result.is_error:
        return {"is_error": True, "content": None}
    if result.structured_content is not None:
        content: Any = to_jsonable_python(result.structured_content, fallback=str)
    else:
        parts: list[Any] = [
            b.text if getattr(b, "type", None) == "text" else {"type": str(getattr(b, "type", "unknown")), "omitted": True}
            for b in result.content
        ]
        content = parts[0] if len(parts) == 1 and isinstance(parts[0], str) else parts
    content = _mask_split_secrets(content, variants)  # linear in the (capped) body
    encoded = json.dumps(content, default=str)
    if len(encoded) > MAX_RESULT_CHARS:
        text = _cut_redacted(encoded, MAX_RESULT_CHARS, variants)
        return {"is_error": False, "content": {"truncated": True, "text": text}}
    return {"is_error": False, "content": redact(content, variants)}


Loader = Callable[[Sequence[McpToolRef]], dict[str, McpServerConn]]
Ensure = Callable[[str, McpServerConn, str], Awaitable[None]]  # (tenant_id, conn, tool): start a managed server


class McpToolset(AbstractToolset[Any]):
    """Exactly the spec's MCP tools. Building it does no I/O: server rows and secrets are read by
    `load` only inside discovery and inside a call that has just reserved its side-effect key, so a
    replayed step (recorded get_tools, or a call whose key is already reserved/completed) needs
    neither the database nor the platform key. One short MCP session per listing or call."""

    def __init__(
        self, refs: Sequence[McpToolRef], load: Loader, timeout: float, ensure: Ensure | None = None
    ) -> None:
        self._ensure = ensure
        self.refs = {mcp_tool_name(r.server, r.tool): r for r in refs}
        self._load = load
        self._timeout = timeout
        self._tools: dict[str, ToolsetTool[Any]] | None = None

    @property
    def id(self) -> str | None:
        return None  # always inside the run's DynamicToolset, which carries the durable id

    def __repr__(self) -> str:
        return f"McpToolset(tools={sorted(self.refs)!r})"

    def _tool(self, tool_def: ToolDefinition) -> ToolsetTool[Any]:
        # Retries only for invalid arguments, which are checked before call_tool runs (nothing sent).
        # call_tool never raises ModelRetry/ValidationError, so no retry follows an attempted call.
        return ToolsetTool(
            toolset=self,
            tool_def=tool_def,
            max_retries=MAX_ARG_RETRIES,
            args_validator=_ARGS_VALIDATOR,
            args_validator_func=_schema_check(tool_def.parameters_json_schema),
        )

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        if self._tools is None:
            self._tools = await self._discover(ctx)
        return self._tools

    async def _discover(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        servers = await asyncio.to_thread(self._load, list(self.refs.values()))
        policy = OutboundPolicy.from_env()
        listed: dict[str, dict[str, Any]] = {}
        for server, conn in servers.items():
            first = next(r.tool for r in self.refs.values() if r.server == server)
            statuses: list[int] = []
            if self._ensure is not None:  # start a stopped managed server, wait for a starting one
                await self._ensure(ctx.deps.tenant_id, conn, first)
            try:
                with anyio.fail_after(self._timeout):
                    async with _session(conn, policy, self._timeout, statuses) as client:
                        listed[server] = {t.name: t for t in await client.list_tools()}
            except Exception as exc:  # noqa: BLE001 - reduced to a stable code
                code = failure_code(exc, statuses[0] if statuses else None)
                log_failure(log, "mcp tool discovery failed", exc, code=code,
                            run_id=ctx.deps.run_id, tenant_id=ctx.deps.tenant_id, server=server)
                raise McpToolUnavailable(server, first, code) from None
        tools: dict[str, ToolsetTool[Any]] = {}
        for name, ref in self.refs.items():
            found = listed[ref.server].get(ref.tool)
            if found is None:
                raise McpToolUnavailable(ref.server, ref.tool, "tool_not_found")
            variants = secret_variants(servers[ref.server].headers.values())
            schema = found.input_schema if isinstance(found.input_schema, dict) else {}
            tools[name] = self._tool(ToolDefinition(
                name=name,
                description=redact(found.description, variants),
                parameters_json_schema=redact(schema, variants) or {"type": "object", "properties": {}},
                metadata={"mcp_server": ref.server, "mcp_tool": ref.tool},
            ))
        return tools

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        """Rebuild from the definition the discovery step recorded: no listing, no I/O."""
        if tool_def.name not in self.refs:
            raise KeyError(tool_def.name)
        return self._tool(tool_def)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        ref = self.refs[name]
        if ctx.tool_call_id is None:
            raise ValueError("tool_call_id is required for an MCP tool call")
        key = f"{ctx.deps.run_id}:{ctx.tool_call_id}"
        loop = asyncio.get_running_loop()

        def call() -> dict[str, Any]:
            # Runs only for a key run_side_effect has just reserved (never for one found reserved or
            # completed), so the server row and secrets are read only when a call will really be
            # made. It runs in a thread (the store is synchronous) and drives the MCP call on the loop.
            conn = self._load([ref])[ref.server]
            result = asyncio.run_coroutine_threadsafe(self._invoke(conn, ref, tool_args, key, ctx), loop).result()
            crash_point("after_tool_call")  # the server answered; side_effects.complete has not run
            return result

        try:
            stored, _performed = await asyncio.to_thread(
                run_side_effect, ctx.deps.side_effects, key, call, external_idempotent=False
            )
        except (ModelRetry, ValidationError):  # never a retry once a call may have been attempted
            raise SideEffectOutcomeUnknown(key) from None
        if stored.get("is_error"):
            # A final result, not a retry: the model sees a fixed message, never the server's text.
            raise ToolFailed(TOOL_ERROR_MESSAGE)
        return stored.get("content")

    async def _invoke(
        self, conn: McpServerConn, ref: McpToolRef, args: dict[str, Any], key: str, ctx: RunContext[Any]
    ) -> dict[str, Any]:
        """One tools/call. Before the request can have been sent, a failure is McpToolUnavailable
        (nothing happened, the run fails); after, it is SideEffectOutcomeUnknown (needs_attention)."""
        sent = False
        result: Any = None
        stored: dict[str, Any] | None = None
        statuses: list[int] = []
        if self._ensure is not None:  # nothing is sent yet: a failure here is McpToolUnavailable
            await self._ensure(ctx.deps.tenant_id, conn, ref.tool)
        variants = secret_variants(conn.headers.values())
        try:
            with anyio.fail_after(self._timeout):
                async with _session(conn, OutboundPolicy.from_env(), self._timeout, statuses) as client:
                    sent = True
                    result = await client.call_tool_mcp(ref.tool, args)
                    stored = _result_json(result, variants)  # bounded: capped body, cut before redaction
        except Exception as exc:  # noqa: BLE001 - reduced to a stable code
            if stored is None and result is not None:
                # The answer arrived but could not be processed: whether it is usable is unknown.
                log_failure(log, "mcp tool result unusable", exc, code="result_unusable", run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                raise SideEffectOutcomeUnknown(key) from None
            if result is None and sent and _definite_rejection(exc):
                # The server refused the request itself (JSON-RPC error): nothing was executed.
                log_failure(log, "mcp tool call rejected", exc, code="call_rejected", run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                return {"is_error": True, "content": None}
            if result is None:
                code = failure_code(exc, statuses[0] if statuses else None) if not sent else _unknown_code(exc)
                log_failure(log, "mcp tool call failed", exc, code=code, run_id=ctx.deps.run_id,
                            tenant_id=ctx.deps.tenant_id, server=ref.server, tool=ref.tool)
                if not sent:
                    raise McpToolUnavailable(ref.server, ref.tool, code) from None
                raise SideEffectOutcomeUnknown(key) from None
            # The answer arrived; only closing the session failed. Keep the answer.
            log_failure(log, "mcp session close failed", exc, code="close_failed", level=logging.INFO,
                        run_id=ctx.deps.run_id, server=ref.server)
        assert stored is not None
        return stored


def mcp_toolset_for_run(engine: Engine, tenant_id: str, refs: Sequence[McpToolRef]) -> McpToolset:
    """The run's MCP toolset. No I/O here: it is built inside every step that evaluates the factory."""
    return McpToolset(
        refs,
        functools.partial(load_servers, engine, tenant_id),
        timeout_s(),
        functools.partial(_ensure_for, engine),
    )


async def _ensure_for(engine: Engine, tenant_id: str, conn: McpServerConn, tool: str) -> None:
    await ensure_managed_running(engine, tenant_id, conn, tool)


class RunToolset(AbstractToolset[Any]):
    """The run's built-in tools plus its MCP tools (`mcp`), its platform tools (`platform`,
    eap_worker.platform_tools: retrieve, ...) and, for an AG-UI turn that declares them, its client
    tools (`client`, eap_worker.client_tools: `external`, never called here); any may be None. Unlike CombinedToolset, rebuilding
    a tool from a recorded definition (the call step) routes by name, so it never lists the MCP
    server's tools or reads the platform tools' data again. Names never clash: AgentSpec validation
    and the reserved platform tool names prevent it, and get_tools refuses it anyway."""

    def __init__(
        self,
        builtin: AbstractToolset[Any],
        mcp: McpToolset | None = None,
        platform: Any | None = None,  # eap_worker.platform_tools.PlatformToolset (imported lazily)
        client: Any | None = None,  # eap_worker.client_tools.ClientToolset (AGUI.7; imported lazily)
    ) -> None:
        self.builtin = builtin
        self.mcp = mcp
        self.platform = platform
        self.client = client

    @property
    def id(self) -> str | None:
        return None

    def _members(self) -> list[AbstractToolset[Any]]:
        return [t for t in (self.builtin, self.mcp, self.platform, self.client) if t is not None]

    async def __aenter__(self) -> RunToolset:
        await self.builtin.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        return await self.builtin.__aexit__(*args)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        merged: dict[str, ToolsetTool[Any]] = {}
        for member in self._members():
            tools = await member.get_tools(ctx)
            clash = sorted(set(merged) & set(tools))
            if clash:  # never let one tool shadow another
                raise ValueError(f"tool name used twice: {clash[0]}")
            merged.update(tools)
        return merged

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        if self.mcp is not None and tool_def.name in self.mcp.refs:
            return await self.mcp.get_tool_for_tool_def(tool_def, ctx)
        if self.platform is not None and tool_def.name in self.platform.names:
            return await self.platform.get_tool_for_tool_def(tool_def, ctx)
        if self.client is not None and tool_def.name in self.client.names:
            return await self.client.get_tool_for_tool_def(tool_def, ctx)
        return await self.builtin.get_tool_for_tool_def(tool_def, ctx)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        return await tool.toolset.call_tool(name, tool_args, ctx, tool)

    def apply(self, visitor: Any) -> None:
        for member in self._members():
            member.apply(visitor)


__all__ = [
    "McpServerConn",
    "McpToolUnavailable",
    "McpToolset",
    "RunToolset",
    "ensure_managed_running",
    "failure_code",
    "get_mcp_runtime",
    "load_servers",
    "mcp_toolset_for_run",
    "set_mcp_runtime",
]
