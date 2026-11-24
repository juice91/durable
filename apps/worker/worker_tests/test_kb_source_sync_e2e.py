"""End to end acceptance of the knowledge-base source sync (brief §19-§20): the real API (in process), the real
DBOS worker (in process), the fixture-backed Acme Semiconductor fake server behind the real Acme adapter, the
published example workflow, `eap.kb_ingest` with the real extractor and the `test:hash` embedder.

The operator path, as the guide describes it: tenant secrets through `PUT /secrets/{name}`, a connection with
`secret:` references, a check, a knowledge base, a source, the published workflow, then runs through the
workflow. Everything a run leaves behind is scanned for the sentinel credentials and the sentinel token."""

from __future__ import annotations

import json
import resource
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from sqlalchemy import text

pytest.importorskip("eap_kb_sources_acme")  # the TI plug-in is optional: this runs the real worker against it

from eap_kb_sources_acme import PROVIDER as TI_PROVIDER, TiProductCatalogAdapter, TokenCache
from eap_kb_sources_acme.fixtures import FakeTiServer, Part, TOKEN_PREFIX, template_part
from eap_kb_sources.registry import register_adapter
from eap_secrets import KEY_ENV, generate_key
from worker_tests.conftest import DBOS_DB_URL, wait_for
from worker_tests.kb_ingest_helpers import cleanup_knowledge
from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import build_pdf  # noqa: E402

import httpx2  # noqa: E402

EXAMPLE = json.loads((REPO_ROOT / "examples" / "workflows" / "kb-source-sync.json").read_text())["spec"]
API = "/api/v1"
SENTINEL_ID = "SENTINEL-CLIENT-ID-5c1e"
SENTINEL_SECRET = "SENTINEL-CLIENT-SECRET-7f3a"
SENTINELS = (SENTINEL_ID, SENTINEL_SECRET, TOKEN_PREFIX)
TERMINAL = ("completed", "failed", "needs_attention", "cancelled")
REFS = {"client_id": "secret:conn-client-id", "client_secret": "secret:conn-client-secret"}


def datasheet(orderable: str, revision: int) -> bytes:
    """A small real PDF: the ingest extracts and embeds it."""
    return build_pdf(
        [[(f"{orderable} Product Datasheet", 18), (f"Revision {revision}", 10)],
         [("Absolute maximum ratings", 14), (f"Supply voltage of {orderable} is 36 volts.", 10)]],
        title=f"{orderable} datasheet",
    )


def two_parts() -> list[Part]:
    parts = []
    for i, orderable in enumerate(("TIE2E1DR", "TIE2E2DR"), start=1):
        part = template_part(orderable, "TIE2E", i, datasheet_rev=1)
        part.datasheet = datasheet(orderable, 1)
        parts.append(part)
    return parts


def catalogue_parts(n: int) -> list[Part]:
    return [template_part(f"CAT{i:03d}DR", f"CATG{i % 3}", i) for i in range(1, n + 1)]


class Slow:
    """Wraps the fake server: a pause per API request, so a run lasts long enough to be cancelled."""

    def __init__(self, server: FakeTiServer, pause: float) -> None:
        self.server, self.pause = server, pause

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "api.vendor.example" and "oauth" not in request.url.path:
            time.sleep(self.pause)
        return self.server(request)


