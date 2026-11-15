"""RAG P1 quality fix 2 at the API: an empty document (ready, 0 chunks) shows its `extraction`
with `empty_reason`, and is no failure (no `error_code`)."""

from __future__ import annotations

import json

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_p1a4_upload_reextract import (  # noqa: F401 - fixtures
    PDF,
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


def test_an_empty_document_shows_empty_reason_and_no_error(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    tenant, h = tenant_of()
    kb = _kb(client, h)
    put = _put(client, h, kb["id"], "index.rst", content=".. toctree::\n\n   linux/index\n", mime_type="text/x-rst").json()
    url = f"{API}/knowledge-bases/{kb['id']}/documents/index.rst"
    with _engine(pg).begin() as conn:  # what the worker leaves for an empty version
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                 " VALUES (:t, :r, 900, 'extract:1', 'extracted', CAST(:d AS jsonb), now())"),
            {"t": tenant, "r": put["run_id"], "d": json.dumps(
                {"extractor": "rst", "extractor_version": 1, "sections": 1, "chars": 0,
                 "empty_reason": "no_extractable_text"})},
        )
        conn.execute(text("UPDATE kb_documents SET active_version = 1, status = 'ready', chunk_count = 0 WHERE id = :id"),
                     {"id": put["document"]["id"]})
    got = client.get(url, headers=h).json()
    assert got["status"] == "ready" and got["chunk_count"] == 0
    assert got["error"] is None and got["error_code"] is None and got["error_reason"] is None
    assert got["extraction"]["empty_reason"] == "no_extractable_text"
    assert got["extraction"]["extractor"] == "rst" and got["extraction"]["chars"] == 0


def test_an_image_only_pdf_is_failed_with_no_text_layer_and_re_extract_selects_it(pg, tenant_of, fake_dbos, kb_cleanup):
    """Not empty: its pages have content part 2's image extraction can read."""
    from eap_schemas.knowledge import EXTRACTION_ERROR_MESSAGES

    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    put = _put(client, h, kb["id"], "scan.pdf", content_base64=_b64(PDF), mime_type="application/pdf").json()
    message = EXTRACTION_ERROR_MESSAGES["no_text_layer"]
    assert message.startswith("Image-only PDF with no text layer")
    with _engine(pg).begin() as conn:  # what the worker leaves (kb_mark_failed)
        conn.execute(text("UPDATE kb_documents SET status = 'failed', error = :e WHERE id = :id"),
                     {"e": f"extraction_failed: {message}", "id": put["document"]["id"]})
    got = client.get(f"{API}/knowledge-bases/{kb['id']}/documents/scan.pdf", headers=h).json()
    assert got["status"] == "failed" and got["active_version"] is None and got["extraction"] is None
    assert (got["error_code"], got["error_reason"]) == ("extraction_failed", "no_text_layer")
    dry = client.post(f"{API}/knowledge-bases/{kb['id']}/reextract", json={"dry_run": True, "formats": ["pdf"]},
                      headers=h)
    assert dry.status_code == 200, dry.text
    assert [d["document_key"] for d in dry.json()["documents"]] == ["scan.pdf"]
