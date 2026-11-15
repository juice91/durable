"""Doxygen-flavoured Markdown (RAG P1, worker side): a line pre-pass, then the Markdown extractor.

Used when the knowledge base's ``chunking.extract.markdown_flavor`` is ``doxygen`` (the MCU+ SDK
pages). ``doxygen_to_markdown(text)`` rewrites Doxygen commands (``\\cmd`` or ``@cmd``) outside
fenced code:

- ``\\page id Title``, ``\\section``, ``\\subsection``, ``\\subsubsection`` -> Markdown headings
  (levels 1 to 4) with ``id`` as the anchor; ``\\mainpage Title`` -> a level 1 heading;
- ``\\anchor id`` -> the anchor of the next heading (unless that heading names its own);
- ``# Title {#id}`` (Doxygen's Markdown heading ids) -> the heading ``Title``, anchor ``id``;
- ``\\ref id "text"`` -> ``text``; ``\\ref id`` -> the title of that page or section in this file,
  else ``id``;
- ``[TOC]``, ``\\ingroup``, ``\\addtogroup``, ``\\{``, ``\\}``, ``\\endcond``, ``\\endif``,
  ``\\image``, ``\\imageStyle``, the ``\\htmlonly``/``\\latexonly`` markers and ``@VAR_...``
  placeholder lines -> dropped;
- ``\\note``, ``\\warning``, ``\\attention``, ``\\remark``, ``\\todo`` -> "Note:", "Warning:" ...;
  ``\\brief`` -> its text;
- ``\\code`` ... ``\\endcode`` (``\\code{.c}``) -> a fence (with the language);
- ``\\snippet file id`` -> a line naming the snippet (its code is in another file);
- ``\\cond X`` / ``\\if X`` -> "Applies when: X", ``\\elseif X`` -> "Otherwise, when: X",
  ``\\else`` -> "Otherwise:"; the section records the condition (``Section.condition``), as the rst
  extractor does for ``ifconfig``.

Plain Markdown passes through unchanged (fenced code is never rewritten).
"""

from __future__ import annotations

import re
from dataclasses import replace

from eap_knowledge.chunking import MAX_CONDITION_CHARS, _clip
from eap_knowledge.extract import _build, _markdown_sections, _normalize
from eap_knowledge.formats._common import OutputMeter, check_output_size, fence_marks, output_budget
from eap_knowledge.model import Document, Section

EXTRACTOR = "markdown-doxygen"

_CMD = r"[\\@]"
_ID = r"[A-Za-z0-9_][A-Za-z0-9_.:-]*"
_HEADING_CMD = re.compile(rf"^\s*{_CMD}(page|section|subsection|subsubsection)\s+({_ID})(?:[ \t]+(.*?))?\s*$")
_MAINPAGE = re.compile(rf"^\s*{_CMD}mainpage(?:[ \t]+(.*?))?\s*$")
_ANCHOR = re.compile(rf"^\s*{_CMD}anchor\s+({_ID})\s*$")
_ATX = re.compile(r"^( {0,3}#{1,6})([ \t].*)?$")
_HEADING_ID = re.compile(rf"^(.*?)[ \t]*\{{#({_ID})\}}[ \t]*$")
_DROP = re.compile(
    rf"^\s*(?:\[TOC\]|{_CMD}(?:ingroup|addtogroup|endcond|endif|imageStyle|image|htmlonly|endhtmlonly"
    rf"|latexonly|endlatexonly)\b.*|{_CMD}(?:cond|if)|{_CMD}[{{}}]|@VAR_[A-Z0-9_]+)\s*$"
)
_CODE_OPEN = re.compile(rf"^(\s*){_CMD}code(?:\{{\.?([A-Za-z0-9_+-]*)\}})?\s*$")
_CODE_CLOSE = re.compile(rf"^\s*{_CMD}endcode\b")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_COND = re.compile(rf"^\s*{_CMD}(cond|if)\s+(.+?)\s*$")
_ELSEIF = re.compile(rf"^\s*{_CMD}(?:elseif|else\s+if)\s+(.+?)\s*$")
_ELSE = re.compile(rf"^\s*{_CMD}else\s*$")
_SNIPPET = re.compile(rf"^\s*{_CMD}snippet\s+(\S+)\s+(\S+)")
_LABELS = {"note": "Note", "warning": "Warning", "attention": "Attention", "remark": "Remark", "todo": "To do"}
_LABEL = re.compile(rf"^(\s*){_CMD}(note|warning|attention|remark|todo)\b[ \t]*")
_BRIEF = re.compile(rf"^(\s*){_CMD}brief\b[ \t]*")
_REF_QUOTED = re.compile(rf'{_CMD}ref\s+{_ID}\s+"([^"\n]*)"')
_REF = re.compile(rf"{_CMD}ref\s+({_ID})")
_LEVELS = {"page": 1, "section": 2, "subsection": 3, "subsubsection": 4}
_CONDITION_LINE = re.compile(r"^(?:Applies when|Otherwise, when): (.+)$", re.M)


def _ids(lines: list[str]) -> dict[str, str]:
    """Page and section ids of this file -> their titles (for ``\\ref``)."""
    titles: dict[str, str] = {}
    for line in lines:
        match = _HEADING_CMD.match(line)
        if match and match.group(3):
            titles.setdefault(match.group(2), match.group(3).strip())
            continue
        atx = _ATX.match(line)
        if atx and atx.group(2):
            heading = _HEADING_ID.match(atx.group(2).strip())
            if heading and heading.group(1).strip():
                titles.setdefault(heading.group(2), heading.group(1).strip())
    return titles