class Stack:
    """The API client, the tenant's headers and helpers; every API response body is kept for the leak scan."""

    def __init__(self, api, worker, tenant: str, server: FakeTiServer) -> None:
        self.api, self.rt, self.engine, self.tenant, self.server = api, worker, worker.engine, tenant, server
        self.h = auth_header(tenant)
        self.responses: list[str] = []
        self.workflow_id: str | None = None

    # ------------------------------------------------------------------------------------------- API
    def call(self, method: str, path: str, *, expect: int | tuple[int, ...] | None = None, headers=None, **kw):
        r = self.api.request(method, f"{API}{path}", headers=headers or self.h, **kw)
        self.responses.append(r.text)
        if expect is not None:
            ok = (expect,) if isinstance(expect, int) else expect
            assert r.status_code in ok, (method, path, r.status_code, r.text)
        return r

    def bootstrap(self, *, source_config: dict[str, Any] | None = None) -> None:
        """Secrets -> connection -> check -> knowledge base -> source -> published workflow (the guide's order)."""
        self.call("PUT", "/secrets/conn-client-id", expect=200, json={"value": SENTINEL_ID})
        self.call("PUT", "/secrets/conn-client-secret", expect=200, json={"value": SENTINEL_SECRET})
        conn = self.call("POST", "/settings/connections", expect=201, json={
            "name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://api.vendor.example",
            "config": {"provider": TI_PROVIDER, "allowed_hosts": ["api.vendor.example", "www.ti.com"]},
            "credential_refs": REFS}).json()
        self.connection_id = conn["id"]
        assert conn["credential_refs"] == REFS
        checked = self.call("POST", f"/settings/connections/{self.connection_id}/check", expect=200).json()
        assert checked["ok"] is True and checked["checked_at"], checked
        kb = self.call("POST", "/knowledge-bases", expect=201, json={"name": f"ti-{uuid.uuid4().hex[:6]}",
                                                                     "embedding_model": "test:hash"}).json()
        self.kb_id = kb["id"]
        config = {"document_policy": {"types": ["datasheet"]}, "page_size": 3, **(source_config or {})}
        src = self.call("POST", f"/knowledge-bases/{self.kb_id}/sources", expect=201, json={
            "connection_id": self.connection_id, "name": "ti-catalogue", "kind": "product_catalog",
            "provider": TI_PROVIDER, "config": config}).json()
        self.source_id = src["id"]
        created = self.call("POST", "/workflows", expect=201, json={"spec": EXAMPLE}).json()
        self.workflow_id = created["id"]
        published = self.call("POST", f"/workflows/{self.workflow_id}/versions", expect=201).json()
        assert [w for w in published["warnings"] if w["code"] != "step_id_convention"] == []  # the system template keeps its ids (PR 147)

    def start(self, input_value: dict[str, Any]) -> str:
        r = self.call("POST", f"/workflows/{self.workflow_id}/runs", expect=202, json={"input": input_value})
        return r.json()["run_id"]

    def wait(self, run_id: str, timeout: float = 150) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.call("GET", f"/runs/{run_id}", expect=200).json()
            if run["status"] in TERMINAL:
                return run
            assert time.monotonic() < deadline, run
            time.sleep(0.25)

    def sync(self, mode: str, parts: list[str] | None = None) -> dict[str, Any]:
        """Run the published workflow to its end: the parent run row (its `output` is the report JSON)."""
        body: dict[str, Any] = {"source_id": self.source_id, "mode": mode}
        if parts is not None:
            body["part_numbers"] = parts
        run = self.wait(self.start(body))
        assert run["status"] == "completed", run
        out = json.loads(run["output"])
        jsonschema.validate(out, EXAMPLE["output_schema"])
        return out

    # ------------------------------------------------------------------------------------------ DB
    def scalar(self, sql: str, **params: Any) -> Any:
        with self.engine.connect() as conn:
            return conn.execute(text(sql), {"t": self.tenant, "s": getattr(self, "source_id", None), **params}).scalar_one()

    def rows(self, sql: str, **params: Any) -> list[Any]:
        with self.engine.connect() as conn:
            return list(conn.execute(text(sql), {"t": self.tenant, "s": getattr(self, "source_id", None), **params}))

    def documents(self) -> list[Any]:
        return self.rows("SELECT document_key, status, latest_version FROM kb_documents WHERE tenant_id = :t ORDER BY 1")

    def wait_ingested(self, count: int) -> list[Any]:
        def ready():
            docs = self.documents()
            return docs if len(docs) == count and all(d.status == "ready" for d in docs) else None
        return wait_for(ready, timeout=150, what=f"{count} datasheets to be ingested")

    def counts(self) -> dict[str, int]:
        tables = ("kb_entities", "kb_entity_identifiers", "kb_entity_parameters", "kb_entity_documents",
                  "kb_source_records", "kb_documents")
        return {t: self.scalar(f"SELECT count(*) FROM {t} WHERE tenant_id = :t") for t in tables}

    def checkpoint(self) -> dict[str, Any]:
        """The source's stored checkpoint: {"cursor": the adapter's cursor, "segments": n}."""
        return self.scalar("SELECT last_checkpoint FROM kb_sources WHERE tenant_id = :t AND id = :s")

    # ----------------------------------------------------------------------------------- leak scan
    def leaks(self) -> list[str]:
        """Where a sentinel shows up: API responses, every app table of the tenant (events, run rows, spans, side
        effects, the kb tables ...), every table of the private DBOS schema (workflow inputs, step outputs and
        events), nothing else is read."""
        found: list[str] = []
        for body in self.responses:
            found += [f"api response: {s}" for s in SENTINELS if s in body]
        with self.engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT c.table_name FROM information_schema.columns c JOIN information_schema.tables t"
                " ON t.table_schema = c.table_schema AND t.table_name = c.table_name AND t.table_type = 'BASE TABLE'"
                " WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'"))]
            for table in tables:
                for sentinel in SENTINELS:
                    n = conn.execute(text(f'SELECT count(*) FROM "{table}" x WHERE x.tenant_id = :t AND x::text LIKE :p'),
                                     {"t": self.tenant, "p": f"%{sentinel}%"}).scalar_one()
                    if n:
                        found.append(f"table {table}: {sentinel}")
        found += dbos_leaks(self.rt.settings.dbos_system_schema)
        return found


