"""`html.extract`: the readable article, or the matching links, of an HTML page. Pure: no I/O, no network.

A workflow fetches the page with `http.request` and hands its `text` here. The platform knows no publisher:
the page, the patterns and the allowed hosts are all arguments.

- Arguments: `eap_schemas.tools.HtmlExtractInput`. `html` over 2 MiB is `html_too_large`; an empty page, or one
  with no markup at all, is `html_unparseable`; no `include_patterns` in links mode is `no_patterns`. Also
  answered as results: `html_too_complex` (more than 50,000 elements, or nested deeper than 150 levels),
  `base_url_invalid` (not an https URL), `selector_invalid`, `invalid_pattern` (also more than 10 patterns, or one over 200 characters or empty). Errors are
  `{error: {code, message}}`, never exceptions.
- Parsing: selectolax (Lexbor, an HTML5 parser, tolerant of broken markup), CSS selectors by the same library.
  Regular expressions run on RE2 (linear time, the `re2` package; never the stdlib `re`): at most 10 patterns per
  list of at most 200 characters, a page chooses the URLs they see, never their cost.
- Safety: selectolax parses HTML5 only: no DTD, no entity beyond the HTML character references, no external
  resource, no network, no script run. The work is bounded: 2 MiB of input, 50,000 elements, 150 levels deep.
- `article`: `{title, text, published_at, canonical_url, og_image, description, lang, graphemes}`.
  - `title`: og:title, else the first h1, else the title element. `description`: og:description or the
    description meta, else "". `lang`: the html element's lang or null.
  - `published_at`: `article:published_time` (also og: and itemprop spellings), else `datePublished` of a
    JSON-LD block (graphs and lists searched), else the first `<time datetime>`; ISO 8601 text, null when absent
    or unreadable.
  - `canonical_url` (link rel=canonical, else og:url) and `og_image` (og:image, og:image:secure_url,
    twitter:image) are resolved against `base_url`; anything not an absolute https URL is null, and `og_image`
    is also null unless its host is `base_url`'s or in `allowed_hosts` (pass the image CDN host there).
  - `text`: a readability heuristic. Scripts, styles, navigation, footers, asides, forms and frames are
    removed; the container holding most of the paragraph text (an `<article>` or `itemprop=articleBody` if
    there is one, then the child that keeps at least 60% of its parent's paragraph text, repeatedly) is the
    content; inside it, blocks whose class or id names an ad, share bar, related list, comments and the like
    (and are small next to the content) are dropped. Paragraphs are one per line. Cut at `max_graphemes`
    (default 4000, extended grapheme clusters) without a trailing space.
  - Output text never contains markup-like `<tag` / `</tag` sequences (a page may carry escaped ones, so the
    `<` is dropped) or `<think>` blocks.
- `links`: `{items: [{id: l1.., url, title, order}]}`. Anchors under `root_selector` (the whole page when
  absent), their href resolved against `base_url`; https only (javascript:, data:, mailto: and http: are dropped). Dropped: a host that is neither `base_url`'s nor in
  `allowed_hosts` (the page controls its HTML: this is the same-origin guard), an URL matching none of
  `include_patterns` or any of `exclude_patterns` (patterns see the normalised URL), an anchor without a title.
  Normalised: fragment, `utm_*` and `fbclid` query keys and a trailing slash dropped, scheme and host lower
  case. Duplicates (by normalised URL) keep the first. `limit` (default 20, at most 60) in page order; `order`
  is the 1-based position in the result. `exclude_urls` (at most 200, compared by the same normalised URL, so
  utm, fragment and trailing-slash variants match) and `exclude_terms` (at most 50, case-insensitive substring of
  the title) drop links after the patterns and before `limit`, so `limit` counts kept links. The result also
  carries `dropped: {scheme, host, duplicate, pattern, no_title, excluded_url, excluded_term}` (counts of the
  anchors examined; scanning stops once `limit` links are kept). Title: the anchor's text, else its `title`, `aria-label`, or an
  image's `alt`; for an image-only link, the headline (first heading, else first paragraph) of the enclosing card, up to
  three levels up and only if the card holds no other article link; whitespace collapsed, at most 200 graphemes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import re2
import regex
from pydantic import Field
from pydantic_ai import RunContext
from selectolax.lexbor import LexborHTMLParser, LexborNode, SelectolaxError

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import HTML_EXTRACT_MAX_CHARS, HTML_EXTRACT_TOOL_ID, TOOL_DECLARATIONS

TITLE_MAX_GRAPHEMES = 200
MAX_PATTERN_COUNT = 10
MAX_PATTERN_CHARS = 200
MAX_NODES = 50_000
MAX_DEPTH = 150

_HARD_REMOVE = "script, style, noscript, template, iframe, svg, form, nav, footer, aside, button, select, canvas, dialog"
_NOISE_ATTR = re.compile(
    r"(?:^|[\s_-])(?:ads?|adv|advert\w*|banner|breadcrumbs?|comments?|footer|menu|navbar|nav|popup|promo\w*|related|"
    r"share|sharing|sidebar|social|subscribe|recommend\w*|newsletter|taboola|outbrain|paywall|toolbar|tags?)(?:$|[\s_-])",
    re.I,
)
_BLOCKS = frozenset(
    "p div br li ul ol h1 h2 h3 h4 h5 h6 blockquote section article tr table pre figcaption dd dt dl hr main header".split()
)
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_MARKUP = re.compile(r"<(?=[A-Za-z/!?<])")
_SPACE = re.compile(r"\s+")
_GRAPHEME = regex.compile(r"\X")
_TRACKING = re.compile(r"^(?:utm_.*|fbclid)$", re.I)


_TAG = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9:-]*)[^>]*?(/?)>")
_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
PRESCAN_DEPTH = 5000


def _nests_absurdly(html: str) -> bool:
    """Whether the tags, counted without a parser, nest deeper than PRESCAN_DEPTH. Lexbor's time grows with the
    square of the depth (190,000 open divs take a minute), so such a page is refused before it is parsed. A
    generous bound: unclosed `<p>` and `<li>` raise the count, a real page does not reach it."""
    depth = 0
    for closing, name, selfclosing in _TAG.findall(html):
        if closing:
            depth = max(0, depth - 1)
        elif not selfclosing and name.lower() not in _VOID:
            depth += 1
            if depth > PRESCAN_DEPTH:
                return True
    return False


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _clean(text: str) -> str:
    """No think-block, no markup-looking sequence (decoded entities can spell one)."""
    text = _THINK.sub("", text)
    if re.search(r"<think>", text, re.I):
        text = text[: re.search(r"<think>", text, re.I).start()]  # type: ignore[union-attr]
    while (stripped := _MARKUP.sub("", text)) != text:  # "<<script": removing one `<` can expose another tag start
        text = stripped
    return text


def _cut(text: str, limit: int) -> str:
    clusters = _GRAPHEME.findall(text)
    return text if len(clusters) <= limit else "".join(clusters[:limit]).rstrip()


def _graphemes(text: str) -> int:
    return len(_GRAPHEME.findall(text))


def _oneline(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _https(url: str | None, base_url: str) -> str | None:
    if not url or not url.strip():
        return None
    absolute = urljoin(base_url, url.strip())
    try:
        parts = urlsplit(absolute)
    except ValueError:
        return None
    return absolute if parts.scheme == "https" and parts.hostname else None


# ------------------------------------------------------------------------------------- tree


class _N:
    """A plain copy of an element (tag, attributes, children) or text (tag "#text"): identity is stable."""

    __slots__ = ("tag", "attrs", "children", "text", "score", "length")

    def __init__(self, tag: str, attrs: dict[str, str | None], text: str = "") -> None:
        self.tag, self.attrs, self.text = tag, attrs, text
        self.children: list[_N] = []
        self.score = 0
        self.length = 0


class _TooComplex(Exception):
    pass


def _copy(node: LexborNode, depth: int = 0) -> _N:
    if depth > MAX_DEPTH:
        raise _TooComplex
    out = _N(node.tag, dict(node.attributes))
    for child in node.iter(include_text=True):
        if child.tag == "-text":
            out.children.append(_N("#text", {}, child.text(deep=False) or ""))
        elif not child.tag.startswith("-"):
            out.children.append(_copy(child, depth + 1))
    return out


def _text_of(node: _N) -> str:
    if node.tag == "#text":
        return node.text
    return "".join(_text_of(c) for c in node.children)


def _link_text(node: _N) -> int:
    if node.tag == "a":
        return len(_oneline(_text_of(node)))
    return sum(_link_text(c) for c in node.children)


def _measure(node: _N) -> int:
    """Characters of text under `node`, stored on it (the noise test reads it)."""
    node.length = len(node.text) if node.tag == "#text" else sum(_measure(c) for c in node.children)
    return node.length


def _score(node: _N) -> int:
    """Paragraph text under `node`: a `p` of 15 or more characters and mostly not links counts its length."""
    if node.tag == "p":
        text = _oneline(_text_of(node))
        node.score = len(text) if len(text) >= 15 and _link_text(node) <= 0.5 * len(text) else 0
        return node.score
    node.score = sum(_score(c) for c in node.children)
    return node.score


def _best_article(node: _N) -> _N | None:
    found: list[_N] = []

    def walk(n: _N) -> None:
        if n.tag == "article" or n.attrs.get("itemprop") == "articleBody":
            found.append(n)
        for child in n.children:
            walk(child)

    walk(node)
    return max(found, key=lambda n: n.score, default=None) if found and max(n.score for n in found) > 0 else None


def _descend(node: _N) -> _N:
    while True:
        best = max((c for c in node.children if c.tag != "#text"), key=lambda c: c.score, default=None)
        if best is None or best.score < 0.6 * node.score or best.tag == "p":
            return node
        node = best


def _noisy(node: _N, content: int) -> bool:
    marker = f"{node.attrs.get('class') or ''} {node.attrs.get('id') or ''}"
    return bool(_NOISE_ATTR.search(marker)) and node.length < 0.5 * content


def _render(root: _N) -> str:
    content = root.length
    lines: list[str] = []
    current: list[str] = []

    def flush() -> None:
        line = _oneline("".join(current))
        current.clear()
        if line:
            lines.append(line)

    def walk(n: _N) -> None:
        if n.tag == "#text":
            current.append(n.text)
            return
        if n is not root and _noisy(n, content):
            return
        block = n.tag in _BLOCKS
        if block:
            flush()
        for child in n.children:
            walk(child)
        if block:
            flush()

    walk(root)
    return "\n".join(lines)


def _readable(parser: LexborHTMLParser) -> str:
    for node in parser.css(_HARD_REMOVE):
        node.decompose()
    body = parser.body
    if body is None:
        return ""
    tree = _copy(body)
    _measure(tree)
    _score(tree)
    start = _best_article(tree) or tree
    chosen = _descend(start) if start.score else start
    return _render(chosen)


# ------------------------------------------------------------------------------------- metadata


def _meta(parser: LexborHTMLParser, *keys: str) -> str | None:
    for key in keys:
        for node in parser.css("meta"):
            attrs = node.attributes
            if (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower() == key:
                content = (attrs.get("content") or "").strip()
                if content:
                    return content
    return None


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def _json_ld_date(node: Any) -> str | None:
    if isinstance(node, list):
        return next((d for item in node if (d := _json_ld_date(item))), None)
    if isinstance(node, dict):
        direct = _iso(node.get("datePublished"))
        if direct:
            return direct
        return _json_ld_date(node.get("@graph")) if "@graph" in node else None
    return None


def _published_at(parser: LexborHTMLParser) -> str | None:
    found = _iso(_meta(parser, "article:published_time", "og:article:published_time", "datepublished"))
    if found:
        return found
    for node in parser.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text(deep=True) or "")
        except ValueError:
            continue
        found = _json_ld_date(data)
        if found:
            return found
    for node in parser.css("time[datetime]"):
        found = _iso(node.attributes.get("datetime"))
        if found:
            return found
    return None


def _article(parser: LexborHTMLParser, base_url: str, max_graphemes: int, allowed: set[str]) -> dict[str, Any]:
    # Metadata first: the readability pass removes nodes from the document.
    h1 = parser.css_first("h1")
    title = _meta(parser, "og:title") or (_oneline(h1.text()) if h1 and h1.text().strip() else None)
    if not title:
        node = parser.css_first("title")
        title = _oneline(node.text()) if node else ""
    canonical = parser.css_first('link[rel="canonical"]')
    canonical_url = _https(canonical.attributes.get("href") if canonical else None, base_url) or _https(_meta(parser, "og:url"), base_url)
    og_image = _https(_meta(parser, "og:image", "og:image:secure_url", "twitter:image"), base_url)
    if og_image and (urlsplit(og_image).hostname or "").lower() not in allowed:
        og_image = None
    description = _meta(parser, "og:description", "description") or ""
    html_node = parser.css_first("html")
    lang = (html_node.attributes.get("lang") or "").strip() if html_node else ""
    published = _published_at(parser)
    text = _cut(_clean(_readable(parser)), max_graphemes)
    return {
        "title": _oneline(_clean(title)),
        "text": text,
        "published_at": published,
        "canonical_url": canonical_url,
        "og_image": og_image,
        "description": _oneline(_clean(description)),
        "lang": lang or None,
        "graphemes": _graphemes(text),
    }


# ------------------------------------------------------------------------------------- links


def _normalise(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def _safe_normalise(url: str) -> str | None:
    try:
        return _normalise(url.strip()) if url.strip() else None
    except ValueError:
        return None


def _anchor_title(anchor: LexborNode) -> str:
    candidates = [anchor.text(), anchor.attributes.get("title"), anchor.attributes.get("aria-label")]
    img = anchor.css_first("img[alt]")
    candidates.append(img.attributes.get("alt") if img else None)
    for candidate in candidates:
        title = _oneline(candidate or "")
        if title:
            return _cut(_clean(title), TITLE_MAX_GRAPHEMES)
    return ""


def _compile(patterns: list[str]) -> list[Any] | str:
    compiled = []
    if len(patterns) > MAX_PATTERN_COUNT:
        return "too many"
    for pattern in patterns:
        if not pattern or len(pattern) > MAX_PATTERN_CHARS:
            return pattern or "empty"
        try:
            compiled.append(re2.compile(pattern))
        except re2.error:
            return pattern
    return compiled


def _hosts(base_url: str, allowed_hosts: list[str] | None) -> set[str]:
    return {(urlsplit(base_url).hostname or "").lower(), *(h.strip().lower() for h in allowed_hosts or [])}


_CARD_LEVELS = 3
_CARD_BARRED = {"nav", "aside", "footer", "button"}


def _article_url(anchor: LexborNode, base_url: str, hosts: set[str], inc: list[Any], exc: list[Any]) -> str | None:
    """The normalised https article URL of an anchor: same host and pattern checks as the links loop."""
    href = (anchor.attributes.get("href") or "").strip()
    try:
        absolute = urljoin(base_url, href)
        parts = urlsplit(absolute)
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if parts.scheme != "https" or host not in hosts:
        return None
    url = _normalise(absolute)
    if not any(p.search(url) for p in inc) or any(p.search(url) for p in exc):
        return None
    return url


def _own_text(node: LexborNode) -> str:
    """Text of a node in document order, without anything inside a nested anchor (depth is capped upstream)."""
    parts: list[str] = []
    for child in node.iter(include_text=True):
        if child.tag == "-text":
            parts.append(child.text_content or "")
        elif child.tag != "a":
            parts.append(_own_text(child))
    return "".join(parts)


def _barred(node: LexborNode) -> bool:
    parent: LexborNode | None = node
    while parent is not None:
        if parent.tag in _CARD_BARRED or parent.tag == "a":
            return True
        parent = parent.parent
    return False


def _one_article_url(container: LexborNode, own: str, base_url: str, hosts: set[str], inc: list[Any], exc: list[Any]) -> bool:
    for node in container.iter():
        if node.tag == "a" and (url := _article_url(node, base_url, hosts, inc, exc)) not in (None, own):
            return False
    return True


def _card_title(anchor: LexborNode, own: str, inc: list[Any], exc: list[Any], hosts: set[str], base_url: str) -> str:
    """Headline of the card an image-only anchor sits in: the first heading, else the first paragraph, of the nearest
    enclosing container (at most three levels up) that holds no other article URL."""
    container = anchor.parent
    for _ in range(_CARD_LEVELS):
        if container is None or container.tag in ("body", "html", "-undef"):
            return ""
        if not _one_article_url(container, own, base_url, hosts, inc, exc):
            return ""
        for selector in ("h1,h2,h3,h4,h5,h6", "p"):
            for candidate in container.css(selector):
                if _barred(candidate):
                    continue
                title = _oneline(_own_text(candidate))
                if title:
                    return _cut(_clean(title), TITLE_MAX_GRAPHEMES)
        container = container.parent
    return ""


def _links(
    parser: LexborHTMLParser, base_url: str, root_selector: str | None, include: list[str] | None,
    exclude: list[str] | None, allowed_hosts: list[str] | None, limit: int,
    exclude_urls: list[str] | None = None, exclude_terms: list[str] | None = None,
) -> dict[str, Any]:
    if not include:
        return _error("no_patterns", "include_patterns needs at least one pattern")
    inc, exc = _compile(include), _compile(exclude or [])
    if isinstance(inc, str) or isinstance(exc, str):
        return _error("invalid_pattern", "a pattern is not a valid RE2 regular expression")
    try:
        roots = parser.css(root_selector) if root_selector else [parser.root]
    except SelectolaxError:
        return _error("selector_invalid", "root_selector is not a valid CSS selector")
    hosts = _hosts(base_url, allowed_hosts)
    blocked = {u for raw in exclude_urls or [] if (u := _safe_normalise(raw))}
    terms = [t.casefold() for t in exclude_terms or [] if t.strip()]
    dropped = {"scheme": 0, "host": 0, "duplicate": 0, "pattern": 0, "no_title": 0, "excluded_url": 0, "excluded_term": 0}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if root is None:
            continue
        for anchor in root.css("a[href]"):
            href = (anchor.attributes.get("href") or "").strip()
            try:
                absolute = urljoin(base_url, href)
                parts = urlsplit(absolute)
                host = (parts.hostname or "").lower()
            except ValueError:
                continue
            if parts.scheme != "https":
                dropped["scheme"] += 1
                continue
            if host not in hosts:
                dropped["host"] += 1
                continue
            url = _normalise(absolute)
            if url in seen:
                dropped["duplicate"] += 1
                continue
            if not any(p.search(url) for p in inc) or any(p.search(url) for p in exc):
                dropped["pattern"] += 1
                continue
            title = _anchor_title(anchor) or _card_title(anchor, url, inc, exc, hosts, base_url)
            if not title:
                dropped["no_title"] += 1
                continue
            seen.add(url)
            if url in blocked:
                dropped["excluded_url"] += 1
                continue
            if any(t in title.casefold() for t in terms):
                dropped["excluded_term"] += 1
                continue
            items.append({"id": f"l{len(items) + 1}", "url": url, "title": title, "order": len(items) + 1})
            if len(items) >= limit:
                return {"items": items, "dropped": dropped}
    return {"items": items, "dropped": dropped}


# ------------------------------------------------------------------------------------- the tool


def html_extract(
    ctx: RunContext[RunDeps],
    html: str,
    base_url: str,
    mode: Literal["article", "links"],
    max_graphemes: Annotated[int, Field(ge=1, le=20000)] = 4000,
    root_selector: Annotated[str, Field(max_length=300)] | None = None,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    allowed_hosts: Annotated[list[Annotated[str, Field(min_length=1, max_length=253)]], Field(max_length=20)] | None = None,
    limit: Annotated[int, Field(ge=1, le=60)] = 20,
    exclude_urls: Annotated[list[str], Field(max_length=200)] | None = None,
    exclude_terms: Annotated[list[str], Field(max_length=50)] | None = None,
) -> dict[str, Any]:
    """Extract the article or the links of an HTML page (see the module docstring)."""
    if len(html) > HTML_EXTRACT_MAX_CHARS or len(html.encode("utf-8", errors="replace")) > HTML_EXTRACT_MAX_CHARS:
        return _error("html_too_large", "html is larger than 2 MiB")
    if urlsplit(base_url).scheme != "https" or not urlsplit(base_url).hostname:
        return _error("base_url_invalid", "base_url must be an https URL")
    if "<" not in html:
        return _error("html_unparseable", "the text has no markup")
    if _nests_absurdly(html):
        return _error("html_too_complex", "the page is nested too deeply")
    try:
        parser = LexborHTMLParser(html)
    except Exception:  # noqa: BLE001 - any parser failure is the stable code; the text is never echoed
        return _error("html_unparseable", "the page could not be parsed")
    if len(parser.css("*")) > MAX_NODES:
        return _error("html_too_complex", f"the page has more than {MAX_NODES} elements")
    if mode == "links":
        return _links(parser, base_url, root_selector, include_patterns, exclude_patterns, allowed_hosts, limit, exclude_urls, exclude_terms)
    try:
        return _article(parser, base_url, max_graphemes, _hosts(base_url, allowed_hosts))
    except (_TooComplex, RecursionError):
        return _error("html_too_complex", "the page is nested too deeply")


def register_html_extract_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[HTML_EXTRACT_TOOL_ID], html_extract)


__all__ = ["html_extract", "register_html_extract_tool"]
