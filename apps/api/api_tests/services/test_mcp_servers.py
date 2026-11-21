"""The MCP registry's pure rules (headers, names, input schema errors) and the tool-error translation of a
service's refusal."""

from __future__ import annotations

import pytest

from eap_api.mcp_control.routing import McpToolError, service_error_to_tool_error, translate
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_api.services.mcp_connection import canonical_json, input_errors, mcp_run_tool_id
from eap_api.services.mcp_runtime import BadGateway, Target
from eap_api.services.mcp_servers import MAX_HEADER_VALUE, _default_name, validate_headers
from eap_api.services.revision_conflict import stale_revision
from eap_run_store.definition_errors import DefinitionInvalid


def test_headers_are_lowercased_and_checked_by_position_never_by_value():
    assert validate_headers({"X-Api": "v"}) == {"x-api": "v"}
    for headers, fragment in [
        ({"Host": "v"}, "entry 1 is a header the MCP client sets itself"),
        ({"X-A": "1", "x-a": "2"}, "entry 2 repeats a header name"),
        ({"X-A": ""}, "entry 1 must have a non-empty string value"),
        ({"X-A": "v" * (MAX_HEADER_VALUE + 1)}, "longer than"),
        ({"X-A": "bad\x00value"}, "characters not allowed"),
        ({"bad name": "v"}, "invalid header name"),
    ]:
        with pytest.raises(DefinitionInvalid) as raised:
            validate_headers(headers)
        assert fragment in raised.value.detail
        assert "bad\x00value" not in raised.value.detail


def test_catalog_names_become_valid_server_names():
    assert _default_name("io.github.acme/My Server!") == "My-Server"
    assert _default_name("///") == "server"
    assert len(_default_name("a/" + "x" * 100)) == 64


def test_input_errors_name_locations_and_never_the_rejected_value():
    schema = {"type": "object", "required": ["a"], "properties": {"b": {"type": "integer"}}}
    errors = input_errors(schema, {"b": "SECRET"})
    assert {"loc": ["body", "input", "a"], "msg": "Field required", "type": "missing"} in errors
    assert any(e["loc"] == ["body", "input", "b"] and e["type"] == "type_error" for e in errors)
    assert "SECRET" not in str(errors)
    assert input_errors({"$ref": "#/nope"}, {}) == []  # a schema that cannot be applied checks nothing


def test_ids_and_canonical_json_are_stable():
    assert mcp_run_tool_id("s1", "add") == "mcp:s1/add"
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


def test_a_managed_target_is_told_from_an_external_one():
    assert Target(url="https://x").managed is False
    assert Target(url=None, runtime_id="r1", state="running").managed is True


def test_stale_revision_is_the_412_conflict_with_the_current_resource():
    exc = stale_revision(2, {"id": "x"}, 4)
    assert isinstance(exc, Conflict) and exc.code == "revision_mismatch"
    assert exc.current == {"id": "x"} and exc.current_revision == 4 and "revision 4, you sent 2" in exc.message


def test_a_services_refusal_is_the_tool_error_it_always_was():
    assert service_error_to_tool_error(InvalidInput("bad")).code == "invalid"  # the 422 word, not invalid_input
    assert service_error_to_tool_error(NotFound("gone")).code == "not_found"
    assert service_error_to_tool_error(InvalidInput("x", code="secret_ref_required")).code == "secret_ref_required"
    stale = translate(stale_revision(1, {"id": "x"}, 3))
    assert isinstance(stale, McpToolError) and stale.code == "revision_mismatch"
    assert stale.extra == {"current_revision": 3}  # never the current resource


def test_a_cluster_failure_is_a_502_refusal_with_the_upstream_code():
    err = translate(BadGateway("the logs could not be read"))
    assert err.code == "upstream_error" and "logs" in err.message
