"""P1.A3 acceptance: PDF extraction quality and hostile input (in process; the sandboxed child is
covered in apps/worker). Complements test_p1a3_pdf.py, which it does not repeat."""

from __future__ import annotations

import time

import pytest

from eap_knowledge import ChunkingConfig, ExtractionFailed, chunk
from eap_knowledge.formats.pdf import extract_pdf
from eap_schemas.knowledge import MAX_IMAGE_ONLY_PAGES_LISTED, ExtractConfig
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, manual_pdf


def code_of(data: bytes, **kwargs) -> str:
    with pytest.raises(ExtractionFailed) as info:
        extract_pdf(data, **kwargs)
    return info.value.code


def raw_pdf(objects: list[bytes], *, root: int = 1) -> bytes:
    """A hand-assembled PDF (object n+1 is objects[n]) with a correct xref table."""
    out = b"%PDF-1.7\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, root, start)
    return out


CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
ONE_PAGE = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>"
FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


# ------------------------------------------------------------------------------- headings


def test_outline_wins_over_larger_fonts_and_font_rule_is_the_fallback():
    # Each heading size is used twice: a size used once is never a heading level (the font rule).
    pages = [[("Huge Font Line", 30), *body_lines(0)], body_lines(1), [("Other Huge Line", 30), *body_lines(2)]]
    outline = [("Outline Only", 1, None)]
    doc = extract_pdf(build_pdf(pages, outline=outline)).document
    assert [s.heading_path for s in doc.sections] == [(), ("Outline Only",), ("Outline Only",)]
    assert "Huge Font Line" in doc.sections[0].text  # not promoted to a heading: the outline rules
    font = extract_pdf(build_pdf(pages)).document  # no outline: the font rule finds it
    assert font.sections[0].heading_path == ("Huge Font Line",)
    forced = extract_pdf(build_pdf(pages, outline=outline), ExtractConfig(pdf_headings="font")).document
    assert forced.sections[0].heading_path == ("Huge Font Line",)


def test_outline_nesting_three_levels_and_anchors_follow_pages():
    pages = [body_lines(i) for i in range(5)]
    outline = [("A", 0, None), ("A.1", 1, "A"), ("A.1.x", 2, "A.1"), ("B", 4, None)]
    doc = extract_pdf(build_pdf(pages, outline=outline), source_url="https://h/x.pdf").document
    assert [(s.page, s.heading_path, s.level) for s in doc.sections] == [
        (1, ("A",), 1), (2, ("A", "A.1"), 2), (3, ("A", "A.1", "A.1.x"), 3),
        (4, ("A", "A.1", "A.1.x"), 3), (5, ("B",), 1),
    ]
    for c in chunk(doc, ChunkingConfig(min_tokens=0)):
        assert c.anchor == f"https://h/x.pdf#page={c.page}" and c.page is not None


def test_two_levels_of_font_sizes_make_a_hierarchy():
    pages = [
        [("Part", 26), ("Sub", 18), *body_lines(0)], body_lines(1), [("Sub Two", 18), *body_lines(2)],
        [("Part Two", 26), *body_lines(3)],
    ]
    doc = extract_pdf(build_pdf(pages)).document
    by_page = {s.page: s.heading_path for s in doc.sections}  # the last section of a page wins
    assert by_page == {1: ("Part", "Sub"), 2: ("Part", "Sub"), 3: ("Part", "Sub Two"), 4: ("Part Two",)}
    assert doc.sections[0].heading_path == ("Part",) and doc.sections[0].level == 1


# ------------------------------------------------------------------------------- running lines


def test_running_lines_removed_on_exactly_three_pages_and_body_survives():
    pages = [[("Doc Title", 8), *body_lines(i, 2), (f"Page {i + 1}", 8)] for i in range(3)]
    result = extract_pdf(build_pdf(pages))
    text = "\n".join(s.text for s in result.document.sections)
    assert "Doc Title" not in text and "Page 1" not in text and "paragraph explains" in text


def test_a_line_on_under_half_of_the_pages_is_kept():
    pages = [[("Appears twice", 8), *body_lines(0, 2)], [("Appears twice", 8), *body_lines(1, 2)],
             body_lines(2, 2), body_lines(3, 2), body_lines(4, 2)]
    text = "\n".join(s.text for s in extract_pdf(build_pdf(pages)).document.sections)
    assert text.count("Appears twice") == 2


def test_two_page_documents_keep_their_repeated_lines():
    pages = [[("Same Header", 8), *body_lines(i)] for i in range(2)]
    text = "\n".join(s.text for s in extract_pdf(build_pdf(pages)).document.sections)
    assert text.count("Same Header") == 2


# ------------------------------------------------------------------------------- image-only pages


