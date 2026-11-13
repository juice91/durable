"""RE2 compilation and bounded matching for processor patterns: the only regex engine this package
uses.

RE2 matches in time linear in the input and has no backtracking, so no single search can cause
catastrophic backtracking (ReDoS). It also rejects what needs backtracking: backreferences and
lookarounds are syntax errors. Python's `re` is never used on a pattern here.

Cost model. One search is linear in the text, but its constant is the program size: when RE2's DFA
cache thrashes (`[ab]*a[ab]{999}c` keeps ~1,000 states alive) it falls back to the NFA, which does
work proportional to the program size for every character, measured at about 8 ns per instruction
per character. So:
- a pattern's compiled program (forward and reverse) may have at most MAX_PROGRAM_SIZE
  instructions, which bounds the worst single search at about 8 us per character (about 0.26 s on
  a 32 KiB sample). Blocklist entries are keywords and phrases (16 to 100 instructions measured;
  an email-shaped pattern is 785), so 1,000 refuses only huge repetitions of classes (`.{150}x`,
  `\\pL+` with its large UTF-8 program, `[ab]{999}`);
- finding *every* match is O(n * matches) for patterns such as `a+b|a`, so callers iterate
  matches only through `iter_spans`, which checks the deadline before every search and counts
  matches against a cap. A search itself cannot be interrupted, so the deadline can be overrun by
  at most one search.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Iterator
from typing import Any

import re2

# Upper bound on the memory RE2 may use for one compiled pattern (program and DFA cache). A pattern
# whose program does not fit is a compile error; when the DFA cache fills at match time RE2 falls
# back to its NFA, which is still linear. Together with PATTERN_CACHE_SIZE this bounds the memory of
# cached patterns to about 128 MiB in the worst case (real patterns use far less). A program of at
# most MAX_PROGRAM_SIZE instructions takes a small part of it; the rest is DFA cache, which keeps
# more patterns on the fast DFA path (a smaller cache only means more slow NFA fallbacks).
RE2_MAX_MEM = 2 << 20
PATTERN_CACHE_SIZE = 64
MAX_PROGRAM_SIZE = 1_000

# Short texts a pattern is tried on at compile time: a pattern that matches the empty string on any
# of them is refused (it would match every text, or only ever produce empty matches such as `\b`).
EMPTY_MATCH_PROBES = ("", "a", "ab", "a b", "a\nb", "1 -_.")

Regexp = Any  # re2._Regexp (the binding ships no type stubs)


class Re2PatternError(ValueError):
    """A pattern RE2 refuses (bad syntax, backreference, lookaround, too large) or one that can match
    empty text. Pickle-safe: the constructor args are kept, the message is built in __str__."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


class MatchBudgetExceeded(RuntimeError):
    """`iter_spans` hit its deadline or match cap. `reason` is `budget_exceeded` or
    `too_many_matches`. Pickle-safe."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


BUDGET_EXCEEDED = "budget_exceeded"
TOO_MANY_MATCHES = "too_many_matches"


class MatchBudget:
    """A deadline (time.monotonic) and a cap on non-empty matches, shared by every pattern of one
    processor chain. Exceeding either raises MatchBudgetExceeded, which processors turn into a block
    (fail closed)."""

    def __init__(self, seconds: float, max_matches: int | None) -> None:
        self.deadline = time.monotonic() + seconds
        self.max_matches = max_matches
        self.matches = 0
        # time.monotonic() of the last deadline check that passed (None before the first). Every
        # processor checks after its last search, so a chain that allowed its text has this at or
        # before `deadline`: the evidence that nothing was allowed after the deadline.
        self.last_passed_check: float | None = None

    def check_time(self) -> None:
        now = time.monotonic()
        if now > self.deadline:
            raise MatchBudgetExceeded(BUDGET_EXCEEDED)
        self.last_passed_check = now

    def count_match(self) -> None:
        self.matches += 1
        if self.max_matches is not None and self.matches > self.max_matches:
            raise MatchBudgetExceeded(TOO_MANY_MATCHES)


def _options() -> Any:
    options = re2.Options()
    options.max_mem = RE2_MAX_MEM
    # RE2 otherwise writes every parse error (with the pattern) to stderr.
    options.log_errors = False
    return options


def _reason(exc: Exception) -> str:
    raw = exc.args[0] if exc.args else ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    reason = str(raw).strip() or "pattern does not compile"
    # RE2 quotes the offending fragment of the pattern; keep the message bounded.
    return reason if len(reason) <= 200 else reason[:197] + "..."


def _matches_empty(compiled: Regexp) -> bool:
    for probe in EMPTY_MATCH_PROBES:
        for match in compiled.finditer(probe):
            if match.end() == match.start():
                return True
    return False


@functools.lru_cache(maxsize=PATTERN_CACHE_SIZE)
def _compile_cached(pattern: str, allow_empty_match: bool) -> Regexp:
    try:
        compiled = re2.compile(pattern, _options())
    except re2.error as exc:
        raise Re2PatternError(f"not valid RE2 syntax ({_reason(exc)})") from None
    size = max(compiled.programsize, compiled.reverseprogramsize)
    if size > MAX_PROGRAM_SIZE:
        raise Re2PatternError(
            f"is too complex (RE2 program size {size}, at most {MAX_PROGRAM_SIZE}); use smaller "
            "repetition counts or narrower character classes"
        )
    if not allow_empty_match and _matches_empty(compiled):
        raise Re2PatternError(
            "can match empty text (for example `a*`, `x|`, `^`, or only a zero-width assertion such "
            "as `\\b` or `$`); it must match at least one character"
        )
    return compiled


def compile_re2(pattern: str, *, allow_empty_match: bool = False) -> Regexp:
    """Compile `pattern` with RE2 (cached per pattern, at most PATTERN_CACHE_SIZE) or raise
    Re2PatternError. Unless `allow_empty_match`, a pattern that can match the empty string on a
    short probe text is refused: as a blocklist entry it would match every text or never match."""
    return _compile_cached(pattern, allow_empty_match)


def re2_pattern_error(pattern: str) -> str | None:
    """Why RE2 refuses `pattern` as a blocklist entry, or None when it compiles."""
    try:
        compile_re2(pattern)
    except Re2PatternError as exc:
        return str(exc)
    return None


def iter_spans(regexp: Regexp, text: str, budget: MatchBudget) -> Iterator[tuple[int, int]]:
    """(start, end) character offsets of the non-overlapping, non-empty matches of `regexp`. The
    deadline is checked before every search (the first one included) and after the last; each
    match counts against the cap (MatchBudgetExceeded)."""
    budget.check_time()
    for match in regexp.finditer(text):
        start, end = match.start(), match.end()
        if end > start:
            budget.count_match()
            yield start, end
        # Before the next search (finditer searches again on the next iteration), and after the
        # last one, which may itself have run past the deadline.
        budget.check_time()
    budget.check_time()


def first_span(regexp: Regexp, text: str, budget: MatchBudget) -> tuple[int, int] | None:
    """The first non-empty match: one linear search when the first match is non-empty."""
    for span in iter_spans(regexp, text, budget):
        return span
    return None
