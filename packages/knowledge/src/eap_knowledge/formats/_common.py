"""Helpers shared by the worker-side extractors (no parser imports here)."""

from __future__ import annotations

import re
from collections.abc import Iterable

from eap_knowledge.errors import ExtractionFailed
from eap_knowledge.model import Section

# An extractor's output (headings plus section texts) may be at most OUTPUT_FACTOR times its input,
# or MIN_OUTPUT_BUDGET characters, whichever is larger. Rendering adds a little (fences, pipes,
# YAML indentation); anything far beyond that is an expansion bomb (aliases, substitutions, refs).
OUTPUT_FACTOR = 4
MIN_OUTPUT_BUDGET = 8 * 1024 * 1024
# YAML and JSON render as indented block YAML, which legitimately grows a minified, deeply nested
# file several times over (a numeric table: about 6.5x); their bombs are stopped earlier by the
# loader's alias budget, so the output check only needs to be a loose backstop.
STRUCTURED_OUTPUT_FACTOR = 16

_BACKTICKS = re.compile(r"`{3,}")


def output_budget(input_chars: int, factor: int = OUTPUT_FACTOR) -> int:
    return max(factor * input_chars, MIN_OUTPUT_BUDGET)


class OutputMeter:
    """A running output length that fails ``extract_limit`` the moment it passes ``budget``, so an
    extractor stops before it builds an oversized string, not after."""

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.used = 0

    def add(self, chars: int) -> None:
        self.used += chars
        if self.used > self.budget:
            raise ExtractionFailed("extract_limit", f"extracted text is over {self.budget} characters")


def check_output_size(sections: Iterable[Section], input_chars: int, factor: int = OUTPUT_FACTOR) -> None:
    """Fail ``extract_limit`` when the extracted text is over ``output_budget(input_chars, factor)``."""
    budget = output_budget(input_chars, factor)
    total = 0
    for section in sections:
        total += len(section.heading) + len(section.text)
        if total > budget:
            raise ExtractionFailed("extract_limit", f"extracted text is over {budget} characters")


def fence_marks(text: str) -> str:
    """A backtick fence longer than any run of three or more backticks in ``text`` (so the code
    can never close it early)."""
    longest = max((len(m) for m in _BACKTICKS.findall(text)), default=2)
    return "`" * max(3, longest + 1)


def fence(text: str, language: str = "") -> str:
    """``text`` in a backtick fence longer than any backtick run inside it."""
    marks = fence_marks(text)
    return f"{marks}{language}\n{text.rstrip()}\n{marks}"


__all__ = [
    "MIN_OUTPUT_BUDGET",
    "OUTPUT_FACTOR",
    "STRUCTURED_OUTPUT_FACTOR",
    "OutputMeter",
    "check_output_size",
    "fence",
    "fence_marks",
    "output_budget",
]
