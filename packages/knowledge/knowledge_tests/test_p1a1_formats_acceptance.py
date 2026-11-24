"""RAG P1.A1 acceptance (tester): the MIME/extension table, magic-byte boundaries, ExtractLimits.from_env
with exact messages, pickle-safe errors and the parser-free registry import."""

from __future__ import annotations

import pickle
import subprocess
import sys

import pytest

from eap_schemas.knowledge import EXTRACTION_ERROR_CODES, EXTRACTION_ERROR_MESSAGES, ExtractConfig

from eap_knowledge import (
    EXTRACTOR_VERSIONS,
    FORMAT_SPECS,
    SUPPORTED_MIME_TYPES,
    ExtractionFailed,
    ExtractLimits,
    InvalidChunkingConfig,
    UnsupportedMimeType,
    detect_binary_type,
    extractor_for,
    format_for_filename,
    format_for_mime,
    has_pdf_signature,
    is_worker_extracted,
)
from eap_knowledge.formats import CANONICAL_MIME_TYPES, canonical_mime_type, site_for

TABLE = {  # mime -> (format, canonical)
    "text/markdown": ("markdown", "text/markdown"), "text/x-markdown": ("markdown", "text/markdown"),
    "text/md": ("markdown", "text/markdown"),
    "text/html": ("html", "text/html"), "application/xhtml+xml": ("html", "text/html"),
    "text/plain": ("text", "text/plain"),
    "text/x-rst": ("rst", "text/x-rst"), "text/prs.fallenstein.rst": ("rst", "text/x-rst"),
    "application/pdf": ("pdf", "application/pdf"),
    "application/yaml": ("yaml", "application/yaml"), "application/x-yaml": ("yaml", "application/yaml"),
    "text/yaml": ("yaml", "application/yaml"),
    "application/json": ("json", "application/json"),
}
CODE = {  # source code: format -> canonical, aliases
    "python": ("text/x-python", ("text/x-script.python", "application/x-python-code")),
    "typescript": ("text/typescript", ("application/typescript", "text/x-typescript")),
    "tsx": ("text/tsx", ("text/x-tsx",)),
    "javascript": ("text/javascript", ("application/javascript", "application/x-javascript", "text/ecmascript")),
    "shell": ("text/x-shellscript", ("application/x-sh", "text/x-sh")),
    "toml": ("application/toml", ("text/x-toml", "text/toml")),
    "sql": ("application/sql", ("text/x-sql", "text/sql")),
    "dockerfile": ("text/x-dockerfile", ("text/dockerfile",)),
    "css": ("text/css", ()),
}
for _fmt, (_canonical, _aliases) in CODE.items():
    for _mime in (_canonical, *_aliases):
        TABLE[_mime] = (_fmt, _canonical)
VARIANTS = ["{}", "{}; charset=utf-8", "{};charset=UTF-8", "{} ; charset=utf-8", "  {}  ", "{}; charset=\"utf-8\"; q=1",
            "{};", "{}\t;boundary=x"]


@pytest.mark.parametrize("mime", sorted(TABLE))
def test_every_alias_in_every_case_and_parameter_variant(mime):
    fmt, canonical = TABLE[mime]
    for variant in VARIANTS:
        for text in (variant.format(mime), variant.format(mime.upper()), variant.format(mime.title())):
            assert format_for_mime(text).format == fmt, text
            assert canonical_mime_type(text) == canonical, text
            assert extractor_for(text) is None or fmt in {"rst", "pdf", "yaml", "json", *CODE}


def test_supported_list_is_canonical_first_then_aliases_without_duplicates():
    assert CANONICAL_MIME_TYPES == (
        "text/markdown", "text/html", "text/plain", "text/x-rst", "application/pdf", "application/yaml", "application/json",
        "text/x-python", "text/typescript", "text/tsx", "text/javascript", "text/x-shellscript", "application/toml",
        "application/sql", "text/x-dockerfile", "text/css",
    )
    assert SUPPORTED_MIME_TYPES[:16] == CANONICAL_MIME_TYPES
    assert sorted(SUPPORTED_MIME_TYPES) == sorted(TABLE)
    assert all(format_for_mime(m).mime_type == TABLE[m][1] for m in SUPPORTED_MIME_TYPES)


