"""P1.A3: the PDF extractor (eap_knowledge.formats.pdf) and the sandbox child's dispatch
(eap_knowledge.sandbox.run, in process; the real child process is tested in the worker)."""

from __future__ import annotations

import base64
import json
from importlib import metadata
from pathlib import Path

import pytest

from eap_knowledge import ChunkingConfig, ExtractionFailed, chunk
from eap_knowledge.formats.pdf import IMAGE_ONLY_MAX_CHARS, MAX_PAGE_CHARS, extract_pdf, remove_running_lines
from eap_knowledge.sandbox import run as sandbox_run
from eap_schemas.knowledge import ExtractConfig
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, manual_pdf


def _codes(fn) -> str:
    with pytest.raises(ExtractionFailed) as info:
        fn()
    return info.value.code


# ------------------------------------------------------------------------------- extraction


def test_outline_headings_map_pages_to_heading_paths():
    result = extract_pdf(manual_pdf(), source_url="https://docs.example/trm.pdf")
    doc = result.document
    assert doc.format == "pdf" and doc.title == "AM62x TRM" and result.pages == 6
    by_page = {s.page: s for s in doc.sections}
    assert by_page[1].heading_path == ("Intro",) and by_page[2].heading_path == ("Intro",)
    assert by_page[3].heading_path == ("Setup",) and by_page[3].level == 1
    assert by_page[4].heading_path == ("Setup", "Pins") and by_page[4].level == 2
    assert by_page[5].heading_path == ("Setup", "Pins")
    assert all(s.anchor == f"page={s.page}" for s in doc.sections)


def test_running_headers_and_footers_are_removed():
    doc = extract_pdf(manual_pdf()).document
    text = "\n".join(s.text for s in doc.sections)
    assert "Technical Reference Manual" not in text and "Copyright" not in text
    assert "alpha paragraph explains the UART pin mux" in text


def test_running_line_rule_needs_more_than_half_the_pages_and_three_pages():
    pages = [["Header", f"body {w}", "Footer 1"] for w in ("a", "b", "c", "d")]
    pages[3] = ["Other", "body d", "Different"]
    out = remove_running_lines(pages)
    assert out[0] == ["body a"] and out[3] == ["Other", "body d", "Different"]
    two = [["Header", "x"], ["Header", "y"]]
    assert remove_running_lines(two) == two  # too few pages to tell


def test_chunks_carry_page_anchors_and_pages():
    doc = extract_pdf(manual_pdf(), source_url="https://docs.example/trm.pdf#frag").document
    chunks = chunk(doc, ChunkingConfig(min_tokens=0))
    assert chunks and all(c.page is not None for c in chunks)
    for c in chunks:
        assert c.anchor == f"https://docs.example/trm.pdf#page={c.page}"


def test_font_rule_without_an_outline():
    pages = [
        [("Board Bring-up", 24), *body_lines(0)], body_lines(1), [("Clock Tree", 18), *body_lines(2)],
        [("12", 30), *body_lines(3)], [("Power Rails", 18), *body_lines(4)], [("Debugging", 24), *body_lines(5)],
    ]
    doc = extract_pdf(build_pdf(pages)).document  # outline (the default) falls back to font
    assert [(s.page, s.heading_path, s.level) for s in doc.sections] == [
        (1, ("Board Bring-up",), 1),
        (2, ("Board Bring-up",), 1),
        (3, ("Board Bring-up", "Clock Tree"), 2),
        (4, ("Board Bring-up", "Clock Tree"), 2),  # "12" is only digits: never a heading
        (5, ("Board Bring-up", "Power Rails"), 2),
        (6, ("Debugging",), 1),
    ]
    assert "Board Bring-up" not in doc.sections[0].text


def test_a_size_used_once_is_not_a_heading_level():
    pages = [[("Lonely Callout", 24), *body_lines(0)], body_lines(1), body_lines(2)]
    doc = extract_pdf(build_pdf(pages)).document
    assert all(s.heading_path == () for s in doc.sections)
    assert "Lonely Callout" in doc.sections[0].text


