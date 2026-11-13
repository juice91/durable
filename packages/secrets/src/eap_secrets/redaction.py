"""Scrubbing secret values (MCP server header values) out of what an MCP server sent back: tool
results in the worker, and the tool listing the API's connection test returns. Shared by both, so a
server that echoes a header value into a tool name, description, schema or result never gets it
past either. Pure standard library (the eap_secrets import contract)."""

from __future__ import annotations

import base64
import binascii
import bisect
import functools
import json
import re
from collections.abc import Iterable, Iterator, Sequence
from typing import Any
from urllib.parse import quote

__all__ = [
    "MIN_FRAGMENT",
    "REDACTED",
    "SecretVariants",
    "leaves_with_kind",
    "mask_split_secrets",
    "redact",
    "secret_variants",
    "variants_pattern",
]

REDACTED = "[REDACTED]"
# Shorter credential parts are not scrubbed on their own (they would mangle ordinary text).
MIN_FRAGMENT = 4
_MAX_DECODE_RUN = 200_000
# Runs that may be base64 (std or url-safe) or hex of something that contains a secret.
_ENCODED_RUN = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")


class SecretVariants(list):
    """secret_variants' result: a list of forms, plus `.encoded` (the base64/hex forms) and `.hex`
    (the hex forms among them). In a whole-string piece check, base64 matches exactly (re-cased
    base64 is a different encoding) and hex case-insensitively (hex decoding ignores case)."""

    encoded: frozenset[str] = frozenset()
    hex: frozenset[str] = frozenset()


def secret_variants(values: Iterable[str]) -> SecretVariants:
    """Every string to scrub for these header values, longest first: the value, each credential
    part after an auth scheme ("Bearer <token>" -> token), a Basic credential's decoded form and its
    password, and the URL-encoded, base64 (std/url-safe, with and without padding), hex and
    JSON-escaped forms of each. Matching is case-insensitive (see `redact`), which also covers
    upper-cased copies, lower-case percent-encoding and upper-case hex."""
    values = [v for v in values if v]
    found: set[str] = set()
    encoded: set[str] = set()
    hex_forms: set[str] = set()
    for value in values:
        base = {value}
        parts = value.split()
        base.update(p for p in parts[1:] if len(p) >= MIN_FRAGMENT)
        for part in parts[1:]:
            try:
                decoded = base64.b64decode(part, validate=True).decode("utf-8")
            except (binascii.Error, ValueError):
                continue
            base.add(decoded)
            base.update(p for p in decoded.split(":", 1)[1:] if len(p) >= MIN_FRAGMENT)
        for item in base:
            raw = item.encode()
            for enc in (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()):
                encoded.update({enc, enc.rstrip("=")})
            encoded.add(raw.hex())
            hex_forms.add(raw.hex())
            found.update({item, quote(item, safe=""), json.dumps(item)[1:-1]})
    encoded -= found  # a form that is also plain text (e.g. an all-hex token) stays case-insensitive
    result = SecretVariants(
        sorted((f for f in found | encoded if len(f) >= MIN_FRAGMENT or f in values), key=len, reverse=True)
    )
    result.encoded = frozenset(encoded)
    result.hex = frozenset(hex_forms & encoded)
    return result


@functools.lru_cache(maxsize=64)
def variants_pattern(variants: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(re.escape(v) for v in variants), re.IGNORECASE)


def _decodings(run: str) -> Iterator[str]:
    """What an encoded-looking run may decode to (base64 std/url-safe, hex), as text."""
    if len(run) > _MAX_DECODE_RUN:
        return
    body = run.rstrip("=")
    body = body[: len(body) - len(body) % 4] if len(body) % 4 == 1 else body  # a cut-off run
    padded = body + "=" * (-len(body) % 4)
    for decode in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            yield decode(padded).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            pass
    if len(run) % 2 == 0:
        try:
            yield bytes.fromhex(run).decode("utf-8", "ignore")
        except ValueError:
            pass


def _redact_str(s: str, pattern: re.Pattern[str]) -> str:
    s = pattern.sub(REDACTED, s)

    def check(m: re.Match[str]) -> str:  # a secret inside base64/hex of a larger blob (e.g. JSON)
        return REDACTED if any(pattern.search(d) for d in _decodings(m.group(0))) else m.group(0)

    return _ENCODED_RUN.sub(check, s)


