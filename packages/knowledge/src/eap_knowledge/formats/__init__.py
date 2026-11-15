"""The document format registry (RAG P1, task P1.A1): MIME type <-> format <-> extensions, where a
format is extracted (the API's ``extract()`` or the worker's sandbox), whether it is binary, which
size cap applies, and the worker extractors' current versions.

This module imports no parser. The extractor modules next to it (``formats/rst.py``,
``formats/pdf.py``, ...) do, and only the worker's sandbox child imports them; the API imports this
registry alone (import-linter keeps ``docutils``, ``pypdf`` and the sandbox out of ``eap_api``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eap_schemas.knowledge import ExtractConfig

from eap_knowledge.errors import UnsupportedMimeType

ExtractionSite = Literal["api", "worker"]
CapClass = Literal["text", "structured", "binary"]

# The current version of each worker extractor, recorded per chunk (`metadata.eap.extractor_version`)
# and in the `extracted` event. Bump a version whenever its output for some input can change; the
# bulk re-extract endpoint then re-extracts the documents recorded with an older one.
EXTRACTOR_VERSIONS: dict[str, int] = {
    "rst": 1,
    "pdf": 1,
    "yaml": 3,  # 2: comments kept (the leading block as a preamble, the rest on their keys);
    # 3: a short preamble opens the first content section. (The title fallback added with it never
    # changes a stored document: the API always passes a title, so only the preamble move counts.)
    "json": 1,  # 1 still: JSON output only differs in the title fallback, which API uploads never reach
    "markdown-doxygen": 1,
}  # keys: eap_schemas.knowledge.EXTRACTOR_IDS, in the same order (a contract test checks it)

# How far into a file the PDF signature may start (the decisions' "within its first 1,024 bytes").
PDF_SIGNATURE_WINDOW = 1024


@dataclass(frozen=True)
class FormatSpec:
    """One document format.

    - ``format``: the registry name (``markdown``, ``html``, ``text``, ``rst``, ``pdf``, ``yaml``,
      ``json``).
    - ``mime_type``: the canonical MIME type; ``aliases`` map to it.
    - ``extensions``: lower-case, with the dot (Studio and the import CLI map files with them).
    - ``site``: ``api`` (``extract()`` validates on upload, ``eap.kb_chunk`` in the worker) or
      ``worker`` (``eap.kb_extract_chunk`` through the sandbox). Markdown is ``api`` unless the
      knowledge base's ``extract.markdown_flavor`` is ``doxygen`` (``site_for``).
    - ``binary``: uploaded as ``content_base64`` (else as ``content``, UTF-8 text).
    - ``cap_class``: ``text`` (EAP_KB_MAX_DOCUMENT_BYTES), ``structured`` (EAP_KB_MAX_STRUCTURED_BYTES)
      or ``binary`` (EAP_KB_MAX_BINARY_BYTES); see ``ExtractLimits.max_bytes_for``.
    - ``extractor``: the worker extractor id for ``worker`` formats, else None.
    """

    format: str
    mime_type: str
    aliases: tuple[str, ...]
    extensions: tuple[str, ...]
    site: ExtractionSite
    binary: bool
    cap_class: CapClass
    extractor: str | None

    @property
    def mime_types(self) -> tuple[str, ...]:
        return (self.mime_type, *self.aliases)


FORMAT_SPECS: tuple[FormatSpec, ...] = (
    FormatSpec("markdown", "text/markdown", ("text/x-markdown", "text/md"), (".md", ".markdown"), "api", False, "text", None),
    FormatSpec("html", "text/html", ("application/xhtml+xml",), (".html", ".htm"), "api", False, "text", None),
    FormatSpec("text", "text/plain", (), (".txt", ".text"), "api", False, "text", None),
    FormatSpec("rst", "text/x-rst", ("text/prs.fallenstein.rst",), (".rst", ".rest"), "worker", False, "text", "rst"),
    FormatSpec("pdf", "application/pdf", (), (".pdf",), "worker", True, "binary", "pdf"),
    FormatSpec("yaml", "application/yaml", ("application/x-yaml", "text/yaml"), (".yaml", ".yml"), "worker", False, "structured", "yaml"),
    FormatSpec("json", "application/json", (), (".json",), "worker", False, "structured", "json"),
)

_BY_MIME: dict[str, FormatSpec] = {m: spec for spec in FORMAT_SPECS for m in spec.mime_types}
_BY_EXTENSION: dict[str, FormatSpec] = {ext: spec for spec in FORMAT_SPECS for ext in spec.extensions}
_BY_FORMAT: dict[str, FormatSpec] = {spec.format: spec for spec in FORMAT_SPECS}

# Canonical types first, in registry order, then aliases: the 415/422 messages list these.
SUPPORTED_MIME_TYPES: tuple[str, ...] = tuple(s.mime_type for s in FORMAT_SPECS) + tuple(
    a for s in FORMAT_SPECS for a in s.aliases
)
CANONICAL_MIME_TYPES: tuple[str, ...] = tuple(s.mime_type for s in FORMAT_SPECS)


def _base_type(mime_type: str) -> str:
    return mime_type.split(";", 1)[0].strip().lower()


def format_for_mime(mime_type: str) -> FormatSpec:
    """The format of a MIME type (parameters such as ``; charset=utf-8`` and case ignored).
    Raises ``UnsupportedMimeType`` (listing the canonical types) for anything else."""
    try:
        return _BY_MIME[_base_type(mime_type)]
    except KeyError:
        raise UnsupportedMimeType(mime_type, CANONICAL_MIME_TYPES) from None


def canonical_mime_type(mime_type: str) -> str:
    """The canonical MIME type for a type or alias (``text/yaml`` -> ``application/yaml``)."""
    return format_for_mime(mime_type).mime_type


def format_by_name(name: str) -> FormatSpec:
    """The spec of a registry format name; KeyError for an unknown one."""
    return _BY_FORMAT[name]


def format_for_filename(filename: str) -> FormatSpec | None:
    """The format of a file by its (case-insensitive) extension, or None when it is not supported."""
    dot = filename.rfind(".")
    if dot < 0 or "/" in filename[dot:]:
        return None
    return _BY_EXTENSION.get(filename[dot:].lower())


def site_for(mime_type: str, extract: ExtractConfig | None = None) -> ExtractionSite:
    """Where a document of this type is extracted, given its knowledge base's ``chunking.extract``.
    Markdown moves to the worker only when ``markdown_flavor`` is ``doxygen``. Raises
    ``UnsupportedMimeType``."""
    return "worker" if extractor_for(mime_type, extract) is not None else "api"


def extractor_for(mime_type: str, extract: ExtractConfig | None = None) -> str | None:
    """The worker extractor id (an ``EXTRACTOR_VERSIONS`` key) for a document, or None when the API
    path (``extract()`` and ``eap.kb_chunk``) handles it. Raises ``UnsupportedMimeType``."""
    spec = format_for_mime(mime_type)
    if spec.format == "markdown" and extract is not None and extract.markdown_flavor == "doxygen":
        return "markdown-doxygen"
    return spec.extractor


def is_worker_extracted(mime_type: str, extract: ExtractConfig | None = None) -> bool:
    return extractor_for(mime_type, extract) is not None


# Magic bytes (the decisions' own table instead of libmagic). Two checks, for two questions:
# - `detect_binary_type`: is a TEXT upload really a binary file? Signatures at offset 0 only, so text
#   that merely mentions "%PDF-1.7" (or a PNG header in prose) is never misread as binary (415).
# - `has_pdf_signature`: is a PDF upload really a PDF? `%PDF-` may start anywhere in the first
#   PDF_SIGNATURE_WINDOW bytes (junk before the header is legal and readers accept it).
# Images are part 2 (P1.B2) and listed so a text upload that is really an image is detected.
def detect_binary_type(data: bytes) -> str | None:
    """The MIME type of a known binary signature at offset 0 of ``data``, or None:
    ``application/pdf``, ``image/png``, ``image/jpeg`` or ``image/webp``."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def has_pdf_signature(data: bytes) -> bool:
    """True when ``%PDF-`` starts before byte PDF_SIGNATURE_WINDOW (it may end past it)."""
    return data.find(b"%PDF-", 0, PDF_SIGNATURE_WINDOW + 4) != -1


__all__ = [
    "CANONICAL_MIME_TYPES",
    "EXTRACTOR_VERSIONS",
    "FORMAT_SPECS",
    "PDF_SIGNATURE_WINDOW",
    "SUPPORTED_MIME_TYPES",
    "CapClass",
    "ExtractionSite",
    "FormatSpec",
    "canonical_mime_type",
    "detect_binary_type",
    "extractor_for",
    "format_by_name",
    "format_for_filename",
    "format_for_mime",
    "has_pdf_signature",
    "is_worker_extracted",
    "site_for",
]
