"""P1.C2 acceptance: no path parameter, query string or email reaches an exported span or an access
log line, for every route family of the API (a sweep over `app.routes`), for a 404 (the bare path),
a 405 and a trailing-slash redirect, and for the SSE stream route (the span ends after the stream).
"""

from __future__ import annotations

import json
import logging
import re
import uuid

import pytest

from eap_observability.http import ACCESS_LOGGER, iter_routes, redact_path

from .conftest import get_auth_header
from .run_helpers import read_all_frames
from .test_runs import FULL_RUN, make_agent, seed, start
from .test_telemetry import traced  # noqa: F401 - fixture

PARAM = "zq7{name}.who@example.com"  # every path parameter value: a needle and an email
QUERY_KEY = "zqquery"
QUERY_VALUE = "zq7qv@example.com"
QUERY = f"{QUERY_KEY}={QUERY_VALUE}&token=zq7token"
NEEDLES = ("zq7", QUERY_KEY, "who@example.com")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
SKIP_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
BODY_METHODS = {"POST", "PUT", "PATCH"}


def _blob(value) -> str:
    return json.dumps(value, default=str)


def _span_blobs(spans) -> list[str]:
    return [
        _blob({"name": s.name, "attributes": dict(s.attributes or {}),
               "events": [dict(e.attributes or {}) for e in s.events]})
        for s in spans()
    ]


def _log_blobs(logs) -> list[str]:
    return [_blob({"body": r.body, "attrs": dict(r.attributes or {})}) for r in logs()
            if not str(r.body).startswith("HTTP Request:")]  # the in-process client's own httpx line


def _access_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER]


def _routes(app) -> list[tuple[str, str]]:
    found = set()
    for route in iter_routes(app.routes):
        path = getattr(route, "path", None)
        if not isinstance(path, str) or path in SKIP_PATHS:
            continue
        for method in getattr(route, "methods", None) or ():
            if method not in ("HEAD", "OPTIONS"):
                found.add((method, path))
    return sorted(found)


def _fill(template: str) -> str:
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: PARAM.format(name=m.group(1)), template)


def _assert_clean(spans, logs, caplog, where: str) -> None:
    for blob in _span_blobs(spans):
        assert not any(n in blob for n in NEEDLES), (where, blob)
    for blob in _log_blobs(logs):
        assert not any(n in blob for n in NEEDLES), (where, blob)
    for line in _access_lines(caplog):
        assert not any(n in line for n in NEEDLES) and not EMAIL.search(line) and "?" not in line, (where, line)


def test_the_sweep_covers_every_route_family(traced):  # noqa: F811
    _, app, *_ = traced
    routes = _routes(app)
    families = {path.split("/")[3] for _, path in routes if path.startswith("/api/v1/") and len(path.split("/")) > 3}
    assert len(routes) > 70 and {"agents", "runs", "knowledge-bases", "conversations", "mcp-servers"} <= families, families
    assert sum("{" in path for _, path in routes) > 50


def test_every_route_in_the_api_leaks_no_parameter_query_or_email(traced, caplog):  # noqa: F811
    client, app, _fake, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    headers = get_auth_header(tenant, "alice")
    routes = _routes(app)
    for method, template in routes:
        kwargs = {"json": {}} if method in BODY_METHODS else {}
        r = client.request(method, f"{_fill(template)}?{QUERY}", headers=headers, **kwargs)
        assert r.status_code < 500 or r.status_code == 503, (method, template, r.status_code, r.text[:200])
    server = [s for s in spans() if s.kind.name == "SERVER"]
    assert len(server) >= len(routes)
    templates = {t for _, t in routes}
    matched = [s for s in server if (s.attributes or {}).get("http.route")]
    assert matched
    for span in matched:
        attrs = span.attributes or {}
        assert attrs["http.route"] in templates
        for key in ("url.path", "http.target"):
            if key in attrs:
                assert attrs[key] == attrs["http.route"], (key, attrs[key])
    _assert_clean(spans, logs, caplog, "route sweep")
    lines = _access_lines(caplog)
    assert len(lines) >= len(routes)
    # One access line per request: method, a route template (never a filled path), status, duration.
    shapes = [re.fullmatch(r'"([A-Z]+) (\S+)" (\d{3}) \d+ms', line) for line in lines]
    assert all(shapes), [l for l, s in zip(lines, shapes) if not s]
    assert {m.group(2) for m in shapes if m} <= templates | {"/api/v1" + "/"}  # type: ignore[union-attr]


UNMATCHED = ["/api/v1/no-such/ana@example.com", "/no-such-route", "/api/v1/memory/resourcess/zq7x@example.com"]


