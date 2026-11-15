"""Chunking config.

The contract type is ``eap_schemas.knowledge.ChunkingConfig`` (pydantic, validated: max_tokens
32..4096, overlap_tokens <= max_tokens / 2, min_tokens < max_tokens), re-exported here. ``chunk``
also accepts any other object with the four attributes (``ChunkingConfigLike``) and validates it
with its own, looser structural rules (``InvalidChunkingConfig``). Every valid ``ChunkingConfig``
passes those rules; the one extra check for both is that ``chunker_version`` is supported here.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from eap_schemas.knowledge import CHUNKER_VERSION, ChunkingConfig

# Versions of the chunking algorithm this package implements. Any change that can move a chunk
# boundary or change an embed text is a new version; a knowledge base keeps the version it was
# created with in its chunking config. Exception, 2026-11-14: version 1 was changed in place so a
# merged section keeps its first section's title, path and anchor (it took a later heading as its
# title). Only merged chunks' embed texts changed. Existing knowledge bases keep the old titles until
# a document is uploaded again with a change (content, title, metadata, source URL or MIME type),
# which stores a new version; then only its merged chunks are re-embedded. An identical upload
# (`unchanged`, no run), a reingest of a ready version and a reindex (it copies chunks without
# re-chunking) keep the old chunks. A new version would have
# left every existing knowledge base on the wrong titles for good (a knowledge base cannot move to a
# new chunker version).
# Version 2 (RAG P1, `eap_knowledge.chunking`): short sections merge into their parent heading's
# section, and code-only groups get a context line in their embed text; v1 stays unchanged.
SUPPORTED_CHUNKER_VERSIONS = frozenset({1, 2})

# The chunker's full identity: the algorithm version plus the Unicode database it ran against
# (NFC, combining marks and categories all come from it). A Python upgrade that changes the Unicode
# version can change output, so the golden tests pin this string.
CHUNKER_ID = f"{CHUNKER_VERSION}/unicode-{unicodedata.unidata_version}"


class ChunkingConfigLike(Protocol):
    @property
    def max_tokens(self) -> int: ...

    @property
    def overlap_tokens(self) -> int: ...

    @property
    def min_tokens(self) -> int: ...

    @property
    def chunker_version(self) -> int: ...


DEFAULT_CHUNKING = ChunkingConfig()

_MIB = 1024 * 1024

# RAG P1 extraction limits: env var -> (default, minimum, maximum). Read by the API (upload caps)
# and the worker (sandbox limits) through `ExtractLimits.from_env(os.environ)`; this package does
# no I/O, so the caller passes the mapping. The sandbox child never reads the environment (it runs
# with an empty one): the worker passes the limits it needs in the payload.
EXTRACT_LIMIT_ENV: dict[str, tuple[str, int, int, int]] = {
    # field: (env var, default, min, max)
    "max_binary_bytes": ("EAP_KB_MAX_BINARY_BYTES", 25 * _MIB, 1, 100 * _MIB),
    "max_structured_bytes": ("EAP_KB_MAX_STRUCTURED_BYTES", 2 * _MIB, 1, 64 * _MIB),
    "max_pdf_pages": ("EAP_KB_MAX_PDF_PAGES", 2_000, 1, 20_000),
    "extract_cpu_seconds": ("EAP_KB_EXTRACT_CPU_SECONDS", 60, 1, 3_600),
    "extract_memory_mb": ("EAP_KB_EXTRACT_MEMORY_MB", 1_024, 256, 65_536),
    "extract_concurrency": ("EAP_KB_EXTRACT_CONCURRENCY", 2, 1, 64),
}

# The upload route's JSON envelope around a base64 payload (field names, title, metadata).
UPLOAD_ENVELOPE_BYTES = 64 * 1024


def _read_bounded(environ: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    # ASCII digits only: int() would also take "1_000", "+5" and non-ASCII digits.
    if not re.fullmatch(r"[0-9]+", raw.strip()):
        raise ValueError(f"{name} must be an integer from {lo} to {hi}, got {raw!r}")
    value = int(raw.strip())
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be an integer from {lo} to {hi}, got {raw!r}")
    return value


@dataclass(frozen=True)
class ExtractLimits:
    """The extraction caps and sandbox limits (RAG P1 decisions "Upload contract" and "Where parsing
    happens"). Defaults: 25 MiB decoded binary (PDF), 2 MiB YAML/JSON, 2,000 PDF pages, 60 s CPU,
    1,024 MB address space (Linux only), 2 concurrent extractions per worker."""

    max_binary_bytes: int = EXTRACT_LIMIT_ENV["max_binary_bytes"][1]
    max_structured_bytes: int = EXTRACT_LIMIT_ENV["max_structured_bytes"][1]
    max_pdf_pages: int = EXTRACT_LIMIT_ENV["max_pdf_pages"][1]
    extract_cpu_seconds: int = EXTRACT_LIMIT_ENV["extract_cpu_seconds"][1]
    extract_memory_mb: int = EXTRACT_LIMIT_ENV["extract_memory_mb"][1]
    extract_concurrency: int = EXTRACT_LIMIT_ENV["extract_concurrency"][1]

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "ExtractLimits":
        """Read every limit; unset or blank means the default. ValueError (naming the variable and
        its bounds) for a non-integer or out-of-range value, so startup can fail early."""
        return cls(**{
            field: _read_bounded(environ, name, default, lo, hi)
            for field, (name, default, lo, hi) in EXTRACT_LIMIT_ENV.items()
        })

    @property
    def extract_wall_seconds(self) -> int:
        """The sandbox child's wall-clock timeout: twice its CPU limit."""
        return 2 * self.extract_cpu_seconds

    def upload_guard_bytes(self, text_cap: int) -> int:
        """The upload route's request-body guard: ``max(text cap, ceil(binary cap * 4 / 3) + 64 KiB)``.
        The exact per-format cap is checked after decoding (``max_bytes_for``)."""
        return max(text_cap, math.ceil(self.max_binary_bytes * 4 / 3) + UPLOAD_ENVELOPE_BYTES)

    def max_bytes_for(self, cap_class: str, text_cap: int) -> int:
        """A format's decoded-size cap by its registry ``cap_class``: ``text`` -> the text cap
        (EAP_KB_MAX_DOCUMENT_BYTES), ``structured`` -> the smaller of it and the YAML/JSON cap,
        ``binary`` -> the binary cap."""
        if cap_class == "text":
            return text_cap
        if cap_class == "structured":
            return min(text_cap, self.max_structured_bytes)
        if cap_class == "binary":
            return self.max_binary_bytes
        raise ValueError(f"unknown cap class {cap_class!r}")


__all__ = [
    "CHUNKER_ID",
    "CHUNKER_VERSION",
    "DEFAULT_CHUNKING",
    "EXTRACT_LIMIT_ENV",
    "SUPPORTED_CHUNKER_VERSIONS",
    "UPLOAD_ENVELOPE_BYTES",
    "ChunkingConfig",
    "ChunkingConfigLike",
    "ExtractLimits",
]
