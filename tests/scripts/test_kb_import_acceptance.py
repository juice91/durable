"""scripts/kb_import.py (RAG P1.A6) acceptance tests, beyond the implementer's: every registry
extension, metadata and template edge cases, report and delete semantics, token secrecy on every
output path, bounded concurrency, retries, exit codes, and a live check against an in-process API
on the dev Postgres (the dev API on :8000 predates P1.A4, so it cannot serve the new CLI)."""

from __future__ import annotations

import base64
import io
import os
import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from eap_knowledge.formats import FORMAT_SPECS

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("kb_import_unit_helpers", _HERE / "test_kb_import.py")
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
sys.modules["kb_import_unit_helpers"] = helpers
_spec.loader.exec_module(helpers)

kb_import = helpers.kb_import
FakeApi, ApiResponse, KB, TOKEN, run_cli = helpers.FakeApi, helpers.ApiResponse, helpers.KB, helpers.TOKEN, helpers.run_cli


def write(root: Path, rel: str, text: str = "# t\n\nbody\n") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def put_bodies(api: FakeApi) -> dict[str, dict[str, Any]]:
    return dict(api.puts())


@pytest.fixture
def small(tmp_path: Path) -> Path:
    root = tmp_path / "small"
    write(root, "a.md")
    write(root, "sub/b.rst", "B\n=\n\ntext\n")
    write(root, "sub/c.txt", "plain\n")
    return root


# ------------------------------------------------------------------- keys and MIME for the registry


@pytest.mark.parametrize(
    ("ext", "spec"),
    [(ext, s) for s in FORMAT_SPECS for ext in s.extensions],
    ids=lambda v: v if isinstance(v, str) else v.format,
)
@pytest.mark.parametrize("case", ["lower", "upper", "mixed"])
def test_every_registry_extension_gets_its_canonical_mime_type(tmp_path: Path, ext: str, spec: Any, case: str) -> None:
    shown = {"lower": ext, "upper": ext.upper(), "mixed": ext[:2].upper() + ext[2:]}[case]
    root = tmp_path / "t"
    name = f"dir one/file{shown}"
    path = root / name
    path.parent.mkdir(parents=True)
    if spec.binary:
        path.write_bytes(b"%PDF-1.7\n\x00\xff bytes")
    else:
        path.write_text('{"a": 1}\n' if spec.format == "json" else "title: x\n" if spec.format == "yaml" else "# x\n\nbody\n")
    api = FakeApi()
    code, _, err = run_cli([str(root), "--kb", KB], api)
    assert code == 0, err
    ((key, body),) = api.puts()
    assert key == f"dir one~file{shown}"
    assert body["mime_type"] == spec.mime_type
    assert ("content_base64" in body) is spec.binary and ("content" in body) is not spec.binary
    if spec.binary:
        assert base64.b64decode(body["content_base64"]) == path.read_bytes()


def test_dotfiles_and_dot_directories_are_skipped_unless_asked(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "visible.md")
    write(root, ".hidden.md")
    write(root, ".cache/inner.md")
    write(root, "docs/.private/x.rst", "X\n=\n\nt\n")
    write(root, "docs/.rst")  # a dotfile that is only an extension
    api = FakeApi()
    code, out, err = run_cli([str(root), "--kb", KB], api)
    assert code == 0, err
    assert set(put_bodies(api)) == {"visible.md"}
    assert "hidden" in out
    api2 = FakeApi()
    code, _, err = run_cli([str(root), "--kb", KB, "--include-hidden"], api2)
    assert code == 0, err
    assert set(put_bodies(api2)) == {
        "visible.md", ".hidden.md", ".cache~inner.md", "docs~.private~x.rst", "docs~.rst",
    }


def test_unsupported_and_extensionless_files_are_skipped_not_failed(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "ok.md")
    for name in ("Makefile", "image.png", "data.csv", "archive.tar.gz", "noext", "trailing."):
        write(root, name, "x")
    api = FakeApi()
    code, out, err = run_cli([str(root), "--kb", KB], api)
    assert code == 0, err
    assert set(put_bodies(api)) == {"ok.md"}
    assert "unsupported type 6" in out


