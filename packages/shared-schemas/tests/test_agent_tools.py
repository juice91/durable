"""Agents as tools: AgentToolRef / AgentSpec.agent_tools (eap_schemas.agent_tools)."""

import uuid

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, canonical_spec_hash
from eap_schemas.agent_tools import (
    AgentToolRef,
    MAX_AGENT_TOOLS_PER_AGENT,
    agent_tool_child_run_id,
    agent_tool_default_name,
)

A = "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab"
B = "7a1d6f6f-7e1b-4f1f-8e2f-1234567890bc"
MODEL = {"provider": "test", "name": "echo"}


def spec(**extra):
    return AgentSpec(name="Boss", model=MODEL, **extra)


def test_a_spec_without_refs_keeps_its_json_and_hash():
    plain = spec()
    assert "agent_tools" not in plain.model_dump(mode="json")
    # The hash of a spec stored before the field existed (same keys, same values) is unchanged.
    assert canonical_spec_hash(plain) == canonical_spec_hash(AgentSpec.model_validate(plain.model_dump(mode="json")))


def test_defaults_are_left_out_of_every_dump():
    ref = AgentToolRef(agent_id=A)
    assert ref.model_dump(mode="json") == {"agent_id": A}
    full = AgentToolRef(agent_id=A, name="helper", description="Help.", version=3, timeout_seconds=60, max_calls_per_run=2)
    assert full.model_dump(mode="json") == {
        "agent_id": A, "name": "helper", "description": "Help.", "version": 3, "timeout_seconds": 60,
        "max_calls_per_run": 2,
    }
    # explicit defaults dump like omitted ones: the hash does not depend on how the spec was written
    explicit = AgentToolRef(agent_id=A, version="latest", timeout_seconds=300, max_calls_per_run=5)
    assert explicit.model_dump(mode="json") == {"agent_id": A}
    a = spec(agent_tools=[{"agent_id": A}])
    b = spec(agent_tools=[{"agent_id": A, "version": "latest", "timeout_seconds": 300, "max_calls_per_run": 5}])
    assert canonical_spec_hash(a) == canonical_spec_hash(b)
    assert canonical_spec_hash(a) != canonical_spec_hash(spec())
    assert a.model_dump(mode="json")["agent_tools"] == [{"agent_id": A}]


def test_a_dump_validates_back_to_an_equal_spec():
    original = spec(agent_tools=[{"agent_id": A, "version": 2}, {"agent_id": B, "name": "other"}])
    again = AgentSpec.model_validate(original.model_dump(mode="json"))
    assert again == original and canonical_spec_hash(again) == canonical_spec_hash(original)


def test_the_agent_id_is_canonicalised_and_must_be_a_uuid():
    assert AgentToolRef(agent_id=A.upper()).agent_id == A
    assert AgentToolRef(agent_id=uuid.UUID(A)).agent_id == A
    with pytest.raises(ValidationError):
        AgentToolRef(agent_id="nope")


@pytest.mark.parametrize("name", ["Upper", "1abc", "has-dash", "", "a" * 42, "with space"])
def test_a_tool_name_must_match_the_pattern(name):
    with pytest.raises(ValidationError):
        AgentToolRef(agent_id=A, name=name)


@pytest.mark.parametrize("name", ["a", "ask_helper", "a" * 41, "h2"])
def test_a_valid_tool_name(name):
    assert AgentToolRef(agent_id=A, name=name).name == name


@pytest.mark.parametrize(
    "bad",
    [{"timeout_seconds": 0}, {"timeout_seconds": 3601}, {"max_calls_per_run": 0}, {"max_calls_per_run": 21},
     {"version": 0}, {"version": -1}, {"version": "oldest"}, {"version": True}, {"extra": 1}],
)
def test_limits_and_versions_are_bounded(bad):
    with pytest.raises(ValidationError):
        AgentToolRef(agent_id=A, **bad)


def test_the_documented_maxima_are_accepted():
    AgentToolRef(agent_id=A, timeout_seconds=3600, max_calls_per_run=20, version=1)


def test_a_blank_description_is_dropped():
    assert AgentToolRef(agent_id=A, description="   ").description is None


def test_each_agent_at_most_once_and_explicit_names_unique():
    with pytest.raises(ValidationError, match="duplicate agent tool"):
        spec(agent_tools=[{"agent_id": A}, {"agent_id": A, "name": "again"}])
    with pytest.raises(ValidationError, match="duplicate agent tool name"):
        spec(agent_tools=[{"agent_id": A, "name": "x"}, {"agent_id": B, "name": "x"}])


def test_at_most_the_maximum_number_of_refs():
    ids = [str(uuid.uuid4()) for _ in range(MAX_AGENT_TOOLS_PER_AGENT + 1)]
    spec(agent_tools=[{"agent_id": i} for i in ids[:MAX_AGENT_TOOLS_PER_AGENT]])
    with pytest.raises(ValidationError, match="at most"):
        spec(agent_tools=[{"agent_id": i} for i in ids])


def test_an_explicit_name_cannot_take_another_tools_model_facing_name():
    with pytest.raises(ValidationError, match="already used"):
        spec(tools=["tickets.create"], agent_tools=[{"agent_id": A, "name": "tickets__create"}])
    with pytest.raises(ValidationError, match="already used"):
        spec(mcp_tools=[{"server": "orders", "tool": "lookup"}], agent_tools=[{"agent_id": A, "name": "mcp__orders__lookup"}])
    with pytest.raises(ValidationError, match="already used"):
        spec(agent_tools=[{"agent_id": A, "name": "retrieve"}])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Research Helper", "research_helper"),
        ("  Café -- Desk!! ", "caf_desk"),
        ("2nd line", "agent_2nd_line"),
        ("x" * 80, "x" * 41),
    ],
)
def test_the_default_name_is_derived_from_the_agent_name(name, expected):
    assert agent_tool_default_name(name, A) == expected
    AgentToolRef(agent_id=A, name=agent_tool_default_name(name, A))  # always a valid name


@pytest.mark.parametrize("name", [None, "", "!!!", "日本語"])
def test_a_name_with_nothing_usable_falls_back_to_the_id(name):
    assert agent_tool_default_name(name, A) == "agent_6f0c5e5e"


def test_the_child_run_id_is_deterministic_and_distinct_per_call():
    run = str(uuid.uuid4())
    one = agent_tool_child_run_id(run, "call_1")
    assert one == agent_tool_child_run_id(run, "call_1")
    assert one != agent_tool_child_run_id(run, "call_2")
    assert one != agent_tool_child_run_id(str(uuid.uuid4()), "call_1")
    uuid.UUID(one)
