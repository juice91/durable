"""The explainer's contract: graphemes, caps, validation."""

import pytest
from pydantic import ValidationError

from eap_schemas.workflow_explanations import (
    clip_graphemes, explainer_agent_spec, explainer_output_schema, finalize_explanation, graphemes,
)


def test_graphemes_count_user_perceived_characters():
    assert graphemes("繁體中文abc") == 7
    assert graphemes("é") == 1  # e + combining acute
    assert graphemes("👨‍👩‍👧") == 1  # a ZWJ family
    assert graphemes("👍🏽") == 1
    assert clip_graphemes("字" * 10, 4) == "字" * 4 and clip_graphemes("short", 50) == "short"


def test_finalize_cuts_filters_and_refuses():
    body, dropped = finalize_explanation(
        {"summary": " s ", "inputs": [], "outcomes": [],
         "steps": [{"id": "a", "note": "n" * 400}, {"id": "a", "note": "again"}, {"id": "zz", "note": "no"}]},
        {"a", "b"},
    )
    assert body["summary"] == "s" and body["steps"] == [{"id": "a", "note": "n" * 160}] and dropped == 2
    with pytest.raises(ValidationError):
        finalize_explanation({"summary": "x"}, {"a"})
    with pytest.raises(ValidationError):
        finalize_explanation({"summary": "x", "inputs": [], "outcomes": [], "steps": [], "extra": 1}, set())


def test_agent_spec_is_tool_mode_without_tools():
    spec = explainer_agent_spec("test:echo")
    assert not spec.tools and spec.output.mode == "tool" and spec.output.json_schema == explainer_output_schema()