@pytest.mark.parametrize("mime", [
    "", " ", ";", "text", "pdf", "application/", "/pdf", "application/pdf2", "xapplication/pdf", "application/pdf/x",
    "text/x-rst-ish", "application/x-pdf", "application/vnd.pdf", "text/csv", "image/png", "image/jpeg", "image/webp",
    "application/msword", "application/xml", "text/xml", "application/yaml+json", "text/x-yaml", "text/json",
    "application/ld+json", "text/rst", "text/restructuredtext", "text/x-markdown2", "application/octet-stream",
])
def test_unknown_types_give_the_exact_error(mime):
    expected = ", ".join(CANONICAL_MIME_TYPES)
    for call in (format_for_mime, canonical_mime_type, extractor_for, is_worker_extracted, site_for):
        with pytest.raises(UnsupportedMimeType) as info:
            call(mime)
        assert str(info.value) == f"unsupported document mime type {mime!r}: expected {expected}"
        assert info.value.mime_type == mime
        assert info.value.expected == CANONICAL_MIME_TYPES


def test_the_error_names_the_type_as_sent_not_normalised():
    with pytest.raises(UnsupportedMimeType) as info:
        format_for_mime("IMAGE/PNG; charset=x")
    assert str(info.value).startswith("unsupported document mime type 'IMAGE/PNG; charset=x': expected ")


@pytest.mark.parametrize("name,fmt", [
    ("a.md", "markdown"), ("a.MD", "markdown"), ("a.markdown", "markdown"), ("a.html", "html"), ("a.HTM", "html"),
    ("a.txt", "text"), ("a.text", "text"), ("a.rst", "rst"), ("a.REST", "rst"), ("a.pdf", "pdf"), ("a.Pdf", "pdf"),
    ("a.yaml", "yaml"), ("a.yml", "yaml"), ("a.YML", "yaml"), ("a.json", "json"), ("a.b.c.json", "json"),
    ("dir/sub/x.rst", "rst"), ("C:\\dir\\x.pdf", "pdf"), (".json", "json"), ("a b.md", "markdown"),
])
def test_every_extension_case_insensitively(name, fmt):
    assert format_for_filename(name).format == fmt


@pytest.mark.parametrize("name", [
    "", "md", "README", "a.", "a.md.bak", "a.mdx", "a.jsonl", "a.yaml.gz", "a.pdf ", "a.docx", "a.png", "a.js", "a.py", "Dockerfile", "dir.md/x",
    "dir.rst/file", "a.md/", "a.rst\\b",
])
def test_unsupported_extensions(name):
    assert format_for_filename(name) is None


def test_every_registered_extension_maps_back_to_its_own_format():
    seen: set[str] = set()
    for spec in FORMAT_SPECS:
        for ext in spec.extensions:
            assert ext.startswith(".") and ext == ext.lower() and ext not in seen
            seen.add(ext)
            code = spec.extractor == "code"  # code extensions are opt-in
            assert format_for_filename("x" + ext, include_code=code) is spec
            assert format_for_filename("x" + ext.upper(), include_code=code) is spec
            assert code or format_for_filename("x" + ext, include_code=True) is spec


def test_doxygen_only_changes_markdown_and_none_or_plain_changes_nothing():
    dox = ExtractConfig(markdown_flavor="doxygen")
    for mime in TABLE:
        base = extractor_for(mime)
        assert extractor_for(mime, None) == base == extractor_for(mime, ExtractConfig()) == extractor_for(mime, ExtractConfig(markdown_flavor="plain"))
        expected = "markdown-doxygen" if TABLE[mime][0] == "markdown" else base
        assert extractor_for(mime, dox) == expected
        assert extractor_for(mime, ExtractConfig(pdf_headings="font", rst_tags=["a"])) == base
        assert site_for(mime, dox) == ("worker" if expected else "api")
    assert all(v in EXTRACTOR_VERSIONS for v in {extractor_for(m, dox) for m in TABLE} - {None})


# --- detect_binary_type ----------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n"
PDF = b"%PDF-"


