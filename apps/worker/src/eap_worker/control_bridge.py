"""The Architect's bridge to the platform's MCP control plane (`control__*` tools, `platform.list_capabilities`).

The control plane (`eap_api.mcp_control`, 40+ tools: test suites, scorers, eval runs, schedules, knowledge base
sources, MCP servers, ...) is what external agents drive over MCP. The Architect used to know only a
hand-written subset; this module lets it find and use every control tool itself.

The seam. The handlers live in the API process and take the API's `Ctx`; the worker runs Architect tools. So
the worker calls the API's own MCP endpoint (`POST {EAP_API_INTERNAL_URL}/api/v1/mcp`) over HTTP, as the
Architect's run, and the handler code exists once. Each call presents a run token (`eap_run_store.run_tokens`):
signed with a key derived from `EAP_SECRETS_KEY`, naming the tenant and the run, valid for two minutes and minted per
call, so no long-lived key exists anywhere. The API accepts it only while the run is a live run of the built-in Architect
and records the Architect run as the actor of every call (`architect-run:<run id>` in the audit record and as the
`created_by` of what it writes). Tools tagged `human_only` there (`approvals.decide`, `agent.chat`) are refused for
this principal and hidden here.

The catalog is the control plane's own registry: `tools/list` of that endpoint (name, description, the `manage`
tag, the input schema from the tool's signature). Nothing here lists a tool by hand.

Model-facing names: `eval.suite.create` is `control__eval_suite_create` (`eap_schemas.architect.control_tool_name`).
The model never sees the dotted names except in `platform__list_capabilities`; a call maps back through the
catalog, so the mapping round-trips even for names with underscores.

Gating: a `read` tool runs at once. A `manage` tool is declared `kind="unapproved"`, so the run defers it to
the approval card exactly like `architect__publish_workflow`, and only an approved call is sent. The card
summary comes from `control_approval_summary` (the tool's description and the arguments in plain words).
An approved manage call goes through the side-effect store (key `control:<run>:<tool call>`): a crashed and
replayed call returns its recorded result, and an unknown outcome is raised, never guessed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from eap_observability import log_failure
from eap_run_store.run_tokens import mint_run_token, signing_key
from eap_runtime import RunDeps, run_side_effect
from eap_schemas.architect import CONTROL_TOOL_PREFIX, control_tool_name
from eap_worker.read_only import is_read_only, refusal

log = logging.getLogger("eap_worker.control_bridge")

API_URL_ENV = "EAP_API_INTERNAL_URL"
MCP_PATH = "/api/v1/mcp"
LIST_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 330.0  # workflow.run.wait waits up to 300 s inside the handler
CATALOG_TTL_S = 60.0
MAX_RESULT_BYTES = 32 * 1024
READ, MANAGE = "read", "manage"

_ANY_ARGS = SchemaValidator(core_schema.dict_schema(core_schema.str_schema(), core_schema.any_schema()))


@dataclass(frozen=True)
class ControlTool:
    """One control-plane tool as the API's registry describes it."""

    name: str  # `eval.suite.create`
    description: str
    kind: str  # read | manage
    input_schema: dict[str, Any]

    @property
    def tool_name(self) -> str:
        return control_tool_name(self.name)

    @property
    def gated(self) -> bool:
        return self.kind == MANAGE


def error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# ------------------------------------------------------------------------------------ approval summary

_PAREN = re.compile(r"\s*\([^()]*\)")


def _title(description: str) -> str:
    """The first sentence of a control tool's description, without parentheticals, at most 140 characters."""
    flat = " ".join(description.split())
    sentence = re.split(r"(?<=[.;:])\s", _PAREN.sub("", flat), maxsplit=1)[0].rstrip(".;: ")
    if len(sentence) > 140:
        sentence = sentence[:140].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    return sentence or "Run a platform action"


