"""What a created agent or workflow of a tenant may use: the Architect's allowlist (Task 3, D6).

`approved_for_tenant` is the single source (the per-tool rule, `offer_tool`, is shared with the API's
`GET /architect/options` through eap_schemas.architect). The read tool `architect.list_approved_tools` returns it,
the Architect page's pickers (Task 5) show it, and `compile_agent` / `compile_workflow`
(`eap_schemas.architect`) validate every save against it, so the Architect cannot grant what this
list did not offer.

- tools: the worker's tool registry (limited to the worker's allowed tools) minus the code denylist
  (`ARCHITECT_DENIED_TOOL_PREFIXES`: the Architect's own tools, the knowledge and memory platform
  tools, file, shell, process and host tools, anything registered with `host_only`). A tool is
  `requires_approval` when the registry marks it as having a side effect or it is in
  `GATED_TOOL_IDS`; `save_agent` writes the matching `approvals` entries.
- mcp_servers: the tenant's enabled MCP servers with the tools a live `tools/list` returns (best
  effort: a server that does not answer is listed with no tools, so none can be granted from it).
  Every granted MCP tool is gated.
- knowledge_bases: the tenant's knowledge bases.
- models: the worker's model policy (`EAP_ALLOWED_MODELS`) read worker-side, no snapshot table: the
  concrete `provider:name` entries, `test:*` as the scripted test models, `litellm:*` as what the
  gateway's `/models` lists and `anthropic:*` as Anthropic's catalogue (when ANTHROPIC_API_KEY is set). Capabilities are left empty here (discovery metadata lives in the API).
- agents: Architect-made agents of the tenant (draft or published, not archived), which a created workflow's
  agent steps may name (their row ids stay out of every dump). A workflow draft may name a draft agent;
  publishing the workflow needs every agent it names to have a published version (`agent_not_published`).

Reads are blocking SQL run in a thread; nothing here writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_schemas.architect import (
    ARCHITECT_DENIED_TOOL_PREFIXES,
    GATED_TOOL_IDS,
    Allowlist,
    AllowlistAgent,
    AllowlistKnowledgeBase,
    AllowlistMcpServer,
    AllowlistModel,
    AllowlistTool,
    offer_tool,
)
from eap_schemas.architect_seed import rank_model_ids
from eap_schemas.models_policy import ANTHROPIC_CATALOGUE

log = logging.getLogger("eap_worker.architect_allowlist")

# The worker's scripted `test:` models (the API lists the same names, `SCRIPTED_TEST_MODELS`).
SCRIPTED_TEST_MODEL_NAMES = ("echo", "ticket", "first-tool", "mcp", "history", "context", "moderation")
MCP_DISCOVERY_TIMEOUT_S = 8.0
GATEWAY_TIMEOUT_S = 5.0

McpLister = Callable[[Engine, str, Sequence[str]], Awaitable[dict[str, tuple[str, ...]]]]
GatewayLister = Callable[[str, str | None], list[str]]


def registry_tools(registry: Any, allowed: frozenset[str]) -> tuple[AllowlistTool, ...]:
    """The registry's tools that survive the allowed set and the denylist, sorted by id."""
    found: list[AllowlistTool] = []
    for tool_id in sorted(registry.tool_ids()):
        if tool_id not in allowed:
            continue
        meta = registry.get_metadata(tool_id)
        try:
            declaration = registry.get_declaration(tool_id)
            title, description = declaration.title, declaration.description
            schema, example = declaration.input_json_schema(), declaration.example
            result_example, result_schema = declaration.result_example, declaration.result_schema
        except ValueError:  # registered with a string id: metadata only
            title, description = tool_id, str(meta.get("description") or tool_id)
            schema, example, result_example, result_schema = None, None, None, None
        offered = offer_tool(
            tool_id, title, description, side_effect=bool(meta.get("side_effect")), host_only=bool(meta.get("host_only")),
            input_schema=schema, example=example,
            result_example=result_example, result_schema=result_schema,
        )
        if offered is not None:
            found.append(offered)
    return tuple(found)