def test_a_large_running_header_never_hides_the_page_headings():
    # A 14 pt running header over 10 pt body: removed from the text and never a heading
    # candidate, so the real headings after it on the same page are still found.
    pages = []
    for i in range(4):
        pages.append([
            ("AM62x Technical Reference", 14), (f"Heading {'ABCD'[i]} One", 20), *body_lines(i, 2),
            (f"Heading {'ABCD'[i]} Two", 20), *body_lines(i + 10, 2),
        ])
    doc = extract_pdf(build_pdf(pages)).document
    headings = [s.heading for s in doc.sections if s.page == 1]
    assert headings == ["Heading A One", "Heading A Two"]
    assert {s.heading for s in doc.sections} == {f"Heading {c} {n}" for c in "ABCD" for n in ("One", "Two")}
    assert not any("Technical Reference" in s.text or "Technical Reference" in s.heading for s in doc.sections)


def test_formula_lines_are_never_headings():
    pages = []
    for i in range(3):
        pages.append([
            (f"Q Format Basics {'ABC'[i]}", 20), *body_lines(i, 2),
            ("y = a * x + b", 20), ("± k × x(n) ÷ two", 20), ("2.5 3.75 10", 20), *body_lines(i + 10, 2),
        ])
    doc = extract_pdf(build_pdf(pages)).document
    assert {s.heading for s in doc.sections} == {"Q Format Basics A", "Q Format Basics B", "Q Format Basics C"}
    assert "y = a * x + b" in doc.sections[0].text
    from eap_knowledge.formats.pdf import _heading_like

    assert not _heading_like("∑ x(n) h(k - n)") and not _heading_like("∫ f(t) dt over time")
    assert not _heading_like("x1 x2") and not _heading_like("Q15 0x7FFF 0x8000 32767")
    assert _heading_like("I/O Configuration") and _heading_like("Chapter 3. Using the IQmath Library")


def test_dominant_size_ignores_one_large_symbol():
    from eap_knowledge.formats.pdf import _FontLines

    lines = _FontLines()
    lines.visit("A", None, [1, 0, 0, 1, 0, 0], None, 30)
    lines.visit(" small body words here", None, [1, 0, 0, 1, 0, 0], None, 10)
    lines.finish()
    assert lines.lines == [("A small body words here", 10.0)]
    mixed = _FontLines()
    mixed.visit("Half", None, [1, 0, 0, 1, 0, 0], None, 30)
    mixed.visit("half", None, [1, 0, 0, 1, 0, 0], None, 10)
    mixed.finish()
    assert mixed.lines == [("Halfhalf", 0.0)]  # no size has 60 % of the characters


def test_several_outline_entries_on_one_page_split_at_their_titles():
    pages = [
        body_lines(0),
        [("Clocks", 14), *body_lines(1, 2), ("Resets", 14), *body_lines(11, 2), ("Interrupts", 14), *body_lines(21, 2)],
        body_lines(2),
    ]
    outline = [("Intro", 0, None), ("Clocks", 1, None), ("Missing Title", 1, None), ("Resets", 1, None),
               ("Interrupts", 1, None)]
    doc = extract_pdf(build_pdf(pages, outline=outline)).document
    page2 = [(s.heading, s.text.split("\n")[0] if s.text else "") for s in doc.sections if s.page == 2]
    assert [h for h, _ in page2] == ["Clocks", "Missing Title", "Resets", "Interrupts"]
    assert page2[1][1] == ""  # its title is not on the page: no text
    assert page2[0][1].startswith(body_lines(1, 2)[0][0][:20])
    assert page2[2][1].startswith(body_lines(11, 2)[0][0][:20])
    by_page = {s.page: s.heading for s in doc.sections}
    assert by_page[3] == "Interrupts"  # the next page continues the last entry
    # No title in the text: the old rule (the last entry owns the page).
    no_titles = extract_pdf(build_pdf([body_lines(0), body_lines(1), body_lines(2)],
                                      outline=[("A", 1, None), ("B", 1, None)])).document
    assert [(s.page, s.heading, bool(s.text)) for s in no_titles.sections] == [
        (1, "", True), (2, "A", False), (2, "B", True), (3, "B", True)]


