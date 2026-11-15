"""The PDF extractor (RAG P1, task P1.A3; worker side, run only in the sandbox child).

``extract_pdf(data, options, *, title, source_url, max_pages) -> PdfExtraction`` reads a PDF with
``pypdf.PdfReader(strict=False)`` and returns the same ``Document`` model as ``extract()``, plus the
page count and the image-only pages for the ``extracted`` event:

- **Encryption:** an encrypted file is tried with the empty password, else ``document_encrypted``.
- **Limits:** more than ``max_pages`` pages (EAP_KB_MAX_PDF_PAGES, default 2,000) fails
  ``extract_limit``; each page's text is capped at MAX_PAGE_CHARS (200 KiB; the warning
  ``page_text_truncated``); pypdf's own limits (decompression, arrays, outline, page tree) fail
  ``extract_limit``; the external ``jbig2dec`` binary is never called.
- **Text:** ``page.extract_text(extraction_mode="plain")`` per page; NUL and the other C0 controls
  (except tab and newline) are removed, since Postgres ``text`` rejects NUL.
- **Running headers and footers:** a line found among the first or last HEADER_FOOTER_LINES (3)
  lines of more than half the pages (digits normalised to ``#``, so "Page 12 of 400" matches on every
  page) is removed there. Only for documents of at least MIN_PAGES_FOR_REPEATS (3) pages.
- **Headings** (``chunking.extract.pdf_headings``):
  - ``outline`` (the default): the bookmarks (title, depth, destination page); each page's text
    belongs to the deepest entry at or before it (the last one in page order). Entries that own no
    page (several on one page) stay as headings without text, so the heading tree is kept. Without a
    usable outline, the font rule.
  - ``font``: a line of at most 100 characters, not only digits, set in at least 1.25 x the page's
    median font size becomes a heading; distinct heading sizes (largest first) give levels 1-6.
  - ``pages``: no headings.
- **Sections are per page** (a heading's text on each of its pages is its own section with the same
  heading path), so every chunk cites the page it comes from: ``anchor`` is ``page=N`` (the chunk's
  ``anchor_url`` becomes ``source_url#page=N``) and ``page`` is N (``metadata.eap.page``).
- **Image-only pages:** fewer than IMAGE_ONLY_MAX_CHARS (32) non-space characters on a page that has
  an image XObject.
- A PDF with no text at all fails ``no_text_layer`` when it has image-only pages (scanned), else
  ``no_extractable_text``. Either way it stays failed (never an empty document), so a later
  re-extract with image extraction selects it.

Every other failure inside pypdf (a broken file, a polyglot, a recursion) fails ``extract_failed``
with the exception's type name only, never its text.
"""

from __future__ import annotations

import io
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from typing import Any

from eap_schemas.knowledge import ExtractConfig

from eap_knowledge.errors import ExtractionFailed
from eap_knowledge.formats._common import OutputMeter, output_budget
from eap_knowledge.model import Document, Section

