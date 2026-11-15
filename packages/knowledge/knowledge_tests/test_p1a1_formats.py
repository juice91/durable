"""RAG P1.A1 in eap_knowledge: the format registry, EXTRACTOR_VERSIONS, the extraction limits and
the pickle-safe errors."""

from __future__ import annotations

import ast
import math
import pickle
from pathlib import Path

import pytest

from eap_schemas.knowledge import EXTRACTION_ERROR_CODES, EXTRACTOR_IDS, ExtractConfig

from eap_knowledge import (
    EXTRACT_LIMIT_ENV,
    EXTRACTOR_VERSIONS,
    FORMAT_SPECS,
    SUPPORTED_MIME_TYPES,
    ExtractionFailed,
    ExtractLimits,
    InvalidChunkingConfig,
    UnsupportedMimeType,
    detect_binary_type,
    document_format,
    extractor_for,
    format_for_filename,
    format_for_mime,
    has_pdf_signature,
    is_worker_extracted,
)
from eap_knowledge.formats import canonical_mime_type, format_by_name, site_for

ALIASES = {
    "text/markdown": "markdown", "text/x-markdown": "markdown", "text/md": "markdown",
    "text/html": "html", "application/xhtml+xml": "html",
    "text/plain": "text",
    "text/x-rst": "rst", "text/prs.fallenstein.rst": "rst",
    "application/pdf": "pdf",
    "application/yaml": "yaml", "application/x-yaml": "yaml", "text/yaml": "yaml",
    "application/json": "json",
}


@pytest.mark.parametrize("mime,fmt", sorted(ALIASES.items()))
def test_every_alias_maps_to_its_format(mime, fmt):
    assert format_for_mime(mime).format == fmt
    assert format_for_mime(mime.upper() + "; charset=utf-8").format == fmt


def test_registry_covers_exactly_the_decided_types():
    assert set(SUPPORTED_MIME_TYPES) == set(ALIASES)
    assert len(SUPPORTED_MIME_TYPES) == len(set(SUPPORTED_MIME_TYPES))
    assert canonical_mime_type("text/yaml") == "application/yaml"
    assert canonical_mime_type("text/prs.fallenstein.rst") == "text/x-rst"


def test_api_formats_agree_with_extract():
    for spec in FORMAT_SPECS:
        if spec.site == "api":
            for mime in spec.mime_types:
                assert document_format(mime) == spec.format
        else:
            with pytest.raises(UnsupportedMimeType):
                document_format(spec.mime_type)


@pytest.mark.parametrize("mime", ["application/msword", "image/png", "text/csv", "", "pdf", "text/x-rst-ish"])
def test_unknown_types_rejected(mime):
    with pytest.raises(UnsupportedMimeType) as info:
        format_for_mime(mime)
    assert "application/pdf" in str(info.value) and "text/x-rst" in str(info.value)
    with pytest.raises(UnsupportedMimeType):
        extractor_for(mime)


def test_site_binary_caps_and_extractors():
    table = {s.format: (s.site, s.binary, s.cap_class, s.extractor) for s in FORMAT_SPECS}
    assert table == {
        "markdown": ("api", False, "text", None),
        "html": ("api", False, "text", None),
        "text": ("api", False, "text", None),
        "rst": ("worker", False, "text", "rst"),
        "pdf": ("worker", True, "binary", "pdf"),
        "yaml": ("worker", False, "structured", "yaml"),
        "json": ("worker", False, "structured", "json"),
    }
    assert format_by_name("pdf").mime_type == "application/pdf"


def test_markdown_goes_to_the_worker_only_when_doxygen():
    doxygen = ExtractConfig(markdown_flavor="doxygen")
    assert extractor_for("text/markdown") is None and site_for("text/markdown") == "api"
    assert extractor_for("text/markdown", ExtractConfig(markdown_flavor="plain")) is None
    assert extractor_for("text/x-markdown", doxygen) == "markdown-doxygen"
    assert site_for("text/markdown", doxygen) == "worker"
    assert is_worker_extracted("text/markdown", doxygen)
    # Doxygen changes nothing for other formats.
    assert extractor_for("text/html", doxygen) is None
    assert extractor_for("application/x-yaml", doxygen) == "yaml"


@pytest.mark.parametrize("name,fmt", [
    ("a.md", "markdown"), ("A.MARKDOWN", "markdown"), ("x.htm", "html"), ("x.txt", "text"),
    ("docs/index.rst", "rst"), ("x.rest", "rst"), ("m.PDF", "pdf"), ("b.yml", "yaml"),
    ("b.yaml", "yaml"), ("c.json", "json"),
])
def test_extensions(name, fmt):
    assert format_for_filename(name).format == fmt


@pytest.mark.parametrize("name", ["README", "x.docx", "x.png", "dir.md/file", "x.", ".md.bak"])
def test_unknown_extensions(name):
    assert format_for_filename(name) is None


def test_extractor_versions_match_the_contract_ids():
    assert tuple(EXTRACTOR_VERSIONS) == EXTRACTOR_IDS
    assert all(isinstance(v, int) and v >= 1 for v in EXTRACTOR_VERSIONS.values())
    worker_ids = {s.extractor for s in FORMAT_SPECS if s.extractor} | {"markdown-doxygen"}
    assert worker_ids == set(EXTRACTOR_VERSIONS)


