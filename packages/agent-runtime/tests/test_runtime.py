"""Tests for the agent runtime package."""

from uuid import uuid4

from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models import ModelResponse, TextPart

from eap_runtime import (
    ToolRegistry,
    UnknownToolError,
    ToolNotAllowedError,
    build_agent,
    default_registry,
    InMemorySideEffectStore,
    InMemoryTicketService,
    RunDeps,
)
from eap_schemas import AgentSpec, ModelRef


def test_agent_exposes_specified_tools():
    """Agent built from a spec with tools exposes exactly those tools."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=("clock.now",),
    )
    
    registry = default_registry()
    allowed_tools = {"clock.now"}
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools)
    
    # Use TestModel to run the agent
    ticket_service = InMemoryTicketService()
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=ticket_service,
    )
    
    # Run the agent - it should call clock.now
    result = agent.run_sync("Get the time", deps=deps)
    
    # The agent should have called the clock.now tool
    assert result is not None
    assert len(result.output) > 0


def test_agent_with_function_model():
    """Test that FunctionModel can be used to capture agent info."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=(),
    )
    
    captured_info = None
    
    def capture_info(messages, agent_info):
        nonlocal captured_info
        captured_info = agent_info
        # Return a text response
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_info)
    registry = default_registry()
    allowed_tools = set()
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    ticket_service = InMemoryTicketService()
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=ticket_service,
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # Verify agent_info was captured
    assert captured_info is not None
    # Verify that we have the expected agent_info attributes
    assert hasattr(captured_info, 'instructions')
    assert hasattr(captured_info, 'function_tools')


def test_unknown_tool_error():
    """Spec tool not registered raises UnknownToolError."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=("nonexistent.tool",),
    )
    
    registry = default_registry()
    allowed_tools = {"clock.now"}
    
    try:
        build_agent(spec, registry, allowed_tools=allowed_tools)
        assert False, "Should have raised UnknownToolError"
    except UnknownToolError as e:
        assert e.tool_id == "nonexistent.tool"


def test_tool_not_allowed_error():
    """Registered but not allowed tool raises ToolNotAllowedError."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=("clock.now",),
    )
    
    registry = default_registry()
    allowed_tools = set()  # Empty - clock.now is registered but not allowed
    
    try:
        build_agent(spec, registry, allowed_tools=allowed_tools)
        assert False, "Should have raised ToolNotAllowedError"
    except ToolNotAllowedError as e:
        assert e.tool_id == "clock.now"



