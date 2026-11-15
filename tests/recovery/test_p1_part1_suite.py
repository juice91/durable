"""RAG P1.A7: the part 1 suite, whole-system (docs/plans/2026-11-14-milestone-rag-p1.md, "P1.A7").

Everything goes through the real API and a real `python -m eap_worker` whose extractions run in the
real sandbox child (eap_worker.extract_sandbox). The worker-level suites (apps/worker/worker_tests:
test_extract_sandbox*.py, test_kb_extract_chunk*.py, test_kb_embed_split.py,
test_p1c_recovery_acceptance.py) enqueue through DBOSClient; this file adds the API hand-off (the
API decides the extractor site and sets `embed_split`) on top, with the RM.20 invariants of
test_rm_crash_matrix.assert_recovered_once.

| # | What | Proves |
|---|------|--------|
| H1 | The hostile corpus (PDF: corrupt, encrypted, image-only, nested arrays, a flate bomb; rst: deep nesting, a huge line, `include` / `raw :file:` / `csv-table :url:`; YAML: billion laughs, a python tag; JSON: deep nesting, malformed) uploaded over the API between good Markdown, PDF and YAML documents, with OTLP export on | each hostile document fails with its extraction code (run, `run_failed` event, document `error_code` / `error_reason`), or for a file-reading directive ingests without the file; PNG bytes as a PDF and a PDF as rst are refused at the API (415 `unsupported_content`); the worker and the API stay up, the good documents are `ready` and searchable; the search tester keeps the tool-run step list; another tenant gets 404 on every route of the knowledge base; no document text (in any encoding) in the API log, the worker log or any OTLP span or log export, nor in the runs' events and errors |
| X1 | kill -9 at `kb_extract_sandbox` / `kb_extract_chunk` during an API upload of a PDF, and at `kb_extract_chunk` for an rst file | recovery parses again or finds the committed rows; one `extracted` event; no duplicate chunk rows; the split embed steps (the API set the flag); RM.20 invariants; page citations on the API search |
| E1 | kill -9 at `kb_embed_after_call`, `kb_batch_before_commit`, `kb_batch_after_commit` on `embed:batch:1` during an API upload of Markdown | the call step re-calls the provider exactly once more after a kill inside it; a kill inside the write never calls the provider again (the `embedding call` log lines per batch key); one usage row per batch; RM.20 invariants |
"""

from __future__ import annotations

import base64
import json
import re
import sys
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import test_mcp_safety as mcp_safety
import test_rm_crash_matrix as rm_matrix
from conftest import REPO_ROOT, Harness, WorkerProcess, wait_for

sys.path.insert(0, str(REPO_ROOT / "packages" / "knowledge"))
from knowledge_tests.pdf_fixtures import body_lines, build_pdf, flate_bomb_pdf  # noqa: E402
from knowledge_tests.test_p1a3_pdf_acceptance import CATALOG, FONT, ONE_PAGE, PAGE, raw_pdf  # noqa: E402

API = "/api/v1"
BATCH = 4
WORKER_ENV = {**rm_matrix.WORKER_ENV, "EAP_KB_EMBED_BATCH_CHUNKS": str(BATCH)}
TOOL_RUN_STEPS = [
    "eap.start_tool_run", "eap.append_event", "eap.check_tool", "eap.append_event", "eap.tool_call",
    "eap.append_event", "eap.append_event", "eap.finish_run",
]
SPLIT_STEPS = {"eap.kb_embed_call", "eap.kb_embed_write"}


