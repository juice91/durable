"""service_error_to_tool_error: a service refusal reports the code its route refusal gave."""

from __future__ import annotations

from eap_api.mcp_control.routing import service_error_to_tool_error
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable


def test_default_codes_become_the_status_word_and_explicit_codes_stay():
    assert service_error_to_tool_error(NotFound("gone")).code == "not_found"
    assert service_error_to_tool_error(InvalidInput("bad")).code == "invalid"
    assert service_error_to_tool_error(Forbidden("no")).code == "forbidden"
    assert service_error_to_tool_error(Unavailable("down")).code == "unavailable"
    assert service_error_to_tool_error(NotFound("x", code="source_not_found")).code == "source_not_found"
    assert service_error_to_tool_error(Conflict("dup", code="source_name_conflict")).code == "source_name_conflict"


def test_a_stale_revision_keeps_the_current_revision_but_not_the_body():
    err = service_error_to_tool_error(
        Conflict("stale", code="revision_mismatch", current={"id": "a"}, current_revision=7)
    )
    assert err.body == {"error": {"code": "revision_mismatch", "message": "stale", "current_revision": 7}}