def test_pages_mode_has_no_headings():
    doc = extract_pdf(manual_pdf(), ExtractConfig(pdf_headings="pages")).document
    assert all(s.heading_path == () and s.level == 0 for s in doc.sections)
    assert [s.page for s in doc.sections] == [1, 2, 3, 4, 5]


def test_image_only_pages_are_flagged():
    result = extract_pdf(manual_pdf())
    assert result.image_only_pages == (6,)
    assert IMAGE_ONLY_MAX_CHARS == 32
    # A text page with an image is not image-only; a short page without an image is not either.
    pdf = build_pdf([body_lines(0), [("tiny", 10)]], image_pages={0})
    assert extract_pdf(pdf).image_only_pages == ()


def test_encrypted_pdf_fails_but_an_empty_password_extracts():
    pages = [body_lines(0)]
    assert _codes(lambda: extract_pdf(build_pdf(pages, encrypt="secret"))) == "document_encrypted"
    assert extract_pdf(build_pdf(pages, encrypt="")).pages == 1


def test_page_limit():
    pdf = build_pdf([body_lines(i) for i in range(6)])
    assert _codes(lambda: extract_pdf(pdf, max_pages=5)) == "extract_limit"
    assert extract_pdf(pdf, max_pages=6).pages == 6


def test_polyglot_and_garbage_fail_cleanly():
    polyglot = b"%PDF-1.7\n<html><body><script>alert(1)</script></body></html>\n"
    assert _codes(lambda: extract_pdf(polyglot)) == "extract_failed"
    assert _codes(lambda: extract_pdf(b"%PDF-1.4\n" + bytes(range(256)) * 10)) == "extract_failed"


def test_broken_xref_extracts_or_fails_cleanly():
    pdf = build_pdf([body_lines(0), body_lines(1)])
    start = pdf.rindex(b"startxref")
    broken = pdf[:start] + b"startxref\n999999\n%%EOF\n"  # points nowhere: pypdf rebuilds the xref
    try:
        doc = extract_pdf(broken).document
    except ExtractionFailed as exc:
        assert exc.code in ("extract_failed", "no_extractable_text")
    else:
        assert "paragraph explains" in doc.sections[0].text


def test_no_text_at_all_fails_no_extractable_text():
    assert _codes(lambda: extract_pdf(build_pdf([[], []], image_pages={0}))) == "no_extractable_text"


def test_control_characters_are_removed():
    doc = extract_pdf(build_pdf([[("bell\x07 and nul\x00 text here and more words", 10)]])).document
    assert "\x00" not in doc.sections[0].text and "\x07" not in doc.sections[0].text


def test_failure_messages_never_carry_text():
    with pytest.raises(ExtractionFailed) as info:
        extract_pdf(b"%PDF-1.7\nSECRET-DOCUMENT-TEXT")
    assert "SECRET" not in str(info.value) and "SECRET" not in info.value.user_message
    assert MAX_PAGE_CHARS == 200 * 1024


def test_same_bytes_extract_identically():
    pdf = manual_pdf()
    a = chunk(extract_pdf(pdf).document)
    b = chunk(extract_pdf(pdf).document)
    assert [c.embed_text_hash for c in a] == [c.embed_text_hash for c in b]


# ------------------------------------------------------------------------------- sandbox dispatch


def _run(payload: dict) -> dict:
    return sandbox_run(json.dumps(payload).encode())


def test_sandbox_extracts_a_pdf_from_base64():
    result = _run({"extractor": "pdf", "content": base64.b64encode(manual_pdf()).decode(), "source_url": "https://h/m.pdf"})
    assert result["ok"] is True and result["pages"] == 6 and result["image_only_pages"] == [6]
    sections = result["document"]["sections"]
    assert sections[0]["anchor"] == "page=1" and sections[0]["page"] == 1
    json.dumps(result)  # serialisable


