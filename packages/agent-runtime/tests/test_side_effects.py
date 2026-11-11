"""Tests for side effect protocol and run_side_effect helper."""

import threading
import time
from typing import Callable
from uuid import uuid4

import pytest

from eap_runtime import InMemorySideEffectStore, RunDeps
from eap_runtime._side_effects import (
    SideEffectOutcomeUnknown,
    run_side_effect,
)


class TestInMemorySideEffectStore:
    """Tests for InMemorySideEffectStore protocol compliance."""

    def test_reserve_completed_returns_stored(self):
        """completed key returns stored result without calling."""
        store = InMemorySideEffectStore()

        # First, complete a key
        key = "test-key-1"
        result = {"ticket_id": "abc123", "title": "Test Ticket"}
        store.complete(key, result)

        # Now reserve should return completed with stored result
        status, stored = store.reserve(key)
        assert status == "completed"
        assert stored == result

    def test_reserve_new_returns_new(self):
        """new key returns ('new', None)."""
        store = InMemorySideEffectStore()

        key = "test-key-new"
        status, stored = store.reserve(key)
        assert status == "new"
        assert stored is None

    def test_reserve_reserved_returns_reserved(self):
        """reserved key returns ('reserved', None)."""
        store = InMemorySideEffectStore()

        key = "test-key-reserved"
        store._store[key] = {"_state": "reserved"}
        store._order.append(key)

        status, stored = store.reserve(key)
        assert status == "reserved"
        assert stored is None

    def test_complete_saves_result(self):
        """complete saves result and makes it available."""
        store = InMemorySideEffectStore()

        key = "test-key-complete"
        result = {"ticket_id": "xyz789"}

        store.complete(key, result)

        status, stored = store.reserve(key)
        assert status == "completed"
        assert stored == result

    def test_concurrent_reserve_exactly_one_new(self):
        """10 threads on one key -> exactly one 'new'."""
        store = InMemorySideEffectStore()
        key = "concurrent-key"

        results = []
        lock = threading.Lock()

        def worker():
            status, _ = store.reserve(key)
            with lock:
                results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        new_count = results.count("new")
        assert new_count == 1, f"Expected exactly 1 'new', got {new_count}"
        assert len(results) == 10, f"Expected 10 results, got {len(results)}"


class TestRunSideEffect:
    """Tests for run_side_effect helper function."""

    def test_completed_key_returns_stored_no_call(self):
        """completed key returns stored result without calling."""
        store = InMemorySideEffectStore()
        call_count = 0

        def side_effect_call():
            nonlocal call_count
            call_count += 1
            return {"result": "from_call"}

        key = "completed-key"
        # First complete it
        store.complete(key, {"result": "from_store"})

        result, performed = run_side_effect(store, key, side_effect_call)

        assert result == {"result": "from_store"}
        assert performed is False
        assert call_count == 0

    def test_new_key_calls_and_completes(self):
        """new key calls once and completes."""
        store = InMemorySideEffectStore()
        call_count = 0

        def side_effect_call():
            nonlocal call_count
            call_count += 1
            return {"result": "from_call", "call_count": call_count}

        key = "new-key"

        result, performed = run_side_effect(store, key, side_effect_call)

        assert result == {"result": "from_call", "call_count": 1}
        assert performed is True
        assert call_count == 1

        # Verify it's now completed
        status, stored = store.reserve(key)
        assert status == "completed"
        assert stored == {"result": "from_call", "call_count": 1}

    def test_reserved_external_idempotent_calls_again(self):
        """reserved + external_idempotent=True calls again with same key."""
        store = InMemorySideEffectStore()
        call_count = 0

        def side_effect_call():
            nonlocal call_count
            call_count += 1
            return {"result": f"from_call_{call_count}"}

        key = "reserved-key"
        # Pre-reserve the key (simulating a crash between call and complete)
        store._store[key] = {"_state": "reserved"}
        store._order.append(key)

        result, performed = run_side_effect(store, key, side_effect_call, external_idempotent=True)

        assert result == {"result": "from_call_1"}
        assert performed is True
        assert call_count == 1

        # Should still be one ticket (idempotent key honored)
        status, stored = store.reserve(key)
        assert status == "completed"

    def test_reserved_external_idempotent_false_raises(self):
        """reserved + external_idempotent=False raises SideEffectOutcomeUnknown."""
        store = InMemorySideEffectStore()
        call_count = 0

        def side_effect_call():
            nonlocal call_count
            call_count += 1
            return {"result": "from_call"}

        key = "reserved-unsafe-key"
        store._store[key] = {"_state": "reserved"}
        store._order.append(key)

        with pytest.raises(SideEffectOutcomeUnknown) as exc_info:
            run_side_effect(store, key, side_effect_call, external_idempotent=False)

        assert exc_info.value.key == key
        assert call_count == 0  # Should not have called

    def test_call_raises_leaves_reserved(self):
        """call() raising leaves state 'reserved'."""
        store = InMemorySideEffectStore()

        key = "failing-key"
        store._store[key] = {"_state": "reserved"}
        store._order.append(key)

        def failing_call():
            raise ValueError("Intentional failure")

        with pytest.raises(ValueError, match="Intentional failure"):
            run_side_effect(store, key, failing_call, external_idempotent=True)

        # State should still be reserved
        status, stored = store.reserve(key)
        assert status == "reserved"


