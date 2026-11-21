"""The framework-free cores the services share, and the translators around them."""

from types import SimpleNamespace

import pytest

from eap_api.agent_archive import refuse_if_archived as http_refuse_if_archived
from eap_api.mcp_control.routing import service_error_to_tool_error
from eap_api.service_errors import service_error_response, to_http_exception
from eap_api.services.agent_archive import AGENT_ARCHIVED, refuse_if_archived
from eap_api.services.errors import Conflict, InvalidInput, NotFound, PayloadTooLarge
from eap_api.services.run_options import check_spec_payload_size
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors
from eap_api.text_checks import unstorable_strings


def test_archived_agent_is_a_conflict_and_the_wrapper_keeps_the_http_answer():
    with pytest.raises(Conflict) as core:
        refuse_if_archived(SimpleNamespace(archived=True))
    assert core.value.message == AGENT_ARCHIVED
    refuse_if_archived(SimpleNamespace(archived=False))
    with pytest.raises(Exception) as http:
        http_refuse_if_archived(SimpleNamespace(archived=True))
    assert (http.value.status_code, http.value.detail) == (409, AGENT_ARCHIVED)


def test_payload_limit_is_413(monkeypatch):
    monkeypatch.setenv("EAP_MAX_RUN_PAYLOAD_BYTES", "10")
    with pytest.raises(PayloadTooLarge) as exc:
        check_spec_payload_size({"a": "x" * 20}, "in")
    assert service_error_response(exc.value).status_code == 413
    assert service_error_to_tool_error(exc.value).code == "payload_too_large"


def test_tool_error_codes_follow_the_http_words_unless_the_refusal_sets_its_own():
    assert service_error_to_tool_error(InvalidInput("bad")).code == "invalid"
    assert service_error_to_tool_error(NotFound("gone")).code == "not_found"
    assert service_error_to_tool_error(Conflict("taken", code="name_in_use")).code == "name_in_use"


def test_detail_object_is_the_whole_body():
    exc = InvalidInput("x", detail_object={"code": "input_schema_errors", "case": "c", "errors": []})
    assert service_error_response(exc).body == b'{"detail":{"code":"input_schema_errors","case":"c","errors":[]}}'
    assert to_http_exception(exc).detail["case"] == "c"
    assert service_error_to_tool_error(exc).code == "input_schema_errors"


def test_unstorable_strings_names_the_location():
    found = unstorable_strings({"a": ["ok", "bad\x00"]})
    assert found[0]["loc"] == ["body", "a", 1]


def test_workflow_input_checks():
    assert non_finite_errors({"n": float("nan")})[0]["type"] == "finite_number"
    schema = {"type": "object", "required": ["q"]}
    assert input_schema_errors(schema, {"q": 1}) == []
    assert input_schema_errors(schema, {})[0]["type"] == "json_schema.required"
