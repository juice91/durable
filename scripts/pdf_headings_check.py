"""Manual check of the PDF extractor on a real manual (RAG P1.A3; not collected by pytest).

    uv run python scripts/pdf_headings_check.py PATH.pdf [--mode outline|font|pages] [--expect-iqmath]

Prints the page count, the image-only pages and every distinct heading path. With
``--expect-iqmath`` it also asserts the facts snapshotted from C2000Ware's IQmath_Quickstart.pdf
(``libraries/math/IQmath/c28/docs/IQmath_Quickstart.pdf`` in a ``c2000ware-core-sdk`` checkout, which has
no outline, so every mode uses the font rule) on 2026-11-15; the synthetic test
``test_font_rule_snapshot_of_the_iqmath_quickstart`` holds the same shape without the file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from eap_knowledge.formats.pdf import extract_pdf
from eap_schemas.knowledge import ExtractConfig

IQMATH_PATHS = [
    ("C28x IQmath Library", "Chapter 1. Introduction"),
    ("C28x IQmath Library", "Chapter 3. Using the IQmath Library"),
    ("C28x IQmath Library", "Chapter 4. Function Summary", "4.2. IQmath Function Overview"),
    ("C28x IQmath Library", "Chapter 4. Function Summary", "4.3. C28x IQmath Library Benchmarks"),
    ("C28x IQmath Library", "Chapter 6. Revision History"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--mode", choices=["outline", "font", "pages"], default="outline")
    parser.add_argument("--expect-iqmath", action="store_true")
    args = parser.parse_args()
    result = extract_pdf(args.path.read_bytes(), ExtractConfig(pdf_headings=args.mode))
    paths = list(dict.fromkeys(s.heading_path for s in result.document.sections))
    print(f"pages={result.pages} image_only={list(result.image_only_pages)} sections={len(result.document.sections)}")
    for path in paths:
        print("  " + (" > ".join(path) or "(no heading)"))
    if args.expect_iqmath:
        assert result.pages == 85, result.pages
        missing = [p for p in IQMATH_PATHS if p not in paths]
        assert not missing, missing
        page23 = [s for s in result.document.sections if s.page == 23]
        assert "GLOBAL_Q" in page23[0].text and "Acme Semiconductor Inc., 2002" not in page23[0].text
        assert not any(any(ch in s.heading for ch in "=+") for s in result.document.sections if "C++" not in s.heading)
        print("IQmath snapshot: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