@pytest.mark.parametrize(
    ("extractor", "content", "expected"),
    [
        ("rst", "Title\n=====\n\nSome text about the board.\n", "rst"),
        ("yaml", "title: Binding\nproperties:\n  reg: {maxItems: 1}\n", "yaml"),
        ("json", '{"title": "Cfg", "a": {"b": 1}}', "json"),
        ("markdown-doxygen", "\\page intro Intro\nSome text about it.\n", "markdown-doxygen"),
    ],
)
def test_sandbox_dispatches_text_formats(extractor, content, expected):
    result = _run({"extractor": extractor, "content": content, "options": None, "max_tokens": 512})
    assert result["ok"] is True and result["document"]["format"] == expected


def test_sandbox_passes_the_structured_cap_to_yaml():
    # A tiny cap makes an alias-heavy (but valid) document exceed its expansion budget.
    content = "a: &x " + "y" * 200 + "\nb: [*x, *x, *x, *x, *x, *x, *x, *x]\n"
    ok = _run({"extractor": "yaml", "content": content, "limits": {"max_structured_bytes": 1_000_000}})
    capped = _run({"extractor": "yaml", "content": content, "limits": {"max_structured_bytes": 100}})
    assert ok["ok"] is True and capped == {"ok": False, "code": "extract_limit", "detail": capped["detail"]}


def test_sandbox_passes_the_page_limit():
    content = base64.b64encode(build_pdf([body_lines(i) for i in range(3)])).decode()
    result = _run({"extractor": "pdf", "content": content, "limits": {"max_pdf_pages": 2}})
    assert result["ok"] is False and result["code"] == "extract_limit"


