"""RAG P1 quality fixes, acceptance: what extracts as empty (rst) and what never does (PDF)."""

from __future__ import annotations

import pytest

from eap_knowledge import ChunkingConfig, ExtractionFailed, chunk
from eap_knowledge.formats.pdf import extract_pdf
from eap_knowledge.formats.rst import extract_rst
from eap_schemas.knowledge import EXTRACTION_ERROR_CODES, EXTRACTION_ERROR_MESSAGES, ExtractedEventData
from knowledge_tests.pdf_fixtures import body_lines, build_pdf

TOCTREE = ".. toctree::\n   :maxdepth: 2\n\n   linux/index\n   rtos/index\n"


@pytest.mark.parametrize("text", [
    TOCTREE,
    ".. include:: _Users_Guide.rst\n",
    "Title\n=====\n\n.. toctree::\n\n   a\n",
], ids=["toctree-only", "include-only", "title-plus-toctree"])
def test_rst_pages_with_no_prose_extract_without_error_and_chunk_to_nothing(text):
    doc = extract_rst(text)  # no ExtractionFailed: the worker decides what an empty result means
    assert chunk(doc, ChunkingConfig()) == []


def test_title_plus_toctree_has_chars_but_no_chunks():
    """The worker's empty test is `chars == 0 or not chunks`: this page needs the second half."""
    doc = extract_rst("Title\n=====\n\n.. toctree::\n\n   a\n")
    assert doc.extracted_chars > 0 and chunk(doc, ChunkingConfig()) == []


def test_toctree_plus_real_text_is_chunked_not_empty():
    doc = extract_rst("Title\n=====\n\n.. toctree::\n\n   a\n\nReal text here.\n")
    chunks = chunk(doc, ChunkingConfig())
    assert chunks and "Real text here." in chunks[0].text
    assert "linux" not in chunks[0].text


def test_image_only_pdf_fails_no_text_layer():
    with pytest.raises(ExtractionFailed) as err:
        extract_pdf(build_pdf([[], []], image_pages={0, 1}))
    assert err.value.code == "no_text_layer"
    assert err.value.user_message == EXTRACTION_ERROR_MESSAGES["no_text_layer"]


def test_blank_pdf_keeps_no_extractable_text():
    with pytest.raises(ExtractionFailed) as err:
        extract_pdf(build_pdf([[], []]))
    assert err.value.code == "no_extractable_text"


def test_mixed_pdf_with_some_text_pages_is_chunked_and_flags_the_image_page():
    result = extract_pdf(build_pdf([[], body_lines(0), body_lines(1)], image_pages={0}))
    assert result.pages == 3 and result.document.extracted_chars > 0
    assert chunk(result.document, ChunkingConfig())
    assert list(getattr(result, "image_only_pages", ())) == [1]


def test_no_text_layer_is_a_registered_extraction_code_with_a_sentence_message():
    assert "no_text_layer" in EXTRACTION_ERROR_CODES
    assert set(EXTRACTION_ERROR_CODES) == set(EXTRACTION_ERROR_MESSAGES)
    assert "no_text_layer" in EXTRACTION_ERROR_MESSAGES and EXTRACTION_ERROR_MESSAGES["no_text_layer"].endswith(".")


def test_extracted_event_empty_reason_is_additive_and_validated():
    base = {"extractor": "rst", "extractor_version": 1, "sections": 1, "chars": 0}
    assert "empty_reason" not in ExtractedEventData(**base).model_dump(mode="json")  # omitted when None
    assert ExtractedEventData(**base, empty_reason="no_extractable_text").model_dump(mode="json")[
        "empty_reason"] == "no_extractable_text"
    with pytest.raises(ValueError):
        ExtractedEventData(**base, empty_reason="no_text_layer")  # a PDF reason is never an empty one
