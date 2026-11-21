"""ServiceError -> the API's JSON error body (eap_api.service_errors)."""

import json

from eap_api.service_errors import service_error_response
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable


def render(exc):
    response = service_error_response(exc)
    return response.status_code, json.loads(response.body), response.headers


def test_each_error_kind_has_its_status_and_the_detail_code_body():
    assert render(NotFound("Agent not found"))[:2] == (404, {"detail": "Agent not found", "code": "not_found"})
    assert render(Conflict("name taken", code="name_conflict"))[:2] == (
        409, {"detail": "name taken", "code": "name_conflict"})
    assert render(InvalidInput("bad", details=[{"field": "x"}]))[:2] == (
        422, {"detail": "bad", "code": "invalid_input", "errors": [{"field": "x"}]})
    assert render(Forbidden("no"))[0] == 403
    assert render(Unavailable("down"))[0] == 503


def test_a_conflict_with_current_answers_412_with_the_etag():
    status, body, headers = render(Conflict("stale", code="revision_mismatch", current={"id": "a"}, current_revision=7))
    assert status == 412
    assert body == {"detail": "stale", "code": "revision_mismatch", "current_revision": 7, "current": {"id": "a"}}
    assert headers["etag"] == '"7"'
