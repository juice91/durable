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
- **Title**: the ``title`` argument, else the first document's top-level ``title`` string.

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
from collections.abc import Mapping
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
# Characters of scalar text (plus one per node) a YAML stream may expand to through its aliases:
# YAML_EXPANSION_FACTOR x the structured-file cap the caller passes (EAP_KB_MAX_STRUCTURED_BYTES).
YAML_EXPANSION_FACTOR = 4
DEFAULT_STRUCTURED_CAP = ExtractLimits().max_structured_bytes  # 2 MiB
MAX_YAML_EXPANDED_CHARS = YAML_EXPANSION_FACTOR * DEFAULT_STRUCTURED_CAP  # at the default cap
MAX_NESTING = 64
_DUMP_WIDTH = 100
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_WS = re.compile(r"\s+")


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
    loader = _CountingLoader(text, YAML_EXPANSION_FACTOR * structured_cap)
    try:
        documents = []
        while loader.check_data():
            documents.append(loader.get_data())
        return documents
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        where = f"line {mark.line + 1}, column {mark.column + 1}" if mark is not None else "unknown position"
        raise ExtractionFailed("extract_failed", f"invalid YAML at {where}") from None
    except yaml.YAMLError:
        raise ExtractionFailed("extract_failed", "invalid YAML") from None
    except RecursionError:
        raise ExtractionFailed("extract_limit", "YAML nested too deeply") from None
    finally:
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


def _mapping_sections(mapping: Mapping[Any, Any], level: int, prefix: str, max_tokens: int) -> list[_Raw]:
    raw: list[_Raw] = []
    for key, value in mapping.items():
        heading = _heading(key)
        anchor = f"{prefix}{heading}" if heading else None
        body = _dump({key: value})
        if isinstance(value, Mapping) and value and approx_tokens(body) > max_tokens:
            raw.append((level, heading, "", anchor))
            for sub_key, sub_value in value.items():
                sub_heading = _heading(sub_key)
                sub_anchor = f"{anchor}.{sub_heading}" if anchor and sub_heading else anchor
                raw.append((level + 1, sub_heading, _dump({sub_key: sub_value}), sub_anchor))
        else:
            raw.append((level, heading, body, anchor))
    return raw


def _sections(documents: list[Any], max_tokens: int) -> list[_Raw]:
    if len(documents) == 1:
        document = documents[0]
        if isinstance(document, Mapping) and document:
            return [(0, "", "", None), *_mapping_sections(document, 1, "", max_tokens)]
        return [(0, "", "" if document is None else _dump(document), None)]
    raw: list[_Raw] = [(0, "", "", None)]
    for n, document in enumerate(documents, 1):
        if isinstance(document, Mapping) and document:
            raw.append((1, f"Document {n}", "", f"doc-{n}"))
            raw.extend(_mapping_sections(document, 2, f"doc-{n}.", max_tokens))
        else:
            raw.append((1, f"Document {n}", "" if document is None else _dump(document), f"doc-{n}"))
    return raw


def _title(documents: list[Any]) -> str:
    first = documents[0] if documents else None
    if isinstance(first, Mapping) and isinstance(first.get("title"), str):
        return _WS.sub(" ", _clean(first["title"])).strip()
    return ""


def _document(
    documents: list[Any], fmt: str, title: str, source_url: str | None, max_tokens: int, input_chars: int
) -> Document:
    sections = _build(_sections(documents, max_tokens))
    check_output_size(sections, input_chars, STRUCTURED_OUTPUT_FACTOR)
    title = _WS.sub(" ", _clean(_normalize(title))).strip() or _title(documents)
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
    return _document(load_yaml_documents(text, structured_cap), "yaml", title, source_url, max_tokens, len(text))


def extract_json(text: str, *, title: str = "", source_url: str | None = None, max_tokens: int = 512) -> Document:
    """Extract a JSON file; sections as for YAML (one per top-level key, rendered as YAML)."""
    text = _normalize(text)
    return _document([load_json(text)], "json", title, source_url, max_tokens, len(text))


__all__ = [
    "MAX_NESTING",
    "MAX_YAML_ALIASES",
    "DEFAULT_STRUCTURED_CAP",
    "MAX_YAML_EXPANDED_CHARS",
    "YAML_EXPANSION_FACTOR",
    "MAX_YAML_NODES",
    "extract_json",
    "extract_yaml",
    "load_json",
    "load_yaml_documents",
]
