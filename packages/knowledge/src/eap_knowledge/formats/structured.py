"""YAML and JSON extractors (RAG P1, worker side; run in the sandbox child).

``extract_yaml(text, *, title, source_url, max_tokens)`` and ``extract_json(...)`` return the same
``Document`` model as ``extract()``:

- **One section per top-level key**: the heading is the key, the anchor is the key
  (``source_url#<key>``) and the body is ``yaml.safe_dump({key: value})`` (block style, key order
  kept, Unicode kept, width 100). JSON is rendered as YAML too, so both read the same.
- **One level deeper** when a section is over ``max_tokens`` and its value is a mapping: the key
  becomes a heading with no body and each sub-key its own section (heading path
  ``properties > reg-names``, anchor ``properties.reg-names``). A device-tree binding therefore gives
  one section per property.
- **Multi-document YAML streams** (``---``): each document becomes a level-1 section
  ``Document <n>`` (anchor ``doc-<n>``) holding its keys one level down. A document that is not a
  mapping (a list or a scalar) is one section without a heading.
- **Title**, in order of precedence: (1) the ``title`` argument when it is not blank; (2) the
  first document's first top-level ``title``, ``name`` or ``description`` (in that order of keys)
  whose value is a one-line string of at most TITLE_MAX_CHARS characters (whitespace collapsed);
  (3) the file name, the last path segment of ``source_url`` (percent-decoded, without query or
  fragment, control characters stripped, whitespace collapsed, clipped to TITLE_MAX_CHARS);
  (4) ``''``. The upload API passes ``body.title`` or else the document key, and re-extract passes
  the stored title, so a stored document always takes (1): the fallbacks serve direct callers
  only, and no extractor version was bumped for them (json stays 1).
- **Comments** (YAML; extractor version 2): device-tree bindings often keep their only description
  in comments, so they are kept. The stream's leading comment block (the first run of consecutive
  comment lines, before any content) is the document-level preamble: the body of the level-0
  section, as plain text. Every other comment belongs to a key: one on the key's line or inside its
  value goes to that key; one between two keys goes to the key below it (a comment describes what
  follows); one after the last key goes to the last key. A key's comments lead its section's body as
  ``# ...`` lines above the dumped YAML. When a section splits one level deeper, comments inside the
  value go to the sub-keys by the same rule. Comments are read by the same scanner that parses the
  stream (recorded where PyYAML skips them), so nothing is parsed twice and every limit below
  applies unchanged; a comment on a block scalar's indicator line (``key: |  # c``) is not kept,
  nor are licence lines (``SPDX-License-Identifier:``, ``Copyright (c) ...``) or empty rules.
- **A short preamble rides with the first section** (yaml 3): when the leading comment block is at
  most ``max_tokens // PREAMBLE_SHARE`` tokens, its text opens the body of the first content section,
  followed by a blank line, and the level-0 section's body is empty. The first content section is
  the first leaf: the first section that has no subsections, so a heading-only section is skipped:
  the first key's section, or that key's first child when the key splits one level deeper, and in
  a multi-document stream the first key of ``Document 1`` (or ``Document 1`` itself when it is not
  a mapping). So a short binding's header comment reaches the same
  chunk as the facts it describes under either chunker version (chunker v2 never merges the
  preamble). A longer block stays the level-0 section's body, a chunk of its own that the chunker
  splits at ``max_tokens`` as usual. Which section a key splits into is decided before the preamble
  is added, so the preamble never makes a section split.

Safety: YAML is loaded with a ``SafeLoader`` subclass that counts composed nodes and the characters
they hold, charging an alias the full size of the node it repeats (so a billion-laughs document, or
one long string or list repeated through aliases, is measured expanded), and alias references; it
fails ``extract_limit`` past MAX_YAML_NODES, 4 x the structured cap in characters, MAX_YAML_ALIASES or
MAX_NESTING. JSON is checked for nesting depth (MAX_NESTING) before ``json.loads``. Every extractor
also fails ``extract_limit`` when its output is over ``output_budget`` (16 x the input for YAML and
JSON, which render as indented YAML, at least 8 MiB). Invalid input (syntax, an impossible date, an integer too long to convert) fails
``extract_failed`` with a line and column only (never document text).
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml
from yaml.events import AliasEvent, CollectionStartEvent, ScalarEvent

from eap_knowledge.errors import ExtractionFailed
from eap_knowledge.extract import _build, _normalize
from eap_knowledge.config import ExtractLimits
from eap_knowledge.formats._common import STRUCTURED_OUTPUT_FACTOR, check_output_size
from eap_knowledge.model import Document, Section, approx_tokens

MAX_YAML_NODES = 200_000
MAX_YAML_ALIASES = 1_000
# Comments the scanner records (each also keeps its text, at most the input's size in all).
MAX_YAML_COMMENTS = 100_000
# Characters of scalar text (plus one per node) a YAML stream may expand to through its aliases:
# YAML_EXPANSION_FACTOR x the structured-file cap the caller passes (EAP_KB_MAX_STRUCTURED_BYTES).
YAML_EXPANSION_FACTOR = 4
DEFAULT_STRUCTURED_CAP = ExtractLimits().max_structured_bytes  # 2 MiB
MAX_YAML_EXPANDED_CHARS = YAML_EXPANSION_FACTOR * DEFAULT_STRUCTURED_CAP  # at the default cap
MAX_NESTING = 64
# A leading comment block of at most max_tokens // PREAMBLE_SHARE tokens joins the first section.
PREAMBLE_SHARE = 4
# The longest top-level `title` / `name` / `description` string used as the document title.
TITLE_MAX_CHARS = 128
_TITLE_KEYS = ("title", "name", "description")
_DUMP_WIDTH = 100
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")
# Licence boilerplate in comments (most Zephyr bindings open with it): never kept, it says nothing
# about the file and would repeat in every binding's chunk.
_LICENCE_LINE = re.compile(r"^\s*(SPDX-License-Identifier:|SPDX-FileCopyrightText:|Copyright\s*(\(c\)|©|\d))", re.I)


class _CountingLoader(yaml.SafeLoader):
    """``SafeLoader`` with node, alias and depth limits, checked while composing (before any
    object is constructed)."""

    def __init__(self, stream: str, max_chars: int = MAX_YAML_EXPANDED_CHARS) -> None:
        super().__init__(stream)
        self.eap_max_chars = max_chars
        self.eap_nodes = 0
        self.eap_aliases = 0
        self.eap_depth = 0
        self.eap_chars = 0
        self.eap_sizes: dict[str, tuple[int, int]] = {}  # anchor -> (nodes, characters) it expands to
        self.eap_comments: list[tuple[int, str]] = []  # (0-based line, the comment text from its '#')

    def scan_to_next_token(self) -> None:
        """PyYAML's own ``Scanner.scan_to_next_token`` (6.0.3, pinned), also recording each comment
        it skips with its line: the comments come from the one scan that parses the stream. Only the
        lines that mention ``eap_`` or set ``line, start`` differ from upstream (a test compares the
        two, so a PyYAML upgrade that changes the scanner fails instead of diverging). Bounded like
        the composer: the input is at most the structured cap (``_load_yaml``) and at most
        MAX_YAML_COMMENTS comments are kept (more is ``extract_limit``)."""
        if self.index == 0 and self.peek() == "\uFEFF":
            self.forward()
        found = False
        while not found:
            while self.peek() == " ":
                self.forward()
            if self.peek() == "#":
                line, start = self.line, self.pointer
                while self.peek() not in "\0\r\n\x85\u2028\u2029":
                    self.forward()
                self.eap_comments.append((line, self.buffer[start : self.pointer]))
                if len(self.eap_comments) > MAX_YAML_COMMENTS:  # eap_ limit
                    raise ExtractionFailed("extract_limit", f"more than {MAX_YAML_COMMENTS} YAML comments")
            if self.scan_line_break():
                if not self.flow_level:
                    self.allow_simple_key = True
            else:
                found = True

    def compose_document(self) -> Any:
        self.eap_sizes = {}  # anchors are per document (the composer resets its own map too)
        return super().compose_document()

    def construct_object(self, node: Any, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except ExtractionFailed:
            raise
        except (ValueError, OverflowError, TypeError):
            # An impossible timestamp (2024-13-45), an integer over Python's digit limit, an
            # unhashable key: position only, never the value.
            mark = node.start_mark
            raise ExtractionFailed(
                "extract_failed", f"invalid YAML value at line {mark.line + 1}, column {mark.column + 1}"
            ) from None

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            event = self.peek_event()
            self.eap_aliases += 1
            if self.eap_aliases > MAX_YAML_ALIASES:
                raise ExtractionFailed("extract_limit", f"more than {MAX_YAML_ALIASES} YAML aliases")
            nodes, chars = self.eap_sizes.get(event.anchor, (1, 1))
            self._count(nodes, chars)
            return super().compose_node(parent, index)
        event = self.peek_event()
        anchor = event.anchor if isinstance(event, (ScalarEvent, CollectionStartEvent)) else None
        start, start_chars = self.eap_nodes, self.eap_chars
        self._count(1, 1 + (len(event.value) if isinstance(event, ScalarEvent) else 0))
        self.eap_depth += 1
        if self.eap_depth > MAX_NESTING:
            raise ExtractionFailed("extract_limit", f"YAML nested deeper than {MAX_NESTING}")
        try:
            node = super().compose_node(parent, index)
        finally:
            self.eap_depth -= 1
        if anchor is not None:
            self.eap_sizes[anchor] = (self.eap_nodes - start, self.eap_chars - start_chars)
        return node

    def _count(self, nodes: int, chars: int) -> None:
        self.eap_nodes += nodes
        self.eap_chars += chars
        if self.eap_nodes > MAX_YAML_NODES:
            raise ExtractionFailed("extract_limit", f"more than {MAX_YAML_NODES} YAML nodes")
        if self.eap_chars > self.eap_max_chars:
            raise ExtractionFailed("extract_limit", f"YAML expands to over {self.eap_max_chars} characters")


def load_yaml_documents(text: str, structured_cap: int = DEFAULT_STRUCTURED_CAP) -> list[Any]:
    """Every document of a YAML stream, safely (see the module docstring). ``structured_cap`` is
    the configured YAML/JSON size cap; the alias expansion budget is YAML_EXPANSION_FACTOR times it."""
    return _load_yaml(text, structured_cap).documents


@dataclass(frozen=True)
class _Pos:
    """Where a mapping key sits in the source: its heading (as ``_heading`` renders the constructed
    key), its line, the last line of its value, and its value's own keys (None unless the value is a
    block or flow mapping written right there, not an alias to one elsewhere)."""

    heading: str
    line: int
    last: int
    children: tuple[_Pos, ...] | None


@dataclass(frozen=True)
class _Loaded:
    documents: list[Any]
    # Per document: (first line, last line, its top-level keys or None when not a mapping).
    spans: list[tuple[int, int, tuple[_Pos, ...] | None]]
    comments: list[tuple[int, str]]


_MERGE_TAG = "tag:yaml.org,2002:merge"


def _last_line(node: Any) -> int:
    """The last source line holding the node's own content. A block collection's end mark sits at
    the next token, past any comments in between, so it is the last child's last line instead; a
    block scalar's end mark is the start of the following line."""
    if isinstance(node, yaml.ScalarNode):
        end = node.end_mark
        return end.line - 1 if end.column == 0 and end.line > node.start_mark.line else end.line
    if getattr(node, "flow_style", False) or not node.value:
        return node.end_mark.line
    if isinstance(node, yaml.MappingNode):
        key, value = node.value[-1]
        return max(key.start_mark.line, _last_line(value))
    return max(node.value[-1].start_mark.line, _last_line(node.value[-1]))


