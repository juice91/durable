"""`$ref` / `$template` markers at any depth of a step input: validation and the refs they read."""

import pytest
from pydantic import ValidationError

from eap_schemas.workflows import WorkflowSpec, iter_refs

HEAD = {"id": "rejects", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "parse_feed"}


def spec(input_, *, tool="feed.parse"):
    step = {"id": "parse_feed", "type": "tool", "tool_id": tool, "input": input_}
    return {"name": "t", "input_schema": {"type": "object", "properties": {"u": {"type": "string"}}},
            "start": "rejects", "steps": [HEAD, step]}


def refs(input_):
    return iter_refs(WorkflowSpec.model_validate(spec(input_)).steps[1])


def test_the_incident_input_validates_and_reads_its_ref():
    got = refs({"label": "x", "exclude_urls": {"$template": "{{ steps.rejects.output.result }}"}})
    assert got == ["steps.rejects.output.result"]


@pytest.mark.parametrize("value, expected", [
    ({"a": {"$ref": "input.u"}}, ["input.u"]),                                           # depth 2
    ({"a": {"b": {"$ref": "input.u"}}}, ["input.u"]),                                    # depth 3
    ({"a": {"b": {"$template": "x {{input.u}}"}}}, ["input.u"]),
    ({"a": [{"$ref": "input.u"}, {"k": [{"$template": "{{steps.rejects.output}}"}]}]},
     ["input.u", "steps.rejects.output"]),                                               # in lists
    ({"a": {"$template": {"deep": {"$not": 1}, "s": "{{input.u}}"}}}, ["input.u"]),
])
def test_nested_markers_are_read_in_document_order(value, expected):
    assert refs(value) == expected


@pytest.mark.parametrize("bad", [
    {"a": {"b": {"$ref": "input.u", "extra": 1}}},
    {"a": [{"$template": "x", "extra": 1}]},
    {"a": {"$ref": "input.u", "$template": "x"}},
    {"a": {"$ref": 3}},
    {"a": {"b": {"$ref": "nope.x"}}},
    {"a": [{"$template": "{{ nope.x }}"}]},
])
def test_a_malformed_nested_marker_is_refused(bad):
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(spec(bad))


def test_a_nested_ref_to_a_later_step_is_refused_like_a_top_level_one():
    late = {"id": "a", "type": "tool", "tool_id": "clock.now", "next": "b",
            "input": {"x": {"y": [{"$ref": "steps.b.output"}]}}}
    with pytest.raises(ValidationError, match="does not run before"):
        WorkflowSpec.model_validate({
            "name": "t", "input_schema": {"type": "object"}, "start": "a",
            "steps": [late, {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}}],
        })


def test_nesting_depth_is_still_bounded():
    deep: object = {"$ref": "input.u"}
    for _ in range(40):
        deep = [deep]
    with pytest.raises(ValidationError, match="nested"):
        WorkflowSpec.model_validate(spec({"a": deep}))
