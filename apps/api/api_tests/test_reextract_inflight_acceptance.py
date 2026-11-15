"""Acceptance (browser check finding A): bulk re-extract never queues a second version over a first
ingest that is still pending/ingesting, even when repeated; a failed first ingest is retried once."""

from __future__ import annotations

import pytest

from .test_p1a4_upload_reextract import (  # noqa: F401 - fixtures and helpers
    API, RST, _engine, _fail_first_ingest, _kb, _kb_runs, _put, _knowledge_env, kb_cleanup,
)
from .test_runs import SPEC, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401
from .run_helpers import requires_pg
from sqlalchemy import text

pytestmark = requires_pg


def test_pending_and_ingesting_first_ingests_are_not_reextracted(pg, tenant_of, fake_dbos, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    docs = {k: _put(client, h, kb["id"], k, content=RST, mime_type="text/x-rst").json()["document"]
            for k in ("pending.rst", "ingesting.rst", "failed.rst")}
    with _engine(pg).begin() as conn:
        conn.execute(text("UPDATE kb_documents SET status = 'ingesting' WHERE id = CAST(:i AS uuid)"),
                     {"i": docs["ingesting.rst"]["id"]})
    url = f"{API}/knowledge-bases/{kb['id']}/reextract"
    runs, calls = _kb_runs(pg, kb["id"]), len(fake_dbos.calls)
    # A brand-new bulk import in flight: nothing is selected, nothing is started (dry run or not).
    assert client.post(url, json={"dry_run": True}, headers=h).json()["selected"] == 0
    r = client.post(url, json={}, headers=h)
    assert r.status_code in (200, 202) and r.json()["selected"] == 0
    assert _kb_runs(pg, kb["id"]) == runs and len(fake_dbos.calls) == calls
    # One first ingest fails: only it is selected, and repeating the request starts nothing more.
    _fail_first_ingest(pg, docs["failed.rst"]["id"])
    dry = client.post(url, json={"dry_run": True}, headers=h).json()
    assert [d["document_key"] for d in dry["documents"]] == ["failed.rst"]
    first = client.post(url, json={}, headers=h)
    assert first.status_code == 202 and first.json()["selected"] == 1 and first.json()["started"] == 1
    again = client.post(url, json={}, headers=h)
    assert again.json()["selected"] == 0
    assert _kb_runs(pg, kb["id"]) == runs + 1