# P1.A1 review 2026-11-15: the 1 KiB window moved to has_pdf_signature (the positive check on PDF
# uploads); detect_binary_type (the text-type 415) checks offset 0 only.
@pytest.mark.parametrize("offset,expected", [
    (0, True), (1, True), (512, True), (1022, True), (1023, True), (1024, False), (1025, False), (5000, False),
])
def test_pdf_signature_window_boundaries(offset, expected):
    assert has_pdf_signature(b" " * offset + PDF + b"1.7\n") is expected
    assert has_pdf_signature(b"\x00" * offset + PDF) is expected  # signature is the last bytes
    assert has_pdf_signature(b"A" * offset + PDF + b"\x00" * 3000) is expected
    # detect_binary_type sees a PDF only at offset 0.
    assert detect_binary_type(b" " * offset + PDF + b"1.7\n") == ("application/pdf" if offset == 0 else None)


def test_a_pdf_signature_cut_by_the_window_or_by_the_input_end():
    assert has_pdf_signature(b"\x00" * 1023 + b"%PDF") is False      # truncated at the end
    assert has_pdf_signature(b"\x00" * 1022 + b"%PDF") is False
    assert detect_binary_type(b"%PDF") is None and detect_binary_type(b"%PD") is None and detect_binary_type(b"%") is None
    assert detect_binary_type(b"%PDF-") == "application/pdf" and has_pdf_signature(b"%PDF-") is True
    assert has_pdf_signature(b"\x00" * 1023 + b"%PDF-") is True  # starts inside, ends past it
    assert detect_binary_type(b"\x00" * 1023 + b"%PDF-") is None
    assert has_pdf_signature(b"\x00" * 1024 + b"%PDF-") is False
    assert detect_binary_type(b"%pdf-1.4") is None and detect_binary_type(b"%PDF 1.4") is None
    assert has_pdf_signature(b"%pdf-1.4") is False and has_pdf_signature(b"%PDF 1.4") is False


def test_text_that_mentions_a_pdf_header_is_not_binary():
    assert detect_binary_type(b"# Formats\n\nA PDF starts with %PDF-1.7.\n") is None


@pytest.mark.parametrize("data,expected", [
    (PNG, "image/png"), (PNG + b"x" * 100, "image/png"),
    (PNG[:7], None), (PNG[:4], None), (PNG[:1], None), (b"\x89PNG\r\n\x1a\r", None), (b"\x89png\r\n\x1a\n", None),
    (b" " + PNG, None), (b"\x00" + PNG, None),
    (b"\xff\xd8\xff", "image/jpeg"), (b"\xff\xd8\xff\xe0\x00\x10JFIF", "image/jpeg"), (b"\xff\xd8\xff\xdb", "image/jpeg"),
    (b"\xff\xd8", None), (b"\xff", None), (b"\xff\xd8\x00", None), (b"\x00\xff\xd8\xff", None),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"), (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
    (b"RIFF\x00\x00\x00\x00WEB", None), (b"RIFF\x00\x00\x00\x00", None), (b"RIFF\x00\x00\x00", None), (b"RIFF", None),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", None), (b"RIFF\x00\x00\x00\x00AVI LIST", None),
    (b"riff\x00\x00\x00\x00WEBP", None), (b"RIFF\x00\x00\x00\x00webp", None), (b"\x00RIFF\x00\x00\x00\x00WEBP", None),
    (b"GIF89a", None), (b"BM", None), (b"PK\x03\x04", None), (b"\x7fELF", None), (b"", None),
    (b"# Title\n\ntext\n", None), (b"{\"a\": 1}", None), ("%PDF- é".encode(), "application/pdf"),
])
def test_image_magic_and_truncated_inputs(data, expected):
    assert detect_binary_type(data) == expected


def test_signatures_are_checked_on_bytes_only_and_never_raise():
    for n in range(0, 14):
        for sig in (PNG, b"\xff\xd8\xff", b"RIFF\x00\x00\x00\x00WEBP", PDF):
            assert detect_binary_type(sig[:n]) in (None, "image/png", "image/jpeg", "image/webp", "application/pdf")
    assert detect_binary_type(bytearray(PNG)) == "image/png" and detect_binary_type(memoryview(PNG).tobytes()) == "image/png"


def test_an_image_signature_wins_over_a_later_pdf_marker():
    assert detect_binary_type(PNG + b"%PDF-1.4") == "image/png"
    assert detect_binary_type(b"\xff\xd8\xff" + b"%PDF-") == "image/jpeg"


