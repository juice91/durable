"""Object storage for chat attachment bytes (attachments r2, D0).

`BlobStore` is the one interface the API and worker use; the bytes never touch Postgres. Backends,
chosen by `EAP_BLOB_BACKEND`:

- `fs` (default): `FsBlobStore`, a directory (`EAP_BLOB_DIR`, default `./.blobs`). Dev and tests.
- `s3`: `S3BlobStore`, any S3-compatible endpoint (MinIO, AWS S3). Variables:
  `EAP_BLOB_S3_ENDPOINT`, `EAP_BLOB_S3_BUCKET`, `EAP_BLOB_S3_REGION` (default `us-east-1`),
  `EAP_BLOB_S3_ACCESS_KEY`, `EAP_BLOB_S3_SECRET_KEY`.

Keys are `attachments/{tenant_id}/{attachment_id}` (`attachment_key`); the tenant id in the key is
informational, authorisation stays in the API.
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Protocol, runtime_checkable

__all__ = [
    "BlobNotFound",
    "BlobStore",
    "FsBlobStore",
    "S3BlobStore",
    "attachment_key",
    "blob_store_from_env",
]

DEFAULT_BLOB_DIR = "./.blobs"
DEFAULT_S3_REGION = "us-east-1"


class BlobNotFound(KeyError):
    """The key has no blob (`BlobStore.open` raises it when iteration starts)."""


@runtime_checkable
class BlobStore(Protocol):
    async def put(self, key: str, chunks: AsyncIterator[bytes], size: int, media_type: str) -> None:
        """Store `chunks` under `key`, replacing any blob there. Raises ValueError, storing nothing
        (an existing blob is left alone), when the chunks do not add up to exactly `size` bytes."""
        ...

    def open(self, key: str) -> AsyncIterator[bytes]:
        """The blob's bytes in chunks. Raises `BlobNotFound` when iteration starts if there is none."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the blob. Idempotent: a missing key is not an error."""
        ...

    async def exists(self, key: str) -> bool: ...


def attachment_key(tenant_id: str, attachment_id: str) -> str:
    return f"attachments/{tenant_id}/{attachment_id}"


def blob_store_from_env() -> BlobStore:
    """The store `EAP_BLOB_BACKEND` selects (`fs` when unset). ValueError names the bad variable."""
    backend = os.environ.get("EAP_BLOB_BACKEND", "").strip().lower() or "fs"
    if backend == "fs":
        from eap_blob_store.fs import FsBlobStore

        return FsBlobStore(os.environ.get("EAP_BLOB_DIR", "").strip() or DEFAULT_BLOB_DIR)
    if backend == "s3":
        from eap_blob_store.s3 import S3BlobStore

        values = {}
        for name in ("EAP_BLOB_S3_ENDPOINT", "EAP_BLOB_S3_BUCKET", "EAP_BLOB_S3_ACCESS_KEY", "EAP_BLOB_S3_SECRET_KEY"):
            values[name] = os.environ.get(name, "").strip()
            if not values[name]:
                raise ValueError(f"EAP_BLOB_BACKEND=s3 needs {name}")
        return S3BlobStore(
            endpoint=values["EAP_BLOB_S3_ENDPOINT"],
            bucket=values["EAP_BLOB_S3_BUCKET"],
            region=os.environ.get("EAP_BLOB_S3_REGION", "").strip() or DEFAULT_S3_REGION,
            access_key=values["EAP_BLOB_S3_ACCESS_KEY"],
            secret_key=values["EAP_BLOB_S3_SECRET_KEY"],
            path_style=True,
        )
    raise ValueError(f"EAP_BLOB_BACKEND must be 'fs' or 's3', not {backend!r}")


def __getattr__(name: str):  # lazy: importing the package must not import aiobotocore
    if name == "FsBlobStore":
        from eap_blob_store.fs import FsBlobStore

        return FsBlobStore
    if name == "S3BlobStore":
        from eap_blob_store.s3 import S3BlobStore

        return S3BlobStore
    raise AttributeError(name)
