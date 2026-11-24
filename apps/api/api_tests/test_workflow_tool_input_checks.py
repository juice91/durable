"""Publish-time check of tool step inputs (eap_run_store.workflow_checks.tool_input_problems): a structurally
invalid input is refused, every value a marker supplies is left alone. Pure: no database."""

import json
import re
import uuid
from pathlib import Path

import pytest

from eap_run_store.workflow_checks import tool_input_problems
from eap_schemas.workflows import WorkflowSpec

ROOT = Path(__file__).resolve().parents[3]
HEAD = {"id": "rejects", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "parse_feed"}


def problems(input_, tool="feed.parse"):
    step = {"id": "parse_feed", "type": "tool", "tool_id": tool, "input": input_}
    spec = WorkflowSpec.model_validate({
        "name": "t", "input_schema": {"type": "object", "properties": {"u": {"type": "string"}}},
        "start": "rejects", "steps": [HEAD, step],
    })
    return tool_input_problems(spec)


FEED = {"text": "<rss/>", "label": "A"}


def test_the_incident_input_is_accepted():
    assert problems({"feeds": [FEED], "exclude_urls": {"$template": "{{ steps.rejects.output.result }}"}}) == []


@pytest.mark.parametrize("input_", [
    {"text": {"$ref": "input.u"}},                                       # a whole field
    {"text": "x", "max_items": {"$ref": "input.u"}},                     # a type a string would break
    {"feeds": [{"text": {"$ref": "input.u"}, "label": "A"}]},            # nested, in a list
    {"feeds": [{"text": "x", "label": {"$template": "{{input.u}}"}}]},
    {"feeds": {"$ref": "input.u"}},                                      # the whole list is a marker
    {"feeds": [{"$ref": "input.u"}]},                                    # a whole list item
    {"text": "x", "exclude_urls": [{"$ref": "input.u"}, "https://x.example"]},
])
def test_a_marker_is_never_refused_for_its_type(input_):
    assert problems(input_) == []


@pytest.mark.parametrize("input_, where", [
    ({"feeds": [{"text": "x"}]}, "feeds.0.label"),                       # missing field next to nothing marked
    ({"feeds": [{"text": {"$ref": "input.u"}}]}, "feeds.0.label"),       # missing field next to a marker
    ({"text": "x", "nope": {"$ref": "input.u"}}, "nope"),                # an unknown field holding a marker
    ({"text": "x", "max_items": "many"}, "max_items"),
    ({"text": 5}, "text"),
])
def test_an_error_outside_a_marker_is_refused(input_, where):
    found = problems(input_)
    assert found and all("parse_feed" in p for p in found)
    assert any(f"'{where}'" in p for p in found), found


def test_an_unknown_tool_is_left_to_the_reference_check():
    assert problems({"x": 1}, tool="no.such.tool") == []


@pytest.mark.parametrize("path", sorted((ROOT / "examples" / "workflows").glob("*.json")), ids=lambda p: p.stem)
def test_every_example_workflow_has_valid_tool_inputs(path):
    text = re.sub(r"\$\{agents\.[a-z0-9_-]+\}", lambda _m: str(uuid.uuid4()), path.read_text())
    spec = WorkflowSpec.model_validate(json.loads(text)["spec"])
    assert tool_input_problems(spec) == []
