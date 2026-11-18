"""`json.query` (eap_worker.json_query_tool): a JMESPath expression over JSON data. Pure."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, JsonQueryInput
from eap_worker.json_query_tool import json_query

DATA = {"items": [{"id": "c1", "title": "A", "n": 1}, {"id": "c2", "title": "B", "n": 2}, {"id": "c3", "title": "C", "n": 3}],
        "feed": [{"post": {"record": {"text": "t1", "embed": {"external": {"uri": "u1"}}}}},
                 {"post": {"record": {"text": "t2"}}}]}


def query(expression, data=DATA):
    return json_query(None, data=data, expression=expression)


def test_projection():
    assert query("items[].title") == {"result": ["A", "B", "C"]}


def test_filter_and_pipe():
    assert query("items[?id=='c3'] | [0]") == {"result": {"id": "c3", "title": "C", "n": 3}}


def test_multiselect_hash_with_nulls_for_missing_paths():
    out = query("{links: feed[].post.record.embed.external.uri, titles: feed[].post.record.text}")
    assert out == {"result": {"links": ["u1"], "titles": ["t1", "t2"]}}


def test_a_null_result_is_a_result():
    assert query("items[?id=='zz'] | [0]") == {"result": None}
    assert query("nothing.here") == {"result": None}


def test_an_invalid_expression_is_reported_without_echoing_data():
    for bad in ("items[", "items[?id==", "", "{a:"):
        out = query(bad)
        assert out["error"]["code"] == "expression_invalid"


def test_a_runtime_type_error_in_a_function_is_expression_invalid():
    assert query("length(`1`)")["error"]["code"] == "expression_invalid"


def test_a_result_over_256_kib_is_too_large():
    data = {"rows": ["x" * 1000] * 300}
    assert query("rows", data)["error"]["code"] == "result_too_large"
    assert query("length(rows)", data) == {"result": 300}


def test_input_limits():
    with pytest.raises(ValidationError):
        JsonQueryInput(data={}, expression="a" * 501)
    with pytest.raises(ValidationError):
        JsonQueryInput(data="x" * (1024 * 1024 + 1), expression="@")
    assert TOOL_DECLARATIONS["json.query"].side_effect is False