EXTRACTOR = "pdf"
DEFAULT_MAX_PAGES = 2_000
MAX_PAGE_CHARS = 200 * 1024
HEADER_FOOTER_LINES = 3
HEADER_FOOTER_SHARE = 0.5
MIN_PAGES_FOR_REPEATS = 3
IMAGE_ONLY_MAX_CHARS = 32
# The font rule.
FONT_HEADING_RATIO = 1.25
FONT_HEADING_MAX_CHARS = 100
# A line's size is its DOMINANT size: the one at least this share of its non-space characters use
# (else the line has no size and is never a heading), so one large symbol cannot lift a line.
FONT_DOMINANT_SHARE = 0.6
# A heading has at least this many letters, and letters are at least this share of its non-space
# characters; a line with a math symbol is never a heading (formulas are often set large).
FONT_HEADING_MIN_LETTERS = 3
FONT_HEADING_LETTER_SHARE = 0.5
MATH_SYMBOLS = frozenset("=+<>^±×÷∑∫∏√∂∆∇∞≈≠≤≥≡∝∈∉⊂⊃∪∩∧∨¬→←↔⇒⇔")  # not "/" (I/O) or "*"
# A size becomes a heading level when that many candidate lines use it in the document, or when it
# is used once but is larger than a size that qualifies (a one-off chapter title above sections).
FONT_LEVEL_MIN_OCCURRENCES = 2
# A page with fewer non-space characters than this (a chapter-divider page holding only its title)
# is measured against the document's median font size, not its own.
FONT_SHORT_PAGE_CHARS = 200
# "C++", "C#"-like names: a `++` glued to a word is not arithmetic.
_PLUS_PLUS = re.compile(r"(?<=\w)\+\+")
MAX_LEVEL = 6
# Headings per document (outline entries or font headings); past it, further ones stay text.
MAX_HEADINGS = 10_000
MAX_TITLE_CHARS = 500

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DIGITS = re.compile(r"\d+")
_BLANK_RUNS = re.compile(r"\n{3,}")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    """``document`` plus what the ``extracted`` event reports: ``pages`` (the page count) and
    ``image_only_pages`` (1-based, all of them; the event lists the first 200)."""

    document: Document
    pages: int
    image_only_pages: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _Heading:
    title: str
    level: int
    path: tuple[str, ...]
    page: int  # 0-based


# --------------------------------------------------------------------------------------- text


def _clean(text: str) -> str:
    """NUL and other C0 controls removed, CR/LF normalised, trailing spaces and blank runs trimmed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip("\n")


def _one_line(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    return _WS.sub(" ", _CONTROL.sub("", text)).strip()[:limit]


def _normalised(line: str) -> str:
    return _DIGITS.sub("#", _WS.sub(" ", line).strip())


def _edge_indexes(lines: list[str]) -> list[int]:
    """Indexes of the first and last HEADER_FOOTER_LINES non-blank lines."""
    filled = [i for i, line in enumerate(lines) if line.strip()]
    return sorted(set(filled[:HEADER_FOOTER_LINES] + filled[-HEADER_FOOTER_LINES:]))


def running_lines(pages: list[list[str]]) -> frozenset[str]:
    """The normalised running headers and footers of ``pages`` (see the module docstring)."""
    if len(pages) < MIN_PAGES_FOR_REPEATS:
        return frozenset()
    counts: Counter[str] = Counter()
    for lines in pages:
        counts.update({_normalised(lines[i]) for i in _edge_indexes(lines)} - {""})
    threshold = HEADER_FOOTER_SHARE * len(pages)
    return frozenset(line for line, n in counts.items() if n > threshold)


def remove_running_lines(pages: list[list[str]], running: frozenset[str] | None = None) -> list[list[str]]:
    """Remove running headers and footers (see the module docstring) from each page's lines."""
    running = running_lines(pages) if running is None else running
    if not running:
        return pages
    out = []
    for lines in pages:
        drop = {i for i in _edge_indexes(lines) if _normalised(lines[i]) in running}
        out.append([line for i, line in enumerate(lines) if i not in drop])
    return out


# --------------------------------------------------------------------------------------- reader


def _open(data: bytes) -> Any:
    from pypdf import PasswordType, PdfReader
    from pypdf.errors import DependencyError, FileNotDecryptedError

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            result = reader.decrypt("")
        except (DependencyError, NotImplementedError):
            raise ExtractionFailed("document_encrypted", "unsupported encryption") from None
        except FileNotDecryptedError:
            raise ExtractionFailed("document_encrypted") from None
        if result == PasswordType.NOT_DECRYPTED:
            raise ExtractionFailed("document_encrypted")
    return reader


def _has_image(page: Any) -> bool:
    """Whether the page's resources (or a form XObject's, one level down) hold an image XObject."""
    try:
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        xobjects = resources.get("/XObject") if resources is not None else None
        if xobjects is None:
            return False
        xobjects = xobjects.get_object()
        for ref in list(xobjects.values())[:256]:
            obj = ref.get_object()
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                return True
            if subtype == "/Form":
                inner = obj.get("/Resources")
                inner = inner.get_object() if inner is not None else None
                inner_x = inner.get("/XObject") if inner is not None else None
                if inner_x is not None and any(
                    r.get_object().get("/Subtype") == "/Image" for r in list(inner_x.get_object().values())[:256]
                ):
                    return True
    except Exception:  # noqa: BLE001 - a broken resource dictionary is "no image"
        return False
    return False