@pytest.fixture
def p1(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """The harness, with the in-process API seeing the same allowlists and batch size as the workers."""
    for key, value in WORKER_ENV.items():
        monkeypatch.setenv(key, value)
    return harness


# --------------------------------------------------------------------------- helpers


def create_kb(h: Harness, headers: dict[str, str] | None = None) -> str:
    r = h.client.post(f"{API}/knowledge-bases", json={"name": f"kb-{uuid.uuid4().hex[:8]}", "embedding_model": "test:hash"},
                      headers=headers or h.headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def put(h: Harness, kb_id: str, key: str, mime: str, body: str | bytes) -> Any:
    doc: dict[str, Any] = {"title": key, "mime_type": mime, "source_url": f"https://docs.example.com/{key}"}
    if isinstance(body, bytes):
        doc["content_base64"] = base64.b64encode(body).decode("ascii")
    else:
        doc["content"] = body
    return h.client.put(f"{API}/knowledge-bases/{kb_id}/documents/{key}", json=doc, headers=h.headers)


def put_ok(h: Harness, kb_id: str, key: str, mime: str, body: str | bytes) -> tuple[str, int, str]:
    r = put(h, kb_id, key, mime, body)
    assert r.status_code == 202, (key, r.status_code, r.text)
    data = r.json()
    h.run_ids.append(data["run_id"])
    return data["document"]["id"], data["document"]["latest_version"], data["run_id"]


def get_doc(h: Harness, kb_id: str, key: str) -> dict[str, Any]:
    r = h.client.get(f"{API}/knowledge-bases/{kb_id}/documents/{key}", headers=h.headers)
    assert r.status_code == 200, r.text
    return r.json()


def step_names(h: Harness, workflow_id: str) -> list[str]:
    return [name for _, name in h.dbos_steps(workflow_id)]


def embedding_calls(*logs: str) -> Counter[str]:
    """Provider calls per request key, from the workers' `embedding call ... key=<k>` log lines."""
    return Counter(m.group(1) for log in logs for m in re.finditer(r"embedding call .*?key=(\S+)", log))


def chunk_contents(h: Harness, doc_id: str) -> list[str]:
    return [r[0] for r in h.sql("SELECT content FROM kb_chunks WHERE tenant_id = :t AND document_id = :d",
                                t=h.tenant, d=doc_id)]


# --------------------------------------------------------------------------- H1: the hostile corpus


def _laughs(depth: int, fan: int) -> str:
    names = "abcdefghijklmnopqrstuvwxyz"
    out = [f"a: &a [{','.join(['x'] * fan)}]"]
    for p, c in zip(names[: depth - 1], names[1:depth], strict=True):
        out.append(f"{c}: &{c} [{','.join('*' + p for _ in range(fan))}]")
    return "\n".join(out) + "\n"


def hostile_corpus(mark: str) -> dict[str, tuple[str, str | bytes, tuple[str, ...]]]:
    """key -> (MIME type, body, the extraction codes it may fail with). Every text body carries `mark`."""
    page = PAGE[:-3] + b" /Resources << /Font << /F1 5 0 R >> >> >>"
    return {
        "corrupt.pdf": ("application/pdf", f"%PDF-1.7\n<html><script>{mark}</script></html>".encode(), ("extract_failed",)),
        "encrypted.pdf": ("application/pdf", build_pdf([[(f"{mark} secret page", 10), *body_lines(0)]], encrypt="pw"),
                          ("document_encrypted",)),
        "image-only.pdf": ("application/pdf", build_pdf([[], []], image_pages={0, 1}), ("no_extractable_text",)),
        "nested.pdf": ("application/pdf", raw_pdf([CATALOG, ONE_PAGE, page, b"[" * 300_000 + b"]" * 300_000, FONT]),
                       ("extract_limit",)),
        "flate-bomb.pdf": ("application/pdf", flate_bomb_pdf(), ("extract_limit", "extract_memory", "extract_timeout")),
        "nested.rst": ("text/x-rst", "- " * 4999 + mark + "\n", ("extract_limit",)),
        "long-line.rst": ("text/x-rst", "Title\n=====\n\n" + mark * 1500 + "\n", ("extract_limit",)),
        "laughs.yaml": ("application/yaml", f"# {mark}\n" + _laughs(11, 10), ("extract_limit",)),
        "python-tag.yaml": ("application/yaml", f"a: !!python/object/apply:os.system [{mark}]\n", ("extract_failed",)),
        "deep.json": ("application/json", "".join(f'{{"{mark}":' for _ in range(200)) + "1" + "}" * 200,
                      ("extract_limit",)),
        "malformed.json": ("application/json", f'{{"{mark}": 1,}}', ("extract_failed",)),
    }


def file_reading_rst(mark: str) -> str:
    """Directives that read local files or URLs: the hardened docutils settings must neither read
    nor fetch them (the document still ingests from its own text)."""
    return (
        f"Board guide\n===========\n\nThe {mark} board guide explains the UART pin mux in detail.\n\n"
        ".. include:: /etc/passwd\n\n"
        ".. raw:: html\n   :file: /etc/passwd\n\n"
        ".. csv-table:: Pins\n   :url: http://127.0.0.1:9/pins.csv\n\n"
        "Wiring\n------\n\nConnect the console cable before power on.\n"
    )


@pytest.fixture
def other_tenant(p1: Harness):
    """A second tenant's headers; its rows are deleted afterwards (the harness cleans only its own)."""
    from sqlalchemy import text

    tenant = f"{p1.tenant}o"
    yield {"Authorization": f"Bearer dev:{tenant}:mallory"}
    with p1.engine.begin() as conn:
        conn.execute(text("DELETE FROM execution_events WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = :t)"),
                     {"t": tenant})
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})


