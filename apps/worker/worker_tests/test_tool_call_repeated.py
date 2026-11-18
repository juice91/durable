"""A model that repeats an identical, invalid tool call ends the turn with `tool_call_repeated` and a
readable message, not `unexpected_model_behavior`. Pure pydantic-ai: no DBOS step is involved."""

from __future__ import annotations

import pickle

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from eap_worker.capabilities import SpecCapability
from eap_worker.errors import ToolCallRepeated, classify


class Spec(BaseModel):
    name: str


def build(script, calls: list[str]) -> Agent:
    agent = Agent(FunctionModel(script), capabilities=[SpecCapability()])

    @agent.tool_plain
    def save(spec: Spec) -> str:
        calls.append(spec.name)
        return "saved"

    return agent


def test_the_same_invalid_call_twice_stops_the_run_with_a_readable_error():
    sent: list[str] = []

    def script(messages, info: AgentInfo) -> ModelResponse:
        sent.append("call")
        return ModelResponse(parts=[ToolCallPart("save", {"spec": '{"name": 1, "x": }'})])

    executed: list[str] = []
    with pytest.raises(ToolCallRepeated) as raised:
        build(script, executed).run_sync("go")
    assert len(sent) == 2 and executed == []  # stopped at the repeat, not after more retries
    failure = classify(raised.value)
    assert failure.code == "tool_call_repeated"
    assert "save" in failure.message and "Input should be a valid dictionary" in failure.message


def test_a_changed_call_is_a_real_retry_and_a_valid_one_runs():
    step = {"n": 0}

    def script(messages, info: AgentInfo) -> ModelResponse:
        step["n"] += 1
        if step["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("save", {"spec": "nope"})])
        if step["n"] == 2:
            return ModelResponse(parts=[ToolCallPart("save", {"spec": {"name": "ok"}})])
        return ModelResponse(parts=[ToolCallPart("final_result", {"response": "done"})]) if info.output_tools else ModelResponse(parts=[])

    executed: list[str] = []
    agent = Agent(FunctionModel(script), capabilities=[SpecCapability()])

    @agent.tool_plain
    def save(spec: Spec) -> str:
        executed.append(spec.name)
        return "saved"

    try:
        agent.run_sync("go")
    except Exception as exc:  # the scripted last turn may not form a final answer; only the tool run matters
        assert not isinstance(exc, ToolCallRepeated)
    assert executed == ["ok"]


def test_the_error_survives_pickling():
    exc = ToolCallRepeated("save", "spec: Input should be a valid dictionary")
    restored = pickle.loads(pickle.dumps(exc))
    assert (restored.tool, restored.detail) == ("save", "spec: Input should be a valid dictionary")
    assert classify(restored).code == "tool_call_repeated"