def dbos_leaks(schema: str, sentinels: tuple[str, ...] = SENTINELS) -> list[str]:
    from sqlalchemy import create_engine

    eng = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    found: list[str] = []
    try:
        with eng.connect() as conn:
            for (table,) in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = :s AND table_type = 'BASE TABLE'"),
                    {"s": schema}).all():
                for sentinel in sentinels:
                    n = conn.execute(text(f'SELECT count(*) FROM "{schema}"."{table}" x WHERE x::text LIKE :p'),
                                     {"p": f"%{sentinel}%"}).scalar_one()
                    if n:
                        found.append(f"dbos {table}: {sentinel}")
    finally:
        eng.dispose()
    return found


@pytest.fixture(scope="module")
def api(worker, worker_settings):
    with workflow_api(worker_settings.dbos_system_schema, worker_settings.app_version) as client:
        yield client


@pytest.fixture
def make_stack(api, worker, tenant, monkeypatch, caplog):
    """A factory: the stack of a fresh tenant over a fake TI server (the real adapter, a mock transport)."""
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    caplog.set_level(0)
    made: list[Stack] = []

    def build(parts: list[Part] | FakeTiServer, *, pause: float = 0.0, **bootstrap) -> Stack:
        if isinstance(parts, FakeTiServer):  # a prepared server (the benchmark world)
            server = parts
            server.client_id, server.client_secret = SENTINEL_ID, SENTINEL_SECRET
        else:
            server = FakeTiServer(parts, client_id=SENTINEL_ID, client_secret=SENTINEL_SECRET)
        handler = Slow(server, pause) if pause else server
        register_adapter("product_catalog", TI_PROVIDER, lambda: TiProductCatalogAdapter(
            http_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            rate_per_second=None, token_cache=TokenCache(), jitter=lambda: 0.0))
        stack = Stack(api, worker, tenant, server)
        stack.caplog = caplog  # type: ignore[attr-defined]
        stack.bootstrap(**bootstrap)
        made.append(stack)
        return stack

    yield build
    register_adapter("product_catalog", TI_PROVIDER, TiProductCatalogAdapter)
    cleanup_knowledge(worker.engine, tenant)
    with worker.engine.begin() as conn:
        for table in ("workflow_schedules", "settings_connections", "tenant_secrets", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})


# ======================================================================================== the tests


def test_targeted_sync_end_to_end_then_the_same_request_again_changes_nothing(make_stack):
    s = make_stack(two_parts())
    out = s.sync("targeted", ["TIE2E1DR", "TIE2E2DR"])
    assert (out["status"], out["mode"], out["source_id"]) == ("completed", "targeted", s.source_id)
    assert (out["discovered"], out["failed"], out["errors"], out["documents_queued"]) == (2, 0, [], 2)
    assert out["inserted"] > 0 and out["updated"] == 0

    # records: the raw payloads per part (detail, parametrics, quality), kept apart from the normalised entities
    types = {r.record_type for r in s.rows("SELECT DISTINCT record_type FROM kb_source_records WHERE tenant_id = :t")}
    assert {"detail", "parametrics", "quality"} <= types
    assert s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t") == 2
    kinds = {r.scheme for r in s.rows("SELECT DISTINCT scheme FROM kb_entity_identifiers WHERE tenant_id = :t")}
    assert {"generic_part_number", "orderable_part_number"} <= kinds
    assert s.scalar("SELECT count(*) FROM kb_entity_parameters WHERE tenant_id = :t") >= 4  # two per part
    attrs = s.rows("SELECT attributes FROM kb_entities WHERE tenant_id = :t")
    assert all(json.dumps(a.attributes).find("ProcessTechnology") >= 0 or a.attributes for a in attrs)

    ingested = s.wait_ingested(2)
    versions = {d.document_key: d.latest_version for d in ingested}
    before = s.counts()

    again = s.sync("targeted", ["TIE2E1DR", "TIE2E2DR"])
    assert (again["inserted"], again["updated"], again["failed"], again["documents_queued"]) == (0, 0, 0, 0)
    assert again["unchanged"] == out["inserted"] and again["discovered"] == 2
    assert s.counts() == before, "no duplicate entities, identifiers, parameters, documents or records"
    assert {d.document_key: d.latest_version for d in s.documents()} == versions, "the datasheets were not re-embedded"
    assert s.leaks() == []


