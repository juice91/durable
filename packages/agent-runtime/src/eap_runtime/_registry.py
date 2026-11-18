"""Tool registry and related errors."""

import re
from typing import Any, Callable

from eap_schemas import ToolDeclaration
from pydantic_ai.tools import Tool


class UnknownToolError(Exception):
    """Raised when a tool is not registered."""

    def __init__(self, tool_id: str):
        self.tool_id = tool_id
        # args are the constructor arguments, so pickle (DBOS step errors) rebuilds it exactly.
        super().__init__(tool_id)

    def __str__(self) -> str:
        return f"Unknown tool: {self.tool_id}"


class ToolNotAllowedError(Exception):
    """Raised when a tool is registered but not allowed."""

    def __init__(self, tool_id: str):
        self.tool_id = tool_id
        super().__init__(tool_id)  # pickle-safe, as UnknownToolError

    def __str__(self) -> str:
        return f"Tool not allowed: {self.tool_id}"


class ToolRegistry:
    """Registry for agent tools."""

    _TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

    def __init__(self) -> None:
        self._tools: dict[str, tuple[Callable, ToolDeclaration | None, dict]] = {}
        self._tool_names: dict[str, str] = {}  # derived_name -> tool_id
        self._summarisers: dict[str, Callable[[dict], str]] = {}  # tool_id -> approval summariser

    def _derive_tool_name(self, tool_id: str) -> str:
        """Derive tool name from tool_id by replacing '.' with '__'."""
        return tool_id.replace(".", "__")

    def register(
        self,
        declaration: ToolDeclaration | str,
        func: Callable,
        *,
        description: str = "",
        side_effect: bool = False,
        external_idempotent: bool = False,
        approval_summary: Callable[[dict], str] | None = None,
    ) -> None:
        """Bind an implementation to a declaration.

        `approval_summary(args) -> str` words what a gated call would do; the approval card shows it
        above the arguments (`summary_for`).

        String IDs remain supported for dynamically registered test and extension tools. Built-in
        tools use declarations so their public metadata cannot drift from their implementations.
        """
        if isinstance(declaration, str):
            tool_id = declaration
            if not self._TOOL_ID_PATTERN.fullmatch(tool_id):
                raise ValueError(
                    f"Invalid tool_id '{tool_id}': must match "
                    f"^[a-z][a-z0-9_.-]{{0,63}}$"
                )
            bound_declaration = None
            metadata = {
                "description": description,
                "side_effect": side_effect,
                "external_idempotent": external_idempotent,
            }
        else:
            tool_id = declaration.id
            bound_declaration = declaration
            implementation_schema = Tool(
                func,
                name=self._derive_tool_name(tool_id),
                description=declaration.description,
            ).tool_def.parameters_json_schema
            if not declaration.matches_implementation_schema(implementation_schema):
                raise ValueError(f"Tool '{tool_id}' implementation does not match its declaration")
            metadata = {
                "description": declaration.description,
                "side_effect": declaration.side_effect,
                "external_idempotent": declaration.external_idempotent,
            }
        if tool_id in self._tools:
            raise ValueError(f"Tool '{tool_id}' is already registered")
        
        # Check for colliding derived name
        derived_name = self._derive_tool_name(tool_id)
        if derived_name in self._tool_names:
            existing_tool = self._tool_names[derived_name]
            raise ValueError(
                f"Tool '{tool_id}' would collide with '{existing_tool}' "
                f"(both derive to '{derived_name}')"
            )
        
        self._tools[tool_id] = (func, bound_declaration, metadata)
        self._tool_names[derived_name] = tool_id
        if approval_summary is not None:
            self._summarisers[tool_id] = approval_summary

    def tool_id_for_name(self, name: str) -> str | None:
        """The tool id behind a model-facing tool name (`a__b` for `a.b`), or None."""
        return self._tool_names.get(name)

    def summary_for(self, tool_id: str, args: dict) -> str | None:
        """The approval summary for a call, or None when the tool has no summariser."""
        if tool_id not in self._tools:
            raise UnknownToolError(tool_id)
        summariser = self._summarisers.get(tool_id)
        return summariser(args) if summariser is not None else None

    def get(self, tool_id: str) -> Callable:
        """Get a registered tool by ID."""
        if tool_id not in self._tools:
            raise UnknownToolError(tool_id)
        return self._tools[tool_id][0]

    def get_metadata(self, tool_id: str) -> dict:
        """Get metadata for a registered tool."""
        if tool_id not in self._tools:
            raise UnknownToolError(tool_id)
        return self._tools[tool_id][2]

    def get_declaration(self, tool_id: str) -> ToolDeclaration:
        """Get the declaration bound to a registered built-in tool."""
        if tool_id not in self._tools:
            raise UnknownToolError(tool_id)
        declaration = self._tools[tool_id][1]
        if declaration is None:
            raise ValueError(f"Tool '{tool_id}' was registered without a declaration")
        return declaration

    def tool_ids(self) -> set[str]:
        """Return all registered tool IDs."""
        return set(self._tools.keys())