@pytest.mark.parametrize("data,expected", [
    (b"%PDF-1.7\n", "application/pdf"),
    (b" %PDF-1.7\n", None),  # offset 0 only: text mentioning a PDF header is not a PDF
    (b"See %PDF-1.7 in the spec", None),
    (b"\x00" * 1023 + b"%PDF-1.4", None),
    (b"\x89PNG\r\n\x1a\n....", "image/png"),
    (b"\xff\xd8\xff\xe0", "image/jpeg"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"RIFF\x00\x00\x00\x00WAVE", None),
    (b"# heading\n", None),
    (b"", None),
])
def test_detect_binary_type(data, expected):
    assert detect_binary_type(data) == expected


@pytest.mark.parametrize("data,expected", [
    (b"%PDF-1.7", True), (b"\x00" * 1023 + b"%PDF-", True), (b"junk\n%PDF-1.4", True),
    (b"\x00" * 1024 + b"%PDF-", False), (b"\x00" * 1022 + b"%PDF", False), (b"%pdf-1.4", False), (b"", False),
])
def test_has_pdf_signature_window(data, expected):
    assert has_pdf_signature(data) is expected


def test_registry_imports_no_parser():
    path = Path(__file__).resolve().parents[1] / "src" / "eap_knowledge" / "formats" / "__init__.py"
    imported = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported <= {"__future__", "dataclasses", "typing", "eap_schemas.knowledge", "eap_knowledge.errors"}


# --- limits ----------------------------------------------------------------------------------------


def test_limits_defaults():
    limits = ExtractLimits.from_env({})
    assert limits == ExtractLimits()
    assert (limits.max_binary_bytes, limits.max_structured_bytes, limits.max_pdf_pages) == (25 * 2**20, 2 * 2**20, 2000)
    assert (limits.extract_cpu_seconds, limits.extract_memory_mb, limits.extract_concurrency) == (60, 1024, 2)
    assert limits.extract_wall_seconds == 120


def test_limits_read_every_variable():
    env = {name: str(lo) for name, _default, lo, _hi in EXTRACT_LIMIT_ENV.values()}
    limits = ExtractLimits.from_env(env)
    assert [getattr(limits, f) for f in EXTRACT_LIMIT_ENV] == [lo for _n, _d, lo, _h in EXTRACT_LIMIT_ENV.values()]
    env = {name: f" {hi} " for name, _default, _lo, hi in EXTRACT_LIMIT_ENV.values()}
    limits = ExtractLimits.from_env(env)
    assert [getattr(limits, f) for f in EXTRACT_LIMIT_ENV] == [hi for _n, _d, _l, hi in EXTRACT_LIMIT_ENV.values()]
    assert ExtractLimits.from_env({"EAP_KB_MAX_PDF_PAGES": "  "}).max_pdf_pages == 2000


@pytest.mark.parametrize("field", list(EXTRACT_LIMIT_ENV))
@pytest.mark.parametrize("kind", ["below", "above", "text", "float"])
def test_limits_out_of_bounds(field, kind):
    name, _default, lo, hi = EXTRACT_LIMIT_ENV[field]
    raw = {"below": str(lo - 1), "above": str(hi + 1), "text": "lots", "float": "1.5"}[kind]
    with pytest.raises(ValueError, match=name):
        ExtractLimits.from_env({name: raw})


def test_upload_guard_and_per_format_caps():
    limits = ExtractLimits()
    text_cap = 5 * 2**20
    assert limits.upload_guard_bytes(text_cap) == math.ceil(25 * 2**20 * 4 / 3) + 64 * 1024
    assert ExtractLimits(max_binary_bytes=1000).upload_guard_bytes(text_cap) == text_cap
    assert limits.max_bytes_for("text", text_cap) == text_cap
    assert limits.max_bytes_for("structured", text_cap) == 2 * 2**20
    assert limits.max_bytes_for("structured", 1000) == 1000
    assert limits.max_bytes_for("binary", text_cap) == 25 * 2**20
    with pytest.raises(ValueError):
        limits.max_bytes_for("image", text_cap)


# --- errors ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    UnsupportedMimeType("application/msword"),
    UnsupportedMimeType("application/msword", ("text/markdown", "application/pdf")),
    InvalidChunkingConfig("chunker_version", 3, "unsupported"),
    ExtractionFailed("document_encrypted"),
    ExtractionFailed("extract_timeout", "cpu 60 s"),
    ExtractionFailed("too_many_attributes", "x"),
])
def test_errors_pickle(exc):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert restored.args == exc.args


def test_unsupported_mime_type_messages():
    assert str(UnsupportedMimeType("x/y")) == (
        "unsupported document mime type 'x/y': expected text/markdown, text/html or text/plain"
    )
    assert str(UnsupportedMimeType("x/y", ("a/b", "c/d"))) == "unsupported document mime type 'x/y': expected a/b, c/d"


@pytest.mark.parametrize("code", EXTRACTION_ERROR_CODES)
def test_extraction_failed_codes(code):
    exc = pickle.loads(pickle.dumps(ExtractionFailed(code, "internal detail /etc/passwd")))
    assert exc.code == code
    assert "passwd" not in exc.user_message and exc.user_message


def test_extraction_failed_other_reasons_read_as_extract_failed():
    exc = ExtractionFailed("too_many_attributes")
    assert exc.code == "extract_failed"
    assert exc.user_message == "The document could not be extracted."