def test_a_workflow_run_result_is_the_report_the_sync_row_holds(make_stack):
    s = make_stack(two_parts())
    out = s.sync("targeted", ["TIE2E1DR"])
    run_id = out["run_id"]
    sync = s.call("GET", f"/knowledge-bases/{s.kb_id}/sources/{s.source_id}/syncs/{run_id}", expect=200).json()
    assert sync["run_status"] == "completed" and sync["run_id"] == run_id
    assert sync["report"]["discovered"] == out["discovered"] == 1
    listing = s.call("GET", f"/knowledge-bases/{s.kb_id}/sources/{s.source_id}/syncs", expect=200).json()["items"]
    assert [i["run_id"] for i in listing] == [run_id]
    s.wait_ingested(1)


def test_a_full_walk_stops_on_the_daily_budget_and_the_next_run_resumes_the_cursor(make_stack):
    s = make_stack(catalogue_parts(7), source_config={"product_daily_limit": 10, "page_size": 3})
    first = s.sync("full")
    assert first["complete"] is False and first["stop_reason"] == "budget", first
    cursor = s.checkpoint()["cursor"]
    assert cursor.get("page") == 1 and cursor["daily_budget"]["used"] == {"product": 10}, (cursor, first)
    entities = s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t")
    assert entities == 3  # page one: 1 listing call + 3 x 3 detail calls = the whole budget
    assert first["checkpoint"] == s.checkpoint()

    s.call("PATCH", f"/knowledge-bases/{s.kb_id}/sources/{s.source_id}", expect=200,
           json={"config": {"document_policy": {"types": ["datasheet"]}, "page_size": 3, "product_daily_limit": 1000}})
    before_pages = [r.query.get("Page") for r in s.server.requests if r.path == "/v1/products"]
    second = s.sync("full")
    assert second["complete"] is True and second["stop_reason"] is None and second["source_revision"], second
    pages = [r.query.get("Page") for r in s.server.requests if r.path == "/v1/products"][len(before_pages):]
    assert pages == [["1"], ["2"]], "the resume starts at the stored page, not at the first"
    assert s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t") == 7
    assert "page" not in s.checkpoint()["cursor"]
    assert s.leaks() == []


def test_an_incremental_run_resumes_from_the_checkpoint_and_then_touches_only_what_changed(make_stack):
    s = make_stack(catalogue_parts(7), source_config={"product_daily_limit": 10, "page_size": 3})
    s.sync("incremental")  # one page, then the budget is spent
    assert s.checkpoint()["cursor"]["page"] == 1
    s.call("PATCH", f"/knowledge-bases/{s.kb_id}/sources/{s.source_id}", expect=200,
           json={"config": {"document_policy": {"types": ["datasheet"]}, "page_size": 3, "product_daily_limit": 1000}})
    done = s.sync("incremental")
    assert done["complete"] is True and s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t") == 7
    s.server.requests.clear()
    quiet = s.sync("incremental")
    assert (quiet["inserted"], quiet["updated"], quiet["failed"]) == (0, 0, 0) and quiet["unchanged"] == 7
    probe = "/v1/products/EXAMPLE-PART-001"  # the workflow's connection check
    assert [r.path for r in s.server.requests if r.path.startswith("/v1/products/") and r.path != probe] == [], \
        "no detail call when nothing changed"
    s.server.parts["CAT004DR"].generic = "CATG-renamed"
    s.server.requests.clear()
    changed = s.sync("incremental")
    assert changed["complete"] is True
    detail = {r.path.split("/")[3] for r in s.server.requests if r.path.startswith("/v1/products/")} - {"EXAMPLE-PART-001"}
    assert detail == {"CAT004DR"}


