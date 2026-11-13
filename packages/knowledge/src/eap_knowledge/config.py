"""Chunking config.

The contract type is ``eap_schemas.knowledge.ChunkingConfig`` (pydantic, validated: max_tokens
32..4096, overlap_tokens <= max_tokens / 2, min_tokens < max_tokens), re-exported here. ``chunk``
also accepts any other object with the four attributes (``ChunkingConfigLike``) and validates it
with its own, looser structural rules (``InvalidChunkingConfig``). Every valid ``ChunkingConfig``
passes those rules; the one extra check for both is that ``chunker_version`` is supported here.
"""

from __future__ import annotations

import unicodedata
from typing import Protocol

from eap_schemas.knowledge import CHUNKER_VERSION, ChunkingConfig

# Versions of the chunking algorithm this package implements. Any change that can move a chunk
# boundary or change an embed text is a new version; a knowledge base keeps the version it was
# created with in its chunking config.
SUPPORTED_CHUNKER_VERSIONS = frozenset({1})

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

__all__ = ["CHUNKER_ID", "CHUNKER_VERSION", "DEFAULT_CHUNKING", "SUPPORTED_CHUNKER_VERSIONS", "ChunkingConfig", "ChunkingConfigLike"]
