"""Path globs and the path -> metadata map of a repository source (the `scripts/kb_import.py` semantics).

Globs run over POSIX paths relative to the repository root: `**` any number of path segments (including
none), `*` and `?` within one segment, `[...]` a character class; a pattern without `/` matches the file
name in any directory (`*.pdf`). The map is an ORDERED list of rules `{match: <glob>, metadata: {...}}`
(a JSON object would lose its order in the database): the FIRST matching rule wins, rules are not merged.
Metadata values are flat (string, number, boolean); the keys the platform stamps itself are reserved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Keys the hand-off stamps (`source`, `source_id`, `external_id`, `relationship`) and the adapter adds
# (`path`), plus `eap` (reserved by the knowledge base): a rule may not set them.
RESERVED_KEYS = frozenset({"source", "source_id", "external_id", "relationship", "path", "eap"})
MAX_DOCUMENT_METADATA_KEYS = 16  # eap_schemas.knowledge.validate_kb_metadata
STAMPED_KEYS = 5  # source, source_id, external_id, relationship, path
MAX_MAP_METADATA_KEYS = MAX_DOCUMENT_METADATA_KEYS - STAMPED_KEYS
MAX_RULES = 200
MAX_GLOB_CHARS = 200
MAX_VALUE_CHARS = 256


def glob_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip().lstrip("/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i + 2 if pattern[i + 1 : i + 2] in ("!", "]") else i + 1)
            if end < 0:
                out.append(re.escape(c))
                i += 1
                continue
            body = pattern[i + 1 : end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    try:
        return re.compile("".join(out) + r"\Z")
    except re.error:
        raise ValueError(f"the glob {pattern[:60]!r} is not valid") from None


class Globs:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = [p for p in patterns if p.strip()]
        self._compiled = [glob_regex(p) for p in self.patterns]

    def __bool__(self) -> bool:
        return bool(self._compiled)

    def first(self, path: str) -> int | None:
        return next((i for i, rx in enumerate(self._compiled) if rx.match(path)), None)

    def matches(self, path: str) -> bool:
        return self.first(path) is not None

    def prefixes(self) -> list[str] | None:
        """Literal directory prefixes the patterns can match under (`docs/` for `docs/**/*.md`); None when
        a pattern may match anywhere (no `/`, or it starts with a wildcard)."""
        out: list[str] = []
        for pattern in self.patterns:
            p = pattern.strip().lstrip("/")
            if "/" not in p:
                return None
            literal = []
            for part in p.split("/")[:-1]:
                if any(ch in part for ch in "*?["):
                    break
                literal.append(part)
            if not literal:
                return None
            out.append("/".join(literal) + "/")
        return out


def dir_may_match(directory: str, prefixes: Sequence[str] | None) -> bool:
    """Can a file under `directory` (no trailing slash) match include patterns with these literal prefixes?"""
    if prefixes is None:
        return True
    d = directory + "/"
    return any(p.startswith(d) or d.startswith(p) for p in prefixes)


@dataclass(frozen=True)
class MetadataRule:
    glob: str
    metadata: dict[str, str | int | float | bool]


class MetadataMap:
    """Ordered glob -> flat metadata rules; the first matching glob wins."""

    def __init__(self, rules: Sequence[MetadataRule] = ()) -> None:
        self.rules = list(rules)
        self._globs = Globs(r.glob for r in self.rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def lookup(self, path: str) -> dict[str, Any]:
        index = self._globs.first(path)
        return {} if index is None else dict(self.rules[index].metadata)


def _flat(where: str, value: Any) -> dict[str, str | int | float | bool]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where}: the metadata must be a mapping of key: value")
    if len(value) > MAX_MAP_METADATA_KEYS:
        raise ValueError(
            f"{where}: at most {MAX_MAP_METADATA_KEYS} metadata keys (a document takes {MAX_DOCUMENT_METADATA_KEYS};"
            f" {STAMPED_KEYS} are set by the platform)"
        )
    out: dict[str, str | int | float | bool] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip() or len(key) > MAX_VALUE_CHARS:
            raise ValueError(f"{where}: metadata keys must be non-empty strings")
        if key in RESERVED_KEYS:
            raise ValueError(f"{where}: the metadata key {key!r} is reserved")
        if isinstance(item, float) and item != item:
            raise ValueError(f"{where}: {key!r} is NaN")
        if isinstance(item, str) and len(item) > MAX_VALUE_CHARS:
            raise ValueError(f"{where}: the value of {key!r} is longer than {MAX_VALUE_CHARS} characters")
        if not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"{where}: {key!r} must be a string, number or boolean (flat values only)")
        out[key] = item
    return out


def parse_map(value: Any) -> MetadataMap:
    """The config's `map`: a list of `{match, metadata}` rules in order (None or [] = no metadata).
    ValueError with a short, secret-free message otherwise."""
    if value is None:
        return MetadataMap()
    if isinstance(value, Mapping):
        raise ValueError("map must be a list of {match, metadata} rules (an object does not keep its order)")
    if not isinstance(value, list):
        raise ValueError("map must be a list of {match, metadata} rules")
    if len(value) > MAX_RULES:
        raise ValueError(f"map has more than {MAX_RULES} rules")
    rules: list[MetadataRule] = []
    for n, rule in enumerate(value, 1):
        where = f"map rule {n}"
        if not isinstance(rule, Mapping) or not isinstance(rule.get("match"), str) or not rule["match"].strip():
            raise ValueError(f"{where}: each rule needs `match: <glob>` and `metadata: {{...}}`")
        if len(rule["match"]) > MAX_GLOB_CHARS:
            raise ValueError(f"{where}: the glob is longer than {MAX_GLOB_CHARS} characters")
        glob_regex(rule["match"])  # raises ValueError for a broken class
        rules.append(MetadataRule(rule["match"], _flat(where, rule.get("metadata"))))
    return MetadataMap(rules)


def parse_globs(name: str, value: Any, *, required: bool) -> Globs:
    if value is None:
        value = []
    if not isinstance(value, list) or not all(isinstance(g, str) and g.strip() and len(g) <= MAX_GLOB_CHARS for g in value):
        raise ValueError(f"{name} must be a list of glob strings")
    if len(value) > 100:
        raise ValueError(f"{name} has more than 100 globs")
    if required and not value:
        raise ValueError(f"{name} needs at least one glob")
    for g in value:
        glob_regex(g)
    return Globs(value)
