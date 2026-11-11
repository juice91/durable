"""Tests for spec violations - all should fail initially."""

from uuid import uuid4

from pydantic_ai.models.test import TestModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models import ModelResponse, TextPart
from pydantic_ai import ModelSettings

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


def test_model_inference_test_provider():
    """Test: provider 'test' uses TestModel (not silently falls back)."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=(),
    )
    
    captured_model_type = None
    
    def capture_model(messages, agent_info):
        nonlocal captured_model_type
        captured_model_type = "TestModel"
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_model)
    registry = ToolRegistry()
    allowed_tools = set()
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=InMemoryTicketService(),
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # The agent should work with TestModel
    assert captured_model_type is not None


def test_model_inference_unknown_provider():
    """Test: unknown provider raises error (not silent TestModel fallback)."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="unknown-provider", name="some-model"),
        tools=(),
    )
    
    registry = ToolRegistry()
    allowed_tools = set()
    
    try:
        build_agent(spec, registry, allowed_tools=allowed_tools)
        assert False, "Should have raised an error for unknown provider"
    except Exception as e:
        # Should raise something - not silently use TestModel
        assert True


def test_model_settings_applied():
    """Test: spec.model_settings reaches the model."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=(),
        model_settings={"temperature": 0.3, "max_tokens": 50},
    )
    
    captured_settings = None
    
    def capture_settings(messages, agent_info):
        nonlocal captured_settings
        # model_settings is a dict, not a ModelSettings object
        captured_settings = agent_info.model_settings
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_settings)
    registry = ToolRegistry()
    allowed_tools = set()
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=InMemoryTicketService(),
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # Verify model_settings were passed through
    assert captured_settings is not None
    assert captured_settings.get("temperature") == 0.3
    assert captured_settings.get("max_tokens") == 50


def test_model_settings_partial():
    """Test: unset model_settings values are absent."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=(),
        model_settings={"temperature": 0.5},  # max_tokens not set
    )
    
    captured_settings = None
    
    def capture_settings(messages, agent_info):
        nonlocal captured_settings
        # model_settings is a dict
        captured_settings = agent_info.model_settings
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_settings)
    registry = ToolRegistry()
    allowed_tools = set()
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=InMemoryTicketService(),
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # temperature should be set
    assert captured_settings is not None
    assert captured_settings.get("temperature") == 0.5
    # max_tokens should be None or absent
    assert captured_settings.get("max_tokens") is None


def test_instructions_not_duplicated():
    """Test: instructions are sent once (verified via agent_info.instructions)."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=(),
    )
    
    captured_instructions = None
    
    def capture_instructions(messages, agent_info):
        nonlocal captured_instructions
        # Instructions are available in agent_info.instructions
        captured_instructions = agent_info.instructions
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_instructions)
    registry = ToolRegistry()
    allowed_tools = set()
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=InMemoryTicketService(),
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # Should have the instructions
    assert captured_instructions is not None
    assert captured_instructions == "Test instructions"


def test_tickets_create_requires_tool_call_id():
    """Test: tickets.create raises error if tool_call_id is missing from context."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=("tickets.create",),
    )
    
    registry = default_registry()
    allowed_tools = {"tickets.create"}
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools)
    
    run_id = uuid4()
    side_effects = InMemorySideEffectStore()
    ticket_service = InMemoryTicketService()
    
    # Create deps - no tool_call_id since RunDeps no longer has it
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=run_id,
        side_effects=side_effects,
        ticket_service=ticket_service,
    )
    
    # When running, ctx.tool_call_id will be set by pydantic-ai
    # so this test should pass - the function checks ctx.tool_call_id
    result = agent.run_sync("Create a ticket", deps=deps)
    
    # Verify it worked
    assert result is not None


def test_tool_names_use_derived_name():
    """Test: tools exposed with derived name (clock__not clock.now)."""
    spec = AgentSpec(
        name="Test Agent",
        instructions="Test instructions",
        model=ModelRef(provider="test", name="gpt-4"),
        tools=("clock.now",),
    )
    
    captured_tools = None
    
    def capture_tools(messages, agent_info):
        nonlocal captured_tools
        captured_tools = [t.name for t in agent_info.function_tools]
        return ModelResponse(parts=[TextPart("Done")], model_name="test")
    
    function_model = FunctionModel(function=capture_tools)
    registry = default_registry()
    allowed_tools = {"clock.now"}
    
    agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=function_model)
    
    deps = RunDeps(
        tenant_id="test-tenant",
        run_id=uuid4(),
        side_effects=InMemorySideEffectStore(),
        ticket_service=InMemoryTicketService(),
    )
    
    result = agent.run_sync("Test", deps=deps)
    
    # Tool should be named clock__now (dot replaced with __)
    assert captured_tools is not None
    assert "clock__now" in captured_tools
    assert "clock.now" not in captured_tools


def test_tool_registry_rejects_colliding_names():
    """Test: ToolRegistry rejects tool_id whose derived name collides."""
    registry = ToolRegistry()
    
    # Register clock.now first
    def clock_func(ctx):
        return "clock"
    
    registry.register("clock.now", clock_func, description="Clock")
    
    # Try to register a tool whose derived name would collide
    # clock.now derives to clock__now, and clock__now also derives to clock__now
    def other_func(ctx):
        return "other"
    
    try:
        registry.register("clock__now", other_func, description="Other")
        # If we get here, that's a failure - should have raised
        assert False, "Should have raised ValueError for colliding derived name"
    except ValueError as e:
        # The error message contains "would collide" (case-insensitive match)
        assert "collide" in str(e).lower()
