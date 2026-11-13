"""Built-in PII detectors for `pii_redact`.

Each kind is an RE2 candidate pattern plus a check in plain Python (Luhn for card numbers, ISO 7064
mod 97 for IBANs, octet range for IPv4, the SSA's never-issued ranges for SSNs). RE2 has no
lookarounds, so boundaries are `\\b` in the pattern plus a look at the neighbouring characters here.

Coverage, by design narrow enough to keep false positives low:
- email: `local@domain.tld`;
- phone: `+<country> ...` international numbers (8 to 15 digits) and North American
  `(555) 123-4567`, `555-123-4567`, `555.123.4567`, `555 123 4567` (an optional leading `1`); a bare
  run of digits is not treated as a phone number;
- credit_card: 13 to 19 digits, bare or grouped 4-4-4-4(-3) or 4-6-4/5 by spaces or by dashes,
  Luhn-valid;
- iban: two letters, two check digits and 11 to 30 letters or digits (optionally in groups of
  four), mod-97 valid, upper case;
- ipv4: dotted quad, each octet 0 to 255 without leading zeros, not part of a longer dotted number;
- us_ssn: `AAA-GG-SSSS` with area not 000, 666 or 9xx, group not 00, serial not 0000.

When candidates of different kinds overlap, the first kind in DETECTION_ORDER keeps the span, and a
valid IBAN is never re-read as a card or phone number. Every scan goes through `iter_spans`, under
the caller's time budget.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from eap_schemas.processors import PiiKind

from eap_processors.regex import MatchBudget, Regexp, compile_re2, iter_spans

# Overlaps are resolved in this order (an email's local part can hold digits, an IBAN holds a
# card-like run of digits, a card's groups can look like a phone number). IBAN-shaped tokens are
# claimed first of all when they are valid (see `detect`).
DETECTION_ORDER: tuple[PiiKind, ...] = ("email", "iban", "credit_card", "us_ssn", "ipv4", "phone")


@dataclass(frozen=True)
class Detection:
    kind: PiiKind
    start: int
    end: int


def _compile(pattern: str) -> Regexp:
    return compile_re2(pattern, allow_empty_match=False)


_EMAIL = _compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,63}\b")
# Card layouts: 13 to 19 bare digits, 4-4-4-4 (plus an optional group of 3 for 19 digits) or
# 4-6-4/4-6-5 (Diners, Amex), each with one kind of separator (spaces or dashes).
_CARD = _compile(
    r"\b(?:\d{13,19}"
    r"|\d{4} \d{4} \d{4} \d{4}(?: \d{3})?|\d{4} \d{6} \d{4,5}"
    r"|\d{4}-\d{4}-\d{4}-\d{4}(?:-\d{3})?|\d{4}-\d{6}-\d{4,5})\b"
)
_IBAN = _compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b")
_SSN = _compile(r"\b\d{3}-\d{2}-\d{4}\b")
_IPV4 = _compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_PHONE_INTL = _compile(r"\+\d{1,3}(?:[ .\-]?\(?\d{1,4}\)?){2,6}")
_PHONE_NANP = _compile(r"(?:\b1[ .\-]?)?(?:\(\d{3}\) ?|\b\d{3}[ .\-])\d{3}[ .\-]\d{4}\b")


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def _prefixes(candidate: str, separators: str) -> Iterator[str]:
    """`candidate`, then the candidate with its last separated group dropped, and so on: a greedy
    match can swallow a trailing group that is not part of the number."""
    yield candidate
    end = len(candidate)
    while True:
        cut = max(candidate.rfind(sep, 0, end) for sep in separators)
        if cut <= 0:
            return
        end = cut
        # Skip a run of separators ("1234 - 5678").
        while end > 0 and candidate[end - 1] in separators:
            end -= 1
        if end <= 0:
            return
        yield candidate[:end]


def luhn_valid(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for position, ch in enumerate(reversed(digits)):
        value = ord(ch) - 48
        if position % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def iban_valid(compact: str) -> bool:
    """ISO 13616 / ISO 7064 mod 97-10 on an IBAN without spaces."""
    if not 15 <= len(compact) <= 34 or not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    remainder = 0
    for ch in rearranged:
        if ch.isdigit():
            chunk = ch
        elif "A" <= ch <= "Z":
            chunk = str(ord(ch) - 55)
        else:
            return False
        for d in chunk:
            remainder = (remainder * 10 + ord(d) - 48) % 97
    return remainder == 1


def _card_ok(candidate: str) -> bool:
    digits = _digits(candidate)
    return 13 <= len(digits) <= 19 and digits.strip("0") != "" and luhn_valid(digits)


def _iban_ok(candidate: str) -> bool:
    return iban_valid(candidate.replace(" ", ""))


def _ssn_ok(candidate: str) -> bool:
    area, group, serial = candidate.split("-")
    return area not in ("000", "666") and not area.startswith("9") and group != "00" and serial != "0000"


def _ipv4_ok(candidate: str) -> bool:
    for octet in candidate.split("."):
        if len(octet) > 1 and octet.startswith("0"):
            return False
        if int(octet) > 255:
            return False
    return True


def _phone_ok(candidate: str) -> bool:
    if candidate.count("(") != candidate.count(")"):
        return False
    return 8 <= len(_digits(candidate)) <= 15


def _nanp_ok(candidate: str) -> bool:
    return len(_digits(candidate)) in (10, 11)


def _scan(
    regexp: Regexp,
    text: str,
    budget: MatchBudget,
    check: Callable[[str], bool],
    separators: str = "",
    *,
    standalone: Callable[[str, int, int], bool] | None = None,
) -> Iterator[tuple[int, int]]:
    for start, end in iter_spans(regexp, text, budget):
        candidates: Iterable[str] = _prefixes(text[start:end], separators) if separators else (text[start:end],)
        for candidate in candidates:
            stop = start + len(candidate)
            if standalone is not None and not standalone(text, start, stop):
                continue
            if check(candidate):
                yield start, stop
                break


def _not_in_dotted_number(text: str, start: int, end: int) -> bool:
    """An IPv4 candidate must not be part of a longer dotted number such as `1.2.3.4.5`."""
    before = start >= 2 and text[start - 1] == "." and text[start - 2].isdigit()
    after = end + 1 < len(text) and text[end] == "." and text[end + 1].isdigit()
    return not (before or after)


def _phone_standalone(text: str, start: int, end: int) -> bool:
    """An international number starts at `+` that does not follow a letter or digit, and is not
    followed by another digit."""
    before = start > 0 and (text[start - 1].isalnum() or text[start - 1] in "+")
    after = end < len(text) and text[end].isdigit()
    return not (before or after)


def _nanp_standalone(text: str, start: int, end: int) -> bool:
    return not (start > 0 and (text[start - 1].isalnum() or text[start - 1] in "+"))


_SCANNERS: dict[PiiKind, Callable[[str, MatchBudget], Iterator[tuple[int, int]]]] = {
    "email": lambda text, b: _scan(_EMAIL, text, b, lambda _c: True),
    "credit_card": lambda text, b: _scan(_CARD, text, b, _card_ok, " -"),
    "us_ssn": lambda text, b: _scan(_SSN, text, b, _ssn_ok),
    "ipv4": lambda text, b: _scan(_IPV4, text, b, _ipv4_ok, standalone=_not_in_dotted_number),
    "phone": lambda text, b: _chain(
        _scan(_PHONE_INTL, text, b, _phone_ok, " .-", standalone=_phone_standalone),
        _scan(_PHONE_NANP, text, b, _nanp_ok, standalone=_nanp_standalone),
    ),
}


def _valid_ibans(text: str, budget: MatchBudget) -> Iterator[tuple[int, int]]:
    """(start, end) of every mod-97-valid IBAN: the longest valid prefix of an IBAN-shaped match
    (dropping trailing groups)."""
    return _scan(_IBAN, text, budget, _iban_ok, " ")


def _chain(*iterators: Iterator[tuple[int, int]]) -> Iterator[tuple[int, int]]:
    for iterator in iterators:
        yield from iterator


# Time allowed for one `detect` call when the caller passes no budget. Every scan is linear in the
# text (each candidate pattern is bounded), so this is a backstop, not an expected limit.
DEFAULT_DETECT_SECONDS = 10.0


def detect(text: str, kinds: Iterable[PiiKind], budget: MatchBudget | None = None) -> list[Detection]:
    """Non-overlapping detections of the given kinds, sorted by position. Raises
    MatchBudgetExceeded past the budget's deadline (no match cap: the scans are linear)."""
    if budget is None:
        budget = MatchBudget(DEFAULT_DETECT_SECONDS, None)
    wanted = set(kinds)
    accepted: list[Detection] = []
    # Claimed spans never overlap, so sorted by start they are also sorted by end: a new span
    # overlaps one only if it overlaps its neighbour on either side.
    starts: list[int] = []
    ends: list[int] = []

    def claim(start: int, end: int) -> bool:
        i = bisect.bisect_left(starts, start)
        if (i > 0 and ends[i - 1] > start) or (i < len(starts) and starts[i] < end):
            return False
        starts.insert(i, start)
        ends.insert(i, end)
        return True

    # A valid IBAN is one token: the digits inside it are never a card or phone number, whether or
    # not `iban` is a wanted kind. An IBAN-shaped run that fails mod 97 claims nothing, so a card
    # number after letters and digits (`AB12 4111111111111111`) is still found.
    for start, end in _valid_ibans(text, budget):
        if claim(start, end) and "iban" in wanted:
            accepted.append(Detection("iban", start, end))
    for kind in DETECTION_ORDER:
        if kind not in wanted or kind == "iban":
            continue
        for start, end in _SCANNERS[kind](text, budget):
            if claim(start, end):
                accepted.append(Detection(kind, start, end))
    accepted.sort(key=lambda d: d.start)
    return accepted


__all__ = ["DEFAULT_DETECT_SECONDS", "DETECTION_ORDER", "Detection", "detect", "iban_valid", "luhn_valid"]