@pytest.mark.parametrize("raw", UNMATCHED)
def test_a_404_access_line_is_the_bare_path(traced, caplog, raw):  # noqa: F811
    client, _app, _fake, tenant, _spans, _logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    r = client.get(f"{raw}?{QUERY}", headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 404
    [line] = _access_lines(caplog)
    # The bare path, with id-like segments (here the emails) as {id}.
    assert line.startswith(f'"GET {redact_path(raw)}" 404 '), line
    assert "?" not in line and QUERY_KEY not in line and "token" not in line
    assert "@" not in line and "zq7" not in line, line


@pytest.mark.parametrize("raw", UNMATCHED)
def test_a_404_exports_no_query_string_in_spans_or_logs(traced, raw):  # noqa: F811
    client, _app, _fake, tenant, spans, logs = traced
    r = client.get(f"{raw}?{QUERY}", headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 404
    for blob in _span_blobs(spans) + _log_blobs(logs):
        assert QUERY_KEY not in blob and "zq7qv" not in blob, blob
        # The id in an unmatched path is redacted too (P1.C2 follow-up): no email, no zq7 marker.
        assert not EMAIL.search(blob) and "zq7" not in blob and "%40" not in blob, blob
    server = [s for s in spans() if s.kind.name == "SERVER"]
    assert server
    for span in server:
        attrs = dict(span.attributes or {})
        assert "url.query" not in attrs
        for key in ("url.path", "http.target"):
            if key in attrs:
                assert attrs[key] == redact_path(raw), (key, attrs[key])


def test_unmatched_paths_keep_route_words_and_redact_id_segments():
    assert redact_path("/api/v1/memory/resourcess/zq7x@example.com") == "/api/v1/memory/resourcess/{id}"
    assert redact_path("/api/v1/no-such/ana%40example.com/") == "/api/v1/no-such/{id}/"
    assert redact_path("/api/v1/runs/0b9c6f1e-8f7a-4d5e-9c2b-1a2b3c4d5e6f/x") == "/api/v1/runs/{id}/x"
    assert redact_path("/api/v1/items/123456/deadbeefcafe1234") == "/api/v1/items/{id}/{id}"
    assert redact_path("/api/v1/t/abcDEF123ghiJKL456") == "/api/v1/t/{id}"
    assert redact_path("/no-such-route") == "/no-such-route"


def test_a_405_on_a_resource_path_shows_the_template_not_the_id(traced, caplog):  # noqa: F811
    client, _app, _fake, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    rid = "zq7wrong.method@example.com"
    r = client.patch(f"/api/v1/memory/resources/{rid}?{QUERY}", json={}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 405
    [line] = _access_lines(caplog)
    assert line.startswith('"PATCH /api/v1/memory/resources/{resource_id}" 405 '), line
    for blob in _span_blobs(spans) + _log_blobs(logs):
        assert "zq7" not in blob and QUERY_KEY not in blob, blob


def test_a_trailing_slash_request_exports_no_query_string(traced, caplog):  # noqa: F811
    client, _app, _fake, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    r = client.delete(f"/api/v1/memory/resources/zq7slash.x%40example.com/?{QUERY}",
                      headers=get_auth_header(tenant, "alice"), follow_redirects=False)
    assert r.status_code in (307, 308, 404, 405), r.status_code
    for line in _access_lines(caplog):
        assert QUERY_KEY not in line and "?" not in line, line
    for blob in _span_blobs(spans) + _log_blobs(logs):
        assert QUERY_KEY not in blob and "zq7qv" not in blob, blob
        assert "zq7slash" not in blob and not EMAIL.search(blob), blob  # nor the id in the unmatched path


def test_the_sse_stream_route_logs_and_exports_the_template_only(traced, caplog):  # noqa: F811
    client, app, _fake, tenant, spans, logs = traced
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    h = get_auth_header(tenant, "alice")
    run_id = start(client, h, make_agent(client, h)).json()["run_id"]
    seed(app, run_id, FULL_RUN)
    caplog.clear()
    frames = read_all_frames(client, f"/api/v1/runs/{run_id}/stream?after_seq=-1&{QUERY}", {**h, "Last-Event-ID": "1"})
    assert [f.event for f in frames if f.event == "run_event"], frames
    template = "/api/v1/runs/{run_id}/stream"
    lines = _access_lines(caplog)
    assert len(lines) == 1 and re.fullmatch(rf'"GET {re.escape(template)}" 200 \d+ms', lines[0]), lines
    stream_spans = [s for s in spans() if s.kind.name == "SERVER" and (s.attributes or {}).get("http.route") == template]
    assert stream_spans, [s.name for s in spans()]
    for span in stream_spans:
        attrs = span.attributes or {}
        assert attrs.get("url.path", template) == template and attrs.get("http.target", template) == template
    for span in stream_spans:
        for key in ("url.path", "http.target", "http.url", "url.full"):
            assert run_id not in str((span.attributes or {}).get(key, "")), key
        assert QUERY_KEY not in _blob(dict(span.attributes or {}))
    assert not any(QUERY_KEY in line or run_id in line for line in lines)
    assert uuid.UUID(run_id)