def test_cancelling_the_parent_mid_run_leaves_a_checkpoint_and_a_cancelled_child(make_stack):
    s = make_stack(catalogue_parts(25), pause=0.03, source_config={"page_size": 1})
    parent = s.start({"source_id": s.source_id, "mode": "full"})

    def segment_done():
        row = s.rows("SELECT checkpoint FROM kb_source_syncs WHERE tenant_id = :t AND parent_run_id = :p", p=parent)
        return row[0].checkpoint if row and row[0].checkpoint.get("cursor", {}).get("page") else None

    wait_for(segment_done, timeout=120, interval=0.05, what="a first segment to be checkpointed")
    cancelled = s.call("POST", f"/runs/{parent}/cancel", expect=(202,))
    assert cancelled.json()["status"] in ("failed", "needs_attention", "cancelled")
    run = s.wait(parent)
    assert run["status"] in ("cancelled", "failed")

    def child_ended():
        r = s.rows("SELECT y.run_id, r.status FROM kb_source_syncs y JOIN runs r ON r.tenant_id = y.tenant_id"
                   " AND r.id::text = y.run_id WHERE y.tenant_id = :t AND y.parent_run_id = :p", p=parent)
        return r[0] if r and r[0].status in TERMINAL else None

    child = wait_for(child_ended, timeout=60, what="the child sync to end")
    end = s.rows("SELECT data FROM execution_events WHERE run_id = :r AND event_key = 'end'", r=child.run_id)[0].data
    assert end["error_code"] == "run_cancelled", end  # a cancelled run is a failed run with this stable code
    checkpoint = s.rows("SELECT checkpoint FROM kb_source_syncs WHERE tenant_id = :t AND parent_run_id = :p", p=parent)[0].checkpoint
    assert checkpoint["cursor"]["page"] >= 1, "the cancelled run kept its cursor"
    assert 1 <= s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t") < 25
    # the next full run resumes the stored cursor and completes the walk
    s.server.requests.clear()
    resumed = s.sync("full")
    assert resumed["complete"] is True
    assert s.scalar("SELECT count(*) FROM kb_entities WHERE tenant_id = :t") == 25
    first_page = int(next(r.query["Page"][0] for r in s.server.requests if r.path == "/v1/products"))
    assert first_page >= 1


def test_another_tenant_gets_not_found_on_every_route_and_tool(make_stack, api, worker):
    s = make_stack(two_parts())
    out = s.sync("targeted", ["TIE2E1DR"])
    s.wait_ingested(1)
    other = auth_header(f"t_{uuid.uuid4().hex[:10]}", "mallory")
    kb, src, conn, run = s.kb_id, s.source_id, s.connection_id, out["run_id"]
    routes = [
        ("GET", f"/settings/connections/{conn}"), ("PATCH", f"/settings/connections/{conn}"),
        ("DELETE", f"/settings/connections/{conn}"), ("POST", f"/settings/connections/{conn}/check"),
        ("GET", f"/knowledge-bases/{kb}/sources"), ("GET", f"/knowledge-bases/{kb}/sources/{src}"),
        ("PATCH", f"/knowledge-bases/{kb}/sources/{src}"), ("POST", f"/knowledge-bases/{kb}/sources/{src}/sync"),
        ("GET", f"/knowledge-bases/{kb}/sources/{src}/syncs"), ("GET", f"/knowledge-bases/{kb}/sources/{src}/syncs/{run}"),
        ("POST", f"/runs/{run}/cancel"),
    ]
    bodies = {"PATCH": {"name": "x"}, "POST": {"mode": "full"}}
    for method, path in routes:
        r = api.request(method, f"{API}{path}", headers=other, json=bodies.get(method) if path.endswith(("/sync",)) or method == "PATCH" else None)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        assert not any(x in r.text for x in SENTINELS)
    assert api.get(f"{API}/settings/connections", headers=other).json()["items"] == []
    assert api.get(f"{API}/knowledge-bases/{kb}/documents", headers=other).status_code == 404
    # the tools: the other tenant's context sees no source, no connection, no run
    other_tenant = other["Authorization"].split(":")[1]
    for tool, args, code in (
        ("kb.source.get", {"source_id": src}, "source_not_found"),
        ("settings.connection.check", {"connection_id": conn}, "connection_not_found"),
        ("workflow.run.result", {"run_id": run}, "run_not_found"),
        ("workflow.run.wait", {"run_id": run, "wait_seconds": 1}, "run_not_found"),
        ("kb.source.sync", {"source_id": src, "mode": "full"}, "source_not_found"),
    ):
        result = _call_tool(worker, tool, other_tenant, **args)
        assert result["error"]["code"] == code, (tool, result)
        assert not any(x in json.dumps(result) for x in SENTINELS)
    assert s.scalar("SELECT count(*) FROM kb_source_syncs WHERE tenant_id = :t") == 1  # the refused sync started nothing