def test_image_only_pages_are_all_reported_and_the_event_cap_is_200():
    assert MAX_IMAGE_ONLY_PAGES_LISTED == 200
    n = 230
    pages = [body_lines(0)] + [[] for _ in range(n)]
    result = extract_pdf(build_pdf(pages, image_pages=set(range(1, n + 1))))
    assert result.pages == n + 1 and result.image_only_pages == tuple(range(2, n + 2))
    assert len(result.image_only_pages[:MAX_IMAGE_ONLY_PAGES_LISTED]) == 200


def test_pages_without_text_and_without_an_image_are_not_flagged():
    result = extract_pdf(build_pdf([body_lines(0), [], body_lines(2)], image_pages={2}))
    assert result.image_only_pages == ()


def test_all_image_pages_is_no_text_layer():
    assert code_of(build_pdf([[], [], []], image_pages={0, 1, 2})) == "no_text_layer"


# ------------------------------------------------------------------------------- encryption


@pytest.mark.parametrize("password", ["hunter2", "x", "a much longer real password"])
def test_a_real_password_fails_document_encrypted(password):
    assert code_of(build_pdf([body_lines(0)], encrypt=password)) == "document_encrypted"


def test_encrypted_failures_carry_the_fixed_message_only():
    with pytest.raises(ExtractionFailed) as info:
        extract_pdf(build_pdf([body_lines(0)], encrypt="hunter2"))
    assert info.value.user_message == "The PDF is encrypted with a password."
    assert "hunter2" not in str(info.value) and "alpha" not in info.value.detail


def test_an_empty_password_pdf_extracts_with_its_text_and_outline():
    pdf = build_pdf([body_lines(0), body_lines(1)], outline=[("Only", 0, None)], encrypt="")
    result = extract_pdf(pdf)
    assert result.pages == 2 and "paragraph explains" in result.document.sections[0].text
    assert result.document.sections[0].heading_path == ("Only",)


# ------------------------------------------------------------------------------- corrupt input


@pytest.mark.parametrize("cut", [0.9, 0.6, 0.3, 0.1])
def test_truncated_pdfs_extract_or_fail_cleanly(cut):
    pdf = manual_pdf()
    try:
        result = extract_pdf(pdf[: int(len(pdf) * cut)])
    except ExtractionFailed as exc:
        assert exc.code in ("extract_failed", "no_extractable_text")
        assert "alpha" not in exc.detail and "alpha" not in exc.user_message
    else:
        assert result.pages >= 1


def test_corrupt_bytes_inside_a_valid_file_never_raise_foreign_errors():
    pdf = bytearray(manual_pdf())
    for i in range(300, len(pdf) - 300, 97):
        pdf[i] = (pdf[i] + 41) % 256
    try:
        extract_pdf(bytes(pdf))
    except ExtractionFailed as exc:
        assert exc.code in ("extract_failed", "no_extractable_text", "extract_limit")


@pytest.mark.parametrize("data", [b"%PDF-", b"%PDF-1.7\n", b"%PDF-1.7\n%%EOF", b"%PDF-1.7\n1 0 obj\n<<"])
def test_stub_files_fail_extract_failed_or_no_text(data):
    assert code_of(data) in ("extract_failed", "no_extractable_text")


# ------------------------------------------------------------------------------- max_pages


def test_max_pages_boundaries():
    pdf = build_pdf([body_lines(i) for i in range(4)])
    assert extract_pdf(pdf, max_pages=4).pages == 4
    assert code_of(pdf, max_pages=3) == "extract_limit"
    assert code_of(pdf, max_pages=1) == "extract_limit"


def test_max_pages_is_checked_before_any_page_is_read(monkeypatch):
    import eap_knowledge.formats.pdf as mod

    def explode(*a, **k):
        raise AssertionError("a page was read")

    monkeypatch.setattr(mod, "_page_text", explode)
    assert code_of(build_pdf([body_lines(i) for i in range(4)]), max_pages=2) == "extract_limit"


def test_a_page_tree_declaring_a_huge_count_is_not_trusted():
    data = raw_pdf([CATALOG, b"<< /Type /Pages /Kids [3 0 R] /Count 999999999 >>", b"<< /Type /Page /Parent 2 0 R >>"])
    assert code_of(data) in ("extract_limit", "no_extractable_text", "extract_failed")


# ------------------------------------------------------------------------------- hostile input


def test_deeply_nested_arrays_are_an_extract_limit():
    nest = b"[" * 200_000 + b"]" * 200_000
    page = PAGE[:-3] + b" /Resources << /Font << /F1 5 0 R >> >> >>"
    assert code_of(raw_pdf([CATALOG, ONE_PAGE, page, nest, FONT])) == "extract_limit"