def _gateway_models(base_url: str, api_key: str | None) -> list[str]:
    """Names the OpenAI-compatible gateway lists at `{base}/models` (blocking; [] on any failure)."""
    request = urllib.request.Request(base_url.rstrip("/") + "/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=GATEWAY_TIMEOUT_S) as response:  # noqa: S310 - operator-set URL
            body = json.loads(response.read(2_000_000))
        return sorted({str(m["id"]) for m in body.get("data", []) if isinstance(m, dict) and m.get("id")})
    except Exception as exc:  # noqa: BLE001 - discovery is best effort
        log.warning("architect allowlist: gateway model list failed: %s", type(exc).__name__)
        return []


def models_from_settings(
    settings: Any, gateway: GatewayLister = _gateway_models, environ: Mapping[str, str] | None = None,
) -> tuple[AllowlistModel, ...]:
    """The worker's model policy as allowlist models (see the module docstring). `anthropic:*` lists the
    Anthropic catalogue when ANTHROPIC_API_KEY is set (the provider has no listing the worker calls)."""
    env = os.environ if environ is None else environ
    policy = settings.allowed_models
    found: dict[str, AllowlistModel] = {}

    def add(provider: str, name: str) -> None:
        model_id = f"{provider}:{name}"
        found.setdefault(model_id, AllowlistModel(id=model_id, provider=provider))

    for entry in policy.entries:
        provider, _, name = entry.partition(":")
        if name != "*":
            add(provider, name)
        elif provider == "test":
            for scripted in SCRIPTED_TEST_MODEL_NAMES:
                add("test", scripted)
        elif provider == "anthropic" and (env.get("ANTHROPIC_API_KEY") or "").strip():
            for listed in ANTHROPIC_CATALOGUE:
                if policy.allows("anthropic", listed):
                    add("anthropic", listed)
        elif provider == "litellm" and getattr(settings, "litellm_base_url", None):
            for listed in gateway(settings.litellm_base_url, getattr(settings, "litellm_api_key", None)):
                if policy.allows("litellm", listed):
                    add("litellm", listed)
    # Best first (the API's seed ranking), so the first model, `default_model`, is the ranked pick.
    return tuple(found[model_id] for model_id in rank_model_ids(list(found)))


_MCP_SERVERS = text("SELECT name FROM mcp_servers WHERE tenant_id = :t AND enabled ORDER BY name")
_AGENTS = text(
    "SELECT a.slug, a.name, a.id FROM agents a WHERE a.tenant_id = :t AND a.slug IS NOT NULL "
    "AND NOT a.archived AND NOT a.builtin ORDER BY a.slug"
)


async def discover_mcp_tools(engine: Engine, tenant_id: str, names: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Tool names per server from a live `tools/list`, one short session each; a server that cannot be
    reached maps to no tools. A managed server is started first, as a tool call would."""
    import anyio

    from eap_schemas import McpToolRef
    from eap_secrets.outbound import OutboundPolicy

    from eap_worker import mcp

    out: dict[str, tuple[str, ...]] = {}
    for name in names:
        out[name] = ()
        try:
            conns = await asyncio.to_thread(mcp.load_servers, engine, tenant_id, [McpToolRef(server=name, tool="-")])
            conn = conns[name]
            # The same path as a tool call: a managed server is started when idle (its endpoint is a
            # private in-cluster Service, allowed by identity in `_client`), the whole thing bounded.
            with anyio.fail_after(MCP_DISCOVERY_TIMEOUT_S):
                await mcp.ensure_managed_running(engine, tenant_id, conn, "-")
                async with mcp._session(conn, OutboundPolicy.from_env(), MCP_DISCOVERY_TIMEOUT_S) as client:
                    out[name] = tuple(sorted(t.name for t in await client.list_tools()))
        except Exception as exc:  # noqa: BLE001 - best effort; the server just offers no tools
            log.warning("architect allowlist: mcp discovery failed server=%s %s", name, type(exc).__name__)
    return out


def _read_tenant(engine: Engine, tenant_id: str) -> tuple[list[str], list[dict[str, Any]], list[AllowlistAgent]]:
    from eap_run_store import KnowledgeStore

    with engine.connect() as conn:
        servers = list(conn.execute(_MCP_SERVERS, {"t": tenant_id}).scalars())
        agents = [
            AllowlistAgent(id=r.slug, name=r.name, row_id=str(r.id))
            for r in conn.execute(_AGENTS, {"t": tenant_id})
        ]
    kbs = KnowledgeStore(engine).list_knowledge_bases(tenant_id)
    return servers, kbs, agents


async def approved_for_tenant(
    engine: Engine,
    tenant_id: str,
    *,
    registry: Any | None = None,
    settings: Any | None = None,
    allowed_tools: frozenset[str] | None = None,
    mcp_lister: McpLister | None = None,
    gateway_lister: GatewayLister | None = None,
) -> Allowlist:
    """The tenant's allowlist. The worker's registry, settings and allowed tools are the defaults;
    each can be passed in (tests). Models are listed best first (`rank_model_ids`);
    `default_model` is the first."""
    if registry is None or settings is None or allowed_tools is None:
        from eap_worker.runtime import get_runtime

        rt = get_runtime()
        registry = registry if registry is not None else rt.registry
        settings = settings if settings is not None else rt.settings
        allowed_tools = allowed_tools if allowed_tools is not None else rt.allowed_tools
    server_names, kbs, agents = await asyncio.to_thread(_read_tenant, engine, tenant_id)
    tools_by_server = await (mcp_lister or discover_mcp_tools)(engine, tenant_id, server_names)
    models = await asyncio.to_thread(models_from_settings, settings, gateway_lister or _gateway_models)
    return Allowlist(
        tools=registry_tools(registry, allowed_tools),
        mcp_servers=tuple(
            AllowlistMcpServer(id=n, name=n, tools=tuple(sorted(tools_by_server.get(n, ())))) for n in server_names
        ),
        knowledge_bases=tuple(AllowlistKnowledgeBase(id=str(kb["id"]), name=str(kb["name"])) for kb in kbs),
        models=models,
        agents=tuple(agents),
        default_model=models[0].id if models else None,
    )


__all__ = [
    "ARCHITECT_DENIED_TOOL_PREFIXES",
    "GATED_TOOL_IDS",
    "Allowlist",
    "approved_for_tenant",
    "discover_mcp_tools",
    "models_from_settings",
    "registry_tools",
]