# --- ExtractLimits.from_env ------------------------------------------------------------------------

MIB = 2**20
BOUNDS = {  # env var -> (field, default, min, max)
    "EAP_KB_MAX_BINARY_BYTES": ("max_binary_bytes", 25 * MIB, 1, 100 * MIB),  # max 100 MiB: P1.A1 review
    "EAP_KB_MAX_STRUCTURED_BYTES": ("max_structured_bytes", 2 * MIB, 1, 64 * MIB),
    "EAP_KB_MAX_PDF_PAGES": ("max_pdf_pages", 2000, 1, 20000),
    "EAP_KB_EXTRACT_CPU_SECONDS": ("extract_cpu_seconds", 60, 1, 3600),
    "EAP_KB_EXTRACT_MEMORY_MB": ("extract_memory_mb", 1024, 256, 65536),  # min 256: P1.A1 review
    "EAP_KB_EXTRACT_CONCURRENCY": ("extract_concurrency", 2, 1, 64),
}


def _msg(name, lo, hi, raw):
    return f"{name} must be an integer from {lo} to {hi}, got {raw!r}"


def test_exactly_six_variables_with_the_decided_defaults():
    assert len(BOUNDS) == 6
    base = ExtractLimits.from_env({})
    for name, (field, default, _lo, _hi) in BOUNDS.items():
        assert getattr(base, field) == default, name
    assert ExtractLimits.from_env({"UNRELATED": "1", "EAP_KB_MAX_DOCUMENT_BYTES": "7"}) == base


@pytest.mark.parametrize("name", sorted(BOUNDS))
def test_each_variable_at_its_bounds_alone(name):
    field, default, lo, hi = BOUNDS[name]
    for value in (lo, lo + 1, hi - 1, hi):
        limits = ExtractLimits.from_env({name: str(value)})
        assert getattr(limits, field) == value
        others = {f: getattr(limits, f) for f, *_ in BOUNDS.values() if f != field}
        assert others == {f: d for f, d, *_ in BOUNDS.values() if f != field}  # nothing else moves


@pytest.mark.parametrize("name", sorted(BOUNDS))
@pytest.mark.parametrize("which", ["below", "above", "zero", "negative", "words", "float", "exp", "hex", "empty_unit", "comma"])
def test_each_variable_out_of_bounds_or_non_numeric_gives_the_exact_message(name, which):
    _field, _default, lo, hi = BOUNDS[name]
    raw = {"below": str(lo - 1), "above": str(hi + 1), "zero": "0", "negative": "-5", "words": "many", "float": "2.5",
           "exp": "1e3", "hex": "0x10", "empty_unit": "10MB", "comma": "1,000"}[which]
    with pytest.raises(ValueError) as info:
        ExtractLimits.from_env({name: raw})
    assert str(info.value) == _msg(name, lo, hi, raw)


@pytest.mark.parametrize("name", sorted(BOUNDS))
def test_blank_and_whitespace_mean_default_and_padding_is_stripped(name):
    field, default, lo, _hi = BOUNDS[name]
    for raw in ("", " ", "\t\n"):
        assert getattr(ExtractLimits.from_env({name: raw}), field) == default
    assert getattr(ExtractLimits.from_env({name: f"  {lo}\n"}), field) == lo


def test_the_first_bad_variable_is_reported_by_its_own_name_and_raw_text():
    env = {"EAP_KB_MAX_PDF_PAGES": "20001", "EAP_KB_EXTRACT_CONCURRENCY": "3"}
    with pytest.raises(ValueError) as info:
        ExtractLimits.from_env(env)
    assert str(info.value) == _msg("EAP_KB_MAX_PDF_PAGES", 1, 20000, "20001")
    with pytest.raises(ValueError) as info:
        ExtractLimits.from_env({"EAP_KB_MAX_BINARY_BYTES": " 99999999999 "})
    assert str(info.value) == _msg("EAP_KB_MAX_BINARY_BYTES", 1, 100 * MIB, " 99999999999 ")


