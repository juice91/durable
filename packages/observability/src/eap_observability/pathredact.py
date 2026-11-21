"""Path and URL attribute redaction, free of any web framework (RAG P1.C2).

The pure part of eap_observability.http: turning a request path into its route template or `{id}` placeholders
and cleaning a span's URL attributes. The redacting exporter (eap_observability.redaction) uses it, so it must
not import fastapi or starlette. eap_observability.http re-exports every name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

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