def _call_tool(worker, name: str, tenant: str, **kwargs: Any) -> dict[str, Any]:
    import asyncio

    from pydantic_ai import RunContext
    from pydantic_ai.usage import RunUsage

    deps = worker.tool_deps_for(str(uuid.uuid4()), tenant)
    ctx = RunContext(deps=deps, model=None, usage=RunUsage(), tool_call_id="call-1", tool_name=name)  # type: ignore[arg-type]
    return asyncio.run(worker.registry.get(name)(ctx, **kwargs))


def test_no_secret_value_or_token_reaches_events_rows_dbos_api_or_logs(make_stack, caplog, capfd):
    s = make_stack(two_parts())
    s.sync("targeted", ["TIE2E1DR", "TIE2E2DR"])
    s.wait_ingested(2)
    s.sync("full")
    assert s.server.tokens_issued >= 1  # the server did get (and accept) the real credentials
    assert s.leaks() == []
    logs = caplog.text + capfd.readouterr().out
    for sentinel in SENTINELS:
        assert sentinel not in logs


# ============================================================================== the §19 benchmark world


def _benchmark_report(rows: list[tuple[str, str]]) -> str:
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"| {k.ljust(width)} | {v} |" for k, v in rows)


def test_the_benchmark_world_through_the_real_workflow(make_stack, caplog, capfd):
    """Brief §19 through the published workflow, the real worker, the real stores and `eap.kb_ingest`: 10 products of
    2 families, 3 datasheets (one unchanged, one changed revision), one missing product, one transient 503 and one
    malformed response. Reports the table and holds the size caps of the design (step 32 KiB, checkpoint 8 KiB)."""
    sys.path.insert(0, str(REPO_ROOT / "packages" / "kb-sources-acme"))
    from kb_sources_ti_tests.benchmark import fixtures as bench

    server, names = bench.world()
    for part in server.parts.values():  # real PDFs: the ingest extracts and embeds them
        if part.datasheet is not None:
            part.datasheet = datasheet(part.orderable, 1)
    server.parts[bench.CHANGED].etag = '"old"'
    s = make_stack(server)

    # the previous sync: the unchanged and the changed-to-be datasheets are already stored
    previous = s.sync("targeted", [bench.UNCHANGED, bench.CHANGED])
    assert previous["documents_queued"] == 2 and previous["failed"] == 0
    s.wait_ingested(2)
    stored_versions = {d.document_key: d.latest_version for d in s.documents()}
    server.parts[bench.CHANGED].datasheet = datasheet(bench.CHANGED, 2)  # the changed revision
    server.parts[bench.CHANGED].etag = '"new"'
    server.requests.clear()
    caplog.clear()

    started = time.perf_counter()
    out = s.sync("targeted", names)
    elapsed = time.perf_counter() - started
    expected = bench.EXPECTED
    assert (out["discovered"], out["failed"], out["documents_queued"]) == (
        expected.discovered, expected.failed, expected.documents_queued)
    assert sorted(e["code"] for e in out["errors"]) == sorted(expected.error_codes)
    docs = s.wait_ingested(3)
    versions = {d.document_key: d.latest_version for d in docs}
    changed_keys = [k for k in versions if versions[k] > stored_versions.get(k, 0)]
    assert len(docs) == 3 and len(changed_keys) == 2, (stored_versions, versions)  # the new one and the changed one
    assert len({d.document_key for d in docs}) == 3  # duplicate documents: none

    api_calls = sum(1 for r in server.requests if r.host == "api.vendor.example" and "oauth" not in r.path)
    seen: dict[tuple, int] = {}
    for r in server.requests:
        if r.host == "api.vendor.example" and "oauth" not in r.path:
            key = (r.path, json.dumps(r.query, sort_keys=True))
            seen[key] = seen.get(key, 0) + 1
    retried = sum(n - 1 for n in seen.values())

    # idempotency: the second run over the stored parts is all unchanged and re-embeds nothing
    stored_parts = [n for n in names if n not in (bench.MALFORMED, bench.MISSING)]
    before = s.counts()
    again = s.sync("targeted", stored_parts)
    assert (again["inserted"], again["updated"], again["failed"], again["documents_queued"]) == (0, 0, 0, 0)
    assert again["unchanged"] == out["inserted"] + out["unchanged"]  # the six records of the previous sync were already stored
    assert s.counts() == before
    assert {d.document_key: d.latest_version for d in s.documents()} == versions

    # sizes: the checkpoint rows and every DBOS step output / workflow input of the sync (the root, its segment children)
    run_id = out["run_id"]
    checkpoint_bytes = s.scalar("SELECT octet_length(checkpoint::text) FROM kb_source_syncs WHERE tenant_id = :t AND run_id = :r", r=run_id)
    source_checkpoint = s.scalar("SELECT octet_length(last_checkpoint::text) FROM kb_sources WHERE tenant_id = :t AND id = :s")
    schema = s.rt.settings.dbos_system_schema
    from sqlalchemy import create_engine

    eng = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with eng.connect() as conn:
            step_out = conn.execute(text(f'SELECT coalesce(max(octet_length(output)), 0) FROM "{schema}".operation_outputs'
                                         " WHERE workflow_uuid = :r OR workflow_uuid LIKE :p"), {"r": run_id, "p": f"{run_id}:%"}).scalar_one()
            steps = conn.execute(text(f'SELECT count(*) FROM "{schema}".operation_outputs WHERE workflow_uuid = :r OR workflow_uuid LIKE :p'),
                                 {"r": run_id, "p": f"{run_id}:%"}).scalar_one()
    finally:
        eng.dispose()
    # DBOS keeps no step arguments, so the input of the largest call is computed: the request (mode, parts) plus the
    # checkpoint the root hands to a segment
    step_in = len(json.dumps({"mode": "targeted", "part_numbers": names}).encode()) + max(checkpoint_bytes, source_checkpoint)
    assert steps > 0 and step_out < 32 * 1024 and step_in < 32 * 1024
    assert max(checkpoint_bytes, source_checkpoint) < 8 * 1024

    # resume: a walk of the benchmark catalogue in one-page segments equals the single pass (adapter level in
    # kb_sources_ti_tests/benchmark; here the real stores): the budget stop of the full-walk test above is the resume proof.
    leaks = s.leaks()
    logs = caplog.text + capfd.readouterr().out
    assert leaks == [] and not any(x in logs for x in SENTINELS)

    records = out["inserted"] + out["updated"] + out["unchanged"]
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    table = _benchmark_report([
        ("parts requested / discovered / failed", f"{bench.PARTS} / {out['discovered']} / {out['failed']}"),
        ("errors", ", ".join(sorted(e["code"] for e in out["errors"]))),
        ("datasheets queued (new + changed) / unchanged skipped", f"{out['documents_queued']} / 1"),
        ("second run: inserted / updated / queued", f"{again['inserted']} / {again['updated']} / {again['documents_queued']}"),
        ("kb_documents versions of unchanged after rerun", "unchanged"),
        ("records per second (wall, incl. DBOS)", f"{records / elapsed:,.1f}"),
        ("API calls per product", f"{api_calls / bench.PARTS:.2f} ({api_calls} calls)"),
        ("retries", str(retried)),
        ("duplicate documents", "0"),
        ("largest DBOS step output / segment input (bytes)", f"{step_out} / {step_in} (< 32768)"),
        ("checkpoint: sync row / source row (bytes)", f"{checkpoint_bytes} / {source_checkpoint} (< 8192)"),
        ("process peak RSS (MiB)", f"{peak:.0f}"),
        ("secret-leak scan (api, tables, dbos, logs)", "clean"),
    ])
    print("\n§19 benchmark (real workflow)\n" + table)
    import logging

    logging.getLogger("benchmark").info("§19 benchmark (real workflow)\n%s", table)