# ------------------------------------------------------------------------------------ metadata map


def test_metadata_map_order_is_file_order_and_unmatched_files_have_no_metadata(small: Path, tmp_path: Path) -> None:
    mm = tmp_path / "map.yaml"
    mm.write_text("'**/*.rst': {kind: rst}\n'sub/**': {kind: sub, extra: 1}\n'*.md': {kind: md}\n")
    api = FakeApi()
    code, _, err = run_cli([str(small), "--kb", KB, "--metadata-map", str(mm)], api)
    assert code == 0, err
    sent = put_bodies(api)
    st = kb_import.stamp("")  # every document also carries the importer's stamp
    assert sent["sub~b.rst"]["metadata"] == {"kind": "rst", **st}  # first of two matching globs, no merge
    assert sent["sub~c.txt"]["metadata"] == {"kind": "sub", "extra": 1, **st}
    assert sent["a.md"]["metadata"] == {"kind": "md", **st}


def test_metadata_map_empty_file_and_scalar_types(small: Path, tmp_path: Path) -> None:
    mm = tmp_path / "map.yaml"
    mm.write_text("")
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB, "--metadata-map", str(mm)], api)[0] == 0
    assert all(b["metadata"] == kb_import.stamp("") for b in put_bodies(api).values())
    mm.write_text("'*.md': {s: text, i: 3, f: 1.5, b: true}\n")
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB, "--metadata-map", str(mm)], api)[0] == 0
    assert put_bodies(api)["a.md"]["metadata"] == {"s": "text", "i": 3, "f": 1.5, "b": True, **kb_import.stamp("")}


@pytest.mark.parametrize(
    "content",
    ["'*.md': {a: {b: {c: 1}}}\n", "'*.md': {a: [x]}\n", "'*.md': {a: ~}\n", "'*.md': {1: x}\n",
     "'*.md': {a: .nan}\n", "rules:\n  - match: '*.md'\n    metadata: {a: [1]}\n",
     "rules:\n  - metadata: {a: 1}\n"],
)
def test_nested_or_malformed_metadata_is_rejected_before_any_request(small: Path, tmp_path: Path, content: str) -> None:
    mm = tmp_path / "map.yaml"
    mm.write_text(content)
    api = FakeApi()
    code, out, err = run_cli([str(small), "--kb", KB, "--metadata-map", str(mm)], api)
    assert code == 2 and err.startswith("error:") and out == ""
    assert api.requests == []


def test_missing_metadata_map_file_is_a_usage_error(small: Path, tmp_path: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(small), "--kb", KB, "--metadata-map", str(tmp_path / "nope.yaml")], api)
    assert code == 2 and "cannot read the metadata map" in err and api.requests == []


# ------------------------------------------------------------------------------------ source URL


def test_source_url_percent_encoding_of_special_characters(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "a b/c#d?e%f&g.md")
    write(root, "ünï/日本.rst", "T\n=\n\nx\n")
    api = FakeApi()
    tmpl = "https://h.example/{path}|{path_no_ext}|{name}|{stem}|{dir}|{ext}"
    code, _, err = run_cli([str(root), "--kb", KB, "--source-url-template", tmpl], api)
    assert code == 0, err
    sent = put_bodies(api)
    q = urllib.parse.quote
    assert sent["a b~c#d?e%f&g.md"]["source_url"] == (
        "https://h.example/a%20b/c%23d%3Fe%25f%26g.md|a%20b/c%23d%3Fe%25f%26g|c%23d%3Fe%25f%26g.md"
        "|c%23d%3Fe%25f%26g|a%20b|md"
    )
    jp = sent["ünï~日本.rst"]["source_url"]
    assert jp == f"https://h.example/{q('ünï/日本.rst', safe='/')}|{q('ünï/日本', safe='/')}|{q('日本.rst')}|{q('日本')}|{q('ünï')}|rst"
    assert jp.isascii()


