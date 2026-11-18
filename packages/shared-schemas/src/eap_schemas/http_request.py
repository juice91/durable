"""Secret references in `http.request` arguments, and the words of its approval card. Pure: no I/O.

A reference is a string equal to ``secret:<name>`` anywhere in `headers`, `query`, `json` or `body`; a
header value may also CONTAIN one (``Bearer secret:<name>``). The names are all the approval card
shows (method, host, path and these names), never a value."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from eap_schemas.secrets import SECRET_NAME_PATTERN, SECRET_REF_PREFIX, is_secret_ref

# A reference inside a longer header value: not preceded by a name character, the name greedy.
EMBEDDED_REF = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(SECRET_REF_PREFIX) + "(" + SECRET_NAME_PATTERN[1:-1] + ")")


def _exact(value: Any, found: list[str]) -> None:
    if isinstance(value, str):
        name = is_secret_ref(value)
        if name is not None:
            found.append(name)
    elif isinstance(value, dict):
        for item in value.values():
            _exact(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _exact(item, found)


def http_request_secret_names(args: Any) -> list[str]:
    """The secret names an `http.request` call's arguments refer to (sorted, each once)."""
    if not isinstance(args, dict):
        return []
    found: list[str] = []
    headers = args.get("headers")
    if isinstance(headers, dict):
        for value in headers.values():
            if isinstance(value, str):
                found.extend(EMBEDDED_REF.findall(value))
    for key in ("query", "json", "body"):
        _exact(args.get(key), found)
    return sorted(set(found))


def http_request_summary(args: dict[str, Any]) -> str:
    """The approval card's wording: method, host and path (no query, no credentials) and the secret names."""
    method = str(args.get("method") or "GET").upper()
    try:
        parts = urlsplit(str(args.get("url") or ""))
        host = parts.hostname or "?"
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path or "/"
    except ValueError:
        host, path = "?", ""
    names = http_request_secret_names(args)
    used = f" using secret {', '.join(names)}" if len(names) == 1 else f" using secrets {', '.join(names)}"
    return f"{method} {host}{path}{used if names else ''}"


__all__ = ["EMBEDDED_REF", "http_request_secret_names", "http_request_summary"]
