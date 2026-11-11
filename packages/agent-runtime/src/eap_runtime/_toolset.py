"""Build a Pydantic AI toolset that exposes exactly a spec's tools."""

from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import FunctionToolset

from eap_runtime._registry import ToolNotAllowedError, ToolRegistry, UnknownToolError
from eap_runtime._types import RunDeps
from eap_schemas import AgentSpec


def check_spec_tools(spec: AgentSpec, registry: ToolRegistry, allowed_tools: set[str] | frozenset[str]) -> None:
    """Raise UnknownToolError / ToolNotAllowedError for the first tool the spec may not use."""
    registered = registry.tool_ids()
    for tool_id in spec.tools:
        if tool_id not in registered:
            raise UnknownToolError(tool_id)
        if tool_id not in allowed_tools:
            raise ToolNotAllowedError(tool_id)


def toolset_for_spec(
    spec: AgentSpec,
    registry: ToolRegistry,
    allowed_tools: set[str] | frozenset[str],
) -> FunctionToolset[RunDeps]:
    """Return a FunctionToolset with only `spec.tools`, in spec order, each allowlisted.

    Deterministic and cheap, so it is safe as a durable DynamicToolset factory. Tool names are
    derived from tool ids by replacing '.' with '__' (the same rule as `build_agent`).
    """
    check_spec_tools(spec, registry, allowed_tools)
    tools = [
        Tool(
            registry.get(tool_id),
            name=tool_id.replace(".", "__"),
            description=registry.get_metadata(tool_id).get("description", ""),
        )
        for tool_id in spec.tools
    ]
    return FunctionToolset(tools)
