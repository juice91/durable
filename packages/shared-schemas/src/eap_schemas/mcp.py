"""MCP tool references in agent specs (milestone 3, task 3.5).

An agent spec lists the MCP tools it may use as `{server, tool}` pairs: `server` is the NAME of an
MCP server registered in the agent's tenant, `tool` the tool's name on that server. Publish checks
that each server exists in the tenant and is enabled; whether the tool exists is checked when a run
discovers its tools (the server can change after publish).

The model sees each MCP tool as `mcp_tool_name(server, tool)`: `mcp__<server>__<tool>` with every
character outside `[A-Za-z0-9_-]` replaced by `_`, at most 64 characters (the common provider limit).
A longer name is cut to 55 characters and suffixed with `_` + 8 hex chars of a hash of the exact
(server, tool) pair, so distinct pairs stay distinct. AgentSpec rejects a spec in which two tools
(MCP or built-in) end up with the same model-facing name.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

MCP_TOOL_PREFIX = "mcp__"
MAX_TOOL_NAME = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class McpToolRef(BaseModel):
    """One MCP tool an agent may call: a tenant MCP server name and a tool name on that server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=128)


def mcp_tool_name(server: str, tool: str) -> str:
    """The provider-safe tool name the model sees for `tool` on MCP server `server`."""
    name = f"{MCP_TOOL_PREFIX}{_UNSAFE.sub('_', server)}__{_UNSAFE.sub('_', tool)}"
    if len(name) <= MAX_TOOL_NAME:
        return name
    digest = hashlib.sha256(f"{server}\x00{tool}".encode()).hexdigest()[:8]
    return f"{name[: MAX_TOOL_NAME - 9]}_{digest}"


__all__ = ["MCP_TOOL_PREFIX", "MAX_TOOL_NAME", "McpToolRef", "mcp_tool_name"]
