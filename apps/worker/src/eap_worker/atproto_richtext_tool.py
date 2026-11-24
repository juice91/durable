"""`atproto.richtext`: the text and the `app.bsky.richtext.facet` list of a Bluesky post. Pure: no I/O.

A hashtag or a link is only a real tag or link on Bluesky when the record carries a facet whose index is in UTF-8
BYTES (`byteStart`, `byteEnd`), not characters; Chinese text and emoji make the two differ. A workflow cannot compute
byte offsets with `$template` or JMESPath, so this tool does.

- Arguments: `eap_schemas.tools.AtprotoRichtextInput` (`text`; `append_tags`; `detect` {tags, links}; `max_graphemes`).
- `append_tags`: appended as `\\n` + `#Tag #Tag` (the text is right-trimmed first), unless the text already ends with
  exactly that block (then it is left as is: the tool is idempotent). An appended tag that would not be detected as a
  tag (all digits) is the error `invalid_tag`.
- Detection follows the official `detectFacets` of `@atproto/api` (packages/api/src/rich-text/util.ts and
  detection.ts in github.com/bluesky-social/atproto) and the `app.bsky.richtext.facet` lexicon:
  - tag: `#` or `＃`, at the start of the text or after whitespace; the body runs to whitespace and must contain a
    character that is not a digit, whitespace or punctuation; trailing punctuation (`\\p{P}`) is cut (`#Taiwan，` is
    the tag `Taiwan`); more than 64 graphemes is not a tag. `「#Taiwan」` is no tag: the `#` follows `「`, not whitespace.
  - link: an `http(s)://` URL after the start, whitespace or `(`, running to whitespace; a trailing `.,;:!?` is cut,
    and a trailing `)` when the URL has no `(`.
  A tag inside a link is dropped, so facets never overlap; they are sorted by `byteStart`.
- Result: `{text, facets, graphemes}`; `graphemes` counts extended grapheme clusters of the final text.
- Errors, as `{error: {code, message, ...}}`: `text_too_long` (with `graphemes` and `max_graphemes`; nothing is
  truncated) and `invalid_tag`.
"""

from __future__ import annotations

from typing import Annotated, Any

import regex
from pydantic import Field
from pydantic_ai import RunContext

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import (
    ATPROTO_POST_MAX_GRAPHEMES,
    ATPROTO_RICHTEXT_TOOL_ID,
    TOOL_DECLARATIONS,
    AtprotoRichtextDetect,
)

TAG_TYPE = "app.bsky.richtext.facet#tag"
LINK_TYPE = "app.bsky.richtext.facet#link"
TAG_MAX_GRAPHEMES = 64

# The atproto TAG regex (detectFacets), as the `regex` package reads it. Soft hyphen, word joiner, hair space and
# the zero-width characters are not part of a tag.
_INVISIBLE = "­⁠   ​‌‍⃢"
_TAG = regex.compile(
    rf"(^|\s)[#＃]((?!️)[^\s{_INVISIBLE}]*[^\d\s\p{{P}}{_INVISIBLE}]+[^\s{_INVISIBLE}]*)"
)
_LINK = regex.compile(r"(^|\s|\()(https?://\S+)", regex.I)
_TRAILING_PUNCT = regex.compile(r"\p{P}+$")


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def _graphemes(text: str) -> int:
    return len(regex.findall(r"\X", text))


def _byte_index(text: str, start: int, end: int) -> dict[str, int]:
    before = len(text[:start].encode("utf-8"))
    return {"byteStart": before, "byteEnd": before + len(text[start:end].encode("utf-8"))}


def _links(text: str) -> list[tuple[int, int, str]]:
    found = []
    for m in _LINK.finditer(text):
        uri = m.group(2)
        if uri[-1] in ".,;:!?":
            uri = uri[:-1]
        if uri.endswith(")") and "(" not in uri:
            uri = uri[:-1]
        if len(uri) > len("https://"):
            found.append((m.start(2), m.start(2) + len(uri), uri))
    return found


def _tags(text: str) -> list[tuple[int, int, str]]:
    found = []
    for m in _TAG.finditer(text):
        tag = _TRAILING_PUNCT.sub("", m.group(2).strip())
        if not tag or _graphemes(tag) > TAG_MAX_GRAPHEMES:
            continue
        start = m.start(2) - 1  # the `#` (one character, also for the full-width one)
        found.append((start, start + 1 + len(tag), tag))
    return found


def _facets(text: str, detect: AtprotoRichtextDetect) -> list[dict[str, Any]]:
    links = _links(text) if detect.links else []
    tags = [t for t in (_tags(text) if detect.tags else []) if not any(t[0] < le and ls < t[1] for ls, le, _ in links)]
    items = [(s, e, {"$type": LINK_TYPE, "uri": uri}) for s, e, uri in links] + [
        (s, e, {"$type": TAG_TYPE, "tag": tag}) for s, e, tag in tags
    ]
    return [{"index": _byte_index(text, s, e), "features": [feature]} for s, e, feature in sorted(items, key=lambda i: i[0])]


def atproto_richtext(
    ctx: RunContext[RunDeps],
    text: Annotated[str, Field(max_length=20000)],
    append_tags: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=8)] = [],  # noqa: B006
    detect: AtprotoRichtextDetect = AtprotoRichtextDetect(),  # noqa: B008
    max_graphemes: Annotated[int, Field(ge=1, le=3000)] = ATPROTO_POST_MAX_GRAPHEMES,
) -> dict[str, Any]:
    """Build a Bluesky post's text and facets (see the module docstring)."""
    detect = detect if isinstance(detect, AtprotoRichtextDetect) else AtprotoRichtextDetect.model_validate(detect)
    tags = list(append_tags)
    final = text
    if tags:
        block = " ".join(f"#{tag}" for tag in tags)
        trimmed = text.rstrip()
        already = trimmed == block or (trimmed.endswith(block) and trimmed[-len(block) - 1].isspace())
        if not already:
            final = f"{trimmed}\n{block}" if trimmed else block
        appended = {t[2] for t in _tags(block)}
        if any(tag not in appended for tag in tags):
            return _error("invalid_tag", "an appended tag would not be a Bluesky tag (all digits, or only punctuation)")
    count = _graphemes(final)
    if count > max_graphemes:
        return _error(
            "text_too_long", f"the text is {count} graphemes, over the limit of {max_graphemes}",
            graphemes=count, max_graphemes=max_graphemes,
        )
    return {"text": final, "facets": _facets(final, detect), "graphemes": count}


def register_atproto_richtext_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[ATPROTO_RICHTEXT_TOOL_ID], atproto_richtext)


__all__ = ["atproto_richtext", "register_atproto_richtext_tool"]
