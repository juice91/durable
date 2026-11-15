"""RM.11 acceptance tests, API side: what test_retrieval_api.py does not cover.

Part 1 (fake DBOS client, no worker): publish checks and the search tester's contract.
Part 2 (a real `python -m eap_worker` subprocess): an agent with an attached knowledge base answers
with a citation through the real API, and the search tester returns real hits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.sse import StreamSettings, get_stream_settings
from eap_schemas import AgentSpec, reserved_tool_name_conflicts
from eap_schemas.mcp import mcp_tool_name
from eap_schemas.tools import RESERVED_TOOL_NAMES

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, requires_pg
from .test_retrieval_api import API, _kb, _spec, kb_cleanup  # noqa: F401 - fixtures
from .test_retrieval_api import _env  # noqa: F401 - autouse fixture
from .test_run_e2e import DBOS_URL, REPO_ROOT, _drop_schema
from .test_runs import deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

pytestmark = requires_pg


# ----------------------------------------------------------------------------- publish


def _try_publish(client, headers, spec):
    """Create then publish; the first refusal (create: schema validation, publish: the checks) or the
    publish response. Returns (response, agent_id or None)."""
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=headers)
    if r.status_code != 201:
        return r, None
    return client.post(f"{API}/agents/{r.json()['id']}/versions", headers=headers), r.json()["id"]


@pytest.mark.parametrize("tools", [["retrieve"], ["update_working_memory"], ["knowledge.search"],
                                   ["clock.now", "retrieve"]])
def test_publish_refuses_platform_names_for_specs_with_and_without_knowledge(pg, tenant_of, kb_cleanup, tools):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    for extra in ({}, {"knowledge_bases": [{"id": kb["id"]}]}):
        r, agent_id = _try_publish(client, h, _spec(tools=tools, **extra))
        assert r.status_code == 422, (tools, extra, r.text)
        assert any(name in r.text for name in ("retrieve", "update_working_memory", "knowledge.search")), r.text
        if agent_id is not None:  # the draft was stored, but nothing was published
            versions = client.get(f"{API}/agents/{agent_id}/versions", headers=h).json()
            assert (versions["items"] if isinstance(versions, dict) else versions) == []


def test_mcp_tool_names_can_never_be_a_reserved_name():
    """Every MCP model-facing name starts with `mcp__`, so none can be `retrieve` or
    `update_working_memory`; the check still covers them (reserved_tool_name_conflicts)."""
    for server, tool in [("retrieve", "retrieve"), ("a", "update_working_memory"), ("x" * 99, "retrieve"), ("", "r")]:
        if not server:
            continue
        assert mcp_tool_name(server, tool) not in RESERVED_TOOL_NAMES
    spec = AgentSpec.model_validate(_spec(mcp_tools=[{"server": "retrieve", "tool": "retrieve"}]))
    assert reserved_tool_name_conflicts(spec) == ()
    for name in RESERVED_TOOL_NAMES:
        assert reserved_tool_name_conflicts(AgentSpec.model_validate(_spec(tools=[name]))) == (name,)


def test_hidden_tool_is_not_in_the_catalog_or_a_workflow_tool_step(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    body = client.get(f"{API}/tools", headers=h).json()
    ids = {t["id"] for t in (body["items"] if isinstance(body, dict) else body)}
    assert "knowledge.search" not in ids and "clock.now" in ids
    assert client.post(f"{API}/tools/knowledge.search/runs", json={"input": {}}, headers=h).status_code == 404
    wf = {"name": "W", "input_schema": {"type": "object", "properties": {}}, "start": "s",
          "steps": [{"id": "s", "type": "tool", "tool_id": "knowledge.search", "input": {}}]}
    r = client.post(f"{API}/workflows", json={"spec": wf}, headers=h)
    if r.status_code == 201:
        r = client.post(f"{API}/workflows/{r.json()['id']}/versions", headers=h)
    assert r.status_code == 422 and "knowledge.search" in r.text, r.text


def test_publish_with_a_foreign_and_an_own_kb_fails_whole(pg, tenant_of, kb_cleanup):
    client, _ = pg
    _, h = tenant_of()
    _, other = tenant_of()
    mine, foreign = _kb(client, h), _kb(client, other)
    refs = [{"id": mine["id"]}, {"id": foreign["id"]}]
    r, _ = _try_publish(client, h, _spec(knowledge_bases=refs))
    assert r.status_code == 422 and foreign["id"] in r.json()["detail"]
    assert mine["name"] not in r.text
    # A malformed id is a schema error, not a lookup.
    r, _ = _try_publish(client, h, _spec(knowledge_bases=[{"id": "not-a-uuid"}]))
    assert r.status_code == 422


# ----------------------------------------------------------------------------- search tester (no worker)

SECRET_QUERY = "needle-9f2c-secret"


def test_wait_zero_answers_202_and_the_run_is_readable_by_its_tenant_only(pg, tenant_of, kb_cleanup, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    _, other = tenant_of()
    kb = _kb(client, h)
    run_id = str(uuid.uuid4())
    url = f"{API}/knowledge-bases/{kb['id']}/search?wait=0"
    r = client.post(url, json={"query": SECRET_QUERY, "run_id": run_id}, headers=h)
    assert r.status_code == 202 and r.json()["run_id"] == run_id and r.json()["status"] == "queued"
    run = client.get(f"{API}/runs/{run_id}", headers=h)
    assert run.status_code == 200 and run.json()["kind"] == "tool" and run.json()["tool_id"] == "knowledge.search"
    assert run.json()["status"] == "queued"
    assert client.get(f"{API}/runs/{run_id}", headers=other).status_code == 404
    # Another tenant cannot use this run_id (not even to learn that it exists beyond the conflict) and
    # cannot search this knowledge base.
    assert client.post(url, json={"query": "x", "run_id": run_id}, headers=other).status_code == 404
    kb_other = _kb(client, other)
    r = client.post(f"{API}/knowledge-bases/{kb_other['id']}/search?wait=0", json={"query": "x", "run_id": run_id}, headers=other)
    assert r.status_code == 409 and SECRET_QUERY not in r.text
    assert len(fake_dbos.calls) == 1
    # A failed run is answered as 200 with its error and no result.
    store = app.state.run_stores[0]
    store.mark_running(tenant, uuid.UUID(run_id))
    store.finish_with_end(tenant, uuid.UUID(run_id), "failed", None, "KnowledgeBaseNotFound: gone")
    r = client.post(url, json={"query": SECRET_QUERY, "run_id": run_id}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "failed" and "result" not in r.json()


@pytest.mark.parametrize("change", [{"top_k": 4}, {"min_score": 0.5}, {"filter": {"lang": "en"}}, {"query": "other"}])
def test_a_reused_run_id_with_any_different_setting_is_409(pg, tenant_of, kb_cleanup, fake_dbos, change):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    run_id = str(uuid.uuid4())
    url = f"{API}/knowledge-bases/{kb['id']}/search?wait=0"
    body = {"query": "printer", "top_k": 3, "run_id": run_id}
    assert client.post(url, json=body, headers=h).status_code == 202
    r = client.post(url, json={**body, **change}, headers=h)
    assert r.status_code == 409, change
    assert len(fake_dbos.calls) == 1
    # A second knowledge base of the same tenant with the same run_id is also a different request.
    kb2 = _kb(client, h, name="Docs 2")
    r = client.post(f"{API}/knowledge-bases/{kb2['id']}/search?wait=0", json=body, headers=h)
    assert r.status_code == 409


def test_query_validation_is_422_and_never_echoes_values(pg, tenant_of, kb_cleanup, fake_dbos):
    client, _ = pg
    _, h = tenant_of()
    kb = _kb(client, h)
    url = f"{API}/knowledge-bases/{kb['id']}/search?wait=0"
    bad = [
        {"query": SECRET_QUERY * 200},  # longer than MAX_SEARCH_QUERY_CHARS
        {"query": SECRET_QUERY, "top_k": SECRET_QUERY},
        {"query": SECRET_QUERY, "min_score": SECRET_QUERY},
        {"query": SECRET_QUERY, "min_score": -0.1},
        {"query": SECRET_QUERY, "filter": {"k": [SECRET_QUERY, {"nested": 1}]}},
        {"query": SECRET_QUERY, "filter": {SECRET_QUERY + "!": 1}},
        {"query": SECRET_QUERY, "run_id": SECRET_QUERY},
        {"query": SECRET_QUERY, "bogus": SECRET_QUERY},
        {"query": 5},
        {},
    ]
    for body in bad:
        r = client.post(url, json=body, headers=h)
        assert r.status_code == 422, body
        assert SECRET_QUERY not in r.text, body  # never a rejected value
    r = client.post(url, content=f'{{"query": "{SECRET_QUERY}", ', headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 422 and SECRET_QUERY not in r.text
    assert client.post(f"{url}&wait=99", json={"query": "x"}, headers=h).status_code == 422
    assert client.post(url, json={"query": "x"}).status_code == 401
    assert fake_dbos.calls == []


# ----------------------------------------------------------------------------- real worker

_FILLER = " ".join(f"detail{i}" for i in range(60))  # sections under 40 tokens would be merged by the chunker
GUIDE = (
    "# Printer guide\n\n"
    f"## Paper jams\n\nOpen tray two and pull the jammed paper out slowly, then close the tray. {_FILLER}\n\n"
    f"## Toner\n\nReplace the toner cartridge when the light blinks orange. {_FILLER}\n\n"
    f"## Wifi setup\n\nHold the wifi button for five seconds to pair the printer. {_FILLER}\n"
)
GUIDE_URL = "https://docs.example.test/printer"


class Stack:
    def __init__(self, client, engine, tenant, headers):
        self.client, self.engine, self.tenant, self.h = client, engine, tenant, headers

    def wait(self, fetch, ready, what, timeout=90):
        deadline = time.monotonic() + timeout
        while True:
            value = fetch()
            if ready(value):
                return value
            assert time.monotonic() < deadline, (what, value)
            time.sleep(0.2)

    def knowledge_base(self, name="Printer manual", key="guide.md", content=GUIDE) -> dict:
        kb = _kb(self.client, self.h, name)
        r = self.client.put(f"{API}/knowledge-bases/{kb['id']}/documents/{key}", headers=self.h, json={
            "content": content, "mime_type": "text/markdown", "title": "Printer guide", "source_url": GUIDE_URL})
        assert r.status_code == 202, r.text
        url = f"{API}/knowledge-bases/{kb['id']}/documents/{key}"
        doc = self.wait(lambda: self.client.get(url, headers=self.h).json(), lambda d: d["status"] in ("ready", "failed"),
                        "ingest")
        assert doc["status"] == "ready", doc
        return kb

    def agent(self, *kb_ids) -> str:
        spec = _spec(instructions="Answer from the knowledge base.", knowledge_bases=[{"id": i} for i in kb_ids])
        r = self.client.post(f"{API}/agents", json={"spec": spec}, headers=self.h)
        assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        assert self.client.post(f"{API}/agents/{agent_id}/versions", headers=self.h).status_code == 201
        return agent_id

    def turn(self, agent_id: str, text_in: str) -> tuple[str, dict]:
        conv = self.client.post(f"{API}/agents/{agent_id}/conversations", json={}, headers=self.h)
        assert conv.status_code == 201, conv.text
        run_id = str(uuid.uuid4())
        r = self.client.post(f"{API}/conversations/{conv.json()['id']}/runs", headers=self.h,
                             json={"run_id": run_id, "input": text_in, "version": None})
        assert r.status_code == 201, r.text
        run = self.wait(lambda: self.client.get(f"{API}/runs/{run_id}", headers=self.h).json(),
                        lambda d: d["status"] in ("completed", "failed", "needs_attention"), "turn")
        return run_id, run

    def events(self, run_id: str) -> list[dict]:
        r = self.client.get(f"{API}/runs/{run_id}/events", headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:10]
    env = {"EAP_DATABASE_URL": PG_URL or "", "EAP_DBOS_URL": DBOS_URL,
           "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}", "EAP_APP_VERSION": f"rm11-{suffix}",
           "EAP_EXECUTOR_ID": f"rm11-{suffix}", "EAP_QUEUE_POLL_SECONDS": "0.2",
           "EAP_ALLOWED_EMBEDDING_MODELS": "test:*"}
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "eap_worker"], cwd=REPO_ROOT, env={**os.environ, **env},
                            stdout=log, stderr=subprocess.STDOUT, text=True)
    patch = pytest.MonkeyPatch()
    engine, tenants = None, [f"t{uuid.uuid4().hex[:10]}", f"t{uuid.uuid4().hex[:10]}"]
    try:
        deadline = time.monotonic() + 60
        while "EAP_WORKER_READY" not in open(log.name).read():
            assert proc.poll() is None and time.monotonic() < deadline, open(log.name).read()[-3000:]
            time.sleep(0.3)
        for k, v in env.items():
            patch.setenv(k, v)
        app = create_app(database_url=PG_URL)
        app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.1, heartbeat_interval=10.0)
        engine = app.state.database_engine
        with TestClient(app) as client:
            a = Stack(client, engine, tenants[0], get_auth_header(tenants[0], "alice"))
            b = Stack(client, engine, tenants[1], get_auth_header(tenants[1], "bob"))
            yield a, b
    finally:
        patch.undo()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if engine is not None:
            with engine.begin() as conn:
                for t in tenants:
                    conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            for t in tenants:
                cleanup_tenant(engine, t)
            engine.dispose()
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])
        log.close()
        os.unlink(log.name)


@pytest.fixture(scope="module")
def ready(stack):
    """Tenant A: a knowledge base with the guide ingested through the API and the worker."""
    a, _ = stack
    return a.knowledge_base()


def test_agent_with_a_knowledge_base_answers_with_a_citation_and_the_source_url(stack, ready):
    a, _ = stack
    agent_id = a.agent(ready["id"])
    run_id, run = a.turn(agent_id, "How do I clear a paper jam in tray two?")
    assert run["status"] == "completed", run
    assert run["output"] == f"According to [1] Paper jams: {GUIDE_URL}#paper-jams"
    events = a.events(run_id)
    types = [e["type"] for e in events]
    assert types.index("tool_call") < types.index("embedding") < types.index("tool_result")
    [call] = [e["data"] for e in events if e["type"] == "tool_call"]
    [embedding] = [e["data"] for e in events if e["type"] == "embedding"]
    [result] = [e["data"] for e in events if e["type"] == "tool_result"]
    assert call["tool_name"] == "retrieve" and embedding["model_id"] == "test:hash"
    assert result["status"] == "ok" and result["content"].startswith("[1] Paper jams — " + GUIDE_URL)
    first = result["sources"][0]
    assert first["n"] == 1 and first["url"] == f"{GUIDE_URL}#paper-jams" and first["kb_id"] == ready["id"]
    assert first["document_key"] == "guide.md" and first["doc_version"] == 1 and 0 < first["score"] <= 1
    assert "Open tray two" not in json.dumps(result["sources"])  # structured sources carry no section text
    assert run["output"].count("[1]") == 1 and events[-1]["type"] == "run_completed"


def test_the_search_tester_returns_hits_ordered_by_score(stack, ready):
    a, _ = stack
    r = a.client.post(f"{API}/knowledge-bases/{ready['id']}/search?wait=15", headers=a.h,
                      json={"query": "hold the wifi button to pair", "top_k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["kind"] == "tool" and body["knowledge_base_id"] == ready["id"]
    hits = body["result"]["hits"]
    assert [h["title"] for h in hits][0] == "Wifi setup" and len(hits) == 3
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True) and [h["n"] for h in hits] == [1, 2, 3]
    assert all(h["content_truncated"] is False and h["url"].startswith(GUIDE_URL) for h in hits)
    assert body["result"]["text"].startswith("[1] Wifi setup — ")
    # A tight min_score and a metadata filter that matches nothing give an empty, well-formed result.
    r = a.client.post(f"{API}/knowledge-bases/{ready['id']}/search?wait=15", headers=a.h,
                      json={"query": "wifi", "filter": {"lang": "xx"}})
    assert r.status_code == 200 and r.json()["result"]["hits"] == []


def test_tenant_b_cannot_search_or_attach_tenant_a_knowledge_base(stack, ready):
    a, b = stack
    assert b.client.post(f"{API}/knowledge-bases/{ready['id']}/search?wait=1", headers=b.h,
                         json={"query": "wifi"}).status_code == 404
    r = b.client.post(f"{API}/agents", headers=b.h, json={"spec": _spec(knowledge_bases=[{"id": ready["id"]}])})
    assert r.status_code == 201
    r = b.client.post(f"{API}/agents/{r.json()['id']}/versions", headers=b.h)
    assert r.status_code == 422 and r.json()["detail"] == f"knowledge base '{ready['id']}' does not exist in this tenant"
    # Tenant B's own agent with its own knowledge base never sees tenant A's chunks.
    own = b.knowledge_base(name="B manual", content=f"# Other\n\n## Toner\n\nBravo manual toner text for tenant B. {_FILLER}\n")
    run_id, run = b.turn(b.agent(own["id"]), "toner cartridge light")
    assert run["status"] == "completed" and run["output"].startswith("According to [1] Toner:")
    [result] = [e["data"] for e in b.events(run_id) if e["type"] == "tool_result"]
    assert {s["kb_id"] for s in result["sources"]} == {own["id"]} and "Replace the toner" not in result["content"]