def test_source_url_key_placeholder_encodes_the_tilde_separator_and_prefix(small: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli(
        [str(small), "--kb", KB, "--key-prefix", "p:", "--source-url-template", "https://h/x/{key}"], api
    )
    assert code == 0, err
    # The prefix is a whole key segment: "p:" is used as "p:~" (P1.A6 review).
    assert put_bodies(api)["p:~sub~b.rst"]["source_url"] == "https://h/x/p%3A~sub~b.rst"


def test_source_url_template_literal_braces_and_double_curly(small: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(small), "--kb", KB, "--source-url-template", "https://h/{{literal}}/{name}"], api)
    assert code == 0, err
    assert put_bodies(api)["a.md"]["source_url"] == "https://h/{literal}/a.md"


def test_source_url_not_set_without_template_and_bad_templates_exit_2(small: Path) -> None:
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB], api)[0] == 0
    assert all("source_url" not in b for b in put_bodies(api).values())
    for tmpl in ("//h/{path}", "https://h/{path.__class__}", "https://h/{0}", "https://h/{path!r}x{"):
        api = FakeApi()
        code, _, err = run_cli([str(small), "--kb", KB, "--source-url-template", tmpl], api)
        assert code == 2 and api.requests == [], tmpl


# ------------------------------------------------------------------------------- dry run and report


def test_dry_run_counts_per_format_and_uploads_nothing(small: Path, tmp_path: Path) -> None:
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB], api)[0] == 0
    write(small, "a.md", "# changed\n\nnew body\n")  # modified -> would update
    write(small, "new.yaml", "k: v\n")  # new -> would create
    before = len(api.puts())
    report = tmp_path / "dry.json"
    code, out, err = run_cli([str(small), "--kb", KB, "--dry-run", "--report-json", str(report)], api)
    assert code == 0, err
    assert len(api.puts()) == before and not [r for r in api.requests[-5:] if r[0] in ("DELETE", "POST")]
    data = json.loads(report.read_text())
    assert data["dry_run"] is True
    assert data["counts"] == {"would_create": 1, "would_skip": 2, "would_update": 1}
    assert data["formats"] == {"markdown": 1, "rst": 1, "text": 1, "yaml": 1}
    assert "dry run" in out and "would create 1" in out and "would be unchanged 2" in out


def test_report_distinguishes_created_unchanged_new_version_and_failed(small: Path, tmp_path: Path) -> None:
    api = FakeApi()
    code, out, _ = run_cli([str(small), "--kb", KB], api)
    assert code == 0 and "created 3" in out
    write(small, "a.md", "# edited\n\nmore\n")
    write(small, "sub/zz.md", "# fresh\n")
    api.fail_keys["sub~c.txt"] = (422, "bad text")
    report = tmp_path / "r.json"
    code, out, _ = run_cli([str(small), "--kb", KB, "--report-json", str(report)], api)
    assert code == 1
    data = json.loads(report.read_text())
    assert data["counts"] == {"created": 1, "failed": 1, "new_version": 1, "unchanged": 1}
    by_key = {d["key"]: d for d in data["documents"]}
    assert by_key["a.md"]["result"] == "new_version" and by_key["a.md"]["run_id"]
    assert by_key["sub~zz.md"]["result"] == "created"
    assert by_key["sub~b.rst"]["result"] == "unchanged" and by_key["sub~b.rst"]["run_id"] is None
    assert by_key["sub~c.txt"]["result"] == "failed" and "422" in by_key["sub~c.txt"]["reason"]
    assert "FAILED sub/c.txt: HTTP 422: bad text" in out


def test_empty_and_non_utf8_files_fail_individually_without_stopping_the_rest(small: Path) -> None:
    (small / "empty.md").write_text("")
    (small / "blank.txt").write_text("  \n\n")
    (small / "bin.md").write_bytes(b"\xff\xfe\x00bad")
    api = FakeApi()
    code, out, _ = run_cli([str(small), "--kb", KB], api)
    assert code == 1
    assert set(put_bodies(api)) == {"a.md", "sub~b.rst", "sub~c.txt"}
    for rel in ("empty.md", "blank.txt", "bin.md"):
        assert f"FAILED {rel}" in out


