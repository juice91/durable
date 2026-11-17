"""BlobStore contract: one suite, run against the `fs` backend and, when EAP_TEST_S3_ENDPOINT is set,
against the `s3` backend (an S3-compatibility check on a throwaway container, never a deployed
component). Any S3-compatible server works; the MinIO images are no longer pullable from Docker Hub or
Quay, so the check was run on RustFS:

    docker run -d --name eap-s3-contract-test -p 127.0.0.1:19000:9000 -e RUSTFS_ACCESS_KEY=minioadmin \
        -e RUSTFS_SECRET_KEY=minioadmin -e RUSTFS_VOLUMES=/data rustfs/rustfs:1.0.0-alpha.45
    EAP_TEST_S3_ENDPOINT=http://127.0.0.1:19000 uv run pytest packages/blob-store
    docker rm -f eap-s3-contract-test

EAP_TEST_S3_ACCESS_KEY / EAP_TEST_S3_SECRET_KEY default to minioadmin. Without the endpoint the `s3`
cases are skipped with a reason.
"""

from __future__ import annotations

import asyncio
import functools
import os
import uuid

import pytest

from eap_blob_store import BlobNotFound, BlobStore, FsBlobStore, attachment_key, blob_store_from_env

S3_ENDPOINT = os.environ.get("EAP_TEST_S3_ENDPOINT", "").strip()


async def stream(data: bytes, chunk: int = 64 * 1024):
    for i in range(0, len(data), chunk):
        yield data[i : i + chunk]


async def collect(store: BlobStore, key: str) -> bytes:
    return b"".join([c async for c in store.open(key)])


def sync(fn):
    """Run an async test body to completion (the workspace has no asyncio pytest plugin mode set)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


@pytest.fixture(params=["fs", "s3"])
def store(request, tmp_path) -> BlobStore:
    if request.param == "fs":
        return FsBlobStore(tmp_path / "blobs")
    if not S3_ENDPOINT:
        pytest.skip("EAP_TEST_S3_ENDPOINT is not set (no MinIO container); the s3 contract was not run")
    from eap_blob_store.s3 import S3BlobStore

    s3 = S3BlobStore(
        endpoint=S3_ENDPOINT,
        bucket=f"contract-{uuid.uuid4().hex[:10]}",
        region="us-east-1",
        access_key=os.environ.get("EAP_TEST_S3_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("EAP_TEST_S3_SECRET_KEY", "minioadmin"),
        path_style=True,
    )
    asyncio.run(s3.ensure_bucket())
    return s3


@pytest.mark.parametrize("size", [0, 1, 10 * 1024 * 1024])
@sync
async def test_put_then_open_returns_identical_bytes(store, size):
    data = os.urandom(size)
    key = attachment_key("t1", str(uuid.uuid4()))
    await store.put(key, stream(data), size, "application/octet-stream")
    assert await collect(store, key) == data
    assert await store.exists(key) is True


@sync
async def test_delete_is_idempotent_and_exists_reflects_it(store):
    key = attachment_key("t1", str(uuid.uuid4()))
    assert await store.exists(key) is False
    await store.delete(key)  # never stored: no error
    await store.put(key, stream(b"abc"), 3, "text/plain")
    assert await store.exists(key) is True
    await store.delete(key)
    await store.delete(key)
    assert await store.exists(key) is False


@sync
async def test_open_of_a_missing_key_raises_blob_not_found(store):
    with pytest.raises(BlobNotFound):
        await collect(store, attachment_key("t1", str(uuid.uuid4())))


@sync
async def test_keys_carry_the_tenant_prefix_and_do_not_collide(store):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ka, kb = attachment_key("t1", a), attachment_key("t2", a)
    assert ka == f"attachments/t1/{a}" and kb == f"attachments/t2/{a}"
    await store.put(ka, stream(b"one"), 3, "text/plain")
    await store.put(kb, stream(b"two"), 3, "text/plain")
    assert await collect(store, ka) == b"one" and await collect(store, kb) == b"two"
    await store.delete(ka)
    assert await store.exists(kb) is True
    assert attachment_key("t1", b) != ka


@sync
async def test_put_overwrites_and_rejects_a_size_that_does_not_match(store):
    key = attachment_key("t1", str(uuid.uuid4()))
    await store.put(key, stream(b"first"), 5, "text/plain")
    with pytest.raises(ValueError):
        await store.put(key, stream(b"toolong"), 3, "text/plain")
    assert await collect(store, key) == b"first"  # the failed put left the old bytes alone
    await store.put(key, stream(b"second!"), 7, "text/plain")
    assert await collect(store, key) == b"second!"


@sync
async def test_fs_backend_refuses_keys_that_escape_the_root(tmp_path):
    store = FsBlobStore(tmp_path / "blobs")
    for bad in ("../x", "attachments/../../x", "/etc/passwd", "a//b", ""):
        with pytest.raises(ValueError):
            await store.exists(bad)


def test_blob_store_from_env_selects_the_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("EAP_BLOB_BACKEND", raising=False)
    monkeypatch.setenv("EAP_BLOB_DIR", str(tmp_path))
    assert isinstance(blob_store_from_env(), FsBlobStore)
    monkeypatch.setenv("EAP_BLOB_BACKEND", "nope")
    with pytest.raises(ValueError, match="EAP_BLOB_BACKEND"):
        blob_store_from_env()
    monkeypatch.setenv("EAP_BLOB_BACKEND", "s3")
    for name in ("EAP_BLOB_S3_ENDPOINT", "EAP_BLOB_S3_BUCKET", "EAP_BLOB_S3_ACCESS_KEY", "EAP_BLOB_S3_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="EAP_BLOB_S3_ENDPOINT"):
        blob_store_from_env()
    monkeypatch.setenv("EAP_BLOB_S3_ENDPOINT", "http://127.0.0.1:9")
    monkeypatch.setenv("EAP_BLOB_S3_BUCKET", "b")
    monkeypatch.setenv("EAP_BLOB_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("EAP_BLOB_S3_SECRET_KEY", "s")
    from eap_blob_store.s3 import S3BlobStore

    s3 = blob_store_from_env()
    assert isinstance(s3, S3BlobStore) and s3.region == "us-east-1" and s3.path_style is True