def test_h1_hostile_corpus_end_to_end(p1: Harness, other_tenant: dict[str, str], report: Callable[[str], None]) -> None:
    h = p1
    mark = f"zqh{uuid.uuid4().hex[:10]}x"  # in every hostile document's text
    good = f"zqg{uuid.uuid4().hex[:10]}y"  # in every good document's text
    corpus = hostile_corpus(mark)
    passwd = [line for line in Path("/etc/passwd").read_text().splitlines() if line and not line.startswith("#")][:3]

    with mcp_safety.OtlpCollector() as collector:
        env = {**WORKER_ENV, "OTEL_EXPORTER_OTLP_ENDPOINT": collector.url, "OTEL_BSP_SCHEDULE_DELAY": "200",
               "OTEL_BLRP_SCHEDULE_DELAY": "200", "EAP_KB_EXTRACT_CPU_SECONDS": "10"}
        api = h.start_api(extra_env=env)
        worker = h.start_worker("worker", extra_env=env)
        kb_id = create_kb(h)

        # A good document before, the corpus, more good documents after (all queued together).
        uploads: dict[str, tuple[str, int, str]] = {}
        uploads["before.md"] = put_ok(h, kb_id, "before.md", "text/markdown",
                                      f"# Before\n\nThe {good} handbook explains the console cable and the reset button.\n")
        for key, (mime, body, _codes) in corpus.items():
            uploads[key] = put_ok(h, kb_id, key, mime, body)
        uploads["directives.rst"] = put_ok(h, kb_id, "directives.rst", "text/x-rst", file_reading_rst(mark))
        uploads["after.pdf"] = put_ok(h, kb_id, "after.pdf", "application/pdf", build_pdf(
            [[(f"The {good} manual explains the boot switches.", 10), *body_lines(0)], body_lines(1)]))
        uploads["after.yaml"] = put_ok(h, kb_id, "after.yaml", "application/yaml",
                                       f"title: {good} UART binding\nproperties:\n  reg:\n    maxItems: 1\n")

        # Refused at the API, before any run: the bytes do not match the declared type.
        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 17
        for key, mime, body in (("png-as.pdf", "application/pdf", png), ("pdf-as.rst", "text/x-rst", f"%PDF-1.7 {mark}")):
            r = put(h, kb_id, key, mime, body)
            assert r.status_code == 415 and r.json()["code"] == "unsupported_content", (key, r.status_code, r.text)
            assert mark not in r.text

        runs = {key: h.wait_terminal(run_id, timeout=120) for key, (_, _, run_id) in uploads.items()}
        assert worker.proc.poll() is None, "the worker died during the corpus:\n" + worker.log()[-4000:]
        assert api.proc.poll() is None and h.client.get("/healthz").status_code == 200

        # Every hostile document failed with its code, and nothing of it is stored as chunks.
        codes: dict[str, str] = {}
        for key, (_mime, _body, allowed) in corpus.items():
            doc_id, _, run_id = uploads[key]
            run = runs[key]
            assert run["status"] == "failed", (key, run["status"], run["error"])
            end = h.events(run_id)[-1]
            assert end.type == "run_failed" and end.data["error_code"] == "extraction_failed", (key, end.data)
            codes[key] = end.data["extraction_code"]
            assert codes[key] in allowed, (key, codes[key])
            doc = get_doc(h, kb_id, key)
            assert doc["status"] == "failed" and doc["active_version"] is None, (key, doc)
            assert (doc["error_code"], doc["error_reason"]) == ("extraction_failed", codes[key]), (key, doc)
            assert doc["extraction"] is None
            assert chunk_contents(h, doc_id) == [], key

        # The good documents (and the directive file, from its own text only) are ready.
        for key in ("before.md", "directives.rst", "after.pdf", "after.yaml"):
            assert runs[key]["status"] == "completed", (key, runs[key]["error"], h.worker_logs())
            doc = get_doc(h, kb_id, key)
            assert doc["status"] == "ready" and doc["chunk_count"] >= 1, (key, doc)
        directives = "\n".join(chunk_contents(h, uploads["directives.rst"][0]))
        assert "Connect the console cable" in directives
        for line in passwd:
            assert line not in directives, "an include / raw :file: directive read /etc/passwd"
        assert get_doc(h, kb_id, "after.pdf")["extraction"]["pages"] == 2

        # The search tester over the knowledge base: good documents only, and the tool-run layout.
        r = h.client.post(f"{API}/knowledge-bases/{kb_id}/search", json={"query": "manual boot switches handbook", "top_k": 20},
                          headers=h.headers)
        assert r.status_code == 200, r.text
        search = r.json()
        h.run_ids.append(search["run_id"])
        assert search["status"] == "completed", search
        hit_keys = {hit["document_key"] for hit in search["result"]["hits"]}
        assert hit_keys and hit_keys <= {"before.md", "directives.rst", "after.pdf", "after.yaml"}, hit_keys
        assert step_names(h, search["run_id"]) == TOOL_RUN_STEPS

        # Another tenant: 404 on every route of this knowledge base, and its own search never sees it.
        other = other_tenant
        base = f"{API}/knowledge-bases/{kb_id}"
        for method, url, body in (
            ("GET", base, None), ("GET", f"{base}/documents", None),
            ("GET", f"{base}/documents/after.pdf", None), ("GET", f"{base}/documents/after.pdf?include_content=true", None),
            ("PUT", f"{base}/documents/x.pdf", {"content_base64": base64.b64encode(b"%PDF-1.7\n").decode(),
                                               "mime_type": "application/pdf"}),
            ("POST", f"{base}/documents/after.pdf/reextract", {}), ("POST", f"{base}/documents/after.pdf/reingest", None),
            ("POST", f"{base}/reextract", {"dry_run": True}), ("POST", f"{base}/search", {"query": "manual"}),
            ("DELETE", f"{base}/documents/after.pdf", None),
        ):
            got = h.client.request(method, url, json=body, headers=other)
            assert got.status_code == 404, (method, url, got.status_code, got.text)
            assert good not in got.text
        other_kb = create_kb(h, other)
        other_search = h.client.post(f"{API}/knowledge-bases/{other_kb}/search", json={"query": "manual boot switches"},
                                     headers=other)
        assert other_search.status_code == 200 and other_search.json()["result"]["hits"] == [], other_search.text
        assert get_doc(h, kb_id, "after.pdf")["status"] == "ready"  # untouched by the probes

        wait_for(lambda: all(r_id in {s[2].get("eap.run_id") for s in collector.spans()}
                             for r_id in (uploads["before.md"][2], uploads["corrupt.pdf"][2], uploads["after.pdf"][2])),
                 timeout=30, what="the worker's kb_ingest spans at the collector")
        worker.stop()
        api.stop()

    # No document text, in any encoding, in logs, spans, OTLP logs, events or errors.
    secrets = [mark, good]
    api_log, worker_log = api.log(), worker.log()
    assert "PUT /api/v1/knowledge-bases" in api_log and "EAP_WORKER_READY" in worker_log
    mcp_safety.assert_no_secret("API process log", api_log, secrets)
    mcp_safety.assert_no_secret("worker process log", worker_log, secrets)
    spans = collector.spans()
    assert {"eap-api", "eap-worker"} <= {s[0] for s in spans}
    for path, body in collector.payloads:
        mcp_safety.assert_no_secret(f"OTLP export {path}", body, secrets)
    assert collector.bodies("logs"), "no OTLP log export arrived"
    ingest_runs = [run_id for key, (_, _, run_id) in uploads.items()]
    stored = h.sql("SELECT row_to_json(e)::text FROM execution_events e WHERE run_id::text = ANY(:r)",
                   r=ingest_runs)
    errors = h.sql("SELECT coalesce(error, '') FROM runs WHERE id::text = ANY(:r)", r=ingest_runs)
    doc_errors = h.sql("SELECT coalesce(error, '') FROM kb_documents WHERE tenant_id = :t", t=h.tenant)
    mcp_safety.assert_no_secret("ingest events", repr([r[0] for r in stored]), secrets)
    mcp_safety.assert_no_secret("run and document errors", repr([r[0] for r in [*errors, *doc_errors]]), secrets)
    report(f"P1.A7 H1: {len(corpus)} hostile documents failed with {dict(sorted(Counter(codes.values()).items()))}, "
           f"2 refused at the API, 4 good documents ready; worker and API up; {len(collector.payloads)} OTLP exports "
           f"({len(spans)} spans) and both process logs free of document text")