def test_fixture_tree_imports_then_second_run_is_all_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "docs/a.md")
    write(root, "docs/x/b.rst", "B\n=\n\nt\n")
    write(root, "c.yaml", "k: v\n")
    write(root, "d.json", '{"a": 1}')
    (root / "e.pdf").write_bytes(b"%PDF-1.7\n\x00\x01\x02")
    api = FakeApi()
    mm = tmp_path / "m.yaml"
    mm.write_text("docs/**: {area: docs}\n")
    args = [str(root), "--kb", KB, "--metadata-map", str(mm), "--source-url-template", "https://h/{path}"]
    code, out, err = run_cli(args, api)
    assert code == 0, err
    assert "created 5" in out
    report = tmp_path / "r.json"
    code, out, err = run_cli([*args, "--report-json", str(report)], api)
    assert code == 0, err
    assert json.loads(report.read_text())["counts"] == {"unchanged": 5}
    assert "unchanged 5" in out and "created" not in out and "new version" not in out
    assert len(api.docs) == 5 and all(d["latest_version"] == 1 for d in api.docs.values())


# ----------------------------------------------------------------------------------- delete-missing


def seeded(root: Path) -> FakeApi:
    api = FakeApi()
    assert run_cli([str(root), "--kb", KB, "--key-prefix", "sdk~"], api)[0] == 0
    api.docs["other~keep.md"] = {**api.docs["sdk~a.md"], "document_key": "other~keep.md"}
    api.docs["sdk~gone.md"] = {**api.docs["sdk~a.md"], "document_key": "sdk~gone.md"}
    return api


def deletes(api: FakeApi) -> list[str]:
    return [urllib.parse.unquote(p.rsplit("/", 1)[1]) for m, p, _, _ in api.requests if m == "DELETE"]


def test_delete_missing_is_off_by_default(small: Path) -> None:
    api = seeded(small)
    code, out, _ = run_cli([str(small), "--kb", KB, "--key-prefix", "sdk~"], api)
    assert code == 0 and deletes(api) == []
    assert "sdk~gone.md" in out and "kept" in out and set(api.docs) >= {"sdk~gone.md", "other~keep.md"}


def test_delete_missing_with_prefix_only_touches_that_prefix(small: Path) -> None:
    api = seeded(small)
    # 1 of the prefix's 4 documents is over the default 20 %: allowed explicitly.
    code, out, err = run_cli([str(small), "--kb", KB, "--key-prefix", "sdk~", "--delete-missing", "--max-delete-percent", "25"], api)
    assert code == 0, err
    assert deletes(api) == ["sdk~gone.md"] and "other~keep.md" in api.docs and "sdk~gone.md" not in api.docs
    assert "deleted 1" in out


