"""RAG P1.A4: native-format uploads (content_base64, caps, sniffing, canonical MIME types), binary
documents never returned, the reserved `eap` metadata key, new knowledge bases on chunker v2, and
re-extraction (per document and bulk, outdated selection, chunker v2 switch).

The implementer's focused tests; the acceptance tests live in their own file.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text

from .test_runs import SPEC, fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg

API = "/api/v1"
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 17
RST = "Title\n=====\n\nSome text.\n\nSection\n-------\n\nMore text.\n"
YAML = "title: UART\nproperties:\n  reg:\n    maxItems: 1\n"


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


def _kb(client, headers, **chunking):
    body = {"name": f"kb-{uuid.uuid4().hex[:6]}", "embedding_model": "test:hash"}
    if chunking:
        body["chunking"] = chunking
    r = client.post(f"{API}/knowledge-bases", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _put(client, headers, kb_id, key, **body):
    return client.put(f"{API}/knowledge-bases/{kb_id}/documents/{key}", json=body, headers=headers)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _engine(pg):
    return pg[1].state.database_engine


# --------------------------------------------------------------------------- knowledge base create


def test_new_knowledge_base_defaults_to_chunker_v2_and_keeps_explicit_v1(pg, tenant_of, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    assert _kb(client, h)["chunking"] == {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 64, "chunker_version": 2}
    assert _kb(client, h, chunker_version=1)["chunking"] == {
        "max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1,
    }
    small = _kb(client, h, max_tokens=64, overlap_tokens=8)
    assert small["chunking"]["min_tokens"] == 63
    with_extract = _kb(client, h, extract={"markdown_flavor": "doxygen", "rst_config": {"CONFIG_part_variant": "AM62X"}})
    assert with_extract["chunking"]["extract"] == {"rst_config": {"CONFIG_part_variant": "AM62X"}, "markdown_flavor": "doxygen"}
    for bad in ({"max_tokens": "512"}, {"max_tokens": True}, {"overlap_tokens": 400}, {"chunker_version": 3}, {"x": 1}):
        r = client.post(f"{API}/knowledge-bases", json={"name": "bad", "embedding_model": "test:hash", "chunking": bad}, headers=h)
        assert r.status_code == 422, (bad, r.text)


# ----------------------------------------------------------------------------------------- uploads


def test_each_format_uploads_with_its_canonical_type_and_the_run_input_shape(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    cases = [
        ("guide.md", {"content": "# Guide\n\nText.\n", "mime_type": "text/x-markdown"}, "text/markdown"),
        ("a.rst", {"content": RST, "mime_type": "text/prs.fallenstein.rst"}, "text/x-rst"),
        ("b.yaml", {"content": YAML, "mime_type": "text/yaml; charset=utf-8"}, "application/yaml"),
        ("c.json", {"content": '{"title": "x", "a": 1}', "mime_type": "application/json"}, "application/json"),
        ("d.pdf", {"content_base64": _b64(PDF), "mime_type": "Application/PDF"}, "application/pdf"),
    ]
    for key, body, canonical in cases:
        r = _put(client, h, kb["id"], key, **body)
        assert r.status_code == 202, (key, r.text)
        out = r.json()
        assert out["run_id"] and out["document"]["mime_type"] == canonical
        run = client.get(f"{API}/runs/{out['run_id']}", headers=h).json()
        assert json.loads(run["input"]) == {
            "document_id": out["document"]["id"], "doc_version": 1, "embed_split": True,
        }
    assert len(fake_dbos.calls) == len(cases)
    # Worker formats are not extracted by the API: rst that docutils would reject still uploads.
    assert _put(client, h, kb["id"], "weird.rst", content=".. include:: /etc/passwd\n", mime_type="text/x-rst").status_code == 202


def test_pdf_is_stored_as_base64_hashed_by_its_bytes_and_never_returned(pg, tenant_of, fake_dbos, kb_cleanup):
    client, app = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    first = _put(client, h, kb["id"], "m.pdf", content_base64=_b64(PDF), mime_type="application/pdf")
    assert first.status_code == 202
    doc = first.json()["document"]
    assert doc["content_hash"] == hashlib.sha256(PDF).hexdigest()
    assert doc["content_encoding"] == "base64" and doc["content"] is None
    again = _put(client, h, kb["id"], "m.pdf", content_base64=_b64(PDF), mime_type="application/pdf")
    assert again.status_code == 200 and again.json()["unchanged"] is True and again.json()["run_id"] is None
    got = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/m.pdf?include_content=true", headers=h).json()
    assert got["content"] is None and got["content_encoding"] == "base64"
    listed = client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"]
    assert listed[0]["content"] is None and listed[0]["content_encoding"] == "base64"
    with _engine(pg).connect() as conn:
        stored = conn.execute(text("SELECT content FROM kb_documents WHERE id = :id"), {"id": doc["id"]}).scalar()
    assert base64.b64decode(stored) == PDF
    # A text document still returns its content.
    _put(client, h, kb["id"], "t.txt", content="hello", mime_type="text/plain")
    text_doc = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/t.txt?include_content=true", headers=h).json()
    assert text_doc["content"] == "hello" and text_doc["content_encoding"] is None
    assert len(fake_dbos.calls) == 2


def test_content_shape_and_sniffing(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)

    def put(**body):
        return _put(client, h, kb["id"], "x", **body)

    both = put(content="a", content_base64=_b64(b"a"), mime_type="text/plain")
    assert both.status_code == 422 and "exactly one of content and content_base64" in json.dumps(both.json())
    assert put(mime_type="text/plain").status_code == 422
    b64_text = put(content_base64=_b64(b"plain words"), mime_type="application/yaml")
    assert b64_text.status_code == 422 and "not content_base64" in b64_text.json()["detail"]
    png_as_yaml = put(content_base64=_b64(PNG), mime_type="application/yaml")
    assert png_as_yaml.status_code == 415
    assert png_as_yaml.json()["code"] == "unsupported_content" and png_as_yaml.json()["detected_type"] == "image/png"
    assert "image/png, not application/yaml" in png_as_yaml.json()["detail"]
    pdf_as_rst = put(content="%PDF-1.7 not really rst", mime_type="text/x-rst")
    assert pdf_as_rst.status_code == 415 and pdf_as_rst.json()["detected_type"] == "application/pdf"
    latin1_png = put(content="\x89PNG\r\n\x1a\nrest", mime_type="text/plain")
    assert latin1_png.status_code == 415 and latin1_png.json()["detected_type"] == "image/png"
    not_pdf = put(content_base64=_b64(b"<html>hello</html>"), mime_type="application/pdf")
    assert not_pdf.status_code == 415 and not_pdf.json()["detected_type"] is None
    assert "%PDF-" in not_pdf.json()["detail"]
    png_as_pdf = put(content_base64=_b64(PNG), mime_type="application/pdf")
    assert png_as_pdf.status_code == 415 and png_as_pdf.json()["detected_type"] == "image/png"
    late_magic = put(content_base64=_b64(b"x" * 2000 + PDF), mime_type="application/pdf")
    assert late_magic.status_code == 415
    pdf_as_text = put(content="%PDF-1.7", mime_type="application/pdf")
    assert pdf_as_text.status_code == 422 and "unsupported document mime type" in pdf_as_text.json()["detail"]
    assert put(content_base64="not base64!", mime_type="application/pdf").status_code == 422
    assert put(content_base64="", mime_type="application/pdf").status_code == 422
    assert put(content="x", mime_type="application/msword").status_code == 422
    assert fake_dbos.calls == []
    # Junk before the header within the first 1,024 bytes is a PDF.
    assert put(content_base64=_b64(b"\n" * 1000 + PDF), mime_type="application/pdf").status_code == 202


def test_per_format_caps_and_the_guard_formula(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    from eap_api.routes.knowledge import upload_guard_bytes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    assert upload_guard_bytes() == max(5 * 1024 * 1024, math.ceil(25 * 1024 * 1024 * 4 / 3) + 64 * 1024)
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", "1000")
    monkeypatch.setenv("EAP_KB_MAX_DOCUMENT_BYTES", "4000")
    assert upload_guard_bytes() == max(4000, math.ceil(1000 * 4 / 3) + 64 * 1024)
    at_cap = PDF + b" " * (1000 - len(PDF))
    assert _put(client, h, kb["id"], "a.pdf", content_base64=_b64(at_cap), mime_type="application/pdf").status_code == 202
    over = _put(client, h, kb["id"], "b.pdf", content_base64=_b64(at_cap + b" "), mime_type="application/pdf")
    assert over.status_code == 413 and "max 1000 bytes for application/pdf" in over.json()["detail"]
    # A PDF body far over the text cap is fine (the guard is the binary one).
    monkeypatch.setenv("EAP_KB_MAX_BINARY_BYTES", "20000")
    big = PDF + b" " * 15000
    assert _put(client, h, kb["id"], "c.pdf", content_base64=_b64(big), mime_type="application/pdf").status_code == 202
    # Text keeps its body cap and message; YAML/JSON have their own decoded cap.
    text_over = _put(client, h, kb["id"], "t.txt", content="x" * 5000, mime_type="text/plain")
    assert text_over.status_code == 413 and text_over.json()["detail"] == "request body too large (max 4000 bytes)"
    monkeypatch.setenv("EAP_KB_MAX_STRUCTURED_BYTES", "100")
    y_ok = _put(client, h, kb["id"], "ok.yaml", content="a: " + "b" * 97, mime_type="application/yaml")
    assert y_ok.status_code == 202, y_ok.text
    y_over = _put(client, h, kb["id"], "over.yaml", content="a: " + "b" * 98, mime_type="application/yaml")
    assert y_over.status_code == 413 and "max 100 bytes for application/yaml" in y_over.json()["detail"]
    assert len(fake_dbos.calls) == 3  # a.pdf, c.pdf, ok.yaml


def test_reserved_eap_key_is_refused_on_upload_and_on_agent_save(pg, tenant_of, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    r = _put(client, h, kb["id"], "a.md", content="# A\n\nb\n", mime_type="text/markdown", metadata={"eap": "x"})
    assert r.status_code == 422 and "reserved" in json.dumps(r.json())
    spec = {**SPEC, "knowledge_bases": [{"id": kb["id"], "filter": {"eap": "x"}}]}
    created = client.post(f"{API}/agents", json={"spec": spec}, headers=h)
    assert created.status_code == 422 and "reserved" in json.dumps(created.json())


def test_the_api_process_imports_no_parser():
    code = (
        "import sys, eap_api.app, eap_api.routes.knowledge\n"
        "bad = [m for m in ('docutils', 'pypdf', 'pypdfium2', 'PIL', 'eap_knowledge.sandbox',"
        " 'eap_knowledge.formats.rst', 'eap_knowledge.formats.structured', 'eap_knowledge.formats.doxygen',"
        " 'eap_knowledge.formats.pdf') if m in sys.modules]\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_document_error_codes_are_mapped():
    from eap_api.routes.knowledge import document_error_codes

    assert document_error_codes("extraction_failed: The PDF is encrypted with a password.") == (
        "extraction_failed", "document_encrypted",
    )
    assert document_error_codes("extraction_failed: the document has no extractable text") == ("extraction_failed", None)
    assert document_error_codes("embedding_failed: 3 of 9 sections embedded") == ("embedding_failed", None)
    assert document_error_codes("something else") == (None, None)
    assert document_error_codes(None) == (None, None)


# ------------------------------------------------------------------------------------- re-extract


def _activate(pg, doc_id: str, extractor: str | None, version: int = 1) -> None:
    """What a finished worker ingest leaves: the latest version active and ready, with one chunk
    whose metadata records the extractor (no record for the API path)."""
    meta = {} if extractor is None else {"eap": {"extractor": extractor, "extractor_version": version}}
    with _engine(pg).begin() as conn:
        row = conn.execute(
            text("UPDATE kb_documents SET active_version = latest_version, status = 'ready'"
                 " WHERE id = :id RETURNING tenant_id, kb_id, latest_version"),
            {"id": doc_id},
        ).mappings().one()
        gen = conn.execute(text("SELECT active_generation FROM knowledge_bases WHERE id = :kb"), {"kb": row["kb_id"]}).scalar()
        conn.execute(
            text("INSERT INTO kb_chunks (id, tenant_id, kb_id, document_id, doc_version, generation, ordinal,"
                 " heading_path, title, content, embed_text, embed_text_hash, approx_tokens, metadata)"
                 " VALUES (:cid, :t, :kb, :d, :v, :g, 0, '{}', 't', 'c', 'c', 'h', 1, CAST(:m AS jsonb))"),
            {"cid": str(uuid.uuid4()), "t": row["tenant_id"], "kb": row["kb_id"], "d": doc_id,
             "v": row["latest_version"], "g": gen, "m": json.dumps(meta)},
        )


def _kb_runs(pg, kb_id: str) -> int:
    with _engine(pg).connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM runs WHERE kind = 'kb_ingest' AND knowledge_base_id = CAST(:k AS uuid)"),
            {"k": kb_id},
        ).scalar_one()


def test_reextract_document_makes_a_new_version_and_run_and_replays(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    kb = _kb(client, h)
    doc = _put(client, h, kb["id"], "a.rst", content=RST, mime_type="text/x-rst").json()["document"]
    url = f"{API}/knowledge-bases/{kb['id']}/documents/a.rst/reextract"
    run_id = str(uuid.uuid4())
    r = client.post(url, json={"run_id": run_id}, headers=h)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["run_id"] == run_id and out["document"]["latest_version"] == 2
    assert out["document"]["content_hash"] == doc["content_hash"] and out["document"]["last_run_id"] == run_id
    run = client.get(f"{API}/runs/{run_id}", headers=h).json()
    assert json.loads(run["input"]) == {"document_id": doc["id"], "doc_version": 2, "embed_split": True}
    runs_before, dispatches_before = _kb_runs(pg, kb["id"]), len(fake_dbos.calls)
    replay = client.post(url, json={"run_id": run_id}, headers=h)
    assert replay.status_code == 202 and replay.json()["run_id"] == run_id
    assert replay.json()["document"]["latest_version"] == 2
    assert _kb_runs(pg, kb["id"]) == runs_before and len(fake_dbos.calls) == dispatches_before  # no new run or dispatch
    assert client.post(url, headers=h).json()["document"]["latest_version"] == 3  # no body: a new version
    first_run = _put(client, h, kb["id"], "b.md", content="# B\n\nx\n", mime_type="text/markdown").json()["run_id"]
    assert client.post(url, json={"run_id": first_run}, headers=h).status_code == 409
    # Cross-tenant and missing: 404, nothing written.
    assert client.post(url, headers=other).status_code == 404
    assert client.post(f"{API}/knowledge-bases/{kb['id']}/documents/nope/reextract", headers=h).status_code == 404
    assert client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={}, headers=other).status_code == 404


def test_bulk_reextract_selects_outdated_documents_and_respects_formats(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    import eap_api.routes.knowledge as routes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    docs = {}
    for key, body in {
        "a.rst": {"content": RST, "mime_type": "text/x-rst"},
        "b.yaml": {"content": YAML, "mime_type": "application/yaml"},
        "c.md": {"content": "# C\n\nx\n", "mime_type": "text/markdown"},
        "d.json": {"content": '{"a": 1}', "mime_type": "application/json"},
    }.items():
        docs[key] = _put(client, h, kb["id"], key, **body).json()["document"]
    _activate(pg, docs["a.rst"]["id"], "rst")
    _activate(pg, docs["b.yaml"]["id"], "yaml")
    _activate(pg, docs["c.md"]["id"], None)
    # d.json never finished an ingest: no active version, so it is outdated.
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    calls = len(fake_dbos.calls)

    dry = client.post(url, json={"dry_run": True}, headers=h)
    assert dry.status_code == 200 and dry.json()["dry_run"] is True
    assert [d["document_key"] for d in dry.json()["documents"]] == ["d.json"]
    monkeypatch.setitem(routes.EXTRACTOR_VERSIONS, "yaml", 2)
    dry = client.post(url, json={"dry_run": True}, headers=h).json()
    assert [d["document_key"] for d in dry["documents"]] == ["b.yaml", "d.json"]
    only_rst = client.post(url, json={"formats": ["rst"]}, headers=h)
    assert only_rst.status_code == 200 and only_rst.json()["selected"] == 0
    assert client.post(url, json={"formats": ["docx"]}, headers=h).status_code == 422
    assert len(fake_dbos.calls) == calls

    started = client.post(url, json={"formats": ["yaml"]}, headers=h)
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["selected"] == body["started"] == 1 and body["failed"] == 0
    ref = body["documents"][0]
    assert ref["document_key"] == "b.yaml" and ref["doc_version"] == 2 and ref["status"] == "queued"
    run = client.get(f"{API}/runs/{ref['run_id']}", headers=h).json()
    assert json.loads(run["input"]) == {"document_id": docs["b.yaml"]["id"], "doc_version": 2, "embed_split": True}
    assert len(fake_dbos.calls) == calls + 1
    # API-extracted documents are re-extracted only with only_outdated false.
    md = client.post(url, json={"formats": ["markdown"], "only_outdated": False}, headers=h).json()
    assert [d["document_key"] for d in md["documents"]] == ["c.md"] and md["started"] == 1


def test_chunker_v2_rewrites_the_config_and_reextracts_every_document(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h, chunker_version=1)
    for key in ("a.md", "b.md", "c.txt"):
        mime = "text/plain" if key.endswith(".txt") else "text/markdown"
        assert _put(client, h, kb["id"], key, content="# T\n\nbody\n", mime_type=mime).status_code == 202
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    assert client.post(url, json={"chunker_version": 2, "formats": ["markdown"]}, headers=h).status_code == 422
    assert client.post(url, json={"chunker_version": 1}, headers=h).status_code == 422
    calls = len(fake_dbos.calls)
    r = client.post(url, json={"chunker_version": 2}, headers=h)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["chunker_version"] == 2 and out["chunker_version_changed"] is True
    assert out["selected"] == out["started"] == 3
    assert out["knowledge_base"]["chunking"] == {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 2}
    assert len(fake_dbos.calls) == calls + 3
    docs = client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"]
    assert {d["latest_version"] for d in docs} == {2}
    assert {d["last_run_id"] for d in docs} == {d["run_id"] for d in out["documents"]}
    # Already on v2: nothing changes, and only outdated documents would be selected (none here).
    again = client.post(url, json={"chunker_version": 2}, headers=h)
    assert again.status_code == 200 and again.json()["chunker_version_changed"] is False and again.json()["selected"] == 0


def test_bulk_reextract_reports_a_failed_enqueue_and_goes_on(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    for key in ("a.rst", "b.rst"):
        _put(client, h, kb["id"], key, content=RST, mime_type="text/x-rst")
    fake_dbos.fail_times = 1
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={}, headers=h)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["selected"] == 2 and out["started"] == 1 and out["failed"] == 1
    failed = next(d for d in out["documents"] if d["status"] == "failed")
    assert "reingest" in failed["detail"]
    # The failed one is retried by a reingest of its document (the recorded run is handed off again).
    retry = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/{failed['document_key']}/reingest", headers=h)
    assert retry.status_code == 202 and retry.json()["run_id"] == failed["run_id"]


def test_bulk_reextract_survives_an_unexpected_error_for_one_document(pg, tenant_of, fake_dbos, kb_cleanup, monkeypatch):
    import eap_api.routes.knowledge as routes

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    for key in ("a.rst", "b.rst", "c.rst"):
        _put(client, h, kb["id"], key, content=RST, mime_type="text/x-rst")
    real, calls = routes._create_ingest_run, []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database went away")  # not a HandOffFailed or HTTPException
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "_create_ingest_run", flaky)
    r = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={}, headers=h)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["selected"] == 3 and out["started"] == 2 and out["failed"] == 1
    failed = next(d for d in out["documents"] if d["status"] == "failed")
    assert "reingest" in failed["detail"]
    monkeypatch.setattr(routes, "_create_ingest_run", real)
    # Its version is stored with no run: after the orphan delay a reingest starts it.
    with _engine(pg).begin() as conn:
        conn.execute(text("UPDATE kb_documents SET updated_at = now() - interval '5 minutes' WHERE id = :id"),
                     {"id": failed["document_id"]})
    retry = client.post(f"{API}/knowledge-bases/{kb['id']}/documents/{failed['document_key']}/reingest", headers=h)
    assert retry.status_code == 202, retry.text
    assert json.loads(client.get(f"{API}/runs/{retry.json()['run_id']}", headers=h).json()["input"])["doc_version"] == 2


def test_get_document_shows_its_active_extraction_and_error_codes(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    put = _put(client, h, kb["id"], "m.pdf", content_base64=_b64(PDF), mime_type="application/pdf").json()
    url = f"{API}/knowledge-bases/{kb['id']}/documents/m.pdf"
    assert client.get(url, headers=h).json()["extraction"] is None
    with _engine(pg).begin() as conn:
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                 " VALUES (:t, :r, 900, 'extract:1', 'extracted', CAST(:d AS jsonb), now())"),
            {"t": tenant, "r": put["run_id"], "d": json.dumps(
                {"extractor": "pdf", "extractor_version": 1, "sections": 4, "chars": 900, "pages": 12,
                 "image_only_pages": [3, 7]})},
        )
    # Not shown until version 1 is active.
    assert client.get(url, headers=h).json()["extraction"] is None
    with _engine(pg).begin() as conn:
        conn.execute(text("UPDATE kb_documents SET active_version = 1, status = 'ready' WHERE id = :id"),
                     {"id": put["document"]["id"]})
    # A newer version that fails (its own extracted event included) does not replace version 1's.
    v2 = client.post(f"{url}/reextract", headers=h).json()
    with _engine(pg).begin() as conn:
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                 " VALUES (:t, :r, 900, 'extract:2', 'extracted', CAST(:d AS jsonb), now() + interval '1 second')"),
            {"t": tenant, "r": v2["run_id"], "d": json.dumps(
                {"extractor": "pdf", "extractor_version": 1, "sections": 1, "chars": 5, "pages": 99})},
        )
        conn.execute(
            text("UPDATE kb_documents SET status = 'failed', error = :e WHERE id = :id"),
            {"e": "extraction_failed: The PDF is encrypted with a password.", "id": put["document"]["id"]},
        )
    got = client.get(url, headers=h).json()
    assert got["extraction"] == {
        "extractor": "pdf", "extractor_version": 1, "current_version": 1, "doc_version": 1,
        "run_id": put["run_id"], "sections": 4, "chars": 900, "pages": 12, "image_only_pages": [3, 7],
        "conditions": None, "warnings": None,
    }
    assert got["error_code"] == "extraction_failed" and got["error_reason"] == "document_encrypted"
    # The list stays lean: no extraction there.
    assert client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=h).json()["items"][0]["extraction"] is None