def _refs(line: str, titles: dict[str, str], meter: OutputMeter | None = None) -> str:
    """``\\ref`` resolved; ``meter`` is charged each replacement's growth as it is made, so many refs
    to a long title fail before the long line is built."""
    line = _REF_QUOTED.sub(lambda m: m.group(1), line)

    def resolve(match: re.Match[str]) -> str:
        title = titles.get(match.group(1), match.group(1))
        if meter is not None:
            meter.add(max(0, len(title) - len(match.group(0))))
        return title

    return _REF.sub(resolve, line)


class _MeteredLines(list):  # type: ignore[type-arg]
    """The pre-pass output: every line added is charged to the meter first."""

    def __init__(self, meter: OutputMeter) -> None:
        super().__init__()
        self.meter = meter

    def append(self, line: str) -> None:
        self.meter.add(len(line) + 1)
        super().append(line)

    def extend(self, lines) -> None:  # noqa: ANN001
        lines = list(lines)
        self.meter.add(sum(len(line) + 1 for line in lines))
        super().extend(lines)


def doxygen_to_markdown(text: str) -> str:
    """The Doxygen pre-pass: Markdown with every heading's anchor written as a ``{#id}`` suffix.

    Fails ``extract_limit`` as soon as the output passes ``output_budget(len(text))``."""
    lines = _normalize(text).split("\n")
    titles = _ids(lines)
    meter = OutputMeter(output_budget(len(text)))
    out: list[str] = _MeteredLines(meter)
    fence = ""  # the open Markdown fence, if any
    code_lines: list[str] | None = None  # inside \code ... \endcode: its lines so far
    code_language = ""
    pending_anchor: str | None = None

    def close_code(lines_: list[str]) -> None:
        # The fence is longer than any backtick run in the code, so the code can never close it.
        marks = fence_marks("\n".join(lines_))
        out.extend([f"{marks}{code_language}", *lines_, marks])

    for line in lines:
        if code_lines is not None:
            if _CODE_CLOSE.match(line):
                close_code(code_lines)
                code_lines = None
            else:
                code_lines.append(line)
            continue
        fence_match = _FENCE.match(line)
        if fence:
            out.append(line)
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = ""
            continue
        if fence_match:
            fence = fence_match.group(1)
            out.append(line)
            continue
        code = _CODE_OPEN.match(line)
        if code:
            code_language = code.group(2) or ""
            code_lines = []
            continue
        anchor = _ANCHOR.match(line)
        if anchor:
            pending_anchor = anchor.group(1)
            continue
        heading = _HEADING_CMD.match(line)
        mainpage = _MAINPAGE.match(line)
        if heading or mainpage:
            if heading:
                level, anchor_id, title = _LEVELS[heading.group(1)], heading.group(2), heading.group(3) or heading.group(2)
            else:
                level, anchor_id, title = 1, pending_anchor, (mainpage.group(1) or "") if mainpage else ""
            title = _refs(title.strip(), titles, meter)
            anchor_id = anchor_id or pending_anchor
            out.extend(["", f"{'#' * level} {title}" + (f" {{#{anchor_id}}}" if anchor_id else ""), ""])
            pending_anchor = None
            continue
        atx = _ATX.match(line)
        if atx and atx.group(2) and atx.group(2).strip():
            line = _refs(line, titles, meter)
            if pending_anchor and not _HEADING_ID.match(line):
                line = f"{line.rstrip()} {{#{pending_anchor}}}"
            pending_anchor = None
            out.append(line)
            continue
        if _DROP.match(line):
            continue
        cond = _COND.match(line)
        if cond:
            out.extend([f"Applies when: {cond.group(2)}", ""])
            continue
        elseif = _ELSEIF.match(line)
        if elseif:
            out.extend(["", f"Otherwise, when: {elseif.group(1)}", ""])
            continue
        if _ELSE.match(line):
            out.extend(["", "Otherwise:", ""])
            continue
        snippet = _SNIPPET.match(line)
        if snippet:
            out.append(f"(code snippet {snippet.group(2)} from {snippet.group(1)}, not included)")
            continue
        line = _LABEL.sub(lambda m: f"{m.group(1)}{_LABELS[m.group(2)]}: ", line)
        line = _BRIEF.sub(lambda m: m.group(1), line)
        out.append(_refs(line, titles, meter))
    if code_lines is not None:
        close_code(code_lines)
    return "\n".join(out)


def extract_doxygen_markdown(text: str, *, title: str = "", source_url: str | None = None) -> Document:
    """Extract Doxygen-flavoured Markdown: the pre-pass, then ``extract()``'s Markdown sections,
    with ``{#id}`` heading suffixes as explicit anchors."""
    raw = []
    input_chars = len(text)
    for level, heading, body, explicit in _markdown_sections(doxygen_to_markdown(text)):
        if level:
            match = _HEADING_ID.match(heading)
            if match:
                heading, explicit = match.group(1).strip(), match.group(2)
        raw.append((level, heading, body, explicit))
    sections = tuple(_with_condition(s) for s in _build(raw))
    check_output_size(sections, input_chars)  # `\ref id` repeats a heading's title
    return Document(
        title=" ".join(_normalize(title).split()),
        source_url=source_url or None,
        format=EXTRACTOR,
        sections=sections,
        extracted_chars=sum(len("".join((s.heading + s.text).split())) for s in sections),
    )


def _with_condition(section: Section) -> Section:
    conditions = list(dict.fromkeys(m.strip() for m in _CONDITION_LINE.findall(section.text)))
    if not conditions:
        return section
    return replace(section, condition=_clip("; ".join(conditions), MAX_CONDITION_CHARS))


__all__ = ["EXTRACTOR", "doxygen_to_markdown", "extract_doxygen_markdown"]
