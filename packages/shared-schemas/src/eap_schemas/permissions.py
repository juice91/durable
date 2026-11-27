"""The `permissions` block of an agent or workflow spec (IAM design 3.1) and the extractor of what a spec implies (3.4).

The block says what the spec may use: secrets, knowledge bases, MCP servers and tools, HTTP hosts, agents callable as
tools, built-in tools. P0b ships the SCHEMA and a read-only extractor: nothing validates, grants or enforces a block
(that is P3). On a spec the field is omitted while absent, so every stored spec keeps its JSON and `canonical_*_hash`;
inside the block every empty list is omitted too. An empty-but-present block (`permissions: {}`) is kept: later it will
mean "the spec uses nothing".

`implied_permissions` derives, from the refs a spec actually uses, the block that would cover them, and names the
places it could not resolve statically (`dynamic_paths`): a secret, host or agent computed at run time. Pure: no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from eap_schemas._omit import OmitNoneModel
from eap_schemas.http_request import EMBEDDED_REF
from eap_schemas.secrets import is_secret_ref

HTTP_REQUEST_TOOL = "http.request"
MAX_ENTRIES = 200


class McpPermission(OmitNoneModel):
    server: str = Field(min_length=1, max_length=100)
    tools: tuple[str, ...] = ("*",)  # ["*"] or tool names


class AgentPermission(OmitNoneModel):
    agent_id: str = Field(min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)


class Permissions(OmitNoneModel):
    secrets: tuple[str, ...] = Field(default=(), max_length=MAX_ENTRIES)
    knowledge_bases: tuple[str, ...] = Field(default=(), max_length=MAX_ENTRIES)
    mcp: tuple[McpPermission, ...] = Field(default=(), max_length=MAX_ENTRIES)
    http_hosts: tuple[str, ...] = Field(default=(), max_length=MAX_ENTRIES)
    agents: tuple[AgentPermission, ...] = Field(default=(), max_length=MAX_ENTRIES)
    tools: tuple[str, ...] = Field(default=(), max_length=MAX_ENTRIES)

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in type(self).model_fields:
                if not getattr(self, name):
                    data.pop(name, None)
        return data


class ImpliedReport(BaseModel):
    """What a spec's references imply: the covering block, and where a reference is computed at run time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    permissions: Permissions
    dynamic: bool
    dynamic_paths: tuple[str, ...] = ()
    source: Literal["implied"] = "implied"


def _walk(node: Any, path: str = ""):
    """Every (path, dict) in a JSON tree, parents before children."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _strings(node: Any, path: str = ""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            yield from _strings(value, f"{path}[{index}]")


def _is_dynamic(value: Any) -> bool:
    """A `$ref`, `$template` or other operator object (not a literal)."""
    return isinstance(value, dict) and any(str(k).startswith("$") for k in value)


def _host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    return parts.hostname.lower() + (f":{parts.port}" if parts.port else "")


def implied_permissions(spec: dict[str, Any], kind: Literal["agent", "workflow"]) -> ImpliedReport:
    """The block implied by the references of a stored spec (an agent's or a workflow's JSON)."""
    secrets: set[str] = set()
    kbs: list[str] = []
    mcp: dict[str, list[str]] = {}
    hosts: set[str] = set()
    agents: dict[tuple[str, int | None], None] = {}
    tools: list[str] = []
    dynamic: list[str] = []

    def add_tool(name: Any) -> None:
        if isinstance(name, str) and name not in tools:
            tools.append(name)

    if kind == "agent":
        for name in spec.get("tools") or []:
            add_tool(name)
        for kb in spec.get("knowledge_bases") or []:
            if isinstance(kb, dict) and isinstance(kb.get("id"), str) and kb["id"] not in kbs:
                kbs.append(kb["id"])
        for ref in spec.get("mcp_tools") or []:
            if isinstance(ref, dict) and isinstance(ref.get("server"), str):
                bucket = mcp.setdefault(ref["server"], [])
                if isinstance(ref.get("tool"), str) and ref["tool"] not in bucket:
                    bucket.append(ref["tool"])
        for ref in spec.get("agent_tools") or []:
            if isinstance(ref, dict) and isinstance(ref.get("agent_id"), str):
                version = ref.get("version")
                agents[(ref["agent_id"], version if isinstance(version, int) and not isinstance(version, bool) else None)] = None
    else:
        for path, step in _walk(spec.get("steps"), "steps"):
            step_type = step.get("type")
            if step_type == "agent":
                agent_id = step.get("agent_id")
                if isinstance(agent_id, str):
                    version = step.get("version")
                    agents[(agent_id, version if isinstance(version, int) and not isinstance(version, bool) else None)] = None
                elif agent_id is not None:
                    dynamic.append(f"{path}.agent_id")
            elif step_type == "tool":
                tool_id = step.get("tool_id")
                add_tool(tool_id)
                if tool_id == HTTP_REQUEST_TOOL:
                    args = step.get("input") if isinstance(step.get("input"), dict) else {}
                    url = args.get("url")
                    if isinstance(url, str):
                        host = _host_of(url)
                        if host is None:
                            dynamic.append(f"{path}.input.url")
                        else:
                            hosts.add(host)
                    elif url is not None or _is_dynamic(args.get("url")):
                        dynamic.append(f"{path}.input.url")

    # Secret refs: an exact `secret:<name>` string, or one embedded in a header value, anywhere in the spec but not in
    # prose fields (an agent's instructions and a workflow's description never resolve a secret).
    for path, text in _strings({k: v for k, v in spec.items() if k not in ("instructions", "description")}):
        name = is_secret_ref(text)
        if name is not None:
            secrets.add(name)
        else:
            secrets.update(EMBEDDED_REF.findall(text))
    # A computed secret reference: `$template` that builds a `secret:` prefix at run time.
    for path, node in _walk(spec):
        template = node.get("$template")
        if isinstance(template, str) and re.search(r"secret:\{", template):
            dynamic.append(path)

    permissions = Permissions(
        secrets=tuple(sorted(secrets)),
        knowledge_bases=tuple(kbs),
        mcp=tuple(McpPermission(server=server, tools=tuple(names) or ("*",)) for server, names in sorted(mcp.items())),
        http_hosts=tuple(sorted(hosts)),
        agents=tuple(AgentPermission(agent_id=a, version=v) for a, v in agents),
        tools=tuple(tools),
    )
    return ImpliedReport(permissions=permissions, dynamic=bool(dynamic), dynamic_paths=tuple(dynamic))


@dataclass(frozen=True)
class GrantUses:
    """What a spec uses that an admin must authorise (agent-bound grants): MCP servers by name and literal `secret:`
    names. `dynamic` is true when some reference is computed at run time (only the static part is listed)."""

    mcp_servers: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    dynamic: bool = False

    def __bool__(self) -> bool:
        return bool(self.mcp_servers or self.secrets)


def grant_uses(spec: dict[str, Any], kind: Literal["agent", "workflow"]) -> GrantUses:
    """The MCP servers and secrets a stored spec uses, from the same extractor the implied-permissions report runs (so the
    publish check, the migration report and the report endpoint cannot drift)."""
    report = implied_permissions(spec if isinstance(spec, dict) else {}, kind)
    return GrantUses(
        mcp_servers=tuple(sorted({m.server for m in report.permissions.mcp})),
        secrets=tuple(report.permissions.secrets),
        dynamic=report.dynamic,
    )


__all__ = [
    "AgentPermission", "GrantUses", "ImpliedReport", "McpPermission", "Permissions", "grant_uses", "implied_permissions",
]
