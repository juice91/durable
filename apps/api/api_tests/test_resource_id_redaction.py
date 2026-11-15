"""P1.C2: raw resource ids (they may be email addresses) never reach spans or access logs.

Every route with a resource id in its path, with telemetry on (in-memory exporters): the server
spans' `url.path` / `http.target` carry the route template, the access log (`eap.access`) writes
the template, and no exported span or log record holds the id anywhere.
"""

from __future__ import annotations

import json
import logging

import pytest

from eap_observability.http import ACCESS_LOGGER, RouteTemplateAccessFilter, install_access_logging, iter_routes

from .conftest import get_auth_header
from .test_telemetry import traced  # noqa: F401 - fixture

NEEDLE = "zq7ana"  # in the id only; nothing else the app logs contains it
RID = f"{NEEDLE}.x@example.com"
RID_ENCODED = f"{NEEDLE}.x%40example.com"

AGENT_RESOURCE = "/api/v1/agents/{agent_id}/memory/resources/{resource_id}"
WORKING = "/api/v1/agents/{agent_id}/memory/resources/{resource_id}/working"
TENANT_RESOURCE = "/api/v1/memory/resources/{resource_id}"

# (method, path template, json body): every route whose path holds a resource id.
ROUTES = [
    ("GET", WORKING, None),
    ("PUT", WORKING, {"content": "x"}),
    ("DELETE", WORKING, None),
    ("DELETE", AGENT_RESOURCE, None),
    ("DELETE", TENANT_RESOURCE, None),
]


def _dump(value) -> str:
    return json.dumps(value, default=str)


def _span_blob(span) -> str:
    return _dump({"name": span.name, "attributes": dict(span.attributes or {}),
                  "events": [dict(e.attributes or {}) for e in span.events]})


def test_resource_routes_cover_every_resource_id_path(traced):  # noqa: F811
    _, app, _, _, _, _ = traced
    with_rid = sorted({r.path for r in iter_routes(app.routes) if "{resource_id}" in getattr(r, "path", "")})
    assert with_rid == sorted({AGENT_RESOURCE, WORKING, TENANT_RESOURCE})


@pytest.mark.parametrize("rid", [RID, RID_ENCODED])
@pytest.mark.parametrize(("method", "template", "body"), ROUTES)
def test_no_raw_resource_id_in_spans_or_access_logs(traced, caplog, method, template, body, rid):  # noqa: F811
    client, _app, _, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    path = template.format(agent_id="agent_x", resource_id=rid) + "?include_conversations=false"
    r = client.request(method, path, json=body, headers=get_auth_header(tenant, "alice"))
    assert r.status_code < 500, r.text

    server = [s for s in spans() if s.kind.name == "SERVER"]
    assert server, [s.name for s in spans()]
    for span in server:
        attrs = span.attributes or {}
        assert attrs.get("http.route") == template
        for key in ("url.path", "http.target"):
            if key in attrs:
                assert attrs[key] == template, (key, attrs[key])
    for span in spans():
        blob = _span_blob(span)
        assert NEEDLE not in blob, blob
    # The in-process test client's own httpx line ("HTTP Request: ...") is the client, not the API.
    exported = [_dump({"body": rec.body, "attrs": dict(rec.attributes or {})}) for rec in logs()
                if not str(rec.body).startswith("HTTP Request:")]
    assert all(NEEDLE not in line for line in exported), [line for line in exported if NEEDLE in line]

    lines = [rec.getMessage() for rec in caplog.records if rec.name == ACCESS_LOGGER]
    assert lines == [f'"{method} {template}" {r.status_code} {lines[0].rsplit(" ", 1)[1]}'] if lines else False, lines
    assert all(NEEDLE not in line and "include_conversations" not in line for line in lines)


def test_an_unmatched_path_is_logged_without_its_query(traced, caplog):  # noqa: F811
    client, *_ = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    r = client.get("/api/v1/no-such-route/x?token=abc&resource_id=ana@example.com")
    assert r.status_code == 404
    lines = [rec.getMessage() for rec in caplog.records if rec.name == ACCESS_LOGGER]
    assert len(lines) == 1 and lines[0].startswith('"GET /api/v1/no-such-route/x" 404 '), lines
    assert "token" not in lines[0] and "example" not in lines[0]


def test_uvicorn_access_lines_get_the_template_too(traced):  # noqa: F811
    _, app, *_ = traced
    install_access_logging(app)  # idempotent: one middleware, one filter
    f = RouteTemplateAccessFilter()
    f.apps.append(app)
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "DELETE", f"/api/v1/memory/resources/{RID_ENCODED}?q=1", "1.1", 200), None,
    )
    assert f.filter(record)
    assert record.getMessage() == '127.0.0.1:5000 - "DELETE /api/v1/memory/resources/{resource_id} HTTP/1.1" 200'
    assert logging.getLogger("uvicorn.access").filters  # installed by create_app


@pytest.mark.parametrize(("path", "shown"), [
    ("/api/v1/memory/resources/zq7plain.doe/", "/api/v1/memory/resources/{resource_id}/"),
    ("/api/v1/memory/resources/zq7plain.doe/working", "/api/v1/memory/resources/{resource_id}/working"),
    ("/api/v1/agents/a1/memory/resources/zq7plain.doe/x", "/api/v1/agents/{agent_id}/memory/resources/{resource_id}/x"),
])
def test_a_plain_word_id_on_an_unmatched_variant_is_redacted(traced, caplog, path, shown):  # noqa: F811
    """MINOR-2 (review): unmatched variants of a resource route (a trailing slash, an unknown
    sub-path) take the route's parameter names, so a plain-word resource id is hidden too."""
    client, _app, _, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    r = client.get(f"{path}?q=zq7plain", headers=get_auth_header(tenant, "alice"))
    assert r.status_code in (404, 405), r.status_code
    lines = [rec.getMessage() for rec in caplog.records if rec.name == ACCESS_LOGGER]
    assert len(lines) == 1 and lines[0].startswith(f'"GET {shown}" '), lines
    for span in spans():
        assert "zq7plain" not in _span_blob(span), _span_blob(span)
    assert all("zq7plain" not in str(rec.body) for rec in logs() if not str(rec.body).startswith("HTTP Request:"))
