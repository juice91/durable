"""Secret references at the point of use (secret input milestone, D4).

An agent never holds a credential, only the reference `secret:<name>` (`eap_schemas.secrets`). The
worker swaps a reference for the tenant's stored value at the last moment and scrubs that value from
what the run records:

- A built-in tool's string arguments (top level and nested) equal to a reference are resolved inside
  the tool's own call step (`SecretResolvingToolset`), after the arguments were recorded, so the
  durable event, the approval card and the DBOS checkpoint hold the reference only. The tool's
  result and any failure it raises are redacted with `secret_variants` of the values it received.
- An MCP tool call's string arguments are resolved the same way inside its call step
  (`McpToolset.call_tool`, via `resolve_args`): the recorded arguments keep the reference, the result
  is redacted with the values, and an unknown reference is a `secret_not_found` tool failure.
- An MCP server's decrypted header value or env value equal to a reference is resolved the same way
  (eap_worker.mcp); `McpServerConn.headers` then feeds the existing header redaction.

An unknown reference fails the call with `SecretRefNotFound`, which names the reference (a name, not
a value). Values are read through `eap_run_store.secrets.SecretStore`, tenant-scoped.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from eap_runtime import SideEffectOutcomeUnknown
from eap_schemas.secrets import is_secret_ref
from eap_secrets import SecretsError, load_keyring
from eap_secrets.redaction import redact, secret_variants

log = logging.getLogger("eap_worker.secret_refs")

SECRET_NOT_FOUND = "secret_not_found"


class SecretRefNotFound(Exception):
    """A `secret:<name>` reference with no stored value for the tenant. Carries the reference only."""

    def __init__(self, ref: str):
        self.ref = ref
        super().__init__(ref)  # pickle-safe (DBOS pickles step errors)

    def __str__(self) -> str:
        return f"{SECRET_NOT_FOUND}: {self.ref}"


def secret_store(engine: Any) -> Any:
    """A SecretStore on `engine` with the platform keyring (SecretsError when the key is unset)."""
    from eap_run_store.secrets import SecretStore

    return SecretStore(engine, load_keyring())


def has_ref(value: Any) -> bool:
    """Whether any string in `value` (nested dicts and lists) is a secret reference."""
    if isinstance(value, str):
        return is_secret_ref(value) is not None
    if isinstance(value, dict):
        return any(has_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_ref(v) for v in value)
    return False


def resolve_value(value: Any, lookup: Callable[[str], str | None], used: list[str]) -> Any:
    """`value` with every reference string replaced by `lookup(name)`; each resolved value is appended
    to `used`. Raises SecretRefNotFound for a reference without a value."""
    if isinstance(value, str):
        name = is_secret_ref(value)
        if name is None:
            return value
        resolved = lookup(name)
        if resolved is None:
            raise SecretRefNotFound(value)
        used.append(resolved)
        return resolved
    if isinstance(value, dict):
        return {k: resolve_value(v, lookup, used) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, lookup, used) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve_value(v, lookup, used) for v in value)
    return value


def resolve_string(value: str, engine: Any, tenant_id: str, store: Any | None = None) -> str:
    """One header or env value: the stored secret when it is a reference, else unchanged (blocking)."""
    if is_secret_ref(value) is None:
        return value
    used: list[str] = []
    store = store or secret_store(engine)
    return resolve_value(value, lambda name: store.resolve(tenant_id, name), used)


def resolve_args(engine: Any, tenant_id: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """`args` with every reference string resolved, and the values used (blocking; run in a thread).
    No reference: `args` itself and no values. Raises SecretRefNotFound (names the reference) or
    SecretsError (the platform key is unset or the stored value is unreadable)."""
    if not has_ref(args):
        return args, []
    store = secret_store(engine)
    used: list[str] = []
    return resolve_value(args, lambda name: store.resolve(tenant_id, name), used), used


async def call_with_secrets(
    engine: Any, call: Callable[[dict[str, Any]], Awaitable[Any]], tool_args: dict[str, Any], ctx: RunContext[Any]
) -> Any:
    """`call(args)` with every secret reference in `tool_args` resolved first (inside the call step);
    the result and any failure are redacted with the resolved values. No reference: `call` as is."""
    if not has_ref(tool_args):
        return await call(tool_args)
    tenant_id = ctx.deps.tenant_id

    def read() -> dict[str, str | None]:
        store = secret_store(engine)
        cache: dict[str, str | None] = {}

        def lookup(secret_name: str) -> str | None:
            if secret_name not in cache:
                cache[secret_name] = store.resolve(tenant_id, secret_name)
            return cache[secret_name]

        resolve_value(tool_args, lookup, [])  # raises for a missing reference before the call
        return cache

    try:
        cache = await asyncio.to_thread(read)
    except SecretsError:
        log.warning("secret reference could not be decrypted tenant=%s", tenant_id)
        raise ToolFailed("secret_unreadable: a referenced secret could not be read") from None
    except SecretRefNotFound as exc:
        raise ToolFailed(f"{SECRET_NOT_FOUND}: {exc.ref} is not a stored secret") from None
    used: list[str] = []
    resolved = resolve_value(tool_args, lambda n: cache[n], used)
    variants = secret_variants(used)
    try:
        result = await call(resolved)
    except SideEffectOutcomeUnknown:
        raise
    except ModelRetry as exc:
        raise ModelRetry(redact(str(exc), variants)) from None
    except ToolFailed as exc:
        raise ToolFailed(redact(str(exc), variants)) from None
    except Exception as exc:  # noqa: BLE001 - scrubbed: the message may echo an argument
        message = str(exc)
        if redact(message, variants) != message:
            raise RuntimeError(f"{type(exc).__name__}: {redact(message, variants)}") from None
        raise
    return redact(result, variants)


class SecretFunctionToolset(FunctionToolset[Any]):
    """The run's built-in tools (a FunctionToolset, so step layout and tool handling are unchanged),
    resolving secret references in arguments inside the call step."""

    engine: Any = None

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        return await call_with_secrets(
            self.engine, lambda args: super(SecretFunctionToolset, self).call_tool(name, args, ctx, tool),
            tool_args, ctx,
        )


def with_secret_refs(builtin: FunctionToolset[Any], engine: Any) -> SecretFunctionToolset:
    """`builtin`'s tools in a SecretFunctionToolset."""
    wrapped = SecretFunctionToolset(list(builtin.tools.values()))
    wrapped.engine = engine
    return wrapped