def test_deeply_nested_dictionaries_fail_cleanly():
    nest = b"<< /A " * 50_000 + b"1" + b" >>" * 50_000
    assert code_of(raw_pdf([CATALOG, ONE_PAGE, PAGE, nest])) in ("extract_limit", "extract_failed", "no_extractable_text")


def test_a_pages_tree_loop_fails_cleanly():
    data = raw_pdf([CATALOG, b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>"])
    assert code_of(data) in ("extract_failed", "extract_limit", "no_extractable_text")


def test_a_huge_xref_table_declaration_is_bounded():
    # A page with text, so the outcome code means something: the good file extracts, the declared
    # 900 million entry table is a pypdf limit.
    page = PAGE[:-3] + b" /Resources << /Font << /F1 5 0 R >> >> >>"
    content = b"<< /Length 47 >>\nstream\nBT /F1 12 Tf 72 700 Td (Hello world text) Tj ET\nendstream"
    good = raw_pdf([CATALOG, ONE_PAGE, page, content, FONT])
    assert "Hello world text" in extract_pdf(good).document.sections[0].text
    data = good.replace(b"xref\n0 6\n", b"xref\n0 900000000\n")
    assert data != good
    start = time.monotonic()
    assert code_of(data) == "extract_limit"
    assert time.monotonic() - start < 10


def test_a_zip_polyglot_with_a_pdf_header_fails():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("payload.txt", "SECRET-ZIP-TEXT")
    assert code_of(b"%PDF-1.7\n" + buf.getvalue()) in ("extract_failed", "no_extractable_text")


def test_a_valid_pdf_followed_by_a_trailing_zip_still_extracts_or_fails_cleanly():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a", "b")
    try:
        result = extract_pdf(manual_pdf() + buf.getvalue())
    except ExtractionFailed as exc:
        assert exc.code in ("extract_failed", "no_extractable_text")
    else:
        assert result.pages == 6


def test_javascript_and_launch_actions_are_never_executed_or_returned():
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> /AA << /O << /S /JavaScript /JS (app.alert\\(1\\)) >> >> >>"
    content = b"<< /Length 44 >>\nstream\nBT /F1 12 Tf 72 700 Td (visible words here) Tj ET\nendstream"
    cat = b"<< /Type /Catalog /Pages 2 0 R /OpenAction << /S /Launch /F (cmd.exe) >> >>"
    result = extract_pdf(raw_pdf([cat, ONE_PAGE, page, content, FONT]))
    text = result.document.sections[0].text
    assert "visible words here" in text and "alert" not in text and "cmd.exe" not in text


# ------------------------------------------------------------------------------- real-data parity

# Facts snapshotted from the TI C28x IQmath_Quickstart.pdf (C2000Ware, 85 pages, outline "C28x IQmath
# Library" > "Chapter 3. Using the IQmath Library"): section 3.9 "Selecting the GLOBAL_Q format" is on
# page 23, with its `#define GLOBAL_Q` snippet. The document is rebuilt here, so no test reads ~/ti-rag.
IQMATH_PAGES = 85
IQMATH_GLOBAL_Q_PAGE = 23


def iqmath_like_pdf() -> bytes:
    pages = []
    for n in range(1, IQMATH_PAGES + 1):
        lines = [("Fixed Point Math Library", 8), *body_lines(n, 5)]
        if n == IQMATH_GLOBAL_Q_PAGE:
            lines += [("3.9. Selecting the GLOBAL_Q format", 10), ("#define GLOBAL_Q 24", 10)]
        pages.append(lines)
    outline = [
        ("C28x IQmath Library", 0, None), ("Chapter 2. Installing the IQmath Library", 6, "C28x IQmath Library"),
        ("Chapter 3. Using the IQmath Library", 8, "C28x IQmath Library"),
        ("Chapter 4. Function Summary", 26, "C28x IQmath Library"),
    ]
    return build_pdf(pages, outline=outline, title="Fixed Point Math Library")


def test_iqmath_parity_page_count_headings_and_global_q_on_page_23():
    result = extract_pdf(iqmath_like_pdf(), source_url="https://ti.example/IQmath_Quickstart.pdf")
    assert result.pages == 85 and result.image_only_pages == ()
    assert result.document.title == "Fixed Point Math Library"
    hits = [s for s in result.document.sections if "GLOBAL_Q" in s.text]
    assert [s.page for s in hits] == [23]
    assert hits[0].heading_path == ("C28x IQmath Library", "Chapter 3. Using the IQmath Library")
    assert "Fixed Point Math Library" not in hits[0].text  # the running header is gone
    chunks = chunk(result.document, ChunkingConfig(min_tokens=0))
    found = [c for c in chunks if "#define GLOBAL_Q" in c.text]
    assert len(found) == 1 and found[0].page == 23
    assert found[0].anchor == "https://ti.example/IQmath_Quickstart.pdf#page=23"
