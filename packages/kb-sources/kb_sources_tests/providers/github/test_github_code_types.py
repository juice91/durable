"""Source code in the GitHub adapter: opt-in `document_policy.types`, the Dockerfile name rule, the
`kind` / `language` metadata defaults, the commit-pinned URL; the default types stay as they were."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eap_kb_sources.providers.github.adapter import (
    DEFAULT_TYPES,
    EXTENSION_MIME,
    GithubRepositoryDocsAdapter,
    _Config,
    doc_type,
)
from eap_kb_sources.providers.github.fixtures import FakeGithub, ti_like_repo

from .helpers import REPO, MemoryStore, make_adapter, run_segment

CODE = {
    "include": ["src/**", "deploy/**", "docs/**"],
    "exclude": ["**/*_tests/**"],
    "map": [{"match": "src/tests/**", "metadata": {"kind": "test"}}],
    "document_policy": {"types": ["md", "py", "ts", "tsx", "js", "mjs", "sh", "toml", "sql", "dockerfile"]},
}


def test_default_types_are_unchanged_and_exclude_code():
    assert DEFAULT_TYPES == ("md", "rst", "html", "txt", "yaml", "yml", "json", "pdf")
    cfg = _Config({"repository": "o/r", "include": ["**"]})
    assert cfg.types == frozenset(DEFAULT_TYPES)
    for path in ("a.py", "a.ts", "a/Dockerfile", "a.sh", "a.toml"):
        assert not cfg.admits(path)
    assert cfg.admits("docs/a.md") and cfg.admits("a.yaml")
    assert cfg.metadata("docs/a.md") == {"path": "docs/a.md"}  # no code defaults on a document


def test_code_types_are_accepted_and_unknown_ones_refused():
    GithubRepositoryDocsAdapter().validate_config({"repository": "o/r", **CODE})
    with pytest.raises(ValueError, match="document_policy.types"):
        GithubRepositoryDocsAdapter().validate_config({"repository": "o/r", "include": ["a"], "document_policy": {"types": ["rs"]}})


def test_dockerfile_is_a_file_name_rule():
    for path in ("Dockerfile", "deploy/docker/Dockerfile", "deploy/Dockerfile.api", "x/base.dockerfile"):
        assert doc_type(path) == "dockerfile"
    assert doc_type("src/a.PY") == "py" and doc_type("Makefile") == ""
    cfg = _Config({"repository": "o/r", **CODE})
    assert cfg.admits("deploy/docker/Dockerfile") and cfg.admits("deploy/Dockerfile.api")
    assert not cfg.admits("deploy/MyDockerfile")
    assert EXTENSION_MIME["dockerfile"] == "text/x-dockerfile"


def test_code_documents_get_kind_and_language_defaults_a_map_rule_may_override():
    cfg = _Config({"repository": "o/r", **CODE})
    assert cfg.metadata("src/a.py") == {"path": "src/a.py", "kind": "code", "language": "python"}
    assert cfg.metadata("src/web/B.tsx")["language"] == "tsx"
    assert cfg.metadata("src/x.mjs")["language"] == "javascript"
    assert cfg.metadata("deploy/docker/Dockerfile")["language"] == "dockerfile"
    assert cfg.metadata("src/tests/test_a.py") == {"kind": "test", "path": "src/tests/test_a.py", "language": "python"}


def test_a_full_walk_submits_code_with_its_mime_type_metadata_and_commit_url():
    repo = ti_like_repo()
    repo.put("src/app.py", b"def main():\n    return 1\n")
    repo.put("src/web/Card.tsx", b"export const Card = () => null;\n")
    repo.put("src/tests/test_app.py", b"def test_a():\n    pass\n")
    repo.put("deploy/docker/Dockerfile", b"FROM scratch\n")
    repo.put("src/native.so", b"\x7fELF")
    repo.commit()
    server, store = FakeGithub(repo), MemoryStore()
    result = run_segment(make_adapter(server), store, config=CODE)
    assert result.counters.documents_queued >= 4  # the fixture repo has its own files too
    docs = {k.removeprefix("source:s1:"): d for k, d in store.docs.items()}
    assert docs["src~app.py"]["mime"] == "text/x-python"
    assert docs["src~web~Card.tsx"]["mime"] == "text/tsx"
    assert docs["deploy~docker~Dockerfile"]["mime"] == "text/x-dockerfile"
    assert "src~native.so" not in docs
    assert docs["src~app.py"]["metadata"] == {"path": "src/app.py", "kind": "code", "language": "python"}
    assert docs["src~tests~test_app.py"]["metadata"]["kind"] == "test"
    assert docs["src~app.py"]["url"] == f"https://github.com/{REPO}/blob/{repo.head}/src/app.py"


def test_a_code_file_over_max_bytes_is_refused_by_size():
    repo = ti_like_repo()
    repo.put("src/big.py", b"x = 1\n" * 1000)
    repo.commit()
    server, store = FakeGithub(repo), MemoryStore()
    result = run_segment(make_adapter(server), store, config={**CODE, "max_bytes": 2000})
    assert "src~big.py" not in {k.removeprefix("source:s1:") for k in store.docs}
    assert result.counters.failed >= 1


# ----------------------------------------------------------------------------- the committed example

EXAMPLE = Path(__file__).resolve().parents[5] / "examples" / "kb-sources" / "durable-platform-docs.json"


def example_config() -> _Config:
    raw = json.loads(EXAMPLE.read_text())
    GithubRepositoryDocsAdapter().validate_config(raw)
    return _Config(raw, "s1")


@pytest.mark.parametrize("path, kind", [
    ("README.md", "guide"),
    ("docs/superpowers/specs/x.md", "spec"),
    ("docs/providers/github.md", "reference"),
    ("examples/workflows/a.json", "example"),
    ("apps/api/src/eap_api/app.py", "code"),
    ("apps/worker/src/eap_worker/kb_ingest.py", "code"),
    ("apps/studio/src/components/Card.tsx", "code"),
    ("packages/run-store/src/eap_run_store/knowledge.py", "code"),
    ("deploy/scripts/deploy.sh", "code"),
    ("deploy/k8s/api.yaml", "code"),
    ("deploy/docker/Dockerfile", "code"),
    ("packages/db/pyproject.toml", "code"),
])
def test_the_example_includes_docs_and_code_with_the_decided_kind(path, kind):
    cfg = example_config()
    assert cfg.admits(path), path
    assert cfg.metadata(path)["kind"] == kind


@pytest.mark.parametrize("path", [
    "docs/superpowers/plans/p.md", "docs/reviews/r.md", "docs/architect-dogfood-2026.md",
    "packages/db/src/eap_db/migrations/versions/0001_x.py", "deploy/k8s/eso/crds.yaml", "deploy/k8s/secret.template.yaml",
    "apps/studio/node_modules/x/index.js", "apps/studio/.next/server.js", "apps/studio/dist/a.js", "apps/studio/src/a.d.ts",
    "apps/api/api_tests/test_a.py", "apps/studio/src/Card.test.tsx", "packages/db/tests/test_a.py",
    "apps/api/src/fixtures/big.json", "apps/studio/package.json", "apps/studio/src/__snapshots__/a.snap",
    "scripts/kb_import.py", "infrastructure/main.tf", "apps/studio/pnpm-lock.yaml", "packages/db/uv.lock",
    "apps/studio/public/logo.png", "apps/studio/src/a.min.js",
])
def test_the_example_leaves_out_vendored_generated_test_and_unrelated_files(path):
    assert not example_config().admits(path), path


def test_the_example_keeps_the_phase_e_first_match_rules_and_a_size_cap():
    cfg = example_config()
    assert cfg.metadata("docs/superpowers/specs/x.md") == {
        "kind": "spec", "status": "design", "path": "docs/superpowers/specs/x.md"}
    assert cfg.max_bytes == 262144 and cfg.delete_missing is True
    assert cfg.metadata("apps/api/src/a.py")["component"] == "api"
