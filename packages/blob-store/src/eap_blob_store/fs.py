"""Filesystem backend: one file per key under a root directory (dev and tests)."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import AsyncIterator

from eap_blob_store import BlobNotFound

_READ_CHUNK = 64 * 1024


class FsBlobStore:
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        parts = key.split("/") if key else []
        if not parts or any(p in ("", ".", "..") or "\x00" in p or "\\" in p for p in parts):
            raise ValueError(f"invalid blob key {key!r}")
        return self.root.joinpath(*parts)

    async def put(self, key: str, chunks: AsyncIterator[bytes], size: int, media_type: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.part"
        total = 0
        try:
            with open(tmp, "wb") as handle:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > size:
                        break  # no point writing more: the size is already wrong
                    await asyncio.to_thread(handle.write, chunk)
            if total != size:
                raise ValueError(f"blob {key}: expected {size} bytes, got {total}")
            await asyncio.to_thread(os.replace, tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            handle = await asyncio.to_thread(open, path, "rb")
        except (FileNotFoundError, IsADirectoryError):
            raise BlobNotFound(key) from None
        try:
            while chunk := await asyncio.to_thread(handle.read, _READ_CHUNK):
                yield chunk
        finally:
            handle.close()

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)
