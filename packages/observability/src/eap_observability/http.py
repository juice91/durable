"""HTTP request telemetry without raw path parameters (RAG P1.C2).

Some API paths carry values that must never reach spans or logs: `/memory/resources/{resource_id}`
and `/agents/{agent_id}/memory/resources/{resource_id}[/working]` take a caller's resource id, which
may be an email address (the forget audit record stores only its hash). So:

- Spans: whenever a route matched (the span has `http.route`), `url.path` and `http.target` are set
  to the route template, and `http.url` / `url.full` to the scheme and host plus the template. For
  a request no route matched (a 404, a trailing-slash variant), the path keeps its segments but
  every segment that looks like an id (`redact_path`: a UUID, an email, a long digit or hex run, a
  token mixing letters and digits, anything percent-encoded or with characters outside
  `[A-Za-z0-9._~-]`) becomes `{id}`, and a segment at a parameter position of a route template
  whose leading segments the path matches shows that parameter (`redact_unmatched_path`: a
  plain-word resource id on a trailing-slash or unknown sub-path is hidden too). Every request loses its query string: `http.url`, `url.full`
  and `http.target` are cut at `?` and `url.query` is dropped. Done twice: by the FastAPIInstrumentor `server_request_hook` (at span start) and again in
  the redacting exporter (`template_route_attributes`, which also covers FastAPI's native
  telemetry spans, whose route is set when the request ends).
- Access log: the API logs one line per request itself (`AccessLogMiddleware`, logger
  `eap.access`): method, the route template (or, for a request no route matched, its path with
  id-like segments as `{id}`, `redact_path`), status and duration. Query strings are never logged. uvicorn's own
  access log is turned off (`--no-access-log` in scripts/dev-lib.sh and the documented run
  command); if it is on anyway, `RouteTemplateAccessFilter` on `uvicorn.access` rewrites its path
  the same way.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import unquote
from collections.abc import Iterable, Mapping
from typing import Any

ACCESS_LOGGER = "eap.access"
UVICORN_ACCESS_LOGGER = "uvicorn.access"

_PATH_ATTRIBUTES = ("url.path", "http.target")
_URL_ATTRIBUTES = ("url.full", "http.url")
_QUERY_ATTRIBUTES = ("url.query",)
ID_PLACEHOLDER = "{id}"

_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_DIGITS = re.compile(r"\d{4,}")
_HEX = re.compile(r"[0-9a-fA-F]{12,}")
_PLAIN = re.compile(r"[A-Za-z0-9._~-]*")


def is_id_segment(segment: str) -> bool:
    """Whether one path segment of an unmatched request may be an id (never a fixed path word)."""
    if not segment:
        return False
    decoded = unquote(segment)
    if decoded != segment or "@" in decoded:  # percent-encoded (an encoded email, ...) or an email
        return True
    if _UUID.fullmatch(decoded) or _DIGITS.fullmatch(decoded) or _HEX.fullmatch(decoded):
        return True
    if not _PLAIN.fullmatch(decoded):  # spaces, '+', '=', ':' ...: not a route word
        return True
    has_digit = any(c.isdigit() for c in decoded)
    return len(decoded) >= 16 and has_digit and any(c.isalpha() for c in decoded)


def redact_path(path: str) -> str:
    """`path` (no query) with every id-like segment (is_id_segment) replaced by `{id}`."""
    return "/".join(ID_PLACEHOLDER if is_id_segment(s) else s for s in path.split("/"))


# Route templates with path parameters of the apps install_access_logging saw (split at "/"): an
# unmatched request takes the parameter names of every template whose leading segments it matches
# (`/memory/resources/jane.doe/` and `/memory/resources/jane.doe/working` become
# `/memory/resources/{resource_id}/...`), so a caller-chosen id that looks like a plain word is
# hidden too.
_TEMPLATES: set[tuple[str, ...]] = set()


def _is_param(segment: str) -> bool:
    return len(segment) > 2 and segment.startswith("{") and segment.endswith("}")


def register_route_templates(app: Any) -> None:
    for route in iter_routes(getattr(app, "routes", ()) or ()):
        path = getattr(route, "path", None)
        if isinstance(path, str) and "{" in path:
            _TEMPLATES.add(tuple(path.split("/")))


def redact_unmatched_path(path: str, templates: Iterable[tuple[str, ...]] | None = None) -> str:
    """An unmatched request's path (no query): redact_path, and every segment at a parameter
    position of a route template whose leading segments the path matches (a parameter matches any
    non-empty segment, a literal only itself) shows that parameter, e.g. `{resource_id}`."""
    segments = path.split("/")
    out = [ID_PLACEHOLDER if is_id_segment(s) else s for s in segments]
    for template in _TEMPLATES if templates is None else templates:
        matched = 0
        for i, part in enumerate(template):
            if i >= len(segments) or (_is_param(part) and not segments[i]) or (not _is_param(part) and part != segments[i]):
                break
            matched = i + 1
        for i in range(matched):
            if _is_param(template[i]):
                out[i] = template[i]
    return "/".join(out)


def _without_query(value: str) -> str:
    return value.split("?", 1)[0].split("#", 1)[0]


def iter_routes(routes: Iterable[Any]) -> Iterable[Any]:
    """The matchable routes (each with `path` and `matches(scope)`). FastAPI 0.137+ nests the routes
    of include_router() under tree nodes without a path; `fastapi.routing.iter_route_contexts`
    flattens them (older FastAPI: the routes are matchable as they are)."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover - FastAPI before 0.137.2
        iter_route_contexts = None
    if iter_route_contexts is not None:
        yield from iter_route_contexts(list(routes))
        return
    for route in routes:  # pragma: no cover
        contexts = getattr(route, "effective_route_contexts", None)
        yield from (contexts() if contexts is not None else (route,))


