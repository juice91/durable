"""eap_knowledge: pure, deterministic extraction and chunking for knowledge base documents.

Imports only the standard library, ``eap_schemas`` and ``markdown_it``; no I/O. The format registry
(``eap_knowledge.formats``) is parser-free; the worker-side extractors next to it import their parsers.
"""

from eap_knowledge.chunking import CHARS_PER_TOKEN, MAX_HEADING_CHARS, PREFIX_SHARE, chunk
from eap_knowledge.config import (
    CHUNKER_ID,
    CHUNKER_VERSION,
    DEFAULT_CHUNKING,
    EXTRACT_LIMIT_ENV,
    SUPPORTED_CHUNKER_VERSIONS,
    ChunkingConfig,
    ChunkingConfigLike,
    ExtractLimits,
)
from eap_knowledge.errors import ExtractionFailed, InvalidChunkingConfig, UnsupportedMimeType
from eap_knowledge.extract import MAX_HTML_ATTRIBUTES, MAX_SECTIONS, document_format, extract
from eap_knowledge.formats import (
    CODE_LANGUAGES,
    EXTRACTOR_VERSIONS,
    FORMAT_SPECS,
    SUPPORTED_MIME_TYPES,
    FormatSpec,
    detect_binary_type,
    extractor_for,
    format_for_filename,
    format_for_mime,
    has_pdf_signature,
    is_worker_extracted,
)
from eap_knowledge.model import Chunk, CodeRef, Document, Section, approx_tokens, sha256_hex
from eap_knowledge.slug import anchor_fragment, slugify

__all__ = [
    "CHARS_PER_TOKEN",
    "CHUNKER_ID",
    "CHUNKER_VERSION",
    "CODE_LANGUAGES",
    "DEFAULT_CHUNKING",
    "EXTRACTOR_VERSIONS",
    "EXTRACT_LIMIT_ENV",
    "FORMAT_SPECS",
    "MAX_HEADING_CHARS",
    "MAX_HTML_ATTRIBUTES",
    "MAX_SECTIONS",
    "PREFIX_SHARE",
    "SUPPORTED_CHUNKER_VERSIONS",
    "SUPPORTED_MIME_TYPES",
    "Chunk",
    "CodeRef",
    "ChunkingConfig",
    "ChunkingConfigLike",
    "Document",
    "ExtractLimits",
    "ExtractionFailed",
    "FormatSpec",
    "InvalidChunkingConfig",
    "Section",
    "UnsupportedMimeType",
    "anchor_fragment",
    "approx_tokens",
    "chunk",
    "detect_binary_type",
    "document_format",
    "extract",
    "extractor_for",
    "format_for_filename",
    "format_for_mime",
    "has_pdf_signature",
    "is_worker_extracted",
    "sha256_hex",
    "slugify",
]
