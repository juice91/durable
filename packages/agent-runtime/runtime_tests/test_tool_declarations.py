"""Runtime bindings for shared tool declarations."""

from pydantic_ai.tools import Tool

import pytest

from eap_runtime import ToolRegistry, default_registry
from eap_schemas.tools import ClockNowInput, TOOL_DECLARATIONS, ToolDeclaration

# Declared here, bound by the worker (eap_worker.platform_tools; eap_worker.kb_source_tools; eap_worker.http_tool): they read the
# tenant's secret store, the knowledge-base source tables and the run rows.
WORKER_BOUND_TOOL_IDS = frozenset({
    "secrets.request", "http.request", "kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait",
    "workflow.run.result", "feed.parse", "json.query", "html.extract", "text.check",
})


def test_default_implementations_match_declared_input_schemas() -> None:
    """Changing a built-in function signature cannot silently drift from its declaration."""
    registry = default_registry()

    for tool_id, declaration in TOOL_DECLARATIONS.items():
        if tool_id in WORKER_BOUND_TOOL_IDS:
            continue
        implementation_schema = Tool(
            registry.get(tool_id),
            name=tool_id.replace(".", "__"),
            description=declaration.description,
        ).tool_def.parameters_json_schema

        assert implementation_schema == declaration.input_json_schema()


def test_default_registry_uses_shared_declaration_metadata() -> None:
    """The runtime binding retains the exact shared declaration and its safety flags."""
    registry = default_registry()

    for tool_id, declaration in TOOL_DECLARATIONS.items():
        if tool_id in WORKER_BOUND_TOOL_IDS:
            continue
        assert registry.get_declaration(tool_id) is declaration
        assert registry.get_metadata(tool_id) == {
            "description": declaration.description,
            "side_effect": declaration.side_effect,
            "external_idempotent": declaration.external_idempotent,
        }


def test_registry_rejects_an_implementation_that_disagrees_with_its_declaration() -> None:
    """A declaration-backed registration cannot accept a drifted function signature."""
    declaration = ToolDeclaration(
        id="test.mismatch",
        title="Mismatch",
        description="Used to prove registration validation",
        side_effect=False,
        external_idempotent=False,
        input_model=ClockNowInput,
    )

    def implementation(required_value: str) -> str:
        return required_value

    with pytest.raises(ValueError, match="does not match its declaration"):
        ToolRegistry().register(declaration, implementation)