def test_sandbox_failures_are_codes_without_content(monkeypatch):
    assert _run({"extractor": "pdf", "content": "not base64!!"})["code"] == "extract_failed"
    html = base64.b64encode(b"<html>SECRET</html>").decode()
    assert _run({"extractor": "pdf", "content": html})["code"] == "unsupported_content"
    enc = base64.b64encode(build_pdf([body_lines(0)], encrypt="pw")).decode()
    assert _run({"extractor": "pdf", "content": enc})["code"] == "document_encrypted"
    assert _run({"extractor": "nope", "content": ""})["code"] == "extract_failed"
    assert sandbox_run(b"not json")["code"] == "extract_failed"

    import eap_knowledge.formats.rst as rst

    def boom(*args, **kwargs):
        raise KeyError("SECRET-DOCUMENT-TEXT")

    monkeypatch.setattr(rst, "extract_rst", boom)
    result = _run({"extractor": "rst", "content": "SECRET-DOCUMENT-TEXT"})
    assert result == {"ok": False, "code": "extract_failed", "detail": "KeyError"}

    def oom(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(rst, "extract_rst", oom)
    assert _run({"extractor": "rst", "content": "x"})["code"] == "extract_memory"


def test_sandbox_test_hook_env():
    assert _run({"eap_test": "env"})["env"] is not None


# ------------------------------------------------------------------------------- licence


def test_pypdf_is_the_pinned_bsd_licensed_wheel():
    dist = metadata.distribution("pypdf")
    assert dist.version == "6.21.4"
    assert dist.metadata["License-Expression"] == "BSD-3-Clause"
    licence = [f for f in dist.files or () if "licenses/" in str(f)]
    assert [Path(str(f)).name for f in licence] == ["LICENSE"]
    assert "Redistribution and use in source and binary forms" in licence[0].read_text()
    for f in dist.files or ():
        name = str(f)
        assert name.startswith(("pypdf/", "pypdf-6.21.4.dist-info/")), name
        if name.endswith(".py"):
            text = Path(f.locate()).read_text(errors="replace")
            assert "GNU General Public" not in text and "GNU Affero" not in text, name



def iqmath_font_pdf() -> bytes:
    """A synthetic stand-in for C2000Ware's IQmath_Quickstart.pdf as the font rule sees it (the
    real file has no outline): the facts snapshotted from the real file on 2026-11-15 are the
    shape here, with no copyrighted text. A 26 pt title used once on page 1, 20 pt chapters (one
    on a divider page of its own), 12 pt sections over 9 pt body, 12 pt formula and code lines,
    a running 9 pt header with the page number, a body line repeating a later section title, and a
    "C++ Support" section. The real-file check is scripts/pdf_headings_check.py (manual)."""

    def page(n: int, *lines):
        return [(f"Acme Semiconductor Inc., 2002-2020  {n}", 9), *lines]

    def body(n: int, k: int = 3):
        return body_lines(n, k, size=9)

    pages = [
        page(1, ("C28x IQmath Library", 26), *body(0)),
        page(2, ("Chapter 1. Introduction", 20), *body(1)),
        page(3, ("1.1. Fixed Point Math", 12), *body(2), ("y = _IQmpy(A, B) + C", 12), *body(3, 2)),
        page(4, ("Chapter 2. Installing the IQmath Library", 20)),  # a divider page
        page(5, ("2.1. C++ Support", 12), *body(4), ("Q15 0x7FFF 0x8000", 12), *body(5, 2)),
        page(6, ("Chapter 3. Using the IQmath Library", 20), *body(6)),
        page(7, ("3.9. Selecting the GLOBAL_Q format", 9), *body(7), ("3.10. Q Ranges", 12), *body(8)),
        page(8, ("3.11. Fast Paths", 9), *body(9), ("3.11. Fast Paths", 12), *body(10)),
    ]
    return build_pdf(pages)


def test_font_rule_snapshot_of_the_iqmath_quickstart():
    doc = extract_pdf(iqmath_font_pdf(), ExtractConfig(pdf_headings="font")).document
    paths = {s.page: s.heading_path for s in doc.sections}
    top = "C28x IQmath Library"  # used once, but above the repeated chapter size: the top level
    assert paths[1] == (top,)
    assert paths[2] == (top, "Chapter 1. Introduction")
    assert paths[3] == (top, "Chapter 1. Introduction", "1.1. Fixed Point Math")
    assert paths[4] == (top, "Chapter 2. Installing the IQmath Library")  # the divider page's title
    assert paths[5] == (top, "Chapter 2. Installing the IQmath Library", "2.1. C++ Support")
    assert paths[7] == (top, "Chapter 3. Using the IQmath Library", "3.10. Q Ranges")
    headings = {s.heading for s in doc.sections}
    # Formula and code lines, the 9 pt (body-size) "3.9" line and the running header never head.
    assert not any(ch in h for h in headings for ch in "=_(") and "Q15 0x7FFF 0x8000" not in headings
    assert "3.9. Selecting the GLOBAL_Q format" not in headings
    assert not any("Acme Semiconductor" in s.text or "Acme Semiconductor" in s.heading for s in doc.sections)
    # A body line that repeats a later heading's title, earlier on the page, does not take the split.
    fast = [s for s in doc.sections if s.page == 8]
    assert fast[0].heading == "Chapter 3. Using the IQmath Library" or fast[0].heading.startswith("3.10")
    assert fast[0].text.startswith("3.11. Fast Paths")
    assert fast[-1].heading == "3.11. Fast Paths" and fast[-1].text.startswith(body_lines(10, 3)[0][0][:20])


def test_single_entry_outline_page_splits_at_its_title():
    pages = [body_lines(0), [*body_lines(1, 2), ("3.9 Selecting GLOBAL_Q", 12), *body_lines(11, 2)], body_lines(2)]
    outline = [("3.8 Previous", 0, None), ("3.9 Selecting GLOBAL_Q", 1, None)]
    doc = extract_pdf(build_pdf(pages, outline=outline)).document
    page2 = [(s.heading, s.text) for s in doc.sections if s.page == 2]
    assert [h for h, _ in page2] == ["3.8 Previous", "3.9 Selecting GLOBAL_Q"]
    assert page2[0][1].startswith(body_lines(1, 2)[0][0][:20])  # the 3.8 tail stays under 3.8
    assert page2[1][1].startswith(body_lines(11, 2)[0][0][:20])
