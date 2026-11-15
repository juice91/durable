"""RAG P1.A4 acceptance: cross-tenant 404 on every new route, every MIME alias, the exact edges of
every cap (decoded and encoded sizes, the body guard), magic-byte sniffing edge cases, the reserved
`eap` key on both write paths, re-extract idempotency under concurrent submits, dry runs, the atomic
chunker v2 switch, and old documents and knowledge bases unchanged."""

from __future__ import annotations

import base64
import json
import math
import threading
import uuid

import pytest
from sqlalchemy import text

from .test_runs import SPEC, fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures
from .run_helpers import requires_pg
from .test_p1a4_upload_reextract import PDF, PNG, RST, YAML, _activate, _b64, _engine, _kb, _put

pytestmark = requires_pg

API = "/api/v1"


@pytest.fixture(autouse=True)
def _knowledge_env(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    for name in ("EAP_KB_MAX_DOCUMENT_BYTES", "EAP_KB_MAX_BINARY_BYTES", "EAP_KB_MAX_STRUCTURED_BYTES"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def kb_cleanup(pg):
    yield
    _client, app = pg
    with app.state.database_engine.begin() as conn:
        for t in app.state.test_tenants:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})


def _raw_put(client, headers, kb_id, key, body: dict, pad: int = 0):
    """PUT the exact JSON bytes (compact, plus `pad` trailing spaces) so the body size is known."""
    data = json.dumps(body, separators=(",", ":")).encode("utf-8") + b" " * pad
    return client.put(
        f"{API}/knowledge-bases/{kb_id}/documents/{key}", content=data,
        headers={**headers, "content-type": "application/json"},
    ), len(data)


# ------------------------------------------------------------------------------- cross tenant