def _phrase(key: str, value: Any) -> str:
    if isinstance(value, str):
        text = " ".join(value.split())
        shown = repr(text[:60] + "…") if len(text) > 60 else repr(text)
        return f"{key} {shown}"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} {key}" if len(value) != 1 else f"1 item in {key}"
    if isinstance(value, dict):
        return f"{key} with {len(value)} field{'s' if len(value) != 1 else ''}"
    return f"{key} {value!r}" if value is not None else ""


# TODO(summary-dedup): this overlaps architect_tools.approval_summary_text for the eval tools; fold them into one follow-up.
def control_approval_summary(name: str, description: str | None, args: dict[str, Any]) -> str:
    """The approval card's one-paragraph summary of a gated control call, in the person's words: what the tool
    does (its description's first sentence) and the arguments it was called with (strings quoted, lists
    counted). Pure and deterministic; the same conventions as `architect_tools.approval_summary_text`
    (a sentence, then the specifics, ending with a full stop). `name` is the control name (`eval.suite.create`)
    and is only used when the description is not known."""
    title = _title(description) if description else name.replace(".", " ").replace("_", " ").capitalize()
    parts = [p for k, v in (args or {}).items() if (p := _phrase(str(k), v))]
    return f"{title}." + (f" With {', '.join(parts)}." if parts else "")


def control_summary_for_call(tool_name: str, args: dict[str, Any]) -> str:
    """The card summary for a model-facing `control__*` call: the description from the catalog this process
    listed for the run (a cold process after a restart has none and words it from the tool's name)."""
    for tool in (_catalog_cache[1] if _catalog_cache else ()):
        if tool.tool_name == tool_name:
            return control_approval_summary(tool.name, tool.description, args)
    return control_approval_summary(tool_name[len(CONTROL_TOOL_PREFIX):], None, args)


# ------------------------------------------------------------------------------------------- the client


def api_url() -> str | None:
    value = os.environ.get(API_URL_ENV, "").strip()
    return value.rstrip("/") + MCP_PATH if value else None


def _token(tenant_id: str, run_id: str) -> str | None:
    secret = os.environ.get("EAP_SECRETS_KEY")
    return mint_run_token(signing_key(secret), tenant_id, run_id) if secret else None


def _default_client(url: str, token: str, timeout: float) -> Any:
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    return Client(StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"}), timeout=timeout,
                  init_timeout=timeout)


# `(url, token, timeout) -> a FastMCP client`; replaced in tests to reach an in-process server.
_client_factory: Callable[[str, str, float], Any] = _default_client
_catalog_cache: tuple[float, list[ControlTool]] | None = None


def reset_cache() -> None:
    global _catalog_cache
    _catalog_cache = None


def _tags(tool: Any) -> set[str]:
    meta = getattr(tool, "meta", None) or {}
    return set(((meta.get("fastmcp") or {}).get("tags")) or ())


def _from_listed(tool: Any) -> ControlTool | None:
    tags = _tags(tool)
    if "human_only" in tags or "internal" in tags:  # `internal`: the platform's own calls (provenance.record)
        return None
    schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
    return ControlTool(
        name=tool.name,
        description=(tool.description or "").strip(),
        kind=MANAGE if "manage" in tags else READ,
        input_schema=schema if isinstance(schema, dict) and schema else {"type": "object", "properties": {}},
    )


