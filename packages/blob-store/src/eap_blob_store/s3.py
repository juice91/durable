"""S3-compatible backend on aiobotocore (MinIO in tests).

Path-style addressing. Request checksums are only sent when the API requires them: R2 and MinIO
reject the default CRC checksum headers newer botocore adds. A client is opened per call, deliberately:
an aiobotocore client's connection pool is bound to the event loop it was created on, and one store
is used from several loops (the API's request loop, anyio threads via `from_thread`, the worker's
`asyncio.run`), so a cached client would break across loops. The cost is a new connection per call,
small next to a file transfer; a per-loop cache can replace it if call volume ever makes it matter.
Connect and read timeouts are set so an unreachable store fails fast instead of hanging a request.
"""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError

from eap_blob_store import BlobNotFound

_READ_CHUNK = 64 * 1024
_CONNECT_TIMEOUT = 5  # seconds
_READ_TIMEOUT = 30  # seconds between bytes, not for the whole transfer
_MISSING = {"404", "NoSuchKey", "NotFound"}


class S3BlobStore:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        path_style: bool = True,
    ) -> None:
        self.endpoint = endpoint
        self.bucket = bucket
        self.region = region
        self.path_style = path_style
        self._access_key = access_key
        self._secret_key = secret_key
        self._config = AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=_CONNECT_TIMEOUT,
            read_timeout=_READ_TIMEOUT,
        )

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        async with get_session().create_client(
            "s3",
            endpoint_url=self.endpoint,
            region_name=self.region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            config=self._config,
        ) as client:
            yield client

    async def ensure_bucket(self) -> None:
        """Create the bucket when it is missing (tests; the deployment's bucket is made by the operator)."""
        async with self._client() as client:
            try:
                await client.head_bucket(Bucket=self.bucket)
            except ClientError:
                await client.create_bucket(Bucket=self.bucket)

    async def put(self, key: str, chunks: AsyncIterator[bytes], size: int, media_type: str) -> None:
        # Spool first so a wrong size never replaces the existing object; memory stays bounded.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as spool:
            total = 0
            async for chunk in chunks:
                total += len(chunk)
                if total > size:
                    break
                spool.write(chunk)
            if total != size:
                raise ValueError(f"blob {key}: expected {size} bytes, got {total}")
            spool.seek(0)
            # The spool file itself is the Body: the upload streams from memory/disk, never a full copy.
            async with self._client() as client:
                await client.put_object(Bucket=self.bucket, Key=key, Body=spool, ContentType=media_type)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _code(exc) in _MISSING:
                    raise BlobNotFound(key) from None
                raise
            async with response["Body"] as stream:
                async for chunk in stream.iter_chunks(_READ_CHUNK):
                    if chunk:
                        yield chunk

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=self.bucket, Key=key)  # S3 deletes are idempotent

    async def exists(self, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.head_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if _code(exc) in _MISSING:
                    return False
                raise
            return True


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))
