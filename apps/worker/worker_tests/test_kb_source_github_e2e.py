"""End to end acceptance of the GitHub repository documents source: the real API (in process), the real DBOS
worker (in process), the fake GitHub API behind the real adapter, the published example workflow (unchanged: the
same one that runs the product catalogue), `eap.kb_ingest` with the real extractor and the `test:hash` embedder.

A targeted sync of three files and then an incremental sync of the fake repository through the workflow; the mapped
metadata reaches documents and chunks; a pushed change is one new version; the token sentinel is nowhere (API
responses, every tenant table, the DBOS schema, logs)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
import uuid
from typing import Any

import httpx2
import jsonschema
import pytest
from sqlalchemy import text

from eap_kb_sources.providers.github import GithubRepositoryDocsAdapter
from eap_kb_sources.providers.github.fixtures import TOKEN, FakeGithub, ti_like_repo
from eap_kb_sources.registry import register_adapter
from eap_secrets import KEY_ENV, generate_key
from worker_tests.conftest import wait_for
from worker_tests.kb_ingest_helpers import cleanup_knowledge
from worker_tests.test_kb_source_sync_e2e import API, EXAMPLE, TERMINAL, Stack, api, dbos_leaks  # noqa: F401  (api: fixture)
from worker_tests.test_workflow_run_acceptance import auth_header

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import build_pdf  # noqa: E402

REPO = "TexasInstruments/mcupsdk-core"
SENTINELS = (TOKEN,)
DOCS = 30
CONFIG = {
    "repository": REPO,
    "include": ["docs/**"],
    "exclude": ["docs/big/**", "**/current.md"],
    "map": [
        {"match": "docs/api_guide/device/am263x/**", "metadata": {"sdk_family": "mcu-plus-sdk", "device": "AM263x"}},
        {"match": "**", "metadata": {"sdk_family": "mcu-plus-sdk"}},
    ],
}


class GhStack(Stack):
    """The operator path of the guide for a repository source: a token secret, an api_token connection, a check,
    a knowledge base, a source, the published workflow, then runs through the workflow."""

    def bootstrap(self, *, source_config: dict[str, Any] | None = None) -> None:
        self.call("PUT", "/secrets/repo-token", expect=200, json={"value": TOKEN})
        conn = self.call("POST", "/settings/connections", expect=201, json={
            "name": "github", "kind": "api_token", "base_url": "https://api.github.com",
            "config": {"provider": "github"}, "credential_refs": {"token": "secret:repo-token"}}).json()
        self.connection_id = conn["id"]
        assert conn["credential_refs"] == {"token": "secret:repo-token"}
        checked = self.call("POST", f"/settings/connections/{self.connection_id}/check", expect=200).json()
        assert checked["ok"] is True and checked["checked_at"], checked
        kb = self.call("POST", "/knowledge-bases", expect=201, json={"name": f"gh-{uuid.uuid4().hex[:6]}",
                                                                     "embedding_model": "test:hash"}).json()
        self.kb_id = kb["id"]
        src = self.call("POST", f"/knowledge-bases/{self.kb_id}/sources", expect=201, json={
            "connection_id": self.connection_id, "name": "mcupsdk-docs", "kind": "repository_docs",
            "provider": "github", "config": {**CONFIG, **(source_config or {})}}).json()
        self.source_id = src["id"]
        created = self.call("POST", "/workflows", expect=201, json={"spec": EXAMPLE}).json()
        self.workflow_id = created["id"]
        published = self.call("POST", f"/workflows/{self.workflow_id}/versions", expect=201).json()
        assert published["warnings"] == []

    def chunk_metadata(self) -> list[dict[str, Any]]:
        return [r.metadata for r in self.rows("SELECT metadata FROM kb_chunks WHERE tenant_id = :t")]

    def doc_metadata(self, key: str) -> dict[str, Any]:
        return self.scalar("SELECT metadata FROM kb_documents WHERE tenant_id = :t AND document_key = :k", k=key)

    def leaks(self) -> list[str]:
        found = [f"api response: {x}" for body in self.responses for x in SENTINELS if x in body]
        with self.engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t"
                " ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'"
                " WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'"))]
            for table in tables:
                n = conn.execute(text(f'SELECT count(*) FROM "{table}" x WHERE x.tenant_id = :t AND x::text LIKE :p'),
                                 {"t": self.tenant, "p": f"%{TOKEN}%"}).scalar_one()
                if n:
                    found.append(f"table {table}")
        return found + dbos_leaks(self.rt.settings.dbos_system_schema, SENTINELS)


@pytest.fixture
def make_stack(api, worker, tenant, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    caplog.set_level(0)

    def build(repo, **bootstrap) -> GhStack:
        server = FakeGithub(repo)
        register_adapter("repository_docs", "github", lambda: GithubRepositoryDocsAdapter(
            http_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(server)), jitter=lambda: 0.0))
        stack = GhStack(api, worker, tenant, server)
        stack.repo = repo  # type: ignore[attr-defined]
        stack.bootstrap(**bootstrap)
        return stack

    yield build
    register_adapter("repository_docs", "github", GithubRepositoryDocsAdapter)
    cleanup_knowledge(worker.engine, tenant)
    with worker.engine.begin() as conn:
        for table in ("workflow_schedules", "settings_connections", "tenant_secrets", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})


def repo_with_real_pdfs():
    """The fixture repository with PDFs the real extractor can read (the fixture's are only signature-valid)."""
    repo = ti_like_repo()
    for path in [p for p in repo.files if p.endswith(".pdf")]:
        name = path.rsplit("/", 1)[1]
        repo.put(path, build_pdf([[(f"{name} Technical Reference", 18)], [("Boot modes", 14), (f"{name} supports UART boot.", 10)]], title=name))
    repo.commit()
    return repo


def doc_key(s: GhStack, path: str) -> str:
    return f"source:{s.source_id}:" + path.replace("/", "~")


def calls(s: GhStack, fragment: str) -> int:
    return sum(1 for _h, p, _q, _a in s.server.requests if fragment in p)


def test_targeted_then_incremental_through_the_published_workflow(make_stack):
    s = make_stack(repo_with_real_pdfs())
    paths = ["docs/api_guide/device/am263x/driver_0.md", "docs/api_guide/device/am243x/boot.rst", "docs/pdf/am64x_trm.pdf"]

    # 1. targeted: three files, mapped metadata, ingested, chunks carry the metadata
    out = s.sync("targeted", paths)
    assert (out["status"], out["mode"], out["discovered"], out["failed"], out["errors"]) == ("completed", "targeted", 3, 0, [])
    assert out["documents_queued"] == 3 and out["inserted"] == 3
    docs = s.wait_ingested(3)
    assert {d.document_key for d in docs} == {doc_key(s, p) for p in paths}
    meta = s.doc_metadata(doc_key(s, paths[0]))
    assert meta["sdk_family"] == "mcu-plus-sdk" and meta["device"] == "AM263x" and meta["path"] == paths[0]
    assert meta["source"] == "kb_source" and meta["source_id"] == s.source_id and meta["relationship"] == "file"
    assert s.doc_metadata(doc_key(s, paths[1]))["sdk_family"] == "mcu-plus-sdk"
    assert "device" not in s.doc_metadata(doc_key(s, paths[1]))  # first matching rule only
    chunks = s.chunk_metadata()
    assert chunks and all(c.get("sdk_family") == "mcu-plus-sdk" for c in chunks)
    assert any(c.get("device") == "AM263x" and c.get("path") == paths[0] for c in chunks)
    assert s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t AND entity_type = 'repository'") == 1
    assert calls(s, "/contents/") == 3

    # 2. incremental, no cursor yet: a walk; the three known blobs cost nothing, the rest is new
    s.server.requests.clear()
    walk = s.sync("incremental")
    assert walk["complete"] is True and walk["failed"] == 0, walk
    assert walk["unchanged"] == 3 and walk["documents_queued"] == DOCS - 3
    assert calls(s, "/contents/") == walk["documents_queued"] and calls(s, "/git/trees") == 1
    s.wait_ingested(walk["documents_queued"] + 3)
    versions = {d.document_key: d.latest_version for d in s.documents()}
    assert set(versions.values()) == {1}

    # 3. incremental on an unchanged head: no tree, no contents, nothing new
    s.server.requests.clear()
    again = s.sync("incremental")
    assert again["complete"] is True and again["documents_queued"] == 0 and again["failed"] == 0
    assert again["unchanged"] == walk["discovered"] and calls(s, "/contents/") == 0 and calls(s, "/git/trees") == 0
    assert calls(s, "/commits/") == 1

    # 4. a pushed change: one new version, the rest untouched
    s.repo.put("docs/guides/guide_0.md", b"# Guide 0\n\nRewritten after a push.\n")
    s.repo.commit()
    s.server.requests.clear()
    pushed = s.sync("incremental")
    assert (pushed["updated"], pushed["documents_queued"], pushed["failed"]) == (1, 1, 0), pushed
    assert calls(s, "/contents/") == 1
    wait_for(lambda: {d.document_key: d.latest_version for d in s.documents()}[doc_key(s, "docs/guides/guide_0.md")] == 2 or None,
             timeout=60, what="the changed document to get a second version")
    others = {k: v for k, v in {d.document_key: d.latest_version for d in s.documents()}.items() if not k.endswith("guide_0.md")}
    assert set(others.values()) == {1}

    # 5. the token went to GitHub with every API call and is nowhere in what the run left behind
    assert all(a for h, _p, _q, a in s.server.requests if h == "api.github.com")
    assert s.leaks() == []


def test_the_token_sentinel_is_absent_from_logs_and_a_second_tenant_sees_nothing(make_stack, api, caplog, capfd):
    s = make_stack(repo_with_real_pdfs())
    out = s.sync("full")
    assert out["complete"] is True and out["failed"] == 0
    s.wait_ingested(out["documents_queued"])
    assert s.leaks() == []
    logs = caplog.text + capfd.readouterr().out
    assert TOKEN not in logs
    other = auth_header(f"t_{uuid.uuid4().hex[:10]}", "mallory")
    kb, src, conn = s.kb_id, s.source_id, s.connection_id
    for method, path in (
        ("GET", f"/settings/connections/{conn}"), ("POST", f"/settings/connections/{conn}/check"),
        ("GET", f"/knowledge-bases/{kb}/sources/{src}"), ("GET", f"/knowledge-bases/{kb}/sources/{src}/syncs"),
        ("POST", f"/knowledge-bases/{kb}/sources/{src}/sync"),
    ):
        r = api.request(method, f"{API}{path}", headers=other, json={"mode": "full"} if method == "POST" and path.endswith("/sync") else None)
        assert r.status_code == 404, (method, path, r.status_code)
        assert TOKEN not in r.text
    assert api.get(f"{API}/settings/connections", headers=other).json()["items"] == []
    assert api.get(f"{API}/knowledge-bases/{kb}/documents", headers=other).status_code == 404


def test_removal_is_a_ledger_event_and_a_deletion_only_with_delete_missing(make_stack):
    s = make_stack(repo_with_real_pdfs())
    first = s.sync("full")
    s.wait_ingested(first["documents_queued"])
    gone = "docs/guides/guide_0.md"
    s.repo.remove(gone)
    s.repo.commit()
    out = s.sync("full")
    assert out["documents_queued"] == 0 and out["failed"] == 0
    row = s.rows("SELECT status FROM kb_entity_documents WHERE tenant_id = :t AND document_key = :k", k=doc_key(s, gone))
    assert [r.status for r in row] == ["removed"]
    assert any(d.document_key == doc_key(s, gone) for d in s.documents())  # still in the knowledge base
    s.call("PATCH", f"/knowledge-bases/{s.kb_id}/sources/{s.source_id}", expect=200,
           json={"config": {**CONFIG, "document_policy": {"delete_missing": True}}})
    s.sync("full")
    assert not any(d.document_key == doc_key(s, gone) for d in s.documents())
    assert s.leaks() == []
