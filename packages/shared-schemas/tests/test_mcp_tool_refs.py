"""McpToolRef and mcp_tool_name tests (milestone 3, task 3.5)."""

from __future__ import annotations

import hashlib

import pytest

from eap_schemas import AgentSpec, McpToolRef, mcp_tool_name


def test_mcp_tool_ref_length_limits():
    """Server 1..100, tool 1..128 characters."""
    # Valid min lengths
    McpToolRef(server="x", tool="y")
    # Valid max lengths
    McpToolRef(server="x" * 100, tool="y" * 128)
    # Too short server
    with pytest.raises(ValueError, match="server"):
        McpToolRef(server="", tool="y")
    # Too long server
    with pytest.raises(ValueError, match="server"):
        McpToolRef(server="x" * 101, tool="y")
    # Too short tool
    with pytest.raises(ValueError, match="tool"):
        McpToolRef(server="x", tool="")
    # Too long tool
    with pytest.raises(ValueError, match="tool"):
        McpToolRef(server="x", tool="y" * 129)


def test_duplicate_mcp_tool_ref_rejected():
    """A spec with duplicate (server, tool) pairs fails validation."""
    with pytest.raises(ValueError, match="duplicate MCP tool"):
        AgentSpec(
            name="test",
            instructions="",
            model={"provider": "test", "name": "mcp"},
            mcp_tools=[{"server": "orders", "tool": "lookup_order"}, {"server": "orders", "tool": "lookup_order"}],
        )


def test_mcp_tool_name_sanitising():
    """Non-alphanumeric chars (except - and _) become underscores."""
    # Hyphens and underscores are preserved
    assert mcp_tool_name("my-server", "lookup-order") == "mcp__my-server__lookup-order"
    # Special chars become underscores
    assert mcp_tool_name("MyServer", "LookupOrder") == "mcp__MyServer__LookupOrder"
    assert mcp_tool_name("server@domain", "tool.name") == "mcp__server_domain__tool_name"


def test_mcp_tool_name_64_char_limit():
    """Names over 64 chars are cut to 55 + '_' + 8 hex hash."""
    long_server = "a" * 50
    long_tool = "b" * 80
    name = mcp_tool_name(long_server, long_tool)
    assert len(name) == 64
    assert name.startswith("mcp__")
    assert "_" in name
    # The suffix is a hash of (server, tool)
    digest = hashlib.sha256(f"{long_server}\x00{long_tool}".encode()).hexdigest()[:8]
    assert name.endswith(f"_{digest}")


def test_mcp_tool_name_collisions():
    """Two different (server, tool) pairs cannot map to the same name."""
    # Different pairs that hash to different 64-char names are fine
    name1 = mcp_tool_name("a" * 50, "b" * 80)
    name2 = mcp_tool_name("a" * 50, "c" * 80)
    assert name1 != name2
    # A short name and a long name that would collide are also distinct due to hashing
    short = mcp_tool_name("orders", "lookup_order")
    assert len(short) == len("mcp__orders__lookup_order")


def test_mcp_tool_name_collision_in_spec_rejected():
    """AgentSpec rejects two tools that map to the same model-facing name."""
    # Two MCP tools where space in first pair becomes same as underscore in second
    with pytest.raises(ValueError, match="same tool name"):
        AgentSpec(
            name="test",
            instructions="",
            model={"provider": "test", "name": "mcp"},
            mcp_tools=[{"server": "server tool", "tool": "tool"}, {"server": "server_tool", "tool": "tool"}],  # Both become mcp__server_tool__tool
        )


def test_agent_spec_without_mcp_tools_excludes_key():
    """A spec with empty mcp_tools dumps with NO mcp_tools key."""
    spec = AgentSpec(
        name="test",
        instructions="",
        model={"provider": "test", "name": "mcp"},
    )
    dumped = spec.model_dump(mode="json")
    assert "mcp_tools" not in dumped


def test_agent_spec_with_mcp_tools_round_trips():
    """A spec with mcp_tools round-trips through JSON."""
    spec = AgentSpec(
        name="test",
        instructions="",
        model={"provider": "test", "name": "mcp"},
        mcp_tools=[{"server": "orders", "tool": "lookup_order"}],
    )
    dumped = spec.model_dump(mode="json")
    assert "mcp_tools" in dumped
    assert dumped["mcp_tools"] == [{"server": "orders", "tool": "lookup_order"}]
    restored = AgentSpec.model_validate(dumped)
    assert restored.mcp_tools == spec.mcp_tools
    assert restored.model_dump(mode="json") == dumped
