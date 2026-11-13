"""eap_knowledge: pure, deterministic extraction and chunking for knowledge base documents.

Imports only the standard library, ``eap_schemas`` and ``markdown_it``; no I/O.
"""

from eap_knowledge.chunking import CHARS_PER_TOKEN, MAX_HEADING_CHARS, PREFIX_SHARE, chunk
from eap_knowledge.config import (
    CHUNKER_ID,
    CHUNKER_VERSION,
    DEFAULT_CHUNKING,
    SUPPORTED_CHUNKER_VERSIONS,
    ChunkingConfig,
    ChunkingConfigLike,
)
from eap_knowledge.errors import ExtractionFailed, InvalidChunkingConfig, UnsupportedMimeType
from eap_knowledge.extract import MAX_HTML_ATTRIBUTES, MAX_SECTIONS, document_format, extract
from eap_knowledge.model import Chunk, Document, Section, approx_tokens, sha256_hex
from eap_knowledge.slug import anchor_fragment, slugify

__all__ = [
    "CHARS_PER_TOKEN",
    "CHUNKER_ID",
    "CHUNKER_VERSION",
    "DEFAULT_CHUNKING",
    "MAX_HEADING_CHARS",
    "MAX_HTML_ATTRIBUTES",
    "MAX_SECTIONS",
    "PREFIX_SHARE",
    "SUPPORTED_CHUNKER_VERSIONS",
    "Chunk",
    "ChunkingConfig",
    "ChunkingConfigLike",
    "Document",
    "ExtractionFailed",
    "InvalidChunkingConfig",
    "Section",
    "UnsupportedMimeType",
    "anchor_fragment",
    "approx_tokens",
    "chunk",
    "document_format",
    "extract",
    "sha256_hex",
    "slugify",
]