def test_derived_values_follow_the_limits():
    limits = ExtractLimits.from_env({"EAP_KB_EXTRACT_CPU_SECONDS": "7", "EAP_KB_MAX_BINARY_BYTES": "3000"})
    assert limits.extract_wall_seconds == 14
    assert limits.upload_guard_bytes(10) == -(-3000 * 4 // 3) + 64 * 1024  # ceil(3000 * 4/3) = 4000
    assert limits.upload_guard_bytes(10**9) == 10**9


# --- errors ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", EXTRACTION_ERROR_CODES)
def test_extraction_failed_code_maps_to_its_message_after_a_pickle_round_trip(code):
    for detail in ("", "parser said /etc/passwd line 3"):
        exc = pickle.loads(pickle.dumps(ExtractionFailed(code, detail), protocol=pickle.HIGHEST_PROTOCOL))
        assert (exc.reason, exc.detail, exc.code) == (code, detail, code)
        assert exc.user_message == EXTRACTION_ERROR_MESSAGES[code]
        assert str(exc) == f"document extraction failed: {code}" + (f" ({detail})" if detail else "")
        assert not detail or detail not in exc.user_message


@pytest.mark.parametrize("reason", ["", "too_many_attributes", "too_many_sections", "EXTRACT_TIMEOUT", "extract_timeout ", "x"])
def test_an_unknown_reason_falls_back_to_extract_failed(reason):
    exc = pickle.loads(pickle.dumps(ExtractionFailed(reason, "d")))
    assert exc.reason == reason and exc.code == "extract_failed"
    assert exc.user_message == EXTRACTION_ERROR_MESSAGES["extract_failed"] == "The document could not be extracted."


def test_extraction_failed_stays_a_value_error_with_the_old_signature():
    exc = ExtractionFailed("too_many_attributes")
    assert isinstance(exc, ValueError) and exc.detail == "" and exc.args == ("too_many_attributes", "")
    assert str(exc) == "document extraction failed: too_many_attributes"


@pytest.mark.parametrize("exc", [
    UnsupportedMimeType("x/y"),
    UnsupportedMimeType("x/y", None),
    UnsupportedMimeType("x/y", ("text/markdown", "application/pdf")),
    UnsupportedMimeType("x/y", ()),
    ExtractionFailed("extract_memory"),
    ExtractionFailed("no_extractable_text", "d"),
    InvalidChunkingConfig("chunker_version", 3, "unsupported"),
])
def test_every_error_pickles_by_value(exc):
    for protocol in range(2, pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(exc, protocol=protocol))
        assert type(restored) is type(exc) and restored.args == exc.args and str(restored) == str(exc)
        assert restored.__dict__ == exc.__dict__


def test_unsupported_mime_type_expected_argument():
    default = "unsupported document mime type 'a/b': expected text/markdown, text/html or text/plain"
    assert str(UnsupportedMimeType("a/b")) == default
    assert str(UnsupportedMimeType("a/b", None)) == default
    assert str(UnsupportedMimeType("a/b", ())) == default  # empty falls back to the API-extracted three
    assert str(UnsupportedMimeType("a/b", ("one",))) == "unsupported document mime type 'a/b': expected one"
    assert str(UnsupportedMimeType("a/b", ("one", "two"))) == "unsupported document mime type 'a/b': expected one, two"
    assert isinstance(UnsupportedMimeType("a/b"), ValueError)


# --- the registry imports no parser ----------------------------------------------------------------

@pytest.mark.parametrize("module", ["eap_knowledge.formats", "eap_knowledge.errors", "eap_knowledge.config"])
def test_importing_the_registry_loads_no_parser_or_sandbox(module):
    code = (
        "import sys, json; import " + module + "; "
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in {'docutils','pypdf','pypdfium2','PIL','yaml'} "
        "or m.startswith('eap_knowledge.sandbox') or m.startswith('eap_knowledge.formats.')); print(json.dumps(loaded))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out


def test_importing_eap_knowledge_itself_loads_no_docutils_pypdf_or_sandbox():
    # The API imports eap_knowledge (extract, chunk, formats): none of the worker-side parsers may come with it.
    code = (
        "import sys, json; import eap_knowledge; "
        "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in {'docutils','pypdf','pypdfium2','PIL'} "
        "or m.startswith('eap_knowledge.sandbox'))))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]", out