async def fetch_catalog(tenant_id: str, run_id: str, *, refresh: bool = False) -> list[ControlTool] | dict[str, Any]:
    """The control plane's tools the Architect may call (sorted by name), or an `{"error": ...}` dict."""
    global _catalog_cache
    if not refresh and _catalog_cache is not None and time.monotonic() - _catalog_cache[0] < CATALOG_TTL_S:
        return _catalog_cache[1]
    url, token = api_url(), _token(tenant_id, run_id)
    if url is None or token is None:
        return error("control_unavailable", f"the control plane is not configured for this worker ({API_URL_ENV} and EAP_SECRETS_KEY)")
    from fastmcp.telemetry import suppress_fastmcp_telemetry

    try:
        with suppress_fastmcp_telemetry(), anyio.fail_after(LIST_TIMEOUT_S):
            async with _client_factory(url, token, LIST_TIMEOUT_S) as client:
                listed = await client.list_tools()
    except Exception as exc:  # noqa: BLE001 - reduced to a stable code
        log_failure(log, "control catalog unavailable", exc, code="control_unavailable", run_id=run_id, tenant_id=tenant_id)
        return error("control_unavailable", "the platform control plane could not be reached; try again shortly")
    tools = sorted((t for t in map(_from_listed, listed) if t is not None), key=lambda t: t.name)
    _catalog_cache = (time.monotonic(), tools)
    return tools


def _text_of(result: Any) -> Any:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


def _bound(data: Any) -> Any:
    text = json.dumps(data, default=str)
    if len(text.encode("utf-8")) <= MAX_RESULT_BYTES:
        return data
    return {"truncated": True, "bytes": len(text.encode("utf-8")), "preview": text[:2000]}