def _scale_y(matrix: Any) -> float:
    try:
        return float((float(matrix[1]) ** 2 + float(matrix[3]) ** 2) ** 0.5) or 1.0
    except (TypeError, ValueError, IndexError):
        return 1.0


class _FontLines:
    """Collects (line text, dominant effective font size) from pypdf's text visitor. A line's size is
    the one at least FONT_DOMINANT_SHARE of its non-space characters are set in, else 0."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, float]] = []
        self._text: list[str] = []
        self._chars: Counter[float] = Counter()
        self.sizes: list[tuple[float, int]] = []  # (size, non-space characters) of every fragment

    def visit(self, text: Any, cm: Any, tm: Any, font_dict: Any, font_size: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        try:
            size = round(abs(float(font_size)) * _scale_y(tm) * _scale_y(cm), 2)
        except (TypeError, ValueError):
            size = 0.0
        parts = text.split("\n")
        for i, part in enumerate(parts):
            if i:
                self._flush()
            if part:
                chars = len("".join(part.split()))
                if chars:
                    self.sizes.append((size, chars))
                    self._chars[size] += chars
                self._text.append(part)

    def _flush(self) -> None:
        line = "".join(self._text)
        if line.strip():
            total = sum(self._chars.values())
            size = 0.0
            if total:
                top, chars = self._chars.most_common(1)[0]
                size = top if chars >= FONT_DOMINANT_SHARE * total else 0.0
            self.lines.append((line, size))
        self._text, self._chars = [], Counter()

    def finish(self) -> None:
        self._flush()

    def median(self) -> float:
        weighted = [size for size, chars in self.sizes for _ in range(min(chars, 64)) if size > 0]
        return statistics.median(weighted) if weighted else 0.0


def _page_text(page: Any, fonts: _FontLines | None, warnings: list[str]) -> str:
    if fonts is None:
        raw = page.extract_text(extraction_mode="plain")
    else:
        raw = page.extract_text(extraction_mode="plain", visitor_text=fonts.visit)
        fonts.finish()
    text = _clean(raw or "")
    if len(text) > MAX_PAGE_CHARS:
        warnings.append("page_text_truncated")
        text = text[:MAX_PAGE_CHARS]
    return text


# --------------------------------------------------------------------------------------- headings


def _outline_headings(reader: Any, page_count: int) -> list[_Heading]:
    """The outline as headings in page order (outline order within a page); entries without a
    resolvable page are skipped (their children keep their own pages)."""
    try:
        outline = reader.outline
    except Exception:  # noqa: BLE001 - a broken outline is no outline
        return []
    out: list[_Heading] = []

    def walk(items: Any, depth: int, path: tuple[str, ...]) -> None:
        last_path = path
        for item in items:
            if len(out) >= MAX_HEADINGS:
                return
            if isinstance(item, list):
                walk(item, depth + 1, last_path)
                continue
            title = _one_line(str(getattr(item, "title", "") or ""))
            try:
                page = reader.get_destination_page_number(item)
            except Exception:  # noqa: BLE001 - an unresolvable destination
                page = None
            if not title:
                continue
            last_path = (*path, title)
            if isinstance(page, int) and 0 <= page < page_count:
                out.append(_Heading(title, min(depth, MAX_LEVEL), last_path, page))

    try:
        walk(outline, 1, ())
    except RecursionError:
        return []
    # A stable sort: entries out of page order (rare) are placed by their page.
    return sorted(out, key=lambda h: h.page)


def _heading_like(candidate: str) -> bool:
    """At most 100 characters, at least 3 letters making at least half its non-space characters,
    and no math symbol."""
    if not candidate or len(candidate) > FONT_HEADING_MAX_CHARS:
        return False
    if any(ch in MATH_SYMBOLS for ch in _PLUS_PLUS.sub("", candidate)):
        return False
    dense = "".join(candidate.split())
    letters = sum(1 for ch in dense if ch.isalpha())
    return letters >= FONT_HEADING_MIN_LETTERS and letters >= FONT_HEADING_LETTER_SHARE * len(dense)


FontCandidate = tuple[str, float, int]  # (title, size, occurrence of that text on the page, 0-based)


def _font_headings(
    lines_by_page: list[list[tuple[str, float]]],
    medians: list[float],
    running: frozenset[str] = frozenset(),
    *,
    page_chars: list[int] | None = None,
    document_median: float = 0.0,
) -> dict[int, list[FontCandidate]]:
    """Per page: the lines the font rule makes headings, as (text, size, occurrence). Running
    headers and footers (``running``, normalised as for their removal) are never candidates. A page
    with fewer than FONT_SHORT_PAGE_CHARS characters (``page_chars``) uses ``document_median``. A
    size counts when FONT_LEVEL_MIN_OCCURRENCES candidates use it, or when it is larger than a size
    that does. ``occurrence`` counts the earlier lines on the page with the same text, so a body
    line that repeats a heading's title before it never takes the heading's place."""
    candidates: dict[int, list[FontCandidate]] = {}
    per_size: Counter[float] = Counter()
    total = 0
    for index, (lines, median) in enumerate(zip(lines_by_page, medians, strict=True)):
        if page_chars is not None and page_chars[index] < FONT_SHORT_PAGE_CHARS and document_median > 0:
            median = document_median
        if median <= 0:
            continue
        seen: Counter[str] = Counter()
        for text, size in lines:
            candidate = _one_line(text)
            key = candidate.casefold()
            nth = seen[key]
            seen[key] += 1
            if size >= FONT_HEADING_RATIO * median and _normalised(text) not in running and _heading_like(candidate):
                candidates.setdefault(index, []).append((candidate, size, nth))
                per_size[_size_key(size)] += 1
                total += 1
                if total >= MAX_HEADINGS:
                    break
        if total >= MAX_HEADINGS:
            break
    confirmed = {size for size, n in per_size.items() if n >= FONT_LEVEL_MIN_OCCURRENCES}
    smallest = min(confirmed, default=None)
    accepted = {size for size in per_size if size in confirmed or (smallest is not None and size > smallest)}
    out: dict[int, list[FontCandidate]] = {}
    for index, found in candidates.items():
        kept = [c for c in found if _size_key(c[1]) in accepted]
        if kept:
            out[index] = kept
    return out


def _size_key(size: float) -> float:
    return round(size * 2) / 2


def _font_levels(found: dict[int, list[FontCandidate]]) -> dict[float, int]:
    sizes = sorted({_size_key(c[1]) for page in found.values() for c in page}, reverse=True)
    return {size: min(i + 1, MAX_LEVEL) for i, size in enumerate(sizes)}


# --------------------------------------------------------------------------------------- sections


class _Builder:
    """Per-page sections with a heading stack; also meters the output."""

    def __init__(self, meter: OutputMeter) -> None:
        self.sections: list[Section] = []
        self.stack: list[tuple[int, str]] = []
        self.meter = meter

    def heading(self, title: str, level: int, page: int, path: tuple[str, ...] | None = None) -> tuple[str, ...]:
        if path is None:
            while self.stack and self.stack[-1][0] >= level:
                self.stack.pop()
            self.stack.append((level, title))
            path = tuple(t for _, t in self.stack)
        return path

    def add(self, path: tuple[str, ...], heading: str, level: int, text: str, page: int) -> None:
        self.meter.add(len(heading) + len(text))
        self.sections.append(
            Section(
                heading_path=path, heading=heading, level=level, text=text, anchor=f"page={page + 1}", page=page + 1
            )
        )


def _matches(line: str, title: str) -> bool:
    return _one_line(line).casefold() == title.casefold()


def _split_at_titles(
    text: str, titles: list[str], occurrences: list[int] | None = None
) -> list[tuple[int | None, str]]:
    """``text`` cut at lines equal to ``titles`` (in order): [(title index or None, body)]. Each
    line is matched against the titles not yet used by searching forward; titles before the match
    are skipped (never found on the page), so one missing title never hides the later ones. With
    ``occurrences``, title j matches only the occurrences[j]-th line (0-based) with its text."""
    out: list[tuple[int | None, str]] = []
    current: int | None = None
    body: list[str] = []
    next_title = 0
    seen: Counter[str] = Counter()
    for line in text.split("\n"):
        key = _one_line(line).casefold()
        nth = seen[key]
        seen[key] += 1
        hit = next(
            (
                j for j in range(next_title, len(titles))
                if _matches(line, titles[j]) and (occurrences is None or occurrences[j] == nth)
            ),
            None,
        )
        if hit is not None:
            if current is not None or any(b.strip() for b in body):
                out.append((current, _clean("\n".join(body))))
            current, body, next_title = hit, [], hit + 1
            continue
        body.append(line)
    if current is not None or any(b.strip() for b in body):
        out.append((current, _clean("\n".join(body))))
    return out


def _sections_by_outline(pages: list[str], headings: list[_Heading], builder: _Builder) -> None:
    by_page: dict[int, list[_Heading]] = {}
    for h in headings:
        by_page.setdefault(h.page, []).append(h)
    owner: _Heading | None = None

    def continued(text: str, index: int) -> None:
        if not text:
            return
        if owner is None:
            builder.add((), "", 0, text, index)
        else:
            builder.add(owner.path, owner.title, owner.level, text, index)

    for index, text in enumerate(pages):
        starting = by_page.get(index, [])
        if starting:
            parts = _split_at_titles(text, [h.title for h in starting])
            if any(i is not None for i, _ in parts):
                # Entries starting on this page whose titles are found in its text: each gets the
                # text after its title; text before the first one continues the previous entry
                # (the tail of 3.8 above the "3.9" title stays under 3.8); entries whose title is
                # not in the text keep no text, in outline order.
                emitted = -1
                for i, body in parts:
                    if i is None:
                        continued(body, index)
                        continue
                    for h in starting[emitted + 1 : i]:
                        builder.add(h.path, h.title, h.level, "", index)
                    owner, emitted = starting[i], i
                    builder.add(owner.path, owner.title, owner.level, body, index)
                for h in starting[emitted + 1 :]:
                    builder.add(h.path, h.title, h.level, "", index)
                    owner = h
                continue
        for h in starting[:-1]:  # no title found in the text: the earlier ones keep no text
            builder.add(h.path, h.title, h.level, "", index)
        if starting:
            owner = starting[-1]
            builder.add(owner.path, owner.title, owner.level, text, index)
        else:
            continued(text, index)


def _sections_by_font(
    pages: list[str], found: dict[int, list[tuple[str, float]]], builder: _Builder
) -> None:
    levels = _font_levels(found)
    path: tuple[str, ...] = ()
    current: tuple[str, int] | None = None
    for index, text in enumerate(pages):
        page_found = found.get(index, [])
        page_headings = [(t, levels[_size_key(sz)]) for t, sz, _ in page_found]
        parts = _split_at_titles(text, [t for t, _ in page_headings], [nth for _, _, nth in page_found])
        for hit, body in parts:
            if hit is not None:
                current = page_headings[hit]
                path = builder.heading(current[0], current[1], index)
            if current is None:
                if body:
                    builder.add((), "", 0, body, index)
            elif hit is not None or body:
                builder.add(path, current[0], current[1], body, index)


def _sections_by_page(pages: list[str], builder: _Builder) -> None:
    for index, text in enumerate(pages):
        if text:
            builder.add((), "", 0, text, index)


# --------------------------------------------------------------------------------------- entry


def extract_pdf(
    data: bytes,
    options: ExtractConfig | None = None,
    *,
    title: str = "",
    source_url: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> PdfExtraction:
    """Extract a PDF (the decoded bytes). ``options`` is the knowledge base's ``chunking.extract``
    (``pdf_headings``); ``max_pages`` is ExtractLimits.max_pdf_pages. Raises ExtractionFailed with
    ``document_encrypted``, ``extract_limit``, ``no_text_layer`` (no text, image-only pages),
    ``no_extractable_text`` (no text, no image-only page) or ``extract_failed``."""
    from pypdf import apply_configuration
    from pypdf.errors import LimitReachedError

    mode = (options.pdf_headings if options is not None else None) or "outline"
    try:
        with apply_configuration(jbig2dec_binary=None):
            return _extract(data, mode, title, source_url, max_pages)
    except ExtractionFailed:
        raise
    except LimitReachedError:
        raise ExtractionFailed("extract_limit", "a pypdf limit was reached") from None
    except RecursionError:
        raise ExtractionFailed("extract_limit", "nesting too deep") from None
    except MemoryError:
        raise ExtractionFailed("extract_memory") from None
    except Exception as exc:  # noqa: BLE001 - a pypdf failure is this document's failure (type only)
        raise ExtractionFailed("extract_failed", f"pypdf: {type(exc).__name__}") from None


def _extract(data: bytes, mode: str, title: str, source_url: str | None, max_pages: int) -> PdfExtraction:
    reader = _open(data)
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise ExtractionFailed("extract_limit", f"the PDF has more than {max_pages} pages")
    headings = _outline_headings(reader, page_count) if mode == "outline" else []
    if mode == "outline" and not headings:
        mode = "font"
    warnings: list[str] = []
    texts: list[str] = []
    fonts_by_page: list[_FontLines] = []
    image_only: list[int] = []
    for index in range(page_count):
        page = reader.pages[index]
        fonts = _FontLines() if mode == "font" else None
        texts.append(_page_text(page, fonts, warnings))
        if fonts is not None:
            fonts_by_page.append(fonts)
    split = [t.split("\n") for t in texts]
    running = running_lines(split)
    lines = remove_running_lines(split, running)
    pages = [_clean("\n".join(page_lines)) for page_lines in lines]
    for index, text in enumerate(pages):
        if len("".join(text.split())) < IMAGE_ONLY_MAX_CHARS and _has_image(reader.pages[index]):
            image_only.append(index + 1)
    builder = _Builder(OutputMeter(output_budget(len(data))))
    if mode == "outline":
        _sections_by_outline(pages, headings, builder)
    elif mode == "font":
        everything = _FontLines()
        everything.sizes = [size for f in fonts_by_page for size in f.sizes]
        found = _font_headings(
            [f.lines for f in fonts_by_page], [f.median() for f in fonts_by_page], running,
            page_chars=[len("".join(t.split())) for t in pages], document_median=everything.median(),
        )
        _sections_by_font(pages, found, builder)
    else:
        _sections_by_page(pages, builder)
    sections = tuple(builder.sections)
    chars = sum(len("".join(s.text.split())) for s in sections)
    if chars == 0:
        # Image pages with no text (a scanned PDF) have content an image extractor can read later.
        raise ExtractionFailed("no_text_layer" if image_only else "no_extractable_text")
    chars += sum(len("".join(s.heading.split())) for s in sections)
    doc_title = _one_line(title) or _metadata_title(reader)
    document = Document(
        title=doc_title,
        source_url=source_url or None,
        format=EXTRACTOR,
        sections=sections,
        extracted_chars=chars,
        warnings=tuple(warnings),
    )
    return PdfExtraction(document=document, pages=page_count, image_only_pages=tuple(image_only))


def _metadata_title(reader: Any) -> str:
    try:
        meta = reader.metadata
        value = meta.title if meta is not None else None
    except Exception:  # noqa: BLE001 - broken metadata is no title
        return ""
    return _one_line(value) if isinstance(value, str) else ""


__all__ = [
    "DEFAULT_MAX_PAGES",
    "EXTRACTOR",
    "IMAGE_ONLY_MAX_CHARS",
    "MAX_PAGE_CHARS",
    "PdfExtraction",
    "extract_pdf",
    "remove_running_lines",
    "running_lines",
]