def redact(obj: Any, variants: Sequence[str]) -> Any:
    """`obj` (JSON-like) with every variant replaced by REDACTED, case-insensitively, in keys and
    values, recursively; also any base64/hex run whose decoded text contains a variant."""
    if not variants:
        return obj
    pattern = variants_pattern(tuple(variants))
    if isinstance(obj, str):
        return _redact_str(obj, pattern)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        renamed = 0
        for k, v in obj.items():
            key = redact(k, variants) if isinstance(k, str) else k
            if key != k or key in out:  # a key that held a secret: a unique placeholder name
                renamed += 1
                key = f"{REDACTED}-{renamed}"
            out[key] = redact(v, variants)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, variants) for v in obj]
    return obj


def leaves_with_kind(obj: Any) -> list[tuple[str, bool]]:
    """Every string in `obj` in traversal order, with whether it is a dict key: each key followed by
    its value's strings, list items, nested. (Keys count: a secret can span a key and its value.)"""
    if isinstance(obj, str):
        return [(obj, False)]
    if isinstance(obj, dict):
        return [x for k, v in obj.items() for x in [(str(k), True), *leaves_with_kind(v)]]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in leaves_with_kind(v)]
    return []


def _split_masks(leaves: list[tuple[str, bool]], pattern: re.Pattern[str]) -> dict[int, list[tuple[int, int]]]:
    """Character ranges to mask, per leaf index, for every secret match that spans two or more leaves
    when the leaves are joined, in three orders: keys and values interleaved, values only, keys only."""
    masks: dict[int, list[tuple[int, int]]] = {}
    orders = (
        list(range(len(leaves))),
        [i for i, (_, is_key) in enumerate(leaves) if not is_key],
        [i for i, (_, is_key) in enumerate(leaves) if is_key],
    )
    for indices in orders:
        if len(indices) < 2:
            continue
        starts, pos = [], 0
        for i in indices:
            starts.append(pos)
            pos += len(leaves[i][0])
        joined = "".join(leaves[i][0] for i in indices)
        for match in pattern.finditer(joined):
            first = bisect.bisect_right(starts, match.start()) - 1
            last = bisect.bisect_right(starts, match.end() - 1) - 1
            if first == last:
                continue  # inside one leaf: plain redaction handles it
            for n in range(first, last + 1):
                lo = max(match.start(), starts[n]) - starts[n]
                hi = min(match.end(), starts[n] + len(leaves[indices[n]][0])) - starts[n]
                if hi > lo:
                    masks.setdefault(indices[n], []).append((lo, hi))
    return masks


def _masked(text: str, ranges: list[tuple[int, int]]) -> str:
    out, pos = [], 0
    for lo, hi in sorted(ranges):
        if hi <= pos:
            continue
        out.append(text[pos:max(lo, pos)])
        out.append(REDACTED)
        pos = hi
    out.append(text[pos:])
    return "".join(out)


def mask_split_secrets(content: Any, variants: Sequence[str]) -> Any:
    """`content` with every part of a secret that is split across strings (values, list items, a
    key and its value) replaced by REDACTED in each string it touches; the structure is kept.

    A whole string of 4+ characters that is itself a piece of a secret form is masked too, whatever
    separates it from the other pieces. Text and hex forms match case-insensitively; base64 forms
    exactly (re-cased base64 is a different encoding)."""
    if not variants:
        return content
    leaves = leaves_with_kind(content)
    masks = _split_masks(leaves, variants_pattern(tuple(variants)))
    encoded = getattr(variants, "encoded", frozenset())
    hex_forms = getattr(variants, "hex", frozenset())
    folded_forms = "\0".join(v.lower() for v in variants if v not in encoded or v in hex_forms)
    base64_forms = "\0".join(encoded - hex_forms)
    longest = max(len(v) for v in variants)
    for i, (leaf, _) in enumerate(leaves):
        if MIN_FRAGMENT <= len(leaf) <= longest and (leaf.lower() in folded_forms or leaf in base64_forms):
            masks.setdefault(i, []).append((0, len(leaf)))
    if not masks:
        return content
    counter = iter(range(len(leaves)))

    def rebuild(obj: Any) -> Any:
        if isinstance(obj, str):
            i = next(counter)
            return _masked(obj, masks[i]) if i in masks else obj
        if isinstance(obj, dict):
            out: dict[Any, Any] = {}
            for k, v in obj.items():
                i = next(counter)
                key = _masked(str(k), masks[i]) if i in masks else k
                if key in out:
                    key = f"{key}-{i}"
                out[key] = rebuild(v)
            return out
        if isinstance(obj, (list, tuple)):
            return [rebuild(v) for v in obj]
        return obj

    return rebuild(content)