def test_delete_missing_without_prefix_covers_the_whole_kb(small: Path) -> None:
    api = seeded(small)
    code, _, err = run_cli([str(small), "--kb", KB, "--key-prefix", "sdk~"], api)
    assert code == 0, err
    # No prefix: other keys are "missing"; that is most of the KB, so it needs --yes-delete-all.
    code, _, err = run_cli([str(small), "--kb", KB, "--delete-missing", "--delete-scope", "all"], api)
    assert code == 2 and "refused" in err and "other~keep.md" in api.docs
    code, _, err = run_cli([str(small), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--yes-delete-all"], api)
    assert code == 0, err
    assert "other~keep.md" not in api.docs


def test_delete_missing_refuses_an_empty_tree_even_with_a_prefix(tmp_path: Path) -> None:
    api = seeded(_tree_for(tmp_path))
    empty = tmp_path / "empty"
    empty.mkdir()
    write(empty, "readme.unknown", "x")  # nothing importable
    n = len(api.docs)
    code, out, err = run_cli([str(empty), "--kb", KB, "--key-prefix", "sdk~", "--delete-missing"], api)
    assert code == 0, err
    assert deletes(api) == [] and len(api.docs) == n
    assert "--delete-missing ignored" in out


def _tree_for(tmp_path: Path) -> Path:
    root = tmp_path / "seed"
    write(root, "a.md")
    return root


def test_delete_missing_dry_run_deletes_nothing(small: Path) -> None:
    api = seeded(small)
    code, out, _ = run_cli(
        [str(small), "--kb", KB, "--key-prefix", "sdk~", "--delete-missing", "--dry-run", "--max-delete-percent", "25"], api
    )
    assert code == 0 and deletes(api) == [] and "would be deleted" in out and "would delete sdk~gone.md" in out


# ----------------------------------------------------------------------------------------- token


class Leaky(FakeApi):
    """Echoes the bearer token (with and without `Bearer `) in error bodies and transport errors."""

    def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
        if method == "PUT" and path.endswith("a.md"):
            return ApiResponse(500, {"detail": f"Authorization: Bearer {TOKEN} / raw {TOKEN}"})
        if method == "PUT" and path.endswith("c.txt"):
            raise kb_import.TransportError(f"connect failed for {TOKEN}")
        return super().request(method, path, json=json, params=params)


def test_token_never_appears_in_stdout_stderr_or_json_on_leaky_errors(small: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.json"
    code, out, err = run_cli([str(small), "--kb", KB, "--report-json", str(report)], Leaky())
    assert code == 1
    for text in (out, err, report.read_text()):
        assert TOKEN not in text and "secret-user" not in text and "7f3a" not in text
    assert "FAILED a.md" in out and "FAILED sub/c.txt" in out


def test_token_never_appears_on_usage_and_transport_errors(small: Path, tmp_path: Path) -> None:
    class Down(FakeApi):
        def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
            raise kb_import.TransportError(f"cannot reach host with Bearer {TOKEN}")

    code, out, err = run_cli([str(small), "--kb", KB], Down())
    assert code == 2 and "unreachable" in err
    assert TOKEN not in out + err and "secret-user" not in out + err
    for argv in ([str(small)], [str(small), "--kb", KB, "--concurrency", "0"], [str(tmp_path / "nodir"), "--kb", KB]):
        code, out, err = run_cli(argv, FakeApi()) if "--kb" in argv else (2, "", "")
        assert TOKEN not in out + err


def test_token_on_the_command_line_is_an_error_and_never_echoed(small: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("--token", "--tok", "--token=" + TOKEN):
        for argv in ([str(small), "--kb", KB, flag] + ([] if "=" in flag else [TOKEN]),
                     [flag] + ([] if "=" in flag else [TOKEN]) + ["--kb", KB]):  # token where the directory goes
            out, err = io.StringIO(), io.StringIO()
            code = kb_import.main(argv, client_factory=lambda u, t: FakeApi(), environ={}, out=out, err=err)
            assert code == 2
            assert "never taken from the command line" in err.getvalue()
            assert TOKEN not in out.getvalue() + err.getvalue()
    captured = capsys.readouterr()  # nothing reached the real streams either (argparse writes there)
    assert TOKEN not in captured.out + captured.err


def test_token_file_is_never_printed_and_a_bad_file_is_a_usage_error(small: Path, tmp_path: Path) -> None:
    tf = tmp_path / "tok"
    tf.write_text(f"{TOKEN}\n")
    report = tmp_path / "r.json"
    code, out, err = run_cli([str(small), "--kb", KB, "--token-file", str(tf), "--report-json", str(report)], Leaky(), environ={})
    assert code == 1 and TOKEN not in out + err + report.read_text()
    tf.write_text("\n  \n")
    code, _, err = run_cli([str(small), "--kb", KB, "--token-file", str(tf)], FakeApi(), environ={})
    assert code == 2 and "empty" in err
    code, _, err = run_cli([str(small), "--kb", KB, "--token-file", str(tmp_path / "missing")], FakeApi(), environ={})
    assert code == 2 and "cannot read the --token-file (path not shown)" in err and "missing" not in err


def test_token_file_wins_over_the_environment_and_a_bearer_prefix_is_kept(small: Path, tmp_path: Path) -> None:
    tf = tmp_path / "tok"
    tf.write_text("Bearer dev:acme:alice\n")
    api = FakeApi()
    code, _, err = run_cli([str(small), "--kb", KB, "--token-file", str(tf)], api)
    assert code == 0, err
    assert api.seen["token"] == "Bearer dev:acme:alice" and kb_import.bearer(api.seen["token"]) == "Bearer dev:acme:alice"  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------- concurrency


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_in_flight_requests_never_exceed_the_concurrency_limit(tmp_path: Path, limit: int) -> None:
    root = tmp_path / "t"
    for i in range(12):
        write(root, f"f{i}.md", f"# doc {i}\n")
    api = FakeApi(delay=0.03)
    code, _, err = run_cli([str(root), "--kb", KB, "--concurrency", str(limit)], api)
    assert code == 0, err
    assert api.max_in_flight == limit  # reaches the bound (12 slow files) and never passes it
    assert len(api.docs) == 12


def test_delete_phase_is_bounded_too(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "keep.md")
    api = FakeApi(delay=0.02)
    assert run_cli([str(root), "--kb", KB], api)[0] == 0
    for i in range(10):
        api.docs[f"old{i}.md"] = {**api.docs["keep.md"], "document_key": f"old{i}.md"}
    api.max_in_flight = 0
    code, _, err = run_cli([str(root), "--kb", KB, "--concurrency", "2", "--delete-missing", "--delete-scope", "all", "--yes-delete-all"], api)
    assert code == 0, err
    assert set(api.docs) == {"keep.md"} and api.max_in_flight <= 2


@pytest.mark.parametrize("value", ["0", "-1", "33", "abc", "2.5"])
def test_bad_concurrency_values_exit_2(small: Path, value: str) -> None:
    api = FakeApi()
    if value in ("abc", "2.5"):
        with pytest.raises(SystemExit) as exc:
            run_cli([str(small), "--kb", KB, "--concurrency", value], api)
        assert exc.value.code == 2
    else:
        code, _, err = run_cli([str(small), "--kb", KB, "--concurrency", value], api)
        assert code == 2 and "--concurrency" in err
    assert api.requests == []


# ---------------------------------------------------------------------------------------- retries


class Flaky(FakeApi):
    def __init__(self, statuses: list[int]) -> None:
        super().__init__()
        self.statuses = list(statuses)

    def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
        if method == "PUT" and self.statuses:
            status = self.statuses.pop(0)
            self.requests.append((method, path, json, params))
            return ApiResponse(status, {"detail": f"flaky {status}"})
        return super().request(method, path, json=json, params=params)


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_retryable_statuses_retry_with_the_same_run_id_and_succeed(tmp_path: Path, status: int) -> None:
    root = tmp_path / "t"
    write(root, "one.md")
    sleeps: list[float] = []
    api = Flaky([status, status])
    out_, err_ = io.StringIO(), io.StringIO()
    code = kb_import.main(
        [str(root), "--kb", KB], client_factory=lambda u, t: api, environ={"EAP_API_TOKEN": TOKEN},
        sleep=sleeps.append, out=out_, err=err_,
    )
    assert code == 0, err_.getvalue()
    puts = [b for m, _, b, _ in api.requests if m == "PUT"]
    assert len(puts) == 3 and len({b["run_id"] for b in puts}) == 1
    # Equal jitter: half of 1 s, 2 s fixed, half random (P1.A6 review: no lockstep retries).
    assert len(sleeps) == 2 and 0.5 <= sleeps[0] <= 1 and 1 <= sleeps[1] <= 2 and "created 1" in out_.getvalue()


@pytest.mark.parametrize("status", [400, 401, 404, 409, 413, 415, 422, 500])
def test_other_statuses_are_not_retried(tmp_path: Path, status: int) -> None:
    root = tmp_path / "t"
    write(root, "one.md")
    api = Flaky([status, 202])
    code, out, _ = run_cli([str(root), "--kb", KB], api)
    assert code == 1 and len([1 for m, *_ in api.requests if m == "PUT"]) == 1
    assert f"HTTP {status}" in out


def test_retries_are_bounded_then_the_file_fails_with_exit_1(tmp_path: Path) -> None:
    root = tmp_path / "t"
    write(root, "one.md")
    write(root, "two.md")
    api = Flaky([503] * 50)
    code, out, _ = run_cli([str(root), "--kb", KB], api)
    assert code == 1 and "HTTP 503" in out
    assert 2 <= len([1 for m, *_ in api.requests if m == "PUT"]) <= 2 * kb_import.MAX_ATTEMPTS


# ------------------------------------------------------------------------------------- exit codes


def test_exit_0_when_everything_imports_or_is_unchanged(small: Path) -> None:
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB], api)[0] == 0
    assert run_cli([str(small), "--kb", KB], api)[0] == 0


def test_exit_1_for_any_failed_file_and_for_a_failed_delete(small: Path) -> None:
    api = FakeApi()
    api.fail_keys["a.md"] = (422, "nope")
    assert run_cli([str(small), "--kb", KB], api)[0] == 1

    class NoDelete(FakeApi):
        def request(self, method, path, *, json=None, params=None):  # type: ignore[override]
            if method == "DELETE":
                return ApiResponse(500, {"detail": "boom"})
            return super().request(method, path, json=json, params=params)

    api2 = NoDelete()
    assert run_cli([str(small), "--kb", KB], api2)[0] == 0
    api2.docs["old.md"] = {**api2.docs["a.md"], "document_key": "old.md"}
    code, out, _ = run_cli([str(small), "--kb", KB, "--delete-missing", "--delete-scope", "all", "--max-delete-percent", "25"], api2)
    assert code == 1 and "DELETE FAILED old.md" in out


def test_retry_failed_reingests_an_unchanged_failed_document(small: Path) -> None:
    api = FakeApi()
    assert run_cli([str(small), "--kb", KB], api)[0] == 0
    api.docs["a.md"]["status"] = "failed"
    code, out, _ = run_cli([str(small), "--kb", KB], api)
    assert code == 0 and "last ingest failed" in out and api.docs["a.md"]["status"] == "failed"
    code, out, _ = run_cli([str(small), "--kb", KB, "--retry-failed"], api)
    assert code == 0 and "re-ingest started 1" in out and api.docs["a.md"]["status"] == "pending"
    posts = [b for m, p, b, _ in api.requests if m == "POST"]
    assert len(posts) == 1 and posts[0]["run_id"]


@pytest.mark.parametrize(
    "argv",
    [
        ["{dir}"],  # --kb missing: argparse usage error
        ["{dir}", "--kb", KB, "--nonsense"],
        ["{dir}", "--kb", KB, "--timeout", "x"],
    ],
)
def test_argparse_usage_errors_exit_2(small: Path, argv: list[str]) -> None:
    api = FakeApi()
    with pytest.raises(SystemExit) as exc:
        run_cli([a.replace("{dir}", str(small)) for a in argv], api)
    assert exc.value.code == 2 and api.requests == []


def test_exit_2_for_runtime_usage_problems(small: Path, tmp_path: Path) -> None:
    missing_dir = tmp_path / "nope"
    file_arg = small / "a.md"
    cases = [
        [str(missing_dir), "--kb", KB],
        [str(file_arg), "--kb", KB],
        [str(small), "--kb", KB, "--key-prefix", "a/b"],
        [str(small), "--kb", KB, "--key-prefix", " pad "],
    ]
    for argv in cases:
        api = FakeApi()
        code, out, err = run_cli(argv, api)
        assert code == 2 and err.startswith("error:") and out == "" and api.requests == [], argv
    api = FakeApi()
    api.kb_exists = False
    code, _, err = run_cli([str(small), "--kb", KB], api)
    assert code == 2 and "not found" in err
    api = FakeApi()
    code, _, err = run_cli([str(small), "--kb", KB], api, environ={})
    assert code == 2 and "EAP_API_TOKEN" in err


def test_dry_run_works_without_a_token_and_exits_0(small: Path) -> None:
    api = FakeApi()
    code, out, _ = run_cli([str(small), "--kb", KB, "--dry-run"], api, environ={})
    assert code == 0 and api.requests == [] and "would upload 3" in out


# --------------------------------------------------------------------------------- live (in-process)
# The dev API on :8000 serves the main branch, which predates P1.A4 (no `content_base64`, no native
# formats), so the CLI cannot run against it. This builds the API from this worktree instead, on the
# dev Postgres, with a fake DBOS enqueue (no worker): the CLI drives the real routes over HTTP-shaped
# calls. Token: `Bearer dev:acme:alice`; the knowledge base is throwaway and deleted in `finally`.

PG_URL = os.environ.get("EAP_TEST_DATABASE_URL") or "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
LIVE_TOKEN = "Bearer dev:acme:alice"


class RecordingDBOS:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], tuple[Any, ...]]] = []

    def enqueue(self, options: dict[str, Any], *args: Any) -> None:
        self.calls.append((dict(options), args))


