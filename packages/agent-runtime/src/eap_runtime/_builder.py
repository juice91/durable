"""Agent builder and utilities."""

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models import infer_model

from eap_runtime._registry import (
    ToolRegistry,
    ToolNotAllowedError,
    UnknownToolError,
)
from eap_runtime._types import RunDeps
from eap_schemas import AgentSpec


def build_agent(
    spec: AgentSpec,
    registry: ToolRegistry,
    *,
    allowed_tools: set[str],
    model: Any = None,
) -> Agent[RunDeps, str]:
    """Build a Pydantic AI agent from an AgentSpec."""
    # Validate all spec tools are registered
    for tool_id in spec.tools:
        if tool_id not in registry.tool_ids():
            raise UnknownToolError(tool_id)
        if tool_id not in allowed_tools:
            raise ToolNotAllowedError(tool_id)

    # Build the agent
    if model is None:
        # Use spec.model.provider to determine the model
        provider = spec.model.provider
        if provider == "test":
            from pydantic_ai.models.test import TestModel
            model_instance = TestModel()
        else:
            # Try to infer the model from the provider:name string
            model_str = f"{provider}:{spec.model.name}"
            try:
                model_instance = infer_model(model_str)
            except Exception:
                raise ValueError(f"Unknown model provider: {provider}")
    else:
        model_instance = model

    # Create instructions from spec
    instructions = spec.instructions

    # Build model_settings from spec (only include non-None values)
    model_settings_dict = {}
    if spec.model_settings.temperature is not None:
        model_settings_dict["temperature"] = spec.model_settings.temperature
    if spec.model_settings.max_tokens is not None:
        model_settings_dict["max_tokens"] = spec.model_settings.max_tokens
    
    from pydantic_ai import ModelSettings
    model_settings = ModelSettings(**model_settings_dict) if model_settings_dict else None

    # Create the agent - use instructions only, not system_prompt
    agent = Agent(
        model_instance,
        deps_type=RunDeps,
        instructions=instructions,
        model_settings=model_settings,
        tools=[],
    )

    # Register tools with the agent, using derived name
    for tool_id in spec.tools:
        func = registry.get(tool_id)
        metadata = registry.get_metadata(tool_id)
        description = metadata.get("description", "")
        # Derive tool name: replace '.' with '__'
        tool_name = tool_id.replace(".", "__")
        agent.tool(
            func,
            name=tool_name,
            description=description,
        )

    return agent
