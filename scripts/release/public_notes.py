#!/usr/bin/env python3
"""Sanitise the changelog for the public mirror, and BLOCK the release when anything internal is left.

    public_notes.py sanitize CHANGELOG.md --replacements FILE --patterns FILE [--version X.Y.Z] [--write]

Steps, in order:
  1. drop any `### Internal` block (the internal pull requests live in CHANGELOG.internal.md, which the export
     leaves out; this is the second lock on that door);
  2. apply the export's replacement rules (`old==>new` lines, as `git filter-repo --replace-text` reads them);
  3. scan the result with the export's residual-string patterns (one regular expression per line).
A pattern hit exits 1 and writes nothing: the public release does not go out. Only line numbers and pattern
numbers are printed, never the matched text, so a log cannot repeat what the scan caught. With `--version`
only that version's section is checked and printed, which is the body of the public GitHub Release.

The rules and patterns live in the operator-only export directory; this script takes them as arguments and holds
no internal string itself. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import changelog  # noqa: E402

REMOVED = "***REMOVED***"
INTERNAL_BLOCK = re.compile(r"(?ms)^### Internal\s*$.*?(?=^#{2,3} |\Z)")


class Blocked(Exception):
    def __init__(self, hits: list[tuple[int, int]]):
        self.hits = hits
        super().__init__("residual internal strings: " + ", ".join(f"line {line} (pattern {n})" for line, n in hits))


def read_replacements(path: Path) -> list[tuple[re.Pattern[str], str]]:
    """Rules in file order. `a==>b` is literal, `regex:a==>b` a regular expression, a bare `a` becomes ***REMOVED***."""
    rules: list[tuple[re.Pattern[str], str]] = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        old, arrow, new = line.partition("==>")
        if not arrow:
            new = REMOVED
        if old.startswith("regex:"):
            rules.append((re.compile(old[len("regex:") :]), new))
        else:
            rules.append((re.compile(re.escape(old)), new.replace("\\", "\\\\")))
    return rules


def read_patterns(path: Path) -> list[re.Pattern[str]]:
    return [re.compile(line) for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def strip_internal(text: str) -> str:
    return INTERNAL_BLOCK.sub("", text)


def sanitize(text: str, replacements: Sequence[tuple[re.Pattern[str], str]], patterns: Sequence[re.Pattern[str]]) -> str:
    """The public text, or Blocked when a residual pattern still matches after the replacements."""
    text = strip_internal(text)
    for pattern, new in replacements:
        text = pattern.sub(new, text)
    hits = [(number, index) for number, line in enumerate(text.splitlines(), 1) for index, pattern in enumerate(patterns, 1) if pattern.search(line)]
    if hits:
        raise Blocked(hits)
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("sanitize")
    run.add_argument("file", type=Path)
    run.add_argument("--replacements", type=Path, required=True)
    run.add_argument("--patterns", type=Path, required=True)
    run.add_argument("--version", help="check and print only this version's section")
    run.add_argument("--write", action="store_true", help="rewrite the file with the sanitised text")
    args = parser.parse_args(argv)
    text = args.file.read_text()
    if args.version:
        text = changelog.extract_section(text, args.version)
    try:
        clean = sanitize(text, read_replacements(args.replacements), read_patterns(args.patterns))
    except Blocked as blocked:
        print(f"BLOCKED: {blocked}", file=sys.stderr)
        return 1
    if args.write and not args.version:
        args.file.write_text(clean)
    elif args.version:
        sys.stdout.write(clean)
    return 0


if __name__ == "__main__":
    sys.exit(main())