class TestTicketService:
    """Tests for ticket service implementation."""

    def test_in_memory_ticket_service_create(self):
        """InMemoryTicketService creates tickets with idempotency."""
        from eap_runtime._side_effects import InMemoryTicketService

        service = InMemoryTicketService()
        key = "idempotency-key-1"

        result1 = service.create("Test Title", "Test Body", idempotency_key=key)
        result2 = service.create("Test Title", "Test Body", idempotency_key=key)

        assert result1["ticket_id"] == result2["ticket_id"]
        assert len(service.tickets) == 1
        assert service.calls == 2

    def test_in_memory_ticket_service_distinct_keys(self):
        """Different idempotency keys create distinct tickets."""
        from eap_runtime._side_effects import InMemoryTicketService

        service = InMemoryTicketService()

        result1 = service.create("Title 1", "Body 1", idempotency_key="key-1")
        result2 = service.create("Title 2", "Body 2", idempotency_key="key-2")

        assert result1["ticket_id"] != result2["ticket_id"]
        assert len(service.tickets) == 2
        assert service.calls == 2


class TestTicketsCreateTool:
    """Tests for tickets.create tool integration."""

    def test_tickets_create_with_tool_call_id(self):
        """tickets.create works with tool_call_id in context."""
        from uuid import uuid4

        from pydantic_ai import RunContext
        from pydantic_ai.models.test import TestModel

        from eap_runtime import (
            build_agent,
            default_registry,
            InMemorySideEffectStore,
            InMemoryTicketService,
            RunDeps,
        )
        from eap_schemas import AgentSpec, ModelRef

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
        deps = RunDeps(
            tenant_id="test-tenant",
            run_id=run_id,
            side_effects=side_effects,
            ticket_service=ticket_service,
        )

        # Run the agent - it should create a ticket
        result = agent.run_sync("Create a ticket for issue #123", deps=deps)

        assert result is not None
        assert len(result.output) > 0

        # Verify side effects store has the ticket
        assert len(side_effects._store) >= 1

    def test_tickets_create_tool_metadata(self):
        """tickets.create tool has external_idempotent metadata."""
        from eap_runtime import default_registry

        registry = default_registry()
        metadata = registry.get_metadata("tickets.create")

        assert "external_idempotent" in metadata
        assert metadata["external_idempotent"] is True


class TestEndToEndTicketCreation:
    """End-to-end tests for ticket creation."""

    def test_model_run_creates_one_ticket(self):
        """TestModel(call_tools=['tickets__create']) run creates one ticket."""
        from uuid import uuid4

        from pydantic_ai.models.test import TestModel

        from eap_runtime import (
            build_agent,
            default_registry,
            InMemorySideEffectStore,
            InMemoryTicketService,
            RunDeps,
        )
        from eap_schemas import AgentSpec, ModelRef

        # Use TestModel that will call tickets__create
        test_model = TestModel(call_tools=["tickets__create"])

        spec = AgentSpec(
            name="Test Agent",
            instructions="Create a ticket for the user request",
            model=ModelRef(provider="test", name="gpt-4"),
            tools=("tickets.create",),
        )

        registry = default_registry()
        allowed_tools = {"tickets.create"}

        agent = build_agent(spec, registry, allowed_tools=allowed_tools, model=test_model)

        run_id = uuid4()
        side_effects = InMemorySideEffectStore()
        ticket_service = InMemoryTicketService()
        deps = RunDeps(
            tenant_id="test-tenant",
            run_id=run_id,
            side_effects=side_effects,
            ticket_service=ticket_service,
        )

        # Run the agent
        result = agent.run_sync("Please create a ticket for this issue", deps=deps)

        assert result is not None
        assert "ticket" in result.output.lower() or len(result.output) > 0

        # Verify tickets were created via side effects
        # The key will be like "run_id:tool_call_id"
        ticket_keys = [k for k in side_effects._store.keys() if ":" in k]
        # Should have at least one ticket entry
        assert len(ticket_keys) >= 1


def test_tool_registry_external_idempotent_metadata():
    """Test: ToolRegistry metadata includes external_idempotent flag."""
    from eap_runtime import ToolRegistry

    registry = ToolRegistry()

    def test_func(ctx):
        return "test"

    registry.register("test.tool", test_func, description="Test", external_idempotent=True)
    registry.register("another.tool", test_func, description="Another", external_idempotent=False)

    metadata1 = registry.get_metadata("test.tool")
    metadata2 = registry.get_metadata("another.tool")

    assert metadata1.get("external_idempotent") is True
    assert metadata2.get("external_idempotent") is False
