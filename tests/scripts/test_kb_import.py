"""scripts/kb_import.py (RAG P1.A6): the directory import CLI, driven through an in-memory fake API
(no server). `FakeApi` mimics the knowledge routes the importer uses: GET the knowledge base, the
paged document list, PUT (unchanged / created / new version, by content hash, MIME type, title,
source URL and metadata), POST reingest and DELETE."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import sys
import os
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kb_import.py"
_spec = importlib.util.spec_from_file_location("kb_import", _SCRIPT)
assert _spec is not None and _spec.loader is not None
kb_import = importlib.util.module_from_spec(_spec)
sys.modules["kb_import"] = kb_import
_spec.loader.exec_module(kb_import)

ApiResponse = kb_import.ApiResponse
KB = "kb-1"
TOKEN = "dev:acme:secret-user-7f3a"


class FakeApi:
    """The knowledge routes, in memory. `fail_keys` maps a document key to an (status, detail) the
    PUT answers; `transient` makes the next N PUTs answer 503; `delay` holds each request so the
    concurrency bound can be observed (`max_in_flight`)."""

    def __init__(self, *, page_size: int = 200, delay: float = 0.0) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any, Any]] = []
        self.fail_keys: dict[str, tuple[int, Any]] = {}
        self.transient = 0
        self.page_size = page_size
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()
        self.kb_exists = True

    def request(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None):
        with self._lock:
            self.requests.append((method, path, json, params))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            with self._lock:
                return self._handle(method, path, json, params or {})
        finally:
            with self._lock:
                self.in_flight -= 1

    def _handle(self, method: str, path: str, body: Any, params: dict[str, Any]) -> ApiResponse:
        parts = path.strip("/").split("/")
        assert parts[0] == "knowledge-bases"
        if urllib.parse.unquote(parts[1]) != KB or not self.kb_exists:
            return ApiResponse(404, {"detail": "Knowledge base not found"})
        if len(parts) == 2 and method == "GET":
            return ApiResponse(200, {"id": KB, "name": "kb"})
        if len(parts) == 3 and method == "GET":
            keys = sorted(self.docs)
            after = params.get("after")
            if after:
                keys = [k for k in keys if k > after]
            limit = min(int(params.get("limit", 50)), self.page_size)
            page = keys[:limit]
            return ApiResponse(200, {
                "items": [self.docs[k] for k in page],
                "next_after": page[-1] if len(page) == limit and len(keys) > limit else None,
            })
        key = urllib.parse.unquote(parts[3])
        assert "/" not in key, "a document key is one path segment"
        if len(parts) == 5 and parts[4] == "reingest" and method == "POST":
            self.docs[key]["status"] = "pending"
            return ApiResponse(202, {"run_id": body["run_id"], "status": "queued"})
        if method == "DELETE":
            return ApiResponse(204, None) if self.docs.pop(key, None) is not None else ApiResponse(404, {"detail": "Document not found"})
        assert method == "PUT"
        if self.transient:
            self.transient -= 1
            return ApiResponse(503, {"detail": "busy", "run_id": body.get("run_id")})
        if key in self.fail_keys:
            status, detail = self.fail_keys[key]
            return ApiResponse(status, {"detail": detail})
        assert (body.get("content") is None) != (body.get("content_base64") is None)
        if body.get("content") is not None:
            if "\x00" in body["content"]:
                return ApiResponse(415, {"detail": "the request body contains a NUL character"})
            digest = hashlib.sha256(body["content"].encode()).hexdigest()
        else:
            digest = hashlib.sha256(base64.b64decode(body["content_base64"])).hexdigest()
        title = (body.get("title") or "").strip() or key
        fields = {
            "content_hash": digest,
            "mime_type": body["mime_type"],
            "title": title,
            "source_url": body.get("source_url"),
            "metadata": body.get("metadata") or {},
        }
        doc = self.docs.get(key)
        if doc is not None and all(doc[k] == v for k, v in fields.items()):
            return ApiResponse(200, {"unchanged": True, "created": False, "document": doc, "run_id": None})
        created = doc is None
        version = 1 if created else doc["latest_version"] + 1
        self.docs[key] = {"document_key": key, **fields, "latest_version": version, "status": "pending"}
        return ApiResponse(202, {"unchanged": False, "created": created, "document": self.docs[key], "run_id": body["run_id"]})

    def puts(self) -> list[tuple[str, dict[str, Any]]]:
        return [(urllib.parse.unquote(p.rsplit("/", 1)[1]), b) for m, p, b, _ in self.requests if m == "PUT"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "sdk"
    (root / "docs" / "devices" / "AM62X").mkdir(parents=True)
    (root / "docs" / "devices" / "AM64X").mkdir(parents=True)
    (root / "manuals").mkdir()
    (root / "bindings").mkdir()
    (root / ".git").mkdir()
    (root / "docs" / "index.rst").write_text("Intro\n=====\n\nHello.\n")
    (root / "docs" / "devices" / "AM62X" / "linux.rst").write_text("AM62x\n=====\n\nBoot it.\n")
    (root / "docs" / "devices" / "AM64X" / "linux.rst").write_text("AM64x\n=====\n\nBoot it too.\n")
    (root / "docs" / "guide.md").write_text("# Guide\n\nRead me.\n")
    (root / "manuals" / "trm.pdf").write_bytes(b"%PDF-1.7\n%binary\x00\xff\n")
    (root / "bindings" / "uart.yaml").write_text("title: UART\nproperties:\n  reg: {}\n")
    (root / "bindings" / "pins.JSON").write_text('{"pins": [1, 2]}')
    (root / "notes.txt").write_text("plain text\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n....")
    (root / "Makefile").write_text("all:\n")
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".hidden.md").write_text("# hidden\n")
    return root


def run_cli(args: list[str], api: FakeApi, *, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    seen: dict[str, str] = {}

    def factory(url: str, token: str) -> FakeApi:
        seen["url"], seen["token"] = url, token
        return api

    env = {"EAP_API_TOKEN": TOKEN} if environ is None else environ
    code = kb_import.main(args, client_factory=factory, environ=env, sleep=lambda s: None, out=out, err=err)
    api.seen = seen  # type: ignore[attr-defined]
    return code, out.getvalue(), err.getvalue()


# ----------------------------------------------------------------------------------- keys and types


def test_keys_from_relative_paths_and_mime_types_from_extensions(tree: Path) -> None:
    api = FakeApi()
    code, out, err = run_cli([str(tree), "--kb", KB], api)
    assert code == 0, err
    sent = dict(api.puts())
    assert set(sent) == {
        "docs~index.rst",
        "docs~devices~AM62X~linux.rst",
        "docs~devices~AM64X~linux.rst",
        "docs~guide.md",
        "manuals~trm.pdf",
        "bindings~uart.yaml",
        "bindings~pins.JSON",
        "notes.txt",
    }
    mime = {k: b["mime_type"] for k, b in sent.items()}
    assert mime["docs~index.rst"] == "text/x-rst"
    assert mime["docs~guide.md"] == "text/markdown"
    assert mime["manuals~trm.pdf"] == "application/pdf"
    assert mime["bindings~uart.yaml"] == "application/yaml"
    assert mime["bindings~pins.JSON"] == "application/json"  # extension case ignored
    assert mime["notes.txt"] == "text/plain"
    # Binary as base64 (exact bytes), text as content; the title is the relative path.
    assert base64.b64decode(sent["manuals~trm.pdf"]["content_base64"]) == (tree / "manuals" / "trm.pdf").read_bytes()
    assert "content" not in sent["manuals~trm.pdf"]
    assert sent["docs~index.rst"]["content"].startswith("Intro")
    assert sent["docs~devices~AM62X~linux.rst"]["title"] == "docs/devices/AM62X/linux.rst"
    # No metadata or source URL unless asked for; every PUT carries a run_id.
    # No map metadata or source URL unless asked for; every PUT carries a run_id and the importer's stamp.
    assert all(b["metadata"] == kb_import.stamp("") and "source_url" not in b and b["run_id"] for b in sent.values())
    assert "created 8" in out
    assert "unsupported type 2" in out  # logo.png (part 2) and Makefile
    assert "hidden" in out


def test_key_prefix_and_long_keys(tmp_path: Path) -> None:
    deep = tmp_path / "t" / ("d" * 130) / ("e" * 130)
    deep.mkdir(parents=True)
    (deep / "x.md").write_text("# x\n")
    api = FakeApi()
    code, _, err = run_cli([str(tmp_path / "t"), "--kb", KB, "--key-prefix", "sdk~"], api)
    assert code == 0, err
    (key, _), = api.puts()
    assert key.startswith("sdk~" + "d" * 130 + "~")
    assert len(key) == 256
    assert key == kb_import.document_key(f"{'d' * 130}/{'e' * 130}/x.md", "sdk~")  # stable
    assert kb_import.document_key("a/b.md", "sdk~") == "sdk~a~b.md"


def test_key_collision_fails_both_files(tmp_path: Path) -> None:
    root = tmp_path / "t"
    (root / "a").mkdir(parents=True)
    (root / "a" / "b.md").write_text("# one\n")
    (root / "a~b.md").write_text("# two\n")
    api = FakeApi()
    code, out, _ = run_cli([str(root), "--kb", KB], api)
    assert code == 1
    assert api.puts() == []
    assert out.count("FAILED") == 2 and "is also the key of" in out


def test_include_and_exclude_globs(tree: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli(
        [str(tree), "--kb", KB, "--include", "docs/**", "--include", "*.pdf", "--exclude", "**/AM64X/**"], api
    )
    assert code == 0, err
    assert {k for k, _ in api.puts()} == {
        "docs~index.rst", "docs~devices~AM62X~linux.rst", "docs~guide.md", "manuals~trm.pdf"
    }


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.rst", "a/b/c.rst", True),
        ("docs/*.rst", "docs/a.rst", True),
        ("docs/*.rst", "docs/x/a.rst", False),
        ("docs/**/*.rst", "docs/a.rst", True),
        ("docs/**/*.rst", "docs/x/y/a.rst", True),
        ("docs/**", "docs/x/y/a.rst", True),
        ("docs/**", "other/a.rst", False),
        ("**/AM6?X/**", "d/AM62X/a.rst", True),
        ("file[0-9].md", "file7.md", True),
        ("file[!0-9].md", "file7.md", False),
    ],
)
def test_glob_semantics(pattern: str, path: str, expected: bool) -> None:
    assert bool(kb_import.glob_regex(pattern).match(path)) is expected


# ----------------------------------------------------------------------------------- metadata map


def test_metadata_map_first_matching_glob_wins(tree: Path, tmp_path: Path) -> None:
    map_file = tmp_path / "map.yaml"
    map_file.write_text(
        "docs/devices/AM62X/**: {device: am62x, family: sitara}\n"
        "docs/devices/**: {device: generic}\n"
        "'*.pdf': {kind: manual, pages_known: false, rev: 3}\n"
    )
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--metadata-map", str(map_file)], api)
    assert code == 0, err
    sent = dict(api.puts())
    st = kb_import.stamp("")
    assert sent["docs~devices~AM62X~linux.rst"]["metadata"] == {"device": "am62x", "family": "sitara", **st}  # not merged
    assert sent["docs~devices~AM64X~linux.rst"]["metadata"] == {"device": "generic", **st}
    assert sent["manuals~trm.pdf"]["metadata"] == {"kind": "manual", "pages_known": False, "rev": 3, **st}
    assert sent["docs~guide.md"]["metadata"] == st


def test_metadata_map_rules_form(tmp_path: Path) -> None:
    map_file = tmp_path / "map.yaml"
    map_file.write_text("rules:\n  - match: '*.md'\n    metadata: {a: 1}\n  - match: '**'\n    metadata: {a: 2}\n")
    mm = kb_import.load_metadata_map(map_file)
    assert mm.lookup("x/y.md") == {"a": 1}
    assert mm.lookup("x/y.rst") == {"a": 2}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("'*.md': {a: [1, 2]}\n", "flat values only"),
        ("'*.md': {a: {b: 1}}\n", "flat values only"),
        ("'*.md': {a: null}\n", "flat values only"),
        ("'*.md': {eap: x}\n", "reserved"),
        ("'*.md': [a]\n", "must be a mapping"),
        ("- a\n", "expected a mapping"),
        ("'*.md': {" + ", ".join(f"k{i}: {i}" for i in range(15)) + "}\n", "at most 14"),
        ("a: [unclosed\n", "not valid YAML"),
    ],
)
def test_metadata_map_rejects_non_flat_values(tree: Path, tmp_path: Path, content: str, message: str) -> None:
    map_file = tmp_path / "map.yaml"
    map_file.write_text(content)
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--metadata-map", str(map_file)], api)
    assert code == 2
    assert message in err
    assert api.requests == []  # stopped before any HTTP


# ----------------------------------------------------------------------------- source-URL template


def test_source_url_template(tree: Path) -> None:
    (tree / "docs" / "with space.md").write_text("# s\n")
    api = FakeApi()
    template = "https://docs.example.com/sdk/{path_no_ext}.html?f={name}&d={dir}&e={ext}&s={stem}"
    code, _, err = run_cli([str(tree), "--kb", KB, "--source-url-template", template], api)
    assert code == 0, err
    sent = dict(api.puts())
    assert sent["docs~devices~AM62X~linux.rst"]["source_url"] == (
        "https://docs.example.com/sdk/docs/devices/AM62X/linux.html?f=linux.rst&d=docs/devices/AM62X&e=rst&s=linux"
    )
    assert sent["notes.txt"]["source_url"] == "https://docs.example.com/sdk/notes.html?f=notes.txt&d=&e=txt&s=notes"
    assert sent["docs~with space.md"]["source_url"].startswith("https://docs.example.com/sdk/docs/with%20space.html")


def test_source_url_template_path_and_key() -> None:
    assert kb_import.render_source_url("https://x/{path}#{key}", "a/b.rst", "p~a~b.rst") == "https://x/a/b.rst#p~a~b.rst"


@pytest.mark.parametrize(
    ("template", "message"),
    [("https://x/{nope}", "unknown placeholder"), ("ftp://x/{path}", "http:// or https://"), ("https://x/{path", "not a valid")],
)
def test_bad_source_url_template(tree: Path, template: str, message: str) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--source-url-template", template], api)
    assert code == 2 and message in err and api.requests == []


# ------------------------------------------------------------------------------- dry run and report


def test_dry_run_counts_and_estimate(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    (tree / "docs" / "guide.md").write_text("# Guide\n\nChanged.\n")
    (tree / "docs" / "new.md").write_text("# New\n")
    (tree / "notes.txt").unlink()
    api.requests.clear()
    code, out, err = run_cli([str(tree), "--kb", KB, "--dry-run", "--delete-missing", "--delete-scope", "all"], api)
    assert code == 0, err
    assert {m for m, *_ in api.requests} == {"GET"}  # nothing uploaded or deleted
    assert "dry run" in out
    assert "files by format: json 1, markdown 2, pdf 1, rst 3, yaml 1" in out
    assert "would create 1" in out and "would start a new version 1" in out and "would be unchanged 6" in out
    assert "missing from the tree: 1 (would be deleted)" in out and "would delete notes.txt" in out
    assert "notes.txt" in api.docs


def test_dry_run_metadata_change_counts_as_update(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    map_file = tmp_path / "map.yaml"
    map_file.write_text("'*.pdf': {kind: manual}\n")
    code, out, _ = run_cli([str(tree), "--kb", KB, "--dry-run", "--metadata-map", str(map_file)], api)
    assert code == 0 and "would start a new version 1" in out and "would be unchanged 7" in out


def test_dry_run_without_token_counts_files_only(tree: Path) -> None:
    api = FakeApi()
    code, out, _ = run_cli([str(tree), "--kb", KB, "--dry-run"], api, environ={})
    assert code == 0
    assert api.requests == [] and "would upload 8" in out and "no change estimate" in out


def test_report_unchanged_new_version_and_failed(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    (tree / "docs" / "guide.md").write_text("# Guide\n\nv2\n")
    (tree / "docs" / "nul.md").write_text("# bad\x00\n")
    (tree / "docs" / "latin1.md").write_bytes(b"# caf\xe9\n")
    (tree / "docs" / "empty.rst").write_text("   \n")
    api.fail_keys["bindings~uart.yaml"] = (413, "the document is too large (9 bytes; max 1 bytes for application/yaml)")
    api.fail_keys["manuals~trm.pdf"] = (422, [{"msg": "Value error, bad metadata", "loc": ["body"]}])
    report_path = tmp_path / "report.json"
    code, out, _ = run_cli([str(tree), "--kb", KB, "--report-json", str(report_path)], api)
    assert code == 1
    assert "new version 1" in out and "unchanged 5" in out and "failed 5" in out
    assert "FAILED docs/nul.md: HTTP 415: the request body contains a NUL character" in out
    assert "FAILED docs/latin1.md: not UTF-8 text" in out
    assert "FAILED docs/empty.rst: the file has only whitespace" in out
    assert "FAILED bindings/uart.yaml: HTTP 413: the document is too large" in out
    assert "FAILED manuals/trm.pdf: HTTP 422: Value error, bad metadata" in out
    report = json.loads(report_path.read_text())
    assert report["counts"] == {"failed": 5, "new_version": 1, "unchanged": 5}
    by_key = {d["key"]: d for d in report["documents"]}
    assert by_key["docs~guide.md"]["result"] == "new_version" and by_key["docs~guide.md"]["run_id"]


def test_second_run_reports_every_file_unchanged(tree: Path) -> None:
    api = FakeApi()
    code, out, _ = run_cli([str(tree), "--kb", KB], api)
    assert code == 0 and "created 8" in out
    code, out, _ = run_cli([str(tree), "--kb", KB], api)
    assert code == 0
    assert "documents: unchanged 8" in out
    assert "missing" not in out


def test_unchanged_failed_documents_and_retry_failed(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    api.docs["docs~guide.md"]["status"] = "failed"
    code, out, _ = run_cli([str(tree), "--kb", KB], api)
    assert code == 0 and "unchanged but their last ingest failed: 1" in out
    assert not any(m == "POST" for m, *_ in api.requests)
    code, out, _ = run_cli([str(tree), "--kb", KB, "--retry-failed"], api)
    assert code == 0 and "re-ingest started 1" in out
    posts = [p for m, p, *_ in api.requests if m == "POST"]
    assert posts == [f"/knowledge-bases/{KB}/documents/docs~guide.md/reingest"]


def test_transient_503_retried_with_the_same_run_id(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "a.md").write_text("# a\n")
    api = FakeApi()
    api.transient = 2
    code, out, _ = run_cli([str(root), "--kb", KB], api)
    assert code == 0 and "created 1" in out
    run_ids = {b["run_id"] for _, b in api.puts()}
    assert len(api.puts()) == 3 and len(run_ids) == 1


def test_unknown_knowledge_base(tree: Path) -> None:
    api = FakeApi()
    api.kb_exists = False
    code, _, err = run_cli([str(tree), "--kb", KB], api)
    assert code == 2 and "not found" in err and api.puts() == []


def test_document_list_is_paged(tree: Path) -> None:
    api = FakeApi(page_size=3)
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    code, out, _ = run_cli([str(tree), "--kb", KB, "--dry-run"], api)
    assert code == 0 and "would be unchanged 8" in out
    lists = [p for m, p, _, q in api.requests if m == "GET" and p.endswith("/documents")]
    assert len(lists) >= 3


# ------------------------------------------------------------------------------- --delete-missing


def test_missing_documents_are_reported_but_kept_without_the_flag(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    (tree / "notes.txt").unlink()
    code, out, _ = run_cli([str(tree), "--kb", KB], api)
    assert code == 0
    assert "missing from the tree: 1 (kept; pass --delete-missing to delete)" in out
    assert "notes.txt" in api.docs
    assert not any(m == "DELETE" for m, *_ in api.requests)


def test_delete_missing_deletes_only_with_the_flag_and_within_the_prefix(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk~"], api)[0] == 0
    api.docs["other~kept.md"] = {"document_key": "other~kept.md", "content_hash": "x", "mime_type": "text/markdown",
                                 "title": "t", "source_url": None, "metadata": {}, "latest_version": 1, "status": "ready"}
    (tree / "notes.txt").unlink()
    code, out, _ = run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk~", "--delete-missing"], api)
    assert code == 0
    assert "missing from the tree: 1; deleted 1" in out
    deletes = [p for m, p, *_ in api.requests if m == "DELETE"]
    assert deletes == [f"/knowledge-bases/{KB}/documents/sdk~notes.txt"]
    assert "other~kept.md" in api.docs  # outside the prefix: never considered missing


def test_delete_missing_keeps_files_that_failed_locally(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    (tree / "notes.txt").write_bytes(b"\xff\xfe broken")
    code, out, _ = run_cli([str(tree), "--kb", KB, "--delete-missing", "--delete-scope", "all"], api)
    assert code == 1 and "FAILED notes.txt" in out
    assert "notes.txt" in api.docs and not any(m == "DELETE" for m, *_ in api.requests)


def test_delete_missing_refuses_an_empty_tree(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, _ = run_cli([str(empty), "--kb", KB, "--delete-missing", "--delete-scope", "all"], api)
    assert code == 0 and "ignored: the tree produced no documents" in out
    assert len(api.docs) == 8


# ------------------------------------------------------------------------------------------ token


def test_token_is_never_printed(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    # Make the API echo the token in an error detail, and let a transport error mention it too.
    api.fail_keys["docs~guide.md"] = (400, f"bad header Bearer {TOKEN}")
    report_path = tmp_path / "r.json"
    code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(report_path)], api)
    assert code == 1
    assert api.seen["token"] == TOKEN  # type: ignore[attr-defined]
    for text in (out, err, report_path.read_text()):
        assert TOKEN not in text
        assert "secret-user" not in text
    assert "***" in out


def test_token_from_file_and_not_from_argv(tree: Path, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(f"\n  {TOKEN}  \n")
    api = FakeApi()
    code, out, err = run_cli([str(tree), "--kb", KB, "--token-file", str(token_file)], api, environ={})
    assert code == 0, err
    assert api.seen["token"] == TOKEN  # type: ignore[attr-defined]
    assert TOKEN not in out + err
    with pytest.raises(SystemExit):
        kb_import.build_parser().parse_args([str(tree), "--kb", KB, "--token", TOKEN])


def test_missing_token_is_a_usage_error(tree: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB], api, environ={})
    assert code == 2 and "EAP_API_TOKEN" in err and api.requests == []


def test_unauthorized_token_is_not_echoed(tree: Path) -> None:
    class Refusing(FakeApi):
        def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
            return ApiResponse(401, {"detail": "Invalid Authorization header format"})

    api = Refusing()
    code, out, err = run_cli([str(tree), "--kb", KB], api)
    assert code == 2 and "401" in err and TOKEN not in out + err


def test_httpx_client_sends_bearer_header_and_redacts_errors() -> None:
    assert kb_import.bearer("dev:a:b") == "Bearer dev:a:b"
    assert kb_import.bearer("Bearer dev:a:b") == "Bearer dev:a:b"
    assert kb_import.redact("x Bearer dev:a:b y dev:a:b", "dev:a:b") == "x *** y ***"
    built = kb_import.HttpxClient("http://localhost:8000/api/v1/", TOKEN)
    try:
        request = built._client.build_request("PUT", "/knowledge-bases/kb/documents/a~b.md", params={"limit": 2})
        assert str(request.url) == "http://localhost:8000/api/v1/knowledge-bases/kb/documents/a~b.md?limit=2"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    finally:
        built.close()
    client = kb_import.HttpxClient("http://127.0.0.1:9", TOKEN, timeout=0.5)  # nothing listens on port 9
    try:
        with pytest.raises(kb_import.TransportError) as excinfo:
            client.request("GET", "/knowledge-bases/x")
        assert TOKEN not in str(excinfo.value)
    finally:
        client.close()


# ------------------------------------------------------------------------------------ concurrency


@pytest.mark.parametrize("concurrency", [1, 3])
def test_concurrency_is_bounded(tmp_path: Path, concurrency: int) -> None:
    root = tmp_path / "t"
    root.mkdir()
    for i in range(12):
        (root / f"f{i:02}.md").write_text(f"# {i}\n")
    api = FakeApi(delay=0.02)
    code, out, _ = run_cli([str(root), "--kb", KB, "--concurrency", str(concurrency)], api)
    assert code == 0 and "created 12" in out
    assert api.max_in_flight == concurrency  # reached, never exceeded


@pytest.mark.parametrize("value", ["0", "33"])
def test_concurrency_out_of_range(tree: Path, value: str) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--concurrency", value], api)
    assert code == 2 and "--concurrency" in err


# --------------------------------------------------------------- P1.A6 review: fail-safe deletion


def _seed(api: FakeApi, keys: list[str], prefix: str | None = None) -> None:
    """Documents that are not in the tree; stamped as kb_import's under `prefix` when given."""
    metadata = {} if prefix is None else kb_import.stamp(prefix)
    for key in keys:
        api.docs[key] = {"document_key": key, "content_hash": "x", "mime_type": "text/markdown", "title": key,
                         "source_url": None, "metadata": dict(metadata), "latest_version": 1, "status": "ready"}