async def call_control(tenant_id: str, run_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """One control tool call as the Architect's run: the tool's structured result, or `{"error": {code, message}}`
    (a refusal of the control plane keeps its own code and message)."""
    url, token = api_url(), _token(tenant_id, run_id)
    if url is None or token is None:
        return error("control_unavailable", f"the control plane is not configured for this worker ({API_URL_ENV} and EAP_SECRETS_KEY)")
    from fastmcp.telemetry import suppress_fastmcp_telemetry

    try:
        with suppress_fastmcp_telemetry(), anyio.fail_after(CALL_TIMEOUT_S):
            async with _client_factory(url, token, CALL_TIMEOUT_S) as client:
                result = await client.call_tool(name, args, raise_on_error=False)
    except Exception as exc:  # noqa: BLE001
        log_failure(log, "control call failed", exc, code="control_unavailable", run_id=run_id, tenant_id=tenant_id, tool=name)
        return error("control_unavailable", "the platform control plane did not answer; the call may or may not have run")
    data = getattr(result, "structured_content", None)
    if data is None:
        data = _text_of(result)
    if getattr(result, "is_error", False):
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            return {"error": {"code": str(data["error"].get("code", "error")), "message": str(data["error"].get("message", ""))[:500]}}
        return error("control_error", str(data)[:500] if data else "the call was refused")
    return _bound(data if isinstance(data, dict) else {"result": data})


# ---------------------------------------------------------------------------------------------- tools


def _ids(ctx: RunContext[RunDeps]) -> tuple[str, str]:
    return ctx.deps.tenant_id, str(ctx.deps.run_id)


def _offered(ctx: RunContext[RunDeps], tools: list[ControlTool]) -> list[ControlTool]:
    """The catalog this run may use: for a read-only person, the `read` tools (eap_worker.read_only)."""
    return [t for t in tools if t.kind == READ] if is_read_only(ctx.deps) else tools


def capability_entry(tool: ControlTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "tool": tool.tool_name,
        "description": " ".join(tool.description.split()),
        "kind": tool.kind,
        "requires_approval": tool.gated,
    }


def list_capabilities_result(tools: list[ControlTool], query: str | None = None) -> dict[str, Any]:
    q = (query or "").strip().lower()
    chosen = [t for t in tools if not q or q in t.name.lower() or q in t.description.lower()]
    return {
        "total": len(tools),
        "matched": len(chosen),
        "capabilities": [capability_entry(t) for t in chosen],
        "how_to_call": "Call a capability as its `tool` name. Tools with requires_approval ask the person first.",
    }


async def list_capabilities(ctx: RunContext[RunDeps], query: str | None = None) -> dict[str, Any]:
    """List what the platform's control plane can do, beyond your other tools: each capability's name, tool name
    (call it as that tool), one-line description, kind (read or manage) and whether a call asks the person for
    approval. `query` keeps the ones whose name or description contains that text (for example "eval", "schedule",
    "knowledge"). Call this first when a request is not covered by your other tools."""
    tenant_id, run_id = _ids(ctx)
    tools = await fetch_catalog(tenant_id, run_id)
    if isinstance(tools, dict):
        return tools
    return list_capabilities_result(_offered(ctx, tools), query)


class ControlToolset(AbstractToolset[Any]):
    """The `control__*` tools of the built-in Architect's run. The definitions come from the API's registry inside
    the `get_tools` step (recorded, so a replay does not list again); a call rebuilds from the recorded
    definition."""

    @property
    def id(self) -> str | None:
        return None  # always inside the run's DynamicToolset, which carries the durable id

    @property
    def names(self) -> frozenset[str]:
        return frozenset()

    def owns(self, name: str) -> bool:
        return name.startswith(CONTROL_TOOL_PREFIX)

    def _tool(self, tool_def: ToolDefinition) -> ToolsetTool[Any]:
        return ToolsetTool(toolset=self, tool_def=tool_def, max_retries=1, args_validator=_ANY_ARGS)

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        tenant_id, run_id = _ids(ctx)
        catalog = await fetch_catalog(tenant_id, run_id)
        if isinstance(catalog, dict):  # not configured or unreachable: the Architect runs without the bridge
            log.warning("control bridge off for run %s: %s", run_id, catalog["error"]["code"])
            return {}
        out: dict[str, ToolsetTool[Any]] = {}
        for tool in _offered(ctx, catalog):
            note = " A person approves each call." if tool.gated else ""
            out[tool.tool_name] = self._tool(ToolDefinition(
                name=tool.tool_name,
                description=tool.description + note,
                parameters_json_schema=tool.input_schema,
                kind="unapproved" if tool.gated else "function",
                metadata={"control": tool.name, "control_kind": tool.kind},
            ))
        return out

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        if not self.owns(tool_def.name):
            raise KeyError(tool_def.name)
        return self._tool(tool_def)

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]) -> Any:
        meta = tool.tool_def.metadata or {}
        control_name = meta.get("control")
        if not isinstance(control_name, str):
            return error("unknown_tool", f"{name} is not a control tool")
        tenant_id, run_id = _ids(ctx)
        if meta.get("control_kind") != READ and is_read_only(ctx.deps):
            return refusal()
        if meta.get("control_kind") != MANAGE:
            return await call_control(tenant_id, run_id, control_name, tool_args)
        if ctx.tool_call_id is None:
            raise ValueError("tool_call_id is required for a gated control call")
        key = f"control:{run_id}:{ctx.tool_call_id}"
        loop = asyncio.get_running_loop()

        def call() -> dict[str, Any]:
            return asyncio.run_coroutine_threadsafe(call_control(tenant_id, run_id, control_name, tool_args), loop).result()

        result, _performed = await asyncio.to_thread(run_side_effect, ctx.deps.side_effects, key, call)
        return result


def control_toolset_for_run(spec: Any) -> ControlToolset | None:
    """The bridge for a run whose spec lists `platform.list_capabilities` (only the built-in Architect may: the
    `platform.` prefix is built-in only), else None, so every other run's toolset is unchanged."""
    from eap_schemas.architect import CONTROL_LIST_TOOL_ID

    return ControlToolset() if CONTROL_LIST_TOOL_ID in spec.tools else None


def register_control_tools(registry: Any) -> None:
    registry.register("platform.list_capabilities", list_capabilities, description=(list_capabilities.__doc__ or "").strip(),
                      side_effect=False)


__all__ = [
    "ControlTool", "ControlToolset", "call_control", "control_approval_summary", "control_toolset_for_run",
    "fetch_catalog", "list_capabilities_result", "register_control_tools", "reset_cache",
]
