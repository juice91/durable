"""RAG P1 part 1 branch-review fixes: a text upload is refused at the text cap before it is buffered
to the binary guard (MINOR 1), Markdown left with Doxygen chunks is outdated once the knowledge base
is back on plain Markdown (MINOR 3), and repeating a bulk "re-extract outdated" while the first
call's runs are in flight starts nothing (MINOR 4)."""

from __future__ import annotations

import asyncio

from starlette.requests import Request

from eap_api.request_guard import read_capped_body

from .test_runs import deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures
from .run_helpers import requires_pg
from .test_p1a4_upload_reextract import (  # noqa: F401 - fixtures
    PDF,
    _activate,
    _b64,
    _knowledge_env,
    _kb,
    _put,
    kb_cleanup,
)

API = "/api/v1"


# ------------------------------------------------------------------------------ read_capped_body


def _streamed(chunks: list[bytes], content_length: int | None = None) -> tuple[Request, list[int]]:
    """A request whose body arrives as `chunks`, and the list of chunk indexes the reader pulled."""
    pulled: list[int] = []

    async def receive():
        i = len(pulled)
        pulled.append(i)
        return {"type": "http.request", "body": chunks[i], "more_body": i + 1 < len(chunks)}

    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return Request({"type": "http", "method": "PUT", "headers": headers}, receive), pulled


def _read(request: Request, limit: int, soft: int | None = None, allow=None):
    return asyncio.run(read_capped_body(request, limit, soft_limit=soft, allow_beyond_soft=allow))


def test_a_body_past_the_soft_cap_stops_streaming_when_the_predicate_refuses():
    chunks = [b"a" * 10] * 10
    request, pulled = _streamed(chunks)
    seen: list[bytes] = []
    assert _read(request, 1000, soft=25, allow=lambda p: seen.append(p) or False) == 25
    assert pulled == [0, 1, 2] and seen == [b"a" * 30]  # checked once, at the first chunk past the cap


def test_a_body_the_predicate_accepts_streams_to_the_hard_cap():
    chunks = [b'{"content_base64":"'] + [b"A" * 10] * 10
    request, pulled = _streamed(chunks)
    calls: list[int] = []
    body = _read(request, 1000, soft=25, allow=lambda p: calls.append(len(p)) or b'"content_base64"' in p)
    assert body == b"".join(chunks) and len(pulled) == len(chunks) and len(calls) == 1
    # ...and the hard cap still holds.
    request, _ = _streamed(chunks)
    assert _read(request, 50, soft=25, allow=lambda p: True) is None


def test_without_a_soft_cap_the_reader_is_unchanged():
    request, _ = _streamed([b"x" * 10, b"y" * 10])
    assert _read(request, 20) == b"x" * 10 + b"y" * 10
    request, _ = _streamed([b"x" * 10, b"y" * 11])
    assert _read(request, 20) is None
    request, pulled = _streamed([b"x"], content_length=21)
    assert _read(request, 20) is None and pulled == []  # the declared length is refused unread


# ------------------------------------------------------------------------------------ upload guard


@requires_pg
def test_a_text_body_over_the_text_cap_is_refused_with_the_text_cap(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "4000")
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", "20000")  # the guard is ~92 KiB
    for mime in ("text/markdown", "text/plain", "application/yaml"):
        over = _put(client, h, kb["id"], "big.txt", content="x" * 30000, mime_type=mime)
        assert over.status_code == 413, (mime, over.text)
        assert over.json()["detail"] == "request body too large (max 4000 bytes)"
    # A base64 body far over the text cap still goes through to the binary cap.
    big = _put(client, h, kb["id"], "big.pdf", content_base64=_b64(PDF + b" " * 15000), mime_type="application/pdf")
    assert big.status_code == 202, big.text
    # Base64 for a text type is refused as before (bounded by the binary guard).
    wrong = _put(client, h, kb["id"], "b64.md", content_base64=_b64(b"# x\n" * 3000), mime_type="text/markdown")
    assert wrong.status_code == 413 and wrong.json()["detail"] == "request body too large (max 4000 bytes)"


# ------------------------------------------------------------------------------- bulk re-extract


@requires_pg
def test_markdown_left_with_doxygen_chunks_is_outdated_on_a_plain_markdown_kb(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    docs = {}
    for key in ("doxy.md", "plain.md"):
        docs[key] = _put(client, h, kb["id"], key, content="# T\n\nbody\n", mime_type="text/markdown").json()["document"]
    _activate(pg, docs["doxy.md"]["id"], "markdown-doxygen")  # extracted while the flavour was doxygen
    _activate(pg, docs["plain.md"]["id"], None)  # API path: no record
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    dry = client.post(url, json={"dry_run": True}, headers=h)
    assert dry.status_code == 200, dry.text
    assert [d["document_key"] for d in dry.json()["documents"]] == ["doxy.md"]
    assert [d["document_key"] for d in client.post(url, json={"dry_run": True, "formats": ["markdown"]}, headers=h)
            .json()["documents"]] == ["doxy.md"]
    assert client.post(url, json={"dry_run": True, "formats": ["rst"]}, headers=h).json()["documents"] == []


@requires_pg
def test_a_repeat_bulk_reextract_while_the_first_is_in_flight_starts_nothing(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    import eap_api.routes.knowledge as routes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    doc = _put(client, h, kb["id"], "a.yaml", content="title: UART\n", mime_type="application/yaml").json()["document"]
    _activate(pg, doc["id"], "yaml")
    monkeypatch.setitem(routes.EXTRACTOR_VERSIONS, "yaml", 9)
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    first = client.post(url, json={}, headers=h)
    assert first.status_code == 202 and first.json()["started"] == 1, first.text
    calls = len(fake_dbos.calls)
    again = client.post(url, json={}, headers=h)  # the first call's run is still queued (fake DBOS)
    assert again.status_code == 200 and again.json()["selected"] == 0 and again.json()["started"] == 0, again.text
    assert len(fake_dbos.calls) == calls
    got = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/a.yaml", headers=h).json()
    assert got["latest_version"] == 2 and got["active_version"] == 1 and got["status"] == "pending"
