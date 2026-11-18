"""`feed.parse`: an RSS 2.0 or Atom document into numbered, filtered items. Pure: no I/O, no network.

A workflow fetches the document with `http.request` and hands its `text` here; an agent can do the same. The
platform knows nothing about any publisher: sources, filters and terms are all arguments.

- Arguments: `eap_schemas.tools.FeedParseInput`: `text` (one document) or `feeds` (one to six `{text, label,
  include_terms?}`), exactly one of them (`feed_args_invalid`).
- XML is parsed with defusedxml (no DTD, no entities, no external references), so an entity bomb or an
  external-entity document is `feed_invalid` instead of a hazard. A document that is not XML, or whose root is
  neither RSS (`rss`) nor Atom (`feed`), is `feed_invalid`; with `feeds`, such a feed is skipped and its label
  listed in `failed_feeds`, and the call fails only when none is readable.
- An item (`item` / `entry`) without a title or without a link is `malformed`. Dates: RSS `pubDate` (RFC 822) or
  Atom `published` / `updated` (RFC 3339); an item whose date is missing or unreadable keeps `published_at: null`,
  is never dropped as old, and sorts last.
- Drops are counted in the first category that applies, in this order: malformed, excluded_url, old, no_term,
  term. `exclude_urls` compare by exact link (surrounding space and a trailing slash ignored); `include_terms`
  (top level, and per feed; both must hold) keep only an item whose title plus summary contains one of the terms;
  `exclude_terms` drop an item that contains one. Matching is by case-insensitive substring.
- Items of all feeds are newest first, cut at `max_items`, then numbered `c1`, `c2`, ... once; `summary` is plain
  text (tags stripped, entities decoded, whitespace collapsed) of at most 400 characters; with `feeds` each item has
  `source` (its feed's label). `listing` has one line per item, `c1｜title｜excerpt` (excerpt at most 120
  characters), joined by newlines.
- Result: `{items, listing, count, dropped: {excluded_url, old, no_term, term, malformed}[, failed_feeds]}`, or
  `{error: {code, message}}` with `feed_invalid`, `feed_args_invalid` or `now_invalid` (an unreadable `now`).
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated, Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from pydantic import Field
from pydantic_ai import RunContext

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import FEED_PARSE_TOOL_ID, FEED_TEXT_MAX_CHARS, TOOL_DECLARATIONS, FeedInput

SUMMARY_MAX_CHARS = 400
EXCERPT_MAX_CHARS = 120

_BLOCKS = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_TAGS = re.compile(r"<[^>]*>")
_BREAKS = re.compile(r"</?(?:p|br|div|li|ul|ol|tr|td|table|h[1-6]|blockquote|section|article)\b[^>]*>", re.I)
_SPACE = re.compile(r"\s+")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(node: Element, name: str) -> list[Element]:
    return [c for c in node if _local(c.tag) == name]


def _text_of(node: Element, *names: str) -> str:
    for name in names:
        for child in _children(node, name):
            value = "".join(child.itertext()).strip()
            if value:
                return value
    return ""


def plain_text(raw: str) -> str:
    """Tags stripped (block tags become a space, inline tags vanish), entities decoded, whitespace (non-breaking
    spaces too) collapsed."""
    text = _BREAKS.sub(" ", _BLOCKS.sub(" ", raw))
    return _SPACE.sub(" ", html.unescape(_TAGS.sub("", text))).strip()


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    value: datetime | None = None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        try:
            value = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _atom_link(entry: Element) -> str:
    links = _children(entry, "link")
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href", "").strip()
    return ""


def _raw_items(root: Element) -> list[dict[str, Any]] | None:
    kind = _local(root.tag)
    out: list[dict[str, Any]] = []
    if kind == "rss":
        for channel in _children(root, "channel"):
            for item in _children(channel, "item"):
                link = _text_of(item, "link")
                guid = _text_of(item, "guid")
                out.append({
                    "title": plain_text(_text_of(item, "title")),
                    "link": link or (guid if guid.lower().startswith("http") else ""),
                    "summary": _text_of(item, "description", "encoded"),
                    "date": _text_of(item, "pubDate", "date"),
                })
        return out
    if kind == "feed":
        for entry in _children(root, "entry"):
            out.append({
                "title": plain_text(_text_of(entry, "title")),
                "link": _atom_link(entry),
                "summary": _text_of(entry, "summary", "content"),
                "date": _text_of(entry, "published", "updated"),
            })
        return out
    return None


def _norm_url(url: str) -> str:
    return url.strip().rstrip("/")


def _fmt(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _parse_one(text: str) -> list[dict[str, Any]] | None:
    """The raw items of one document, or None when it is not well-formed RSS or Atom."""
    try:
        root = DefusedET.fromstring(text, forbid_dtd=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None
    return _raw_items(root)


def _matches(haystack: str, terms: list[str]) -> bool:
    return any(term in haystack for term in terms)


def feed_parse(
    ctx: RunContext[RunDeps],
    text: Annotated[str, Field(max_length=FEED_TEXT_MAX_CHARS)] | None = None,
    feeds: Annotated[list[FeedInput], Field(min_length=1, max_length=6)] | None = None,
    max_items: Annotated[int, Field(ge=1, le=50)] = 20,
    max_age_hours: Annotated[float, Field(gt=0)] | None = None,
    include_terms: Annotated[list[str], Field(max_length=200)] | None = None,
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None,
    exclude_terms: Annotated[list[str], Field(max_length=200)] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Parse one or several RSS 2.0 or Atom documents (see the module docstring)."""
    if (text is None) == (feeds is None):
        return _error("feed_args_invalid", "give either text (one feed) or feeds (one to six), not both and not neither")
    if now is None:
        clock = datetime.now(timezone.utc)
    else:
        clock = _parse_date(now)
        if clock is None:
            return _error("now_invalid", "now must be an RFC 3339 timestamp")
    sources: list[tuple[str | None, str, list[str] | None]] = (
        [(None, text, None)] if text is not None
        else [(f.label, f.text, f.include_terms) for f in feeds or []])
    parsed: list[tuple[str | None, list[dict[str, Any]], list[str] | None]] = []
    failed: list[str] = []
    for label, document, own_terms in sources:
        rows = _parse_one(document)
        if rows is None:
            failed.append(label or "")
        else:
            parsed.append((label, rows, own_terms))
    if not parsed:
        return _error("feed_invalid", "the document is not well-formed RSS 2.0 or Atom without a DTD or entities"
                      if text is not None else "no feed is well-formed RSS 2.0 or Atom without a DTD or entities")

    blocked_urls = {_norm_url(u) for u in exclude_urls or []}
    terms = [t.casefold() for t in exclude_terms or [] if t]
    wanted = [t.casefold() for t in include_terms or [] if t]
    dropped = {"excluded_url": 0, "old": 0, "no_term": 0, "term": 0, "malformed": 0}
    kept: list[dict[str, Any]] = []
    for label, rows, own_terms in parsed:
        own = [t.casefold() for t in own_terms or [] if t]
        for row in rows:
            title, link = row["title"], row["link"]
            if not title or not link:
                dropped["malformed"] += 1
                continue
            if _norm_url(link) in blocked_urls:
                dropped["excluded_url"] += 1
                continue
            published = _parse_date(row["date"])
            if max_age_hours is not None and published is not None and clock - published > timedelta(hours=max_age_hours):
                dropped["old"] += 1
                continue
            summary = plain_text(row["summary"])[:SUMMARY_MAX_CHARS].rstrip()
            haystack = f"{title}\n{summary}".casefold()
            if (wanted and not _matches(haystack, wanted)) or (own and not _matches(haystack, own)):
                dropped["no_term"] += 1
                continue
            if _matches(haystack, terms):
                dropped["term"] += 1
                continue
            kept.append({"title": title, "link": link, "summary": summary, "published": published, "source": label})

    floor = datetime.min.replace(tzinfo=timezone.utc)
    kept.sort(key=lambda r: (r["published"] is None, -(r["published"] or floor).timestamp()))
    items = []
    for n, row in enumerate(kept[:max_items], start=1):
        item = {"id": f"c{n}", "title": row["title"], "link": row["link"], "summary": row["summary"],
                "published_at": _fmt(row["published"])}
        if row["source"] is not None:
            item["source"] = row["source"]
        items.append(item)
    listing = "\n".join(f"{i['id']}｜{i['title']}｜{i['summary'][:EXCERPT_MAX_CHARS]}" for i in items)
    result: dict[str, Any] = {"items": items, "listing": listing, "count": len(items), "dropped": dropped}
    if text is None:
        result["failed_feeds"] = failed
    return result


def register_feed_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[FEED_PARSE_TOOL_ID], feed_parse)


__all__ = ["feed_parse", "plain_text", "register_feed_tool"]