def test_wrong_root_is_refused_before_anything_is_uploaded(tree: Path) -> None:
    """BLOCKING 1: one directory too deep changes every key; the import refuses (exit 2) and lists
    every document it would have deleted, with no PUT and no DELETE."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    api.requests.clear()
    code, out, err = run_cli([str(tree / "docs"), "--kb", KB, "--delete-missing", "--delete-scope", "all"], api)
    assert code == 2 and "--delete-missing refused" in err and "--max-delete" in err
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests)
    assert len(api.docs) == 8
    assert "would delete" not in out and out.count("  missing ") == 8  # the exact plan, every key


def test_delete_limits_count_percent_and_overrides(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    for i in range(10):
        (root / f"f{i}.md").write_text(f"# {i}\n")
    api = FakeApi()
    assert run_cli([str(root), "--kb", KB], api)[0] == 0
    _seed(api, [f"gone{i}.md" for i in range(3)])  # 3 of the 13 documents = 23 %
    # Over the default 20 %: refused; a higher --max-delete-percent allows it in the dry run.
    code, out, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--dry-run"], api)
    assert code == 2 and "refused" in out and "23 %" in out  # a dry run gates like the real run (m2)
    assert out.count("  would delete gone") == 3  # the dry run lists exactly what would be deleted
    code, out, _ = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--dry-run", "--max-delete-percent", "25"], api)
    assert code == 0 and "refused" not in out and "(would be deleted)" in out
    # The rules are independent (m1): a count high enough still leaves the percent rule in force.
    code, _, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", "3"], api)
    assert code == 2 and "23 %" in err and len(api.docs) == 13
    code, _, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", "2",
                            "--max-delete-percent", "100"], api)
    assert code == 2 and "exceed the limit of 2 documents" in err and len(api.docs) == 13
    code, out, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", "3",
                              "--max-delete-percent", "25"], api)
    assert code == 0, err
    assert "deleted 3" in out and not any(k.startswith("gone") for k in api.docs)


def test_default_count_limit_and_yes_delete_all(tmp_path: Path) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "keep.md").write_text("# k\n")
    api = FakeApi()
    _seed(api, [f"gone{i:03}.md" for i in range(60)])
    code, _, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete-percent", "100"], api)
    assert code == 2 and "exceed the limit of 50 documents" in err
    code, out, err = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--yes-delete-all"], api)
    assert code == 0 and "deleted 60" in out and set(api.docs) == {"keep.md"}


@pytest.mark.parametrize("value", ["-1", "lots"])
def test_bad_max_delete_values(tree: Path, value: str) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli([str(tree), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", value], FakeApi())
    assert exc.value.code == 2


def test_filtered_files_are_never_missing(tree: Path) -> None:
    """MAJOR 2: files on disk that --include/--exclude/unsupported/hidden leave out are not missing."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    _seed(api, ["logo.png", "Makefile", ".hidden.md"])  # on disk, never imported
    api.requests.clear()
    code, out, err = run_cli(
        [str(tree), "--kb", KB, "--include", "docs/**", "--exclude", "**/AM64X/**", "--delete-missing", "--delete-scope", "all",
         "--yes-delete-all"], api)
    assert code == 0, err
    assert "missing" not in out and not any(m == "DELETE" for m, *_ in api.requests)
    # A file really gone but inside the filters is a candidate; one outside them is not.
    (tree / "docs" / "guide.md").unlink()
    (tree / "notes.txt").unlink()
    code, out, err = run_cli([str(tree), "--kb", KB, "--include", "docs/**", "--delete-missing", "--delete-scope", "all", "--yes-delete-all"], api)
    assert code == 0, err
    assert "missing from the tree: 1; deleted 1" in out
    assert "docs~guide.md" not in api.docs and "notes.txt" in api.docs