def _positions(loader: _CountingLoader, node: Any, depth: int = 0) -> tuple[_Pos, ...] | None:
    """The keys of a mapping node, before construction (which flattens merge keys in place). Keys
    that are not scalars, and merge keys, are left out: their comments go to a neighbouring key.
    Two levels are enough (a section splits at most one level deeper)."""
    if not isinstance(node, yaml.MappingNode):
        return None
    out = []
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.tag == _MERGE_TAG:
            continue
        try:
            heading = _heading(loader.construct_object(key))
        except ExtractionFailed:
            raise
        except Exception:  # noqa: BLE001 - an odd key only loses its comments; construction reports it
            continue
        line = key.start_mark.line
        # An alias's node carries its anchor's (earlier) marks: no children, and the key line bounds it.
        here = value.start_mark.line >= line
        children = _positions(loader, value, depth + 1) if here and depth == 0 else None
        out.append(_Pos(heading, line, max(line, _last_line(value)) if here else line, children))
    return tuple(out)


def _load_yaml(text: str, structured_cap: int = DEFAULT_STRUCTURED_CAP) -> _Loaded:
    if len(text) > structured_cap:
        # The upload and import paths cap YAML at this size already (bytes, so at least as many as
        # characters); a direct caller gets the same bound, so neither the scanner nor the comment
        # record ever sees more.
        raise ExtractionFailed("extract_limit", f"YAML over {structured_cap} characters")
    loader: _CountingLoader | None = None
    try:
        # Inside the try: the reader refuses a non-printable character (a control character the
        # normaliser kept) in its constructor, and that is invalid YAML like any other.
        loader = _CountingLoader(text, YAML_EXPANSION_FACTOR * structured_cap)
        documents: list[Any] = []
        spans: list[tuple[int, int, tuple[_Pos, ...] | None]] = []
        while loader.check_node():
            node = loader.get_node()  # composed under the node, alias and depth limits
            spans.append((node.start_mark.line, _last_line(node), _positions(loader, node)))
            documents.append(loader.construct_document(node))
        return _Loaded(documents, spans, loader.eap_comments)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown position"
        raise ExtractionFailed("extract_failed", f"invalid YAML at {where}") from None
    except yaml.YAMLError:
        raise ExtractionFailed("extract_failed", "invalid YAML") from None
    except RecursionError:
        raise ExtractionFailed("extract_limit", "YAML nested too deeply") from None
    finally:
        if loader is not None:
            loader.dispose()


