"""RAG P1 quality fixes, acceptance (API): empty_reason / no_text_layer in the document view, tenant
isolation of these fields, and what bulk re-extract selects."""

from __future__ import annotations

import json

from sqlalchemy import text

from eap_knowledge import EXTRACTOR_VERSIONS
from eap_schemas.knowledge import EXTRACTION_ERROR_MESSAGES

from .run_helpers import requires_pg
from .test_p1a4_upload_reextract import (  # noqa: F401 - fixtures
    PDF,
    RST,
    YAML,
    _activate,
    _b64,
    _engine,
    _kb,
    _knowledge_env,
    _put,
    kb_cleanup,
)
from .test_runs import deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

pytestmark = requires_pg

API = "/api/v1"


def _make_empty(pg, tenant, doc, extractor="rst", version=1, reason="no_extractable_text"):
    """What the worker leaves for an empty version: an `extracted` event, ready, 0 chunks."""
    data = {"extractor": extractor, "extractor_version": version, "sections": 1, "chars": 0}
    if reason:
        data["empty_reason"] = reason
    with _engine(pg).begin() as conn:
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                 " VALUES (:t, :r, 900, 'extract:1', 'extracted', CAST(:d AS jsonb), now())"),
            {"t": tenant, "r": doc["last_run_id"], "d": json.dumps(data)})
        conn.execute(text("UPDATE kb_documents SET active_version = 1, status = 'ready', chunk_count = 0 WHERE id = :id"),
                     {"id": doc["id"]})


def test_empty_reason_is_listed_per_document_and_never_for_a_normal_one(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    empty = _put(client, h, kb["id"], "toc.rst", content=".. toctree::\n\n   a\n", mime_type="text/x-rst").json()["document"]
    full = _put(client, h, kb["id"], "full.rst", content=RST, mime_type="text/x-rst").json()["document"]
    _make_empty(pg, tenant, empty)
    _activate(pg, full["id"], "rst")
    got = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/toc.rst", headers=h).json()
    assert got["extraction"]["empty_reason"] == "no_extractable_text" and got["error_code"] is None
    other = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/full.rst", headers=h).json()
    assert not (other.get("extraction") or {}).get("empty_reason")


def test_empty_reason_and_no_text_layer_are_invisible_to_another_tenant(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, h = tenant_of()
    _, intruder = tenant_of()
    kb = _kb(client, h)
    empty = _put(client, h, kb["id"], "toc.rst", content=".. toctree::\n\n   a\n", mime_type="text/x-rst").json()["document"]
    scan = _put(client, h, kb["id"], "scan.pdf", content_base64=_b64(PDF), mime_type="application/pdf").json()["document"]
    _make_empty(pg, tenant, empty)
    with _engine(pg).begin() as conn:
        conn.execute(text("UPDATE kb_documents SET status = 'failed', error = :e WHERE id = :id"),
                     {"e": "extraction_failed: " + EXTRACTION_ERROR_MESSAGES["no_text_layer"], "id": scan["id"]})
    for key in ("toc.rst", "scan.pdf"):
        r = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/{key}", headers=intruder)
        assert r.status_code == 404 and "empty_reason" not in r.text and "no_text_layer" not in r.text
    r = client.get(f"{API}/knowledge-bases/{kb['id']}/documents", headers=intruder)
    assert r.status_code == 404 or "toc.rst" not in r.text
    mine = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/scan.pdf", headers=h).json()
    assert mine["error_reason"] == "no_text_layer"
    assert mine["error"].endswith(EXTRACTION_ERROR_MESSAGES["no_text_layer"])


def test_a_v1_yaml_document_is_selected_by_bulk_reextract_since_the_version_bump(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    old = _put(client, h, kb["id"], "old.yaml", content=YAML, mime_type="application/yaml").json()["document"]
    new = _put(client, h, kb["id"], "new.yaml", content=YAML, mime_type="application/yaml").json()["document"]
    _activate(pg, old["id"], "yaml", 1)
    _activate(pg, new["id"], "yaml", EXTRACTOR_VERSIONS["yaml"])  # current (3 since the title/preamble fix)
    dry = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"dry_run": True, "formats": ["yaml"]}, headers=h)
    assert dry.status_code == 200, dry.text
    assert [d["document_key"] for d in dry.json()["documents"]] == ["old.yaml"]


def test_failed_image_only_pdf_is_selected_by_bulk_reextract_and_a_text_pdf_is_not(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    scan = _put(client, h, kb["id"], "scan.pdf", content_base64=_b64(PDF), mime_type="application/pdf").json()["document"]
    good = _put(client, h, kb["id"], "good.pdf", content_base64=_b64(PDF), mime_type="application/pdf").json()["document"]
    with _engine(pg).begin() as conn:
        conn.execute(text("UPDATE kb_documents SET status = 'failed', error = 'extraction_failed: x' WHERE id = :id"),
                     {"id": scan["id"]})
    _activate(pg, good["id"], "pdf", 1)
    dry = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"dry_run": True, "formats": ["pdf"]}, headers=h)
    assert [d["document_key"] for d in dry.json()["documents"]] == ["scan.pdf"]


def test_an_empty_document_at_the_current_extractor_version_is_not_reselected_forever(pg, tenant_of, fake_dbos, kb_cleanup):
    """A toctree page that is ready-empty under the current rst extractor has nothing to re-extract;
    a bulk re-extract (a CLI cron, the Studio button) must not restart it on every run."""
    client, _ = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    empty = _put(client, h, kb["id"], "toc.rst", content=".. toctree::\n\n   a\n", mime_type="text/x-rst").json()["document"]
    _make_empty(pg, tenant, empty, "rst", 1)
    dry = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"dry_run": True, "formats": ["rst"]}, headers=h)
    assert dry.status_code == 200, dry.text
    assert dry.json()["documents"] == []