def route_template(app: Any, method: str, path: str) -> str | None:
    """The template of the route of `app` that fully matches `method` and `path` (a path without
    its query string), else None."""
    from starlette.routing import Match

    scope = {"type": "http", "method": method or "GET", "path": path, "root_path": "", "app": app}
    partial = None
    for route in iter_routes(getattr(app, "routes", ()) or ()):
        try:
            match, _ = route.matches(scope)
        except Exception:  # noqa: BLE001 - a route that cannot match this scope
            continue
        template = getattr(route, "path", None)
        if not isinstance(template, str):
            continue
        if match == Match.FULL:
            return template
        if match == Match.PARTIAL and partial is None:
            partial = template  # the path matched; the method did not (405)
    return partial


def loggable_path(app: Any, method: str, raw: str) -> str:
    """What a log line may show for a request: the route template, else the path without its query
    and with id-like segments as `{id}` (redact_path)."""
    path = _without_query(raw)
    return route_template(app, method, path) or redact_unmatched_path(path)


def template_route_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """`attributes` with no query string anywhere (`url.query` dropped; `http.url`, `url.full` and
    `http.target` cut at `?`), and the path the route template (`http.route`, when a route
    matched) or, unmatched, the path with id-like segments as `{id}` (redact_path)."""
    attrs = dict(attributes or {})
    for key in _QUERY_ATTRIBUTES:
        attrs.pop(key, None)
    route = attrs.get("http.route")
    matched = isinstance(route, str) and bool(route)

    def path_of(value: str) -> str:
        return route if matched else redact_unmatched_path(_without_query(value))

    for key in _PATH_ATTRIBUTES:
        value = attrs.get(key)
        if matched:
            attrs[key] = route
        elif isinstance(value, str):
            attrs[key] = path_of(value)
    for key in _URL_ATTRIBUTES:
        value = attrs.get(key)
        if isinstance(value, str):
            scheme, sep, rest = _without_query(value).partition("://")
            if sep:
                host, slash, path = rest.partition("/")
                attrs[key] = f"{scheme}://{host}{path_of(slash + path)}"
            else:
                attrs[key] = path_of(value)
    return attrs


def server_request_hook(span: Any, scope: Mapping[str, Any]) -> None:
    """FastAPIInstrumentor server_request_hook: the server span's path attributes become the route
    template as soon as the span starts (the instrumentor set `http.route` from the matched route)."""
    if span is None or not span.is_recording():
        return
    attributes = dict(getattr(span, "attributes", None) or {})
    if not isinstance(attributes.get("http.route"), str):
        app = scope.get("app")
        route = route_template(app, scope.get("method", ""), scope.get("path", "")) if app is not None else None
        if route is not None:
            attributes["http.route"] = route
    updates = template_route_attributes(attributes)
    # url.query cannot be removed from a live span; the redacting exporter drops it.
    span.set_attributes({k: v for k, v in updates.items() if k in (*_PATH_ATTRIBUTES, *_URL_ATTRIBUTES)})


class AccessLogMiddleware:
    """One INFO line per HTTP request on `eap.access`: `"<METHOD> <route template or path>" <status>
    <ms>ms`. No query string, no client address, no headers."""

    def __init__(self, app: Any, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.log = logger or logging.getLogger(ACCESS_LOGGER)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            method = scope.get("method", "")
            path = loggable_path(scope.get("app") or self.app, method, scope.get("path", ""))
            self.log.info('"%s %s" %d %dms', method, path, status["code"],
                          int((time.perf_counter() - started) * 1000))


class RouteTemplateAccessFilter(logging.Filter):
    """For uvicorn's access log (args: client, method, path with query, http version, status):
    the path becomes the route template of `apps`, else redact_path of the path without its query."""

    def __init__(self) -> None:
        super().__init__()
        self.apps: list[Any] = []

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            method = args[1] if isinstance(args[1], str) else ""
            raw = _without_query(args[2])
            path = redact_unmatched_path(raw)
            for app in self.apps:
                template = route_template(app, method, raw)
                if template is not None:
                    path = template
                    break
            record.args = (*args[:2], path, *args[3:])
        return True


_uvicorn_filter = RouteTemplateAccessFilter()


def install_access_logging(app: Any) -> None:
    """Add AccessLogMiddleware to `app` (once) and the template filter to `uvicorn.access`. The
    `eap.access` logger gets a stderr handler when it has none, so the lines show in the dev console
    (they also propagate to the OTLP log handler when telemetry is on)."""
    if not getattr(app.state, "eap_access_logging", False):
        app.add_middleware(AccessLogMiddleware)
        app.state.eap_access_logging = True
    access = logging.getLogger(ACCESS_LOGGER)
    access.setLevel(logging.INFO)
    if not access.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
        access.addHandler(handler)
    uvicorn_access = logging.getLogger(UVICORN_ACCESS_LOGGER)
    if _uvicorn_filter not in uvicorn_access.filters:
        uvicorn_access.addFilter(_uvicorn_filter)
    register_route_templates(app)
    if app not in _uvicorn_filter.apps:
        _uvicorn_filter.apps.append(app)
        del _uvicorn_filter.apps[:-8]  # tests create many apps; keep the latest few


__all__ = [
    "ACCESS_LOGGER",
    "AccessLogMiddleware",
    "RouteTemplateAccessFilter",
    "ID_PLACEHOLDER",
    "install_access_logging",
    "is_id_segment",
    "iter_routes",
    "redact_path",
    "redact_unmatched_path",
    "register_route_templates",
    "loggable_path",
    "route_template",
    "server_request_hook",
    "template_route_attributes",
]