def test_every_new_route_is_404_across_tenants_and_writes_nothing(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    _put(client, h, kb["id"], "a.rst", content=RST, mime_type="text/x-rst")
    _put(client, h, kb["id"], "p.pdf", content_base64=_b64(PDF), mime_type="application/pdf")
    base = f"{API}/knowledge-bases/{kb['id']}"
    calls = len(fake_dbos.calls)
    probes = [
        ("PUT", f"{base}/documents/n.pdf", {"content_base64": _b64(PDF), "mime_type": "application/pdf"}),
        ("PUT", f"{base}/documents/n.rst", {"content": RST, "mime_type": "text/x-rst"}),
        ("GET", f"{base}/documents/p.pdf", None),
        ("GET", f"{base}/documents/p.pdf?include_content=true", None),
        ("POST", f"{base}/documents/a.rst/reextract", {}),
        ("POST", f"{base}/documents/p.pdf/reextract", {"run_id": str(uuid.uuid4())}),
        ("POST", f"{base}/reextract", {}),
        ("POST", f"{base}/reextract", {"dry_run": True}),
        ("POST", f"{base}/reextract", {"chunker_version": 2}),
    ]
    for method, url, body in probes:
        r = client.request(method, url, json=body, headers=other)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    assert len(fake_dbos.calls) == calls
    docs = client.get(f"{base}/documents", headers=h).json()["items"]
    assert {d["document_key"]: d["latest_version"] for d in docs} == {"a.rst": 1, "p.pdf": 1}
    assert client.get(f"{base}", headers=h).json()["chunking"]["chunker_version"] == 1
    # An unknown knowledge base is the same 404 for the owner.
    ghost = f"{API}/knowledge-bases/{uuid.uuid4()}"
    assert client.post(f"{ghost}/reextract", json={}, headers=h).status_code == 404
    assert client.post(f"{ghost}/documents/a.rst/reextract", headers=h).status_code == 404


def test_foreign_run_id_on_reextract_is_409_and_creates_nothing(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    mine = _kb(client, h)
    theirs = _kb(client, other)
    _put(client, h, mine["id"], "a.rst", content=RST, mime_type="text/x-rst")
    foreign_run = _put(client, other, theirs["id"], "z.rst", content=RST, mime_type="text/x-rst").json()["run_id"]
    url = f"{API}/knowledge-bases/{mine['id']}/documents/a.rst/reextract"
    assert client.post(url, json={"run_id": foreign_run}, headers=h).status_code == 409
    doc = client.get(f"{API}/knowledge-bases/{mine['id']}/documents/a.rst", headers=h).json()
    assert doc["latest_version"] == 1
    # The other tenant's run is untouched.
    assert client.get(f"{API}/runs/{foreign_run}", headers=other).status_code == 200


# ------------------------------------------------------------------------------- MIME aliases

ALIASES = [
    ("text/markdown", "text/markdown"), ("text/x-markdown", "text/markdown"), ("text/md", "text/markdown"),
    ("text/html", "text/html"), ("application/xhtml+xml", "text/html"),
    ("text/plain", "text/plain"),
    ("text/x-rst", "text/x-rst"), ("text/prs.fallenstein.rst", "text/x-rst"),
    ("application/yaml", "application/yaml"), ("application/x-yaml", "application/yaml"), ("text/yaml", "application/yaml"),
    ("application/json", "application/json"),
    ("application/pdf", "application/pdf"),
]
BODIES = {
    "text/markdown": "# T\n\nbody\n", "text/html": "<html><body><p>hello world</p></body></html>",
    "text/plain": "hello world", "text/x-rst": RST, "application/yaml": YAML,
    "application/json": '{"title": "x", "a": 1}',
}


@pytest.mark.parametrize("alias,canonical", ALIASES)
def test_each_mime_alias_is_stored_as_the_canonical_type(pg, tenant_of, fake_dbos, kb_cleanup, alias, canonical):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    for variant in (alias, alias.upper(), f"{alias}; charset=utf-8", f"  {alias} ;q=1"):
        body = {"content_base64": _b64(PDF)} if canonical == "application/pdf" else {"content": BODIES[canonical]}
        r = _put(client, h, kb["id"], "doc", mime_type=variant, **body)
        assert r.status_code in (200, 202), (variant, r.text)
        assert r.json()["document"]["mime_type"] == canonical, variant
    got = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/doc", headers=h).json()
    assert got["mime_type"] == canonical
    assert got["latest_version"] == 1  # the same bytes under four spellings: one version


# ------------------------------------------------------------------------------------- caps


@pytest.mark.parametrize("cap", [999, 1000, 1001])  # every base64 padding class (0, 1 or 2 "=")
def test_binary_cap_is_exact_on_decoded_bytes_not_the_encoded_text(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch, cap):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", str(cap))
    at = PDF + b" " * (cap - len(PDF))
    ok = _put(client, h, kb["id"], "at.pdf", content_base64=_b64(at), mime_type="application/pdf")
    assert ok.status_code == 202, ok.text
    assert len(_b64(at)) > cap  # the encoded text is longer than the cap and still fits
    over = _put(client, h, kb["id"], "over.pdf", content_base64=_b64(at + b" "), mime_type="application/pdf")
    assert over.status_code == 413
    assert over.json()["detail"] == f"the document is too large ({cap + 1} bytes; max {cap} bytes for application/pdf)"
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/over.pdf", headers=h).status_code == 404


def test_the_guard_is_exact_and_a_body_over_it_never_reaches_the_per_type_check(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    from eap_api.routes.knowledge import upload_guard_bytes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", "1000")
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "4000")
    guard = upload_guard_bytes()
    assert guard == math.ceil(1000 * 4 / 3) + 64 * 1024 > 4000
    body = {"content_base64": _b64(PDF), "mime_type": "application/pdf"}
    size = len(json.dumps(body, separators=(",", ":")))
    at, n = _raw_put(client, h, kb["id"], "g.pdf", body, pad=guard - size)
    assert n == guard and at.status_code == 202, at.text
    over, n = _raw_put(client, h, kb["id"], "g2.pdf", {**body, "document_key": "g2.pdf"}, pad=guard - size - len('"document_key":"g2.pdf",') + 1)
    assert n == guard + 1
    assert over.status_code == 413 and over.json()["detail"] == f"request body too large (max {guard} bytes)"
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/g2.pdf", headers=h).status_code == 404


def test_text_cap_applies_to_the_request_body_exactly(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "4000")
    body = {"content": "hello", "mime_type": "text/plain"}
    size = len(json.dumps(body, separators=(",", ":")))
    at, n = _raw_put(client, h, kb["id"], "a.txt", body, pad=4000 - size)
    assert n == 4000 and at.status_code == 202, at.text
    over, n = _raw_put(client, h, kb["id"], "b.txt", body, pad=4001 - size)
    assert n == 4001 and over.status_code == 413
    assert over.json()["detail"] == "request body too large (max 4000 bytes)"
    # PDFs are not held to the text cap: a body far over it is fine up to the binary cap.
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", "20000")
    big = PDF + b" " * 10000
    assert _put(client, h, kb["id"], "c.pdf", content_base64=_b64(big), mime_type="application/pdf").status_code == 202


def test_structured_cap_counts_utf8_bytes_and_never_exceeds_the_text_cap(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    monkeypatch.setenv("EAP_KB_MAX_STRUCTURED_BYTES", "100")
    prefix = "a: "
    ok = prefix + "é" * 48  # 3 + 96 bytes: one below the cap
    assert len(ok.encode()) == 99
    assert _put(client, h, kb["id"], "ok.yaml", content=ok, mime_type="application/yaml").status_code == 202
    at = prefix + "é" * 48 + "x"  # exactly 100 bytes
    assert len(at.encode()) == 100
    assert _put(client, h, kb["id"], "at.yaml", content=at, mime_type="application/yaml").status_code == 202
    over = prefix + "é" * 49  # 101 bytes, but only 52 characters
    assert len(over) == 52 and len(over.encode()) == 101
    r = _put(client, h, kb["id"], "over.json", content=over, mime_type="application/json")
    assert r.status_code == 413
    assert r.json()["detail"] == "the document is too large (101 bytes; max 100 bytes for application/json)"
    # Markdown, HTML and text are not subject to the structured cap.
    assert _put(client, h, kb["id"], "m.md", content="# T\n\n" + "x" * 500, mime_type="text/markdown").status_code == 202
    # With the structured cap above the text cap, the text cap (the request body) still governs.
    monkeypatch.setenv("EAP_KB_MAX_STRUCTURED_BYTES", "10000")
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "400")
    r = _put(client, h, kb["id"], "big.yaml", content="a: " + "b" * 500, mime_type="application/yaml")
    assert r.status_code == 413 and r.json()["detail"] == "request body too large (max 400 bytes)"


# ------------------------------------------------------------------------------------ sniffing


def test_pdf_signature_window_edges(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)

    def pdf_at(offset: int):
        data = b"\n" * offset + PDF
        return _put(client, h, kb["id"], f"o{offset}.pdf", content_base64=_b64(data), mime_type="application/pdf")

    assert pdf_at(0).status_code == 202
    assert pdf_at(1023).status_code == 202  # the signature starts in the last byte of the window
    late = pdf_at(1024)
    assert late.status_code == 415 and late.json()["code"] == "unsupported_content"
    assert late.json()["detected_type"] is None


def test_polyglots_and_signatures_in_the_wrong_place(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    # PDF bytes declared as text formats, as base64 or as text: 415 naming the real type.
    for mime in ("application/yaml", "application/json", "text/x-rst", "text/markdown", "text/plain"):
        r = _put(client, h, kb["id"], "x", content_base64=_b64(PDF + b"\n" + PNG), mime_type=mime)
        assert r.status_code == 415 and r.json()["detected_type"] == "application/pdf", (mime, r.text)
        r = _put(client, h, kb["id"], "x", content="%PDF-1.7\nkey: value\n", mime_type=mime)
        assert r.status_code == 415 and r.json()["detected_type"] == "application/pdf", (mime, r.text)
    # A PNG, a JPEG and a WebP, as base64 declared as YAML, and as text (UTF-8 and Latin-1 spellings).
    jpeg, webp = b"\xff\xd8\xff\xe0" + b"\x00" * 12, b"RIFF\x00\x00\x00\x00WEBPVP8 "
    for data, kind in ((PNG, "image/png"), (jpeg, "image/jpeg"), (webp, "image/webp")):
        r = _put(client, h, kb["id"], "x", content_base64=_b64(data), mime_type="application/yaml")
        assert r.status_code == 415 and r.json()["detected_type"] == kind, (kind, r.text)
        as_text = data.replace(b"\x00", b"0").decode("latin-1")  # JSON text cannot carry a NUL
        r = _put(client, h, kb["id"], "x", content=as_text, mime_type="text/plain")
        assert r.status_code == 415 and r.json()["detected_type"] == kind, (kind, r.text)
    nul = _put(client, h, kb["id"], "x", content=PNG.decode("latin-1"), mime_type="text/plain")
    assert nul.status_code == 422  # a NUL is refused before sniffing
    # A signature that is not at offset 0 is text.
    for lead in (" ", "\n", "# "):
        r = _put(client, h, kb["id"], "x", content=lead + "%PDF-1.7 is the header of a PDF\n", mime_type="text/markdown")
        assert r.status_code == 202, (lead, r.text)
    # A PDF declared as PDF that is really a PNG: 415 detecting the PNG.
    r = _put(client, h, kb["id"], "p.pdf", content_base64=_b64(PNG + b"%PDF-"), mime_type="application/pdf")
    assert r.status_code == 202  # %PDF- inside the window is a PDF by the signature rule (offset-free)
    r = _put(client, h, kb["id"], "q.pdf", content_base64=_b64(PNG), mime_type="application/pdf")
    assert r.status_code == 415 and r.json()["detected_type"] == "image/png"
    assert "image/png, not application/pdf" in r.json()["detail"]


def test_bom_and_utf16_text(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    # A BOM before ordinary text is fine, for every text type.
    for mime, body in (("text/markdown", "# T\n\nbody\n"), ("text/plain", "hello"), ("application/yaml", YAML)):
        r = _put(client, h, kb["id"], "b", content="﻿" + body, mime_type=mime)
        assert r.status_code in (200, 202), (mime, r.text)
    # A BOM before a binary signature hides nothing the API claims to catch: it is text, not 415.
    assert _put(client, h, kb["id"], "b2", content="﻿%PDF-1.7", mime_type="text/plain").status_code == 202
    # A UTF-16 text file sent as base64 for a text type is not a known binary: 422, not 415.
    for codec in ("utf-16", "utf-16-le", "utf-16-be"):
        raw = YAML.encode(codec)
        r = _put(client, h, kb["id"], "u", content_base64=_b64(raw), mime_type="application/yaml")
        assert r.status_code == 422 and "not content_base64" in r.json()["detail"], (codec, r.text)
    # UTF-16 bytes declared as a PDF: 415 (no %PDF- in the window), nothing detected.
    r = _put(client, h, kb["id"], "u.pdf", content_base64=_b64("%PDF-1.7".encode("utf-16")), mime_type="application/pdf")
    assert r.status_code == 415 and r.json()["detected_type"] is None
    # UTF-16 text inside a JSON string is just text.
    assert _put(client, h, kb["id"], "w", content="héllo 世界", mime_type="text/plain").status_code == 202
    assert fake_dbos.calls  # accepted uploads were handed off; none of the 415/422 above added a document
    keys = {d["document_key"] for d in client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"]}
    assert keys == {"b", "b2", "w"}


# -------------------------------------------------------------------------- reserved key rule

AGENT_KB_FILTERS = ({"eap": "x"}, {"eap": ["a", "b"]}, {"ok": 1, "eap": True})


def test_reserved_eap_key_on_upload_and_on_both_agent_write_paths(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    for meta in ({"eap": "x"}, {"eap": 1}, {"eap": {"extractor": "pdf"}}, {"eap": None}, {"ok": "y", "eap": "x"}):
        r = _put(client, h, kb["id"], "a.md", content="# A\n\nb\n", mime_type="text/markdown", metadata=meta)
        assert r.status_code == 422, (meta, r.text)
    # Rejected before anything is written; a similarly named key is fine.
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents/a.md", headers=h).status_code == 404
    assert fake_dbos.calls == []
    ok = _put(client, h, kb["id"], "a.md", content="# A\n\nb\n", mime_type="text/markdown", metadata={"eap_x": "1", "Eap": "2"})
    assert ok.status_code == 202
    # The same on a re-upload of an existing document, and for a PDF.
    again = _put(client, h, kb["id"], "a.md", content="# A\n\nc\n", mime_type="text/markdown", metadata={"eap": "x"})
    assert again.status_code == 422
    pdf = _put(client, h, kb["id"], "p.pdf", content_base64=_b64(PDF), mime_type="application/pdf", metadata={"eap": "x"})
    assert pdf.status_code == 422
    # Agent save: create and patch, for every filter shape.
    created = client.post(f"{API}/agents", json={"spec": SPEC}, headers=h)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    for filt in AGENT_KB_FILTERS:
        spec = {**SPEC, "knowledge_bases": [{"id": kb["id"], "filter": filt}]}
        assert client.post(f"{API}/agents", json={"spec": spec}, headers=h).status_code == 422, filt
        patched = client.patch(f"{API}/agents/{agent_id}", json={"spec": spec}, headers=h)
        assert patched.status_code == 422 and "reserved" in json.dumps(patched.json()), filt
    good = {**SPEC, "knowledge_bases": [{"id": kb["id"], "filter": {"device": "am62x"}}]}
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": good}, headers=h).status_code == 200
    # The search filter takes the same rule.
    s = client.post(f"{API}/knowledge-bases/{kb['id']}/search", json={"query": "x", "filter": {"eap": "x"}}, headers=h)
    assert s.status_code == 422


# ------------------------------------------------------------- old documents and shapes unchanged

OLD_DOCUMENT_KEYS = {
    "id", "tenant_id", "kb_id", "document_key", "title", "source_url", "mime_type", "metadata", "content_hash",
    "active_version", "latest_version", "status", "error", "chunk_count", "last_run_id", "created_at",
    "updated_at", "content",
}
NEW_DOCUMENT_KEYS = {"content_encoding", "error_code", "error_reason", "extraction"}


def test_text_document_views_only_gain_keys_and_old_values_are_unchanged(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    base = f"{API}/knowledge-bases/{kb['id']}"
    put = _put(client, h, kb["id"], "g.md", content="# G\n\nhello\n", mime_type="text/x-markdown", metadata={"d": "x"})
    assert put.status_code == 202
    assert set(put.json()) >= {"unchanged", "created", "document", "run_id"}
    views = [
        put.json()["document"],
        client.get(f"{base}/documents/g.md", headers=h).json(),
        client.get(f"{base}/documents/g.md?include_content=true", headers=h).json(),
        client.get(f"{base}/documents", headers=h).json()["items"][0],
    ]
    for view in views:
        assert OLD_DOCUMENT_KEYS - {"content"} <= set(view) <= OLD_DOCUMENT_KEYS | NEW_DOCUMENT_KEYS, set(view) ^ OLD_DOCUMENT_KEYS
        assert view["content_encoding"] is None and view["error_code"] is None and view["error_reason"] is None
        assert view["metadata"] == {"d": "x"} and view["status"] == "pending" and view["latest_version"] == 1
        assert view["mime_type"] == "text/markdown"  # an alias is now stored canonical; markdown is its own canonical
    assert views[2]["content"] == "# G\n\nhello\n"
    assert views[1].get("content") is None
    # API-extracted text documents carry no extraction record.
    assert views[1]["extraction"] is None
    # An unchanged re-upload keeps its shape.
    again = _put(client, h, kb["id"], "g.md", content="# G\n\nhello\n", mime_type="text/markdown", metadata={"d": "x"})
    assert again.status_code == 200 and again.json()["unchanged"] is True and again.json()["run_id"] is None
    assert set(again.json()["document"]) <= OLD_DOCUMENT_KEYS | NEW_DOCUMENT_KEYS


def test_a_legacy_knowledge_base_and_run_keep_their_behaviour(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    base = f"{API}/knowledge-bases/{kb['id']}"
    assert kb["chunking"]["chunker_version"] == 1 and kb["chunking"]["min_tokens"] == 40
    doc = _put(client, h, kb["id"], "a.md", content="# A\n\nx\n", mime_type="text/markdown").json()
    # An ingest run created before P1.C5 (no embed_split) is still that version's run.
    with _engine(pg).begin() as conn:
        conn.execute(
            text("UPDATE runs SET input = :i WHERE id = :r"),
            {"i": json.dumps({"document_id": doc["document"]["id"], "doc_version": 1}, sort_keys=True, separators=(",", ":")),
             "r": doc["run_id"]},
        )
    calls = len(fake_dbos.calls)
    replay = client.post(f"{base}/documents/a.md/reingest", json={"run_id": doc["run_id"]}, headers=h)
    assert replay.status_code == 202 and replay.json()["run_id"] == doc["run_id"]
    assert client.get(f"{API}/runs/{doc['run_id']}", headers=h).json()["input"].count("embed_split") == 0
    assert len(fake_dbos.calls) <= calls + 1  # at most the hand-off of that same run
    # Uploading and a non-switching bulk re-extract leave a v1 knowledge base on v1.
    _put(client, h, kb["id"], "b.md", content="# B\n\ny\n", mime_type="text/markdown")
    for body in ({}, {"dry_run": True}, {"only_outdated": False, "formats": ["markdown"]}):
        r = client.post(f"{base}/reextract", json=body, headers=h)
        assert r.status_code in (200, 202), r.text
        assert r.json()["chunker_version"] == 1 and r.json()["chunker_version_changed"] is False
    assert client.get(base, headers=h).json()["chunking"] == kb["chunking"]
    # A knowledge base whose stored chunking predates chunker_version reads as v1 and is not rewritten.
    with _engine(pg).begin() as conn:
        conn.execute(
            text("UPDATE knowledge_bases SET chunking = CAST(:c AS jsonb) WHERE id = :id"),
            {"c": json.dumps({"max_tokens": 512, "overlap_tokens": 64}), "id": kb["id"]},
        )
    r = client.post(f"{base}/reextract", json={"dry_run": True}, headers=h)
    assert r.status_code == 200 and r.json()["chunker_version"] == 1 and r.json()["chunker_version_changed"] is False
    with _engine(pg).connect() as conn:
        stored = conn.execute(text("SELECT chunking FROM knowledge_bases WHERE id = :id"), {"id": kb["id"]}).scalar()
    assert stored == {"max_tokens": 512, "overlap_tokens": 64}


# ------------------------------------------------------------------------------ concurrency


def _race(n, fn):
    """Run fn(i) in n threads released together; returns their results in order."""
    barrier, out, errors = threading.Barrier(n), [None] * n, []

    def run(i):
        try:
            barrier.wait(timeout=10)
            out[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    return out


def _run_count(pg, kb_id) -> int:
    with _engine(pg).connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM runs WHERE kind = 'kb_ingest' AND knowledge_base_id = CAST(:k AS uuid)"), {"k": kb_id}
        ).scalar_one()


def test_concurrent_double_submit_with_one_run_id_makes_one_version_and_one_run(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    _put(client, h, kb["id"], "a.rst", content=RST, mime_type="text/x-rst")
    url = f"{API}/knowledge-bases/{kb['id']}/documents/a.rst/reextract"
    for round_ in range(3):
        run_id = str(uuid.uuid4())
        before = _run_count(pg, kb["id"])
        results = _race(4, lambda i: client.post(url, json={"run_id": run_id}, headers=h))
        # Every duplicate of the request answers its one run: 202, never 409.
        assert all(r.status_code == 202 for r in results), (round_, [r.text for r in results])
        for r in results:
            assert r.json()["run_id"] == run_id
        assert _run_count(pg, kb["id"]) == before + 1  # one run for this run_id, however many submits
        doc = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/a.rst", headers=h).json()
        assert doc["latest_version"] == 2 + round_ and doc["last_run_id"] == run_id
    # The run input names the version it ingests.
    run = client.get(f"{API}/runs/{run_id}", headers=h).json()
    assert json.loads(run["input"])["doc_version"] == 4


def _concurrent_submits(pg, client, h, tenant_of_unused=None):
    kb = _kb(client, h)
    _put(client, h, kb["id"], "a.rst", content=RST, mime_type="text/x-rst")
    url = f"{API}/knowledge-bases/{kb['id']}/documents/a.rst/reextract"
    results = _race(4, lambda i: client.post(url, headers=h))
    assert all(r.status_code in (202, 409) for r in results) and any(r.status_code == 202 for r in results)
    final = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/a.rst", headers=h).json()
    return kb, results, final


def test_a_409_from_concurrent_submits_never_bumps_the_version(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb, results, final = _concurrent_submits(pg, client, h)
    ok = [r for r in results if r.status_code == 202]
    assert final["latest_version"] == 1 + len(ok)  # every 202 bumped once; a 409 started nothing
    assert _run_count(pg, kb["id"]) == 1 + len(ok)


def test_concurrent_submits_never_strand_the_document_without_a_run_for_its_latest_version(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb, results, final = _concurrent_submits(pg, client, h)
    run = client.get(f"{API}/runs/{final['last_run_id']}", headers=h)
    assert run.status_code == 200, "the document's last_run_id names no run: stuck pending until a reingest"
    assert json.loads(run.json()["input"])["doc_version"] == final["latest_version"]


def test_concurrent_bulk_reextracts_leave_every_document_with_a_run_for_its_latest_version(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    for key in ("a.md", "b.md", "c.md"):
        _put(client, h, kb["id"], key, content="# T\n\nbody\n", mime_type="text/markdown")
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    results = _race(3, lambda i: client.post(url, json={"chunker_version": 2}, headers=h))
    assert all(r.status_code in (200, 202) for r in results), [r.text for r in results]
    assert client.get(f"{API}/knowledge-bases/{kb['id']}", headers=h).json()["chunking"]["chunker_version"] == 2
    assert sum(r.json()["chunker_version_changed"] for r in results) == 1  # exactly one request made the switch
    docs = {d["document_key"]: d for d in client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"]}
    refs = [ref for r in results for ref in r.json()["documents"] if ref["status"] != "failed"]
    # Only the request that made the switch bumped the documents: one new version each.
    for r in results:
        if not r.json()["chunker_version_changed"]:
            assert r.status_code == 200 and r.json()["documents"] == [], r.text
    for key, doc in docs.items():
        mine = [ref for ref in refs if ref["document_key"] == key]
        assert doc["latest_version"] == 2 and len(mine) == 1
        latest = next(ref for ref in mine if ref["doc_version"] == doc["latest_version"])
        assert latest["run_id"] == doc["last_run_id"]
        run = client.get(f"{API}/runs/{doc['last_run_id']}", headers=h).json()
        assert json.loads(run["input"])["doc_version"] == doc["latest_version"]


# -------------------------------------------------------------------- dry run and the v2 switch


def _snapshot(pg, client, h, kb_id):
    with _engine(pg).connect() as conn:
        docs = conn.execute(
            text("SELECT document_key, latest_version, active_version, status, error, last_run_id, updated_at"
                 " FROM kb_documents WHERE kb_id = :k ORDER BY document_key"), {"k": kb_id},
        ).all()
        kb = conn.execute(text("SELECT chunking, updated_at, active_generation FROM knowledge_bases WHERE id = :k"), {"k": kb_id}).one()
    return [tuple(d) for d in docs], tuple(kb), _run_count(pg, kb_id)


def test_dry_run_changes_nothing_in_any_mode(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    import eap_api.routes.knowledge as routes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    for key, mime, body in (("a.rst", "text/x-rst", RST), ("b.yaml", "application/yaml", YAML), ("c.md", "text/markdown", "# C\n\nx\n")):
        doc = _put(client, h, kb["id"], key, content=body, mime_type=mime).json()["document"]
        _activate(pg, doc["id"], {"a.rst": "rst", "b.yaml": "yaml", "c.md": None}[key])
    monkeypatch.setitem(routes.EXTRACTOR_VERSIONS, "rst", 9)
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    before, calls = _snapshot(pg, client, h, kb["id"]), len(fake_dbos.calls)
    cases = [
        ({"dry_run": True}, ["a.rst"]),
        ({"dry_run": True, "only_outdated": False}, ["a.rst", "b.yaml", "c.md"]),
        ({"dry_run": True, "only_outdated": False, "formats": ["yaml", "markdown"]}, ["b.yaml", "c.md"]),
        ({"dry_run": True, "chunker_version": 2}, ["a.rst", "b.yaml", "c.md"]),
        ({"dry_run": True, "formats": ["rst"]}, ["a.rst"]),
    ]
    for body, keys in cases:
        r = client.post(url, json=body, headers=h)
        assert r.status_code == 200, (body, r.text)
        out = r.json()
        assert out["dry_run"] is True and out["started"] == 0 and out["failed"] == 0, body
        assert [d["document_key"] for d in out["documents"]] == keys and out["selected"] == len(keys), body
        assert all(d.get("run_id") is None for d in out["documents"]), body
        assert out["chunker_version_changed"] is False
        assert _snapshot(pg, client, h, kb["id"]) == before, body
    assert client.post(url, json={"dry_run": True, "chunker_version": 2}, headers=h).json()["chunker_version"] == 2
    assert len(fake_dbos.calls) == calls
    # A dry run is validated like a real one.
    assert client.post(url, json={"dry_run": True, "formats": ["docx"]}, headers=h).status_code == 422
    assert client.post(url, json={"dry_run": True, "chunker_version": 1}, headers=h).status_code == 422
    assert _snapshot(pg, client, h, kb["id"]) == before


def test_chunker_v2_switch_is_all_or_nothing(pg, tenant_of, fake_dbos, kb_cleanup):
    from sqlalchemy import event

    client, app = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    for key in ("a.md", "b.md", "c.md"):
        _put(client, h, kb["id"], key, content="# T\n\nbody\n", mime_type="text/markdown")
    before, calls = _snapshot(pg, client, h, kb["id"]), len(fake_dbos.calls)
    seen = {"config": 0, "documents": 0}

    def fail_on_documents(conn, cursor, statement, parameters, context, executemany):
        if "UPDATE knowledge_bases SET chunking" in statement:
            seen["config"] += 1
        if "UPDATE kb_documents SET latest_version" in statement and seen["config"]:
            seen["documents"] += 1
            raise RuntimeError("boom: the bump of the documents fails after the config rewrite")

    engine = app.state.database_engine
    event.listen(engine, "before_cursor_execute", fail_on_documents)
    try:
        try:
            r = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"chunker_version": 2}, headers=h)
            assert r.status_code >= 500
        except RuntimeError:
            pass  # the test client re-raises the server error
    finally:
        event.remove(engine, "before_cursor_execute", fail_on_documents)
    assert seen["config"] == 1 and seen["documents"] == 1  # the config was written, then the bump failed
    after = _snapshot(pg, client, h, kb["id"])
    assert after[1][0] == before[1][0] == {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1}
    assert after == before  # configuration, versions, statuses, runs: all rolled back
    assert len(fake_dbos.calls) == calls
    # The retry then works from a clean state.
    ok = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"chunker_version": 2}, headers=h)
    assert ok.status_code == 202 and ok.json()["started"] == 3 and ok.json()["chunker_version_changed"] is True


def test_v2_switch_with_failed_enqueues_keeps_the_commit_and_lists_the_failures(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    for key in ("a.md", "b.md", "c.md"):
        _put(client, h, kb["id"], key, content="# T\n\nbody\n", mime_type="text/markdown")
    fake_dbos.fail_times = 2
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"chunker_version": 2}, headers=h)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["chunker_version_changed"] is True and out["selected"] == 3 and out["started"] == 1 and out["failed"] == 2
    assert out["knowledge_base"]["chunking"]["chunker_version"] == 2
    assert sorted(d["status"] for d in out["documents"]).count("failed") == 2
    docs = client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"]
    assert {d["latest_version"] for d in docs} == {2} and {d["status"] for d in docs} == {"pending"}
    # Every failed one has its run recorded, so a reingest replays that very run.
    for ref in (d for d in out["documents"] if d["status"] == "failed"):
        again = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/{ref['document_key']}/reingest", headers=h)
        assert again.status_code == 202 and again.json()["run_id"] == ref["run_id"]
    # All failing: nothing started, but the switch happened; 200, not 202.
    kb2 = _kb(client, h, chunker_version=1)
    _put(client, h, kb2["id"], "a.md", content="# T\n\nbody\n", mime_type="text/markdown")
    fake_dbos.fail_times = 5
    none = client.post(f"{API}/knowledge-bases/{kb2['id']}/reextract", json={"chunker_version": 2}, headers=h)
    assert none.status_code == 200 and none.json()["started"] == 0 and none.json()["failed"] == 1
    assert none.json()["chunker_version_changed"] is True