def test_key_prefix_is_a_whole_segment(tree: Path) -> None:
    """MAJOR 3: `--key-prefix sdk` is `sdk~`; another tree's `sdk2~` and `sdkA~` documents are never
    touched by --delete-missing."""
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk"], api)
    assert code == 0 and "used as 'sdk~'" in err
    assert all(k.startswith("sdk~") for k in api.docs)
    _seed(api, ["sdk2~a.md", "sdkA~b.md"], prefix="sdk2~")
    _seed(api, ["sdk~gone.md"], prefix="sdk~")
    code, out, err = run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk", "--delete-missing", "--max-delete", "1"], api)
    assert code == 0, err
    assert [p for m, p, *_ in api.requests if m == "DELETE"] == [f"/knowledge-bases/{KB}/documents/sdk~gone.md"]
    assert {"sdk2~a.md", "sdkA~b.md"} <= set(api.docs)
    assert kb_import.normalise_prefix("") == "" and kb_import.normalise_prefix("a~") == "a~"


def test_uploads_run_before_deletes(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    (tree / "docs" / "guide.md").write_text("# Guide v2\n")
    _seed(api, ["gone.md"])
    api.requests.clear()
    code, _, err = run_cli([str(tree), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", "1"], api)
    assert code == 0, err
    methods = [m for m, *_ in api.requests if m in ("PUT", "DELETE")]
    assert methods[-1] == "DELETE" and "DELETE" not in methods[:-1]


def test_a_failed_upload_skips_the_deletes(tree: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    _seed(api, ["gone.md"])
    api.fail_keys["docs~guide.md"] = (500, "boom")
    (tree / "docs" / "guide.md").write_text("# changed\n")
    code, out, _ = run_cli([str(tree), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete", "1"], api)
    assert code == 1
    assert "NOT deleted: 1 file(s) failed, so the 1 deletion(s) were skipped" in out
    assert "gone.md" in api.docs and not any(m == "DELETE" for m, *_ in api.requests)


def test_empty_tree_never_deletes_even_with_yes_delete_all(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out, _ = run_cli([str(empty), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--yes-delete-all"], api)
    assert code == 0 and "ignored: the tree produced no documents" in out and len(api.docs) == 8


# ------------------------------------------------------------------- P1.A6 review: MINOR findings


def test_argparse_errors_never_echo_values(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """MINOR 4: argparse's "unrecognized arguments" message names the flag, never its value."""
    with pytest.raises(SystemExit) as exc:
        kb_import.main([str(tree), "--kb", KB, f"--auth={TOKEN}", "--bogus", TOKEN],
                       client_factory=lambda u, t: FakeApi(), environ={"EAP_API_TOKEN": TOKEN})
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "unrecognized arguments (values not shown): --auth --bogus" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_document_cap_is_checked_before_uploading(tree: Path) -> None:
    """MINOR 5: existing + new keys over the cap stops before any PUT (the dry run warns)."""
    api = FakeApi()
    _seed(api, [f"old{i}.md" for i in range(3)])
    importer = kb_import.Importer(api, KB, sleep=lambda s: None)
    scan = kb_import.scan_tree(tree, include=kb_import.Globs([]), exclude=kb_import.Globs([]))
    with pytest.raises(kb_import.UsageError, match="10-document cap .3 existing \\+ 8 new = 11"):
        importer.run(scan, document_cap=10)
    assert api.puts() == []
    report = importer.run(scan, document_cap=10, dry_run=True)
    assert any("10-document cap" in n for n in report.notes)
    assert importer.run(scan, document_cap=11).counts()["created"] == 8


def test_oversized_files_fail_without_being_read(tree: Path) -> None:
    """MINOR 6: a file over --max-file-bytes fails at scan time, before any read or upload."""
    (tree / "big.txt").write_bytes(b"x" * 100)
    api = FakeApi()
    code, out, _ = run_cli([str(tree), "--kb", KB, "--max-file-bytes", "64"], api)
    assert code == 1 and "FAILED big.txt: the file is too large (100 bytes; --max-file-bytes is 64)" in out
    assert "big.txt" not in dict(api.puts())


def test_a_file_that_grows_after_the_scan_is_refused(tree: Path) -> None:
    scan = kb_import.scan_tree(tree, include=kb_import.Globs([]), exclude=kb_import.Globs([]), max_file_bytes=64)
    f = next(f for f in scan.files if f.rel_path == "notes.txt")
    with open(f.path, "ab") as fh:
        fh.write(b"y" * 200)  # same inode, now too large
    with pytest.raises(ValueError, match="too large"):
        kb_import.load_file(f, metadata_map=None, source_url_template=None, max_file_bytes=64)


def test_swapped_symlink_after_the_scan_is_refused(tree: Path, tmp_path: Path) -> None:
    """MINOR 12: the scan's (device, inode) is checked on the opened file, so a path swapped for a
    link to a file outside the tree is not read."""
    outside = tmp_path / "secret.md"
    outside.write_text("# outside the tree\n")
    scan = kb_import.scan_tree(tree, include=kb_import.Globs([]), exclude=kb_import.Globs([]))
    f = next(f for f in scan.files if f.rel_path == "docs/guide.md")
    f.path.unlink()
    f.path.symlink_to(outside)
    with pytest.raises(ValueError, match="changed after the scan"):
        kb_import.load_file(f, metadata_map=None, source_url_template=None)


def test_retry_after_is_honoured_and_backoff_has_jitter() -> None:
    """MINOR 7: Retry-After wins (plus < 1 s of jitter); otherwise exponential with equal jitter;
    MAX_ATTEMPTS is the first try plus five retries."""
    class Busy(FakeApi):
        def __init__(self) -> None:
            super().__init__()
            self.left = 2

        def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
            if method == "GET" and self.left:
                self.left -= 1
                return ApiResponse(503, {"detail": "retry shortly"}, {"Retry-After": "7"})
            return super().request(method, path, json=json, params=params)

    sleeps: list[float] = []
    importer = kb_import.Importer(Busy(), KB, sleep=sleeps.append)
    importer.check_kb()
    assert len(sleeps) == 2 and all(7 <= s < 8 for s in sleeps)
    rng_importer = kb_import.Importer(FakeApi(), KB, sleep=lambda s: None)
    waits = [rng_importer.backoff(a) for a in range(8)]
    for a, w in enumerate(waits):
        d = min(2**a, 30)
        assert d / 2 <= w <= d
    assert len({round(rng_importer.backoff(3), 6) for _ in range(20)}) > 1  # jittered
    assert kb_import.MAX_ATTEMPTS == 6
    assert kb_import.retry_after_seconds(ApiResponse(503, None, {"retry-after": "Wed, 21 Oct 2015"})) is None
    assert kb_import.retry_after_seconds(ApiResponse(503, None, {"retry-after": "9999"})) == 120


@pytest.mark.parametrize(
    ("status", "body", "result"),
    [
        (202, {"run_id": "r1"}, "retried"),
        (200, {"run_id": None, "unchanged": True}, "unchanged"),
        (409, {"detail": "the document is already being ingested (run x is running)"}, "unchanged"),
        (500, {"detail": "boom"}, "failed"),
    ],
)
def test_retry_failed_reingest_outcomes(tree: Path, status: int, body: dict[str, Any], result: str) -> None:
    """MINOR 8: reingest 200 (already ready) and 409 (already ingesting) are not failures."""
    class Reingest(FakeApi):
        def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
            if method == "POST":
                return ApiResponse(status, body)
            return super().request(method, path, json=json, params=params)

    api = Reingest()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    api.docs["docs~guide.md"]["status"] = "failed"
    report = tree.parent / "r.json"
    code, out, _ = run_cli([str(tree), "--kb", KB, "--retry-failed", "--report-json", str(report)], api)
    assert code == (1 if result == "failed" else 0)
    doc = next(d for d in json.loads(report.read_text())["documents"] if d["key"] == "docs~guide.md")
    assert doc["result"] == result
    if status == 200:
        assert doc["run_id"] is None and "already ingested" in doc["reason"]
    if status == 409:
        assert "already being ingested" in doc["reason"]


@pytest.mark.parametrize("url", ["localhost:8000", "ftp://h/api", "http://", "not a url"])
def test_bad_api_url_is_a_usage_error(tree: Path, url: str) -> None:
    """MINOR 11: a bad --api-url is refused up front, not a traceback from httpx."""
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--api-url", url], api)
    assert code == 2 and "--api-url" in err and api.requests == []


def test_httpx_client_wraps_every_exception_as_transport_error() -> None:
    client = kb_import.HttpxClient("http://localhost:8000/api/v1", TOKEN)
    try:
        def boom(*a: Any, **k: Any) -> Any:
            raise ValueError(f"invalid URL with {TOKEN}")  # e.g. httpx.InvalidURL is not an HTTPError

        client._client.request = boom  # type: ignore[method-assign]
        with pytest.raises(kb_import.TransportError) as excinfo:
            client.request("GET", "/x")
        assert TOKEN not in str(excinfo.value) and "ValueError" in str(excinfo.value)
    finally:
        client.close()


def test_hidden_counts_and_documented_labels(tree: Path) -> None:
    """MINOR 9: dot-files count as `hidden`, dot-directories as `hidden directory` (once each)."""
    api = FakeApi()
    _, out, _ = run_cli([str(tree), "--kb", KB, "--dry-run"], api, environ={})
    assert "hidden 1" in out and "hidden directory 1" in out and "unsupported type 2" in out
    doc = (Path(__file__).resolve().parents[2] / "docs" / "knowledge-import.md").read_text()
    assert "`skipped: hidden`" in doc and "`hidden directory`" in doc


# ------------------------------------------------------------- P1.A6 re-review: M1, M2 and m1-m7


def test_unreadable_directory_skips_every_delete(tree: Path) -> None:
    """M1: a directory os.walk cannot list hides its files, so its documents look missing. Every
    unreadable directory is reported, the deletes are skipped, and the exit status is 1."""
    secret = tree / "secret"
    secret.mkdir()
    for i in range(3):
        (secret / f"s{i}.md").write_text(f"# {i}\n")
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk"], api)[0] == 0
    _seed(api, ["sdk~gone.md"], prefix="sdk~")  # really gone: still not deleted this run
    secret.chmod(0)
    try:
        api.requests.clear()
        code, out, _ = run_cli(
            [str(tree), "--kb", KB, "--key-prefix", "sdk", "--delete-missing", "--yes-delete-all"], api
        )
    finally:
        secret.chmod(0o755)
    assert code == 1
    assert "UNREADABLE secret:" in out
    assert "director(ies) could not be read, so the" in out and "deletion(s) were skipped" in out
    assert not any(m == "DELETE" for m, *_ in api.requests)
    assert all(f"sdk~secret~s{i}.md" in api.docs for i in range(3)) and "sdk~gone.md" in api.docs


def test_unreadable_directory_without_delete_missing_still_exits_1(tree: Path) -> None:
    (tree / "locked").mkdir()
    (tree / "locked").chmod(0)
    try:
        code, out, _ = run_cli([str(tree), "--kb", KB], FakeApi())
    finally:
        (tree / "locked").chmod(0o755)
    assert code == 1 and "UNREADABLE locked:" in out and "could not be read" in out


def test_delete_missing_needs_a_prefix_or_delete_scope_all(tree: Path) -> None:
    """M2: with no --key-prefix the whole knowledge base would be in scope; that needs the explicit
    --delete-scope all."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB], api)[0] == 0
    _seed(api, ["Quarterly report.pdf"])  # a Studio upload
    api.requests.clear()
    code, _, err = run_cli([str(tree), "--kb", KB, "--delete-missing"], api)
    assert code == 2 and "--delete-missing needs --key-prefix" in err and "--delete-scope all" in err
    assert api.requests == []  # refused before any HTTP
    code, out, err = run_cli([str(tree), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete-percent", "100"], api)
    assert code == 0, err
    assert "Quarterly report.pdf" not in api.docs  # scope all: unstamped documents are candidates too


def test_bad_delete_scope_value_is_not_echoed(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        run_cli([str(tree), "--kb", KB, "--delete-scope", "SECRETX"], FakeApi())
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "SECRETX" not in captured.err and "invalid choice (value not shown)" in captured.err


def test_uploads_are_stamped_and_only_stamped_documents_are_deleted(tree: Path) -> None:
    """M2: every upload carries metadata.import_tool = "kb_import" and import_prefix; under scope
    `prefix`, an unstamped document (Studio upload) or one stamped by a nested prefix's tree is kept
    and reported, never deleted."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB, "--key-prefix", "mcu"], api)[0] == 0
    assert all(d["metadata"] == {"import_tool": "kb_import", "import_prefix": "mcu~"} for d in api.docs.values())
    _seed(api, ["mcu~manual.pdf"])  # uploaded by hand under the prefix
    _seed(api, ["mcu~am263~x.md", "mcu~am263~y.md"], prefix="mcu~am263~")  # a nested tree's documents
    _seed(api, ["mcu~gone.md"], prefix="mcu~")  # ours, and gone
    api.requests.clear()
    code, out, err = run_cli([str(tree), "--kb", KB, "--key-prefix", "mcu", "--delete-missing", "--yes-delete-all"], api)
    assert code == 0, err
    assert [p for m, p, *_ in api.requests if m == "DELETE"] == [f"/knowledge-bases/{KB}/documents/mcu~gone.md"]
    assert {"mcu~manual.pdf", "mcu~am263~x.md", "mcu~am263~y.md"} <= set(api.docs)
    assert "kept, not created by kb_import under this prefix: 3" in out
    assert "  kept mcu~manual.pdf" in out and "  kept mcu~am263~x.md" in out


def test_stamp_keys_are_reserved_in_the_metadata_map(tree: Path, tmp_path: Path) -> None:
    for key in ("import_tool", "import_prefix"):
        map_file = tmp_path / "map.yaml"
        map_file.write_text(f"'*.md': {{{key}: x}}\n")
        code, _, err = run_cli([str(tree), "--kb", KB, "--metadata-map", str(map_file)], FakeApi())
        assert code == 2 and "reserved (the importer stamps it)" in err


def test_max_delete_count_never_lifts_the_percent_rule(tree: Path) -> None:
    """m1: a script that always passes --max-delete 100 keeps the wrong-root protection."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk"], api)[0] == 0
    api.requests.clear()
    code, out, err = run_cli([str(tree / "docs"), "--kb", KB, "--key-prefix", "sdk", "--delete-missing", "--max-delete", "100"], api)
    assert code == 2 and "100 %" in err and "over the limit of 20 %" in err
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests) and len(api.docs) == 8
    code, _, err = run_cli([str(tree / "docs"), "--kb", KB, "--key-prefix", "sdk", "--delete-missing", "--max-delete", "all"], api)
    assert code == 2 and "over the limit of 20 %" in err  # `all` lifts the count only


def test_dry_run_over_the_limit_exits_2(tree: Path) -> None:
    """m2: CI using --dry-run as a gate sees the refusal (exit 2), with the full plan printed."""
    api = FakeApi()
    assert run_cli([str(tree), "--kb", KB, "--key-prefix", "sdk"], api)[0] == 0
    api.requests.clear()
    code, out, _ = run_cli([str(tree / "docs"), "--kb", KB, "--key-prefix", "sdk", "--delete-missing", "--dry-run"], api)
    assert code == 2 and "--delete-missing refused" in out and out.count("  would delete sdk~") == 8
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests)


def test_shortened_keys_are_reported_as_kept(tmp_path: Path) -> None:
    """m3: a gone file whose key was hash-shortened cannot be checked; it is listed, never deleted."""
    root = tmp_path / "t"
    deep = root / ("d" * 130) / ("e" * 130)
    deep.mkdir(parents=True)
    (deep / "x.md").write_text("# x\n")
    (root / "keep.md").write_text("# k\n")
    api = FakeApi()
    assert run_cli([str(root), "--kb", KB, "--key-prefix", "p"], api)[0] == 0
    (long_key,) = [k for k in api.docs if len(k) == 256]
    (deep / "x.md").unlink()
    code, out, err = run_cli([str(root), "--kb", KB, "--key-prefix", "p", "--delete-missing", "--yes-delete-all"], api)
    assert code == 0, err
    assert long_key in api.docs
    assert "kept, shortened keys that cannot be checked against the tree: 1" in out and f"  kept {long_key}" in out


@pytest.mark.parametrize(
    "argv_tail",
    [["--concurrency", "SECRETX"], ["--max-file-bytes", "SECRETX"], ["--timeout", "SECRETX"]],
)
def test_argparse_value_errors_never_echo_the_value(tree: Path, capsys: pytest.CaptureFixture[str], argv_tail: list[str]) -> None:
    """m4: "invalid int value: 'X'" is printed as "invalid int value (value not shown)"."""
    with pytest.raises(SystemExit) as exc:
        run_cli([str(tree), "--kb", KB, *argv_tail], FakeApi())
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "SECRETX" not in captured.out + captured.err


def test_token_file_path_is_never_echoed(tree: Path) -> None:
    """m4: a token pasted as the --token-file path is not printed in the error."""
    code, out, err = run_cli([str(tree), "--kb", KB, "--token-file", "SECRETX-dev:acme:x"], FakeApi(), environ={})
    assert code == 2 and "cannot read the --token-file (path not shown)" in err
    assert "SECRETX" not in out + err


def test_keys_are_unicode_nfc(tmp_path: Path) -> None:
    """m5: an NFD checkout (macOS from a zip) gets the same NFC keys as an NFC one (Linux)."""
    import unicodedata

    root = tmp_path / "t"
    root.mkdir()
    nfd = unicodedata.normalize("NFD", "café.md")
    (root / nfd).write_text("# c\n")
    api = FakeApi()
    assert run_cli([str(root), "--kb", KB], api)[0] == 0
    (key,) = api.docs
    assert key == unicodedata.normalize("NFC", "café.md")
    assert kb_import.document_key(nfd) == kb_import.document_key(unicodedata.normalize("NFC", "café.md"))
    # A second run is unchanged, and the document is never "missing" from its own tree.
    code, out, _ = run_cli([str(root), "--kb", KB, "--delete-missing", "--delete-scope", "all"], api)
    assert code == 0 and "unchanged 1" in out and "missing" not in out


def test_docs_tell_how_to_unblock_deletes_after_a_permanent_failure() -> None:
    """m6: a permanently failing file blocks --delete-missing until it is excluded; the docs say so."""
    doc = (Path(__file__).resolve().parents[2] / "docs" / "knowledge-import.md").read_text()
    assert "therefore blocks" in doc and "`--exclude` it to let the deletes run" in doc
    assert "--delete-scope all" in doc and "import_tool" in doc and "UNREADABLE" in doc


def test_a_fifo_swapped_in_after_the_scan_does_not_block(tree: Path) -> None:
    """m7: os.open uses O_NONBLOCK, so a FIFO at the path is rejected instead of blocking."""
    scan = kb_import.scan_tree(tree, include=kb_import.Globs([]), exclude=kb_import.Globs([]))
    f = next(f for f in scan.files if f.rel_path == "notes.txt")
    f.path.unlink()
    os.mkfifo(f.path)
    result: dict[str, Any] = {}

    def read() -> None:
        try:
            kb_import.load_file(f, metadata_map=None, source_url_template=None)
        except ValueError as exc:
            result["error"] = str(exc)

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "load_file blocked on a FIFO"
    assert "changed after the scan" in result["error"]


# ------------------------------------------------------------------- --report-json (quality fix 3)


def test_report_json_in_a_missing_directory_is_refused_before_any_upload(tree: Path, tmp_path: Path) -> None:
    """The TI quality check: `--report-json nonexistent-dir/x.json` uploaded everything, then died
    with a FileNotFoundError traceback and lost the report. It is a usage error (exit 2) up front."""
    api = FakeApi()
    code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(tmp_path / "nope" / "x.json")], api)
    assert code == 2
    assert "--report-json: the directory" in err and "does not exist" in err
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests) and not api.docs
    assert not (tmp_path / "nope").exists()  # never created


def test_report_json_that_is_a_directory_is_refused(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(tmp_path)], api)
    assert code == 2 and "is a directory" in err
    assert not api.docs


def test_report_json_parent_that_is_a_file_is_not_a_directory(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    (tmp_path / "f").write_text("x")
    code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(tmp_path / "f" / "r.json")], api)
    assert code == 2 and "is not a directory" in err and "does not exist" not in err
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests) and not api.docs


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_report_json_unwritable_directory_is_refused_before_any_upload(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(ro / "r.json")], api)
        dry, _, dry_err = run_cli([str(tree), "--kb", KB, "--dry-run", "--report-json", str(ro / "r.json")], api)
    finally:
        ro.chmod(0o700)
    assert code == 2 and "the directory" in err and "is not writable" in err
    assert dry == 2 and "is not writable" in dry_err
    assert not any(m in ("PUT", "DELETE") for m, *_ in api.requests) and not api.docs


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere")
def test_report_json_existing_read_only_file_is_refused(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    target = tmp_path / "r.json"
    target.write_text("{}")
    target.chmod(0o400)
    try:
        code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(target)], api)
    finally:
        target.chmod(0o600)
    assert code == 2 and "exists and is not writable" in err and not api.docs


def test_report_json_write_failure_after_upload_is_reported_not_a_traceback(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory checks out, but the write still fails (removed mid-run, read-only): the text
    report is printed, an error names the path, and the run exits 1, without a traceback."""
    api = FakeApi()
    report_path = tmp_path / "out" / "report.json"
    report_path.parent.mkdir()

    def boom(self: Path, *a: object, **k: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", boom)
    code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(report_path)], api)
    assert code == 1
    assert "created" in out and api.docs  # the uploads happened and the text report printed
    assert "the JSON report could not be written" in err and "Permission denied" in err
    assert "Traceback" not in err