# --------------------------------------------------------------------------- X1: crashes in eap.kb_extract_chunk


def pdf_manual(tag: str) -> bytes:
    """Six text pages, so the PDF makes several chunks."""
    return build_pdf([[(f"Chapter {i} of the {tag} manual", 14), *body_lines(i, count=10)] for i in range(6)],
                     outline=[(f"Chapter {i}", i, None) for i in range(6)])


def rst_guide(tag: str) -> str:
    sections = [f"{tag} guide\n{'=' * (len(tag) + 6)}\n\nIntro text.\n"]
    for i in range(8):
        words = " ".join(f"w{tag}{i}x{j}" for j in range(80))
        sections.append(f"Section {i}\n{'-' * (9 + len(str(i)))}\n\nSection {i} explains {words}.\n")
    return "\n".join(sections)


EXTRACT_CASES = {
    # name: (crash point, MIME type)
    "pdf-sandbox": ("kb_extract_sandbox", "application/pdf"),
    "pdf-rows": ("kb_extract_chunk", "application/pdf"),
    "rst-rows": ("kb_extract_chunk", "text/x-rst"),
}


@pytest.mark.parametrize("case", list(EXTRACT_CASES))
def test_x1_crash_in_kb_extract_chunk_through_the_api(p1: Harness, report: Callable[[str], None], case: str) -> None:
    h = p1
    crash_at, mime = EXTRACT_CASES[case]
    kb_id = create_kb(h)
    first = rm_matrix.start_worker(h, "worker1-crashing", crash_at=crash_at)
    tag = uuid.uuid4().hex[:6]
    body: str | bytes = pdf_manual(tag) if mime == "application/pdf" else rst_guide(tag)
    doc_id, version, run_id = put_ok(h, kb_id, "manual.pdf" if mime == "application/pdf" else "guide.rst", mime, body)
    rm_matrix.crash(h, first, crash_at)
    before = h.events(run_id)
    usage_before = rm_matrix.usage_rows(h, run_id)
    rows_at_kill = h.sql("SELECT count(*) FROM kb_chunks WHERE tenant_id = :t AND document_id = :d", t=h.tenant, d=doc_id)[0][0]
    if crash_at == "kb_extract_sandbox":
        assert rows_at_kill == 0 and [e.event_key for e in before] == ["start"]
    else:  # the rows and the extracted event committed; the step checkpoint did not
        assert rows_at_kill > 0 and [e.event_key for e in before] == ["start", f"extract:{version}"]
    assert usage_before == {}

    rm_matrix.recover(h, run_id)
    stats = h.sql(
        "SELECT count(*) AS n, count(DISTINCT ordinal) AS ordinals, count(embedding) AS embedded FROM kb_chunks"
        " WHERE tenant_id = :t AND document_id = :d", t=h.tenant, d=doc_id)[0]
    n = stats.n
    assert n >= 2 and stats.ordinals == n and stats.embedded == n, stats
    if rows_at_kill:
        assert n == rows_at_kill  # the committed rows were kept, never duplicated or replaced
    batch_keys = [f"embed:batch:{i}" for i in range((n + BATCH - 1) // BATCH)]
    events = rm_matrix.assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=["start", f"extract:{version}", "chunk:0", *batch_keys, "finalize:0", "end"],
        expected_requests={k: ("test:hash", "embedding") for k in batch_keys},
    )
    [extracted] = [e for e in events if e.type == "extracted"]
    assert extracted.data["extractor"] == ("pdf" if mime == "application/pdf" else "rst")
    assert tag not in json.dumps(extracted.data)
    names = set(step_names(h, run_id))
    assert "eap.kb_extract_chunk" in names and SPLIT_STEPS <= names and "eap.kb_embed_batch" not in names
    doc = get_doc(h, kb_id, "manual.pdf" if mime == "application/pdf" else "guide.rst")
    assert (doc["status"], doc["active_version"], doc["chunk_count"]) == ("ready", version, n)
    assert doc["extraction"]["extractor"] == extracted.data["extractor"]
    hits = rm_matrix.search(h, kb_id, f"Chapter 3 of the {tag} manual" if mime == "application/pdf" else f"Section 3 w{tag}3x1")
    assert hits
    if mime == "application/pdf":
        assert all(isinstance(hit.get("page"), int) and "#page=" in (hit["url"] or "") for hit in hits), hits
    report(f"P1.A7 X1 {case} ({crash_at}): {n} chunks, rows at the kill {rows_at_kill}, one extracted event, "
           f"{len(batch_keys)} split batches")


# --------------------------------------------------------------------------- E1: crashes in the embed split


EMBED_CASES = {
    # name: (crash point, provider calls for embed:batch:1 in all)
    "in-the-call": ("kb_embed_after_call", 2),
    "write-before-commit": ("kb_batch_before_commit", 1),
    "write-after-commit": ("kb_batch_after_commit", 1),
}


@pytest.mark.parametrize("case", list(EMBED_CASES))
def test_e1_crash_in_the_embed_split_through_the_api(p1: Harness, report: Callable[[str], None], case: str) -> None:
    h = p1
    crash_at, calls_b1 = EMBED_CASES[case]
    kb_id = create_kb(h)
    first = rm_matrix.start_worker(h, "worker1-crashing", crash_at=crash_at, event_key="embed:batch:1")
    doc_id, version, run_id = put_ok(h, kb_id, "handbook.md", "text/markdown", rm_matrix.markdown_doc(10, tag="split"))
    rm_matrix.crash(h, first, crash_at)
    before = h.events(run_id)
    usage_before = rm_matrix.usage_rows(h, run_id)
    committed = {"embed:batch:0", "embed:batch:1"} if crash_at == "kb_batch_after_commit" else {"embed:batch:0"}
    assert set(usage_before) == committed, sorted(usage_before)
    at_kill = step_names(h, run_id)
    # The call of batch 1 is checkpointed unless the kill was inside it.
    assert at_kill.count("eap.kb_embed_call") == (1 if crash_at == "kb_embed_after_call" else 2), at_kill
    assert "eap.kb_embed_batch" not in at_kill

    second: WorkerProcess = rm_matrix.recover(h, run_id)
    stats = h.sql(
        "SELECT count(*) AS n, count(DISTINCT ordinal) AS ordinals, count(embedding) AS embedded FROM kb_chunks"
        " WHERE tenant_id = :t AND document_id = :d", t=h.tenant, d=doc_id)[0]
    n = stats.n
    assert n >= 10 and stats.ordinals == n and stats.embedded == n, stats
    batch_keys = [f"embed:batch:{i}" for i in range((n + BATCH - 1) // BATCH)]
    rm_matrix.assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=["start", "chunk:0", *batch_keys, "finalize:0", "end"],
        expected_requests={k: ("test:hash", "embedding") for k in batch_keys},
    )
    calls = embedding_calls(first.log(), second.log())
    assert calls == Counter({k: (calls_b1 if k == "embed:batch:1" else 1) for k in batch_keys}), calls
    names = step_names(h, run_id)
    assert names.count("eap.kb_embed_write") == len(batch_keys) and "eap.kb_embed_batch" not in names
    doc = get_doc(h, kb_id, "handbook.md")
    assert (doc["status"], doc["active_version"], doc["chunk_count"]) == ("ready", version, n)
    report(f"P1.A7 E1 {case} ({crash_at}): {n} chunks, {len(batch_keys)} batches, provider calls {dict(calls)}")