def _json_depth_ok(text: str) -> bool:
    """Whether brackets outside strings nest at most MAX_NESTING deep (a linear scan)."""
    depth = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_NESTING:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def load_json(text: str) -> Any:
    if not _json_depth_ok(text):
        raise ExtractionFailed("extract_limit", f"JSON nested deeper than {MAX_NESTING}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionFailed("extract_failed", f"invalid JSON at line {exc.lineno}, column {exc.colno}") from None
    except RecursionError:
        raise ExtractionFailed("extract_limit", "JSON nested too deeply") from None
    except (ValueError, OverflowError):  # an integer over Python's digit limit; no position given
        raise ExtractionFailed("extract_failed", "invalid JSON number") from None


# --- sections -------------------------------------------------------------------------------------

# A raw section: (level, heading, body, explicit anchor).
_Raw = tuple[int, str, str, str | None]


def _clean(text: str) -> str:
    return _CONTROL.sub("", text)


def _heading(key: Any) -> str:
    return _WS.sub(" ", _clean(key if isinstance(key, str) else _scalar_text(key))).strip()


def _scalar_text(value: Any) -> str:
    """A key that is not a string, as YAML would write it (``true``, ``8``, ``2024-01-01``)."""
    try:
        text = yaml.safe_dump(value, default_flow_style=True, width=_DUMP_WIDTH)
    except yaml.YAMLError:
        return str(value)
    return text.removesuffix("\n...\n").strip()


class _Dumper(yaml.SafeDumper):
    """``SafeDumper`` writing multi-line strings as literal blocks (``|``), as they were written."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\n" not in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value)
    # A literal block cannot hold trailing spaces before a line break (PyYAML would fall back to a
    # quoted string full of escapes), so they go: the text reads as written, minus that whitespace.
    # PyYAML still falls back by itself for anything else a block cannot hold.
    value = "\n".join(line.rstrip(" \t") for line in value.split("\n"))
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")


_Dumper.add_representer(str, _represent_str)


def _dump(value: Any) -> str:
    try:
        text = yaml.dump(
            value, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=_DUMP_WIDTH, default_flow_style=False
        )
    except yaml.YAMLError:
        raise ExtractionFailed("extract_failed", "a value cannot be rendered") from None
    return _clean(text.removesuffix("...\n")).rstrip("\n")


_Comments = list[tuple[int, str]]


def _assign(comments: _Comments, keys: Sequence[_Pos]) -> tuple[_Comments, list[_Comments]]:
    """Split ``comments`` (sorted by line) between ``keys`` (in source order): (those before the
    first key's line, one list per key). A comment on a key's line or within its value is that
    key's; one after a key's value goes to the next key, or to the last key when none follows."""
    before: _Comments = []
    per: list[_Comments] = [[] for _ in keys]
    lines = [k.line for k in keys]
    for comment in comments:
        i = bisect_right(lines, comment[0]) - 1
        if i < 0:
            before.append(comment)
        elif comment[0] <= keys[i].last or i == len(keys) - 1:
            per[i].append(comment)
        else:
            per[i + 1].append(comment)
    return before, per


def _comment_lines(comments: _Comments) -> list[str]:
    """Each comment's text: the leading ``#`` marks and one space removed, control characters
    stripped; comments with no text (``#``, ``#####`` rules) and licence lines (SPDX, Copyright)
    are dropped."""
    out = []
    for _, raw in comments:
        line = _clean(raw.lstrip("#").removeprefix(" ").rstrip())
        if line.strip() and not _LICENCE_LINE.match(line):
            out.append(line)
    return out


def _plain(comments: _Comments) -> str:
    return "\n".join(_comment_lines(comments))


def _with_comments(comments: _Comments, body: str) -> str:
    lines = _comment_lines(comments)
    if not lines:
        return body
    lead = "\n".join(f"# {line}" for line in lines)
    return f"{lead}\n{body}" if body else lead


def _by_heading(keys: Sequence[_Pos], per: list[_Comments]) -> dict[str, tuple[_Pos, _Comments]]:
    out: dict[str, tuple[_Pos, _Comments]] = {}
    for pos, comments in zip(keys, per):
        if pos.heading in out:  # two keys with one heading (8 and "8"): their comments together
            first, earlier = out[pos.heading]
            out[pos.heading] = (first, earlier + comments)
        else:
            out[pos.heading] = (pos, comments)
    return out


def _mapping_sections(
    mapping: Mapping[Any, Any],
    level: int,
    prefix: str,
    max_tokens: int,
    keys: Sequence[_Pos] = (),
    comments: _Comments | None = None,
) -> tuple[_Comments, list[_Raw]]:
    """The mapping's sections, and the comments that fall before its first key (the caller places
    them). ``keys`` are the mapping's source positions, ``comments`` the ones inside it."""
    before, per = _assign(comments or [], keys)
    placed = _by_heading(keys, per)
    raw: list[_Raw] = []
    for key, value in mapping.items():
        heading = _heading(key)
        anchor = f"{prefix}{heading}" if heading else None
        pos, own = placed.pop(heading, (None, []))
        body = _dump({key: value})
        if isinstance(value, Mapping) and value and approx_tokens(_with_comments(own, body)) > max_tokens:
            children = pos.children if pos is not None and pos.children else ()
            head = [c for c in own if c[0] <= pos.line] if pos is not None else own
            inner = [c for c in own if pos is not None and c[0] > pos.line]
            first, sub_per = _assign(inner, children)
            sub_placed = _by_heading(children, sub_per)
            if not children:
                head = own  # no positions below (an alias): every comment stays on the key
            raw.append((level, heading, _with_comments(head, ""), anchor))
            for n, (sub_key, sub_value) in enumerate(value.items()):
                sub_heading = _heading(sub_key)
                sub_anchor = f"{anchor}.{sub_heading}" if anchor and sub_heading else anchor
                _, sub_own = sub_placed.pop(sub_heading, (None, []))
                if n == 0:
                    sub_own = first + sub_own
                raw.append((level + 1, sub_heading, _with_comments(sub_own, _dump({sub_key: sub_value})), sub_anchor))
        else:
            raw.append((level, heading, _with_comments(own, body), anchor))
    return before, raw


def _leading_block(comments: _Comments, first_line: int) -> tuple[_Comments, _Comments]:
    """(the stream's leading comment block, the rest): consecutive comment lines from the first
    one, all before ``first_line`` (the first content line)."""
    block: _Comments = []
    for comment in comments:
        if comment[0] >= first_line or (block and comment[0] != block[-1][0] + 1):
            break
        block.append(comment)
    return block, comments[len(block):]


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def _sections(loaded: _Loaded, max_tokens: int) -> list[_Raw]:
    documents, spans = loaded.documents, loaded.spans
    comments = sorted(loaded.comments, key=lambda c: c[0])
    first_line = spans[0][0] if spans else 1 << 62
    preamble, rest = _leading_block(comments, first_line)
    if not documents:
        return [(0, "", _plain(comments), None)]
    doc_keys = [_Pos(f"doc-{n}", start, last, None) for n, (start, last, _) in enumerate(spans, 1)]
    before, per_doc = _assign(rest, doc_keys)
    if per_doc:
        per_doc[0] = before + per_doc[0]
    else:  # JSON: no source positions and no comments
        per_doc = [[] for _ in documents]
    head = _plain(preamble)
    short = bool(head) and approx_tokens(head) <= max_tokens // PREAMBLE_SHARE
    if len(documents) == 1:
        document, keys = documents[0], (spans[0][2] if spans else None)
        if isinstance(document, Mapping) and document:
            lead, raw = _mapping_sections(document, 1, "", max_tokens, keys or (), per_doc[0])
            if raw:
                level, heading, body, anchor = raw[0]
                raw[0] = (level, heading, _with_comments(lead, body), anchor)
                if short:
                    _carry(raw, head)
                    head = ""
            return [(0, "", head, None), *raw]
        body = "" if document is None else _dump(document)
        return [(0, "", _join(head, _plain(per_doc[0]), body), None)]
    raw_all: list[_Raw] = []
    for n, (document, (_, _, keys), doc_comments) in enumerate(zip(documents, spans, per_doc), 1):
        if isinstance(document, Mapping) and document:
            lead, raw = _mapping_sections(document, 2, f"doc-{n}.", max_tokens, keys or (), doc_comments)
            raw_all.append((1, f"Document {n}", _plain(lead), f"doc-{n}"))
            raw_all.extend(raw)
        else:
            body = "" if document is None else _dump(document)
            raw_all.append((1, f"Document {n}", _join(_plain(doc_comments), body), f"doc-{n}"))
    if short:
        _carry(raw_all, head)
        head = ""
    return [(0, "", head, None), *raw_all]


def _carry(raw: list[_Raw], head: str) -> None:
    """Open the first leaf section of ``raw`` (the first one the next section is not nested
    under) with ``head``, so a heading-only section never holds the preamble alone."""
    i = next(i for i in range(len(raw)) if i + 1 == len(raw) or raw[i + 1][0] <= raw[i][0])
    level, heading, body, anchor = raw[i]
    raw[i] = (level, heading, _join(head, body), anchor)


def _short_line(value: Any) -> str:
    """``value`` as a title: a one-line string of at most TITLE_MAX_CHARS characters once its
    whitespace is collapsed, else ``''``."""
    if not isinstance(value, str) or "\n" in value.strip():
        return ""
    line = _WS.sub(" ", _clean(_normalize(value))).strip()
    return line if len(line) <= TITLE_MAX_CHARS else ""


def _file_name(source_url: str | None) -> str:
    """The last path segment of ``source_url``, percent-decoded, cleaned like any title and clipped
    to TITLE_MAX_CHARS characters (``''`` when there is none)."""
    if not source_url:
        return ""
    try:
        path = urlsplit(source_url).path
    except ValueError:  # an invalid URL (e.g. a bad IPv6 host): no file name
        return ""
    name = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    return _WS.sub(" ", _clean(_normalize(name))).strip()[:TITLE_MAX_CHARS].rstrip()


def _title(documents: list[Any], source_url: str | None) -> str:
    """The fallback title (see the module docstring): a short ``title``/``name``/``description``
    of the first document, else the file name of ``source_url``."""
    first = documents[0] if documents else None
    if isinstance(first, Mapping):
        for key in _TITLE_KEYS:
            if line := _short_line(first.get(key)):
                return line
    return _file_name(source_url)


def _document(
    loaded: _Loaded, fmt: str, title: str, source_url: str | None, max_tokens: int, input_chars: int
) -> Document:
    documents = loaded.documents
    sections = _build(_sections(loaded, max_tokens))
    check_output_size(sections, input_chars, STRUCTURED_OUTPUT_FACTOR)
    title = _WS.sub(" ", _clean(_normalize(title))).strip() or _title(documents, source_url)
    return Document(
        title=title,
        source_url=source_url or None,
        format=fmt,
        sections=sections,
        extracted_chars=_non_ws_len(sections),
    )


def _non_ws_len(sections: tuple[Section, ...]) -> int:
    return sum(len("".join((s.heading + s.text).split())) for s in sections)


def extract_yaml(
    text: str,
    *,
    title: str = "",
    source_url: str | None = None,
    max_tokens: int = 512,
    structured_cap: int = DEFAULT_STRUCTURED_CAP,
) -> Document:
    """Extract a YAML file (one or more documents). ``max_tokens`` is the knowledge base's
    ``chunking.max_tokens``: a mapping section over it splits one level deeper. ``structured_cap``
    is ``ExtractLimits.max_structured_bytes`` (EAP_KB_MAX_STRUCTURED_BYTES): aliases may expand the
    stream to YAML_EXPANSION_FACTOR times it."""
    text = _normalize(text)
    return _document(_load_yaml(text, structured_cap), "yaml", title, source_url, max_tokens, len(text))


def extract_json(text: str, *, title: str = "", source_url: str | None = None, max_tokens: int = 512) -> Document:
    """Extract a JSON file; sections as for YAML (one per top-level key, rendered as YAML)."""
    text = _normalize(text)
    return _document(_Loaded([load_json(text)], [], []), "json", title, source_url, max_tokens, len(text))


__all__ = [
    "MAX_NESTING",
    "MAX_YAML_ALIASES",
    "DEFAULT_STRUCTURED_CAP",
    "MAX_YAML_EXPANDED_CHARS",
    "YAML_EXPANSION_FACTOR",
    "MAX_YAML_NODES",
    "PREAMBLE_SHARE",
    "TITLE_MAX_CHARS",
    "extract_json",
    "extract_yaml",
    "load_json",
    "load_yaml_documents",
]