class TestClientAdapter:
    """The importer's `ApiClient` over a FastAPI TestClient (the same routes the dev API serves)."""

    __test__ = False

    def __init__(self, client: Any, token: str) -> None:
        self.client, self.token = client, token

    def request(self, method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None):
        r = self.client.request(method, "/api/v1" + path, json=json, params=params, headers={"Authorization": self.token})
        return ApiResponse(r.status_code, r.json() if r.content else None)


def test_live_import_into_a_throwaway_kb_then_rerun_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import uuid

    from eap_api.app import create_app
    from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
    from fastapi.testclient import TestClient

    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    dbos = RecordingDBOS()
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "kb-import-acceptance"), client=dbos
    )
    root = tmp_path / "tree"
    write(root, "guide/intro.md", "# Intro\n\nHello knowledge base.\n")
    write(root, "guide/setup.rst", "Setup\n=====\n\nInstall it.\n")
    write(root, "ref/notes.txt", "plain reference text\n")
    (root / "ref" / "spec.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n")
    kb_id: str | None = None
    with TestClient(app) as client:
        api = TestClientAdapter(client, LIVE_TOKEN)
        try:
            name = f"kb_import acceptance {uuid.uuid4().hex[:8]}"
            created = api.request("POST", "/knowledge-bases", json={"name": name, "embedding_model": "test:hash"})
            assert created.status == 201, created.body
            kb_id = created.body["id"]
            out, err = io.StringIO(), io.StringIO()
            args = [str(root), "--kb", kb_id, "--key-prefix", "acc~", "--metadata-map", str(_map(tmp_path))]
            run = lambda extra=(): kb_import.main(  # noqa: E731
                [*args, *extra], client_factory=lambda u, t: api, environ={"EAP_API_TOKEN": LIVE_TOKEN},
                sleep=lambda s: None, out=out, err=err,
            )
            assert run(["--dry-run"]) == 0 and "would create 4" in out.getvalue()
            assert api.request("GET", f"/knowledge-bases/{kb_id}/documents").body["items"] == []
            out.truncate(0), out.seek(0)
            assert run() == 0, err.getvalue()
            assert "created 4" in out.getvalue()
            docs = api.request("GET", f"/knowledge-bases/{kb_id}/documents", params={"limit": 200}).body["items"]
            assert sorted(d["document_key"] for d in docs) == [
                "acc~guide~intro.md", "acc~guide~setup.rst", "acc~ref~notes.txt", "acc~ref~spec.pdf",
            ]
            assert {d["mime_type"] for d in docs} == {"text/markdown", "text/x-rst", "text/plain", "application/pdf"}
            assert all(d["metadata"] == {"area": "live", **kb_import.stamp("acc~")} for d in docs if d["document_key"].startswith("acc~guide"))
            assert len(dbos.calls) == 4  # one ingest run enqueued per new document
            out.truncate(0), out.seek(0)
            assert run() == 0, err.getvalue()
            assert "unchanged 4" in out.getvalue() and "created" not in out.getvalue()
            assert len(dbos.calls) == 4  # the no-op started no run
            assert TOKEN not in out.getvalue() + err.getvalue() and "alice" not in out.getvalue() + err.getvalue()
        finally:
            if kb_id is not None:
                assert api.request("DELETE", f"/knowledge-bases/{kb_id}").status in (200, 204)
                assert api.request("GET", f"/knowledge-bases/{kb_id}").status == 404
    app.state.database_engine.dispose()


def _map(tmp_path: Path) -> Path:
    path = tmp_path / "live-map.yaml"
    path.write_text("guide/**: {area: live}\n")
    return path
