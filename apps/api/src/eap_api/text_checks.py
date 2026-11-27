"""Framework-free text checks shared by the request guards and the services."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

MAX_REPORTED_ERRORS = 20
UNSTORABLE_ERROR_TYPE = "string_unstorable"

UTF8_BOM = b"\xef\xbb\xbf"
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


def decode_utf8_json_body(body: bytes) -> str | None:
    """The body as text, or None unless it is strict UTF-8 (an optional BOM) with no NUL byte.

    FastAPI parses with json.loads(bytes), which guesses UTF-16/32 from BOMs and NUL bytes. A body
    that passes here has neither (UTF-16 BOMs are not valid UTF-8, UTF-32's contain NUL), so
    json.loads decodes it as UTF-8 too: the text scanned below is exactly the text parsed next."""
    if b"\x00" in body:
        return None
    try:
        return body.removeprefix(UTF8_BOM).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def json_depth_exceeds(text: str, limit: int) -> bool:
    """Whether JSON text nests objects/arrays deeper than ``limit``, by scanning its characters
    (no parsing, no recursion); brackets inside strings are ignored."""
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth -= 1
    return False
