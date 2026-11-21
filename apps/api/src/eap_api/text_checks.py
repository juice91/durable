"""Framework-free text checks shared by the request guards and the services."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

MAX_REPORTED_ERRORS = 20
UNSTORABLE_ERROR_TYPE = "string_unstorable"

_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")
_LOC_PART_MAX = 100


def unstorable_reason(text: str) -> str | None:
    """Why PostgreSQL cannot store ``text`` in a text or JSONB column, or None if it can."""
    match = _UNSTORABLE.search(text)
    if match is None:
        return None
    if match.group() == "\x00":
        return "a NUL character (\\u0000)"
    return f"an unpaired surrogate (\\u{ord(match.group()):04x})"


def _key_label(key: str) -> str:
    return _UNSTORABLE.sub("?", key[:_LOC_PART_MAX])


def unstorable_strings(data: Any, prefix: Sequence[int | str] = ("body",)) -> list[dict[str, Any]]:
    """Compact 422 items for every unstorable string in parsed JSON, keys included, in document
    order (at most MAX_REPORTED_ERRORS). Iterative: the caller bounds the depth first. A key's
    location shows it with its unstorable characters replaced by '?'."""
    found: list[dict[str, Any]] = []
    # (value, loc, None) to visit, or (None, loc, message) for a key error found on the way down.
    stack: list[tuple[Any, tuple[int | str, ...], str | None]] = [(data, tuple(prefix), None)]
    while stack and len(found) < MAX_REPORTED_ERRORS:
        item, loc, key_error = stack.pop()
        if key_error is not None:
            found.append({"loc": list(loc), "msg": key_error, "type": UNSTORABLE_ERROR_TYPE})
        elif isinstance(item, str):
            if reason := unstorable_reason(item):
                found.append({"loc": list(loc), "msg": f"string contains {reason}, which cannot be stored",
                              "type": UNSTORABLE_ERROR_TYPE})
        elif isinstance(item, dict):
            children: list[tuple[Any, tuple[int | str, ...], str | None]] = []
            for key, value in item.items():
                key_loc = (*loc, _key_label(key))
                if reason := unstorable_reason(key):
                    children.append((None, key_loc, f"key contains {reason}, which cannot be stored"))
                children.append((value, key_loc, None))
            stack.extend(reversed(children))
        elif isinstance(item, list):
            stack.extend(reversed([(value, (*loc, i), None) for i, value in enumerate(item)]))
    return found[:MAX_REPORTED_ERRORS]
