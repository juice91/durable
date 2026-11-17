"""`image_for`: map a catalog package to a runner image, args and env (no cluster needed)."""

from __future__ import annotations

from pathlib import Path

import pytest
from eap_mcp_runtime.images import (
    NODE_IMAGE_ENV,
    PYTHON_IMAGE_ENV,
    PackageSpec,
    UnsupportedPackage,
    image_for,
)

REPO = Path(__file__).resolve().parents[3]


def test_npm_runs_on_the_node_runner_with_the_package_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NODE_IMAGE_ENV, "reg/node:1")
    image, args, env = image_for(PackageSpec("npm", "@modelcontextprotocol/server-memory", "2.1.1"))
    assert image == "reg/node:1"
    assert args == ()
    assert env == {"MCP_PACKAGE": "@modelcontextprotocol/server-memory@2.1.1"}


def test_pypi_runs_on_the_python_runner_pinned_with_double_equals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PYTHON_IMAGE_ENV, "reg/py:1")
    image, args, env = image_for(PackageSpec("pypi", "mcp-server-time", "2026.11.11"))
    assert image == "reg/py:1"
    assert args == ()
    assert env == {"MCP_PACKAGE": "mcp-server-time==2026.11.11"}


def test_oci_uses_the_entry_image_unchanged() -> None:
    assert image_for(PackageSpec("oci", "mcp/everything", "latest", image="mcp/everything@sha256:" + "a" * 64)) == (
        "mcp/everything@sha256:" + "a" * 64,
        (),
        {},
    )
    assert image_for(PackageSpec("oci", "mcp/everything", "1.2")) == ("mcp/everything:1.2", (), {})


def test_default_images_are_sha_tagged_local_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(NODE_IMAGE_ENV, raising=False)
    monkeypatch.delenv(PYTHON_IMAGE_ENV, raising=False)
    node = image_for(PackageSpec("npm", "x", "1"))[0]
    py = image_for(PackageSpec("pypi", "x", "1"))[0]
    assert node.startswith("pydanticos-mcp-runner-node:") and py.startswith("pydanticos-mcp-runner-python:")


@pytest.mark.parametrize(
    "registry,ident,version",
    [
        ("npm", "x; rm -rf /", "1"),
        ("npm", "x", "1 && id"),
        ("npm", "x", "$(id)"),
        ("pypi", "x`id`", "1"),
        ("pypi", "x", "1\n2"),
        ("pypi", "--index-url=http://evil", "1"),
        ("npm", "-g", "1"),
        ("npm", "x", "-rf"),
        ("npm", "", "1"),
        ("npm", "x", ""),
    ],
)
def test_hostile_identifiers_and_versions_are_refused(registry: str, ident: str, version: str) -> None:
    with pytest.raises(UnsupportedPackage):
        image_for(PackageSpec(registry, ident, version))


def test_unknown_registry_is_refused() -> None:
    with pytest.raises(UnsupportedPackage):
        image_for(PackageSpec("nuget", "x", "1"))


def test_runner_dockerfiles_are_non_root_and_never_interpolate_the_package() -> None:
    for name, tool in (("node", "npx"), ("python", "uvx")):
        text = (REPO / f"deploy/docker/mcp-runner-{name}.Dockerfile").read_text()
        assert "USER 10001:10001" in text
        assert "MCP_PACKAGE" in text and tool in text
        assert "ARG MCP_PACKAGE" not in text
        assert 'sh -c' not in text


def test_default_runner_tag_is_a_content_hash_of_the_runner_files_not_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    from eap_mcp_runtime.images import NODE_FILES, PYTHON_FILES

    monkeypatch.delenv("EAP_MCP_RUNNER_TAG", raising=False)
    monkeypatch.delenv(PYTHON_IMAGE_ENV, raising=False)
    monkeypatch.delenv(NODE_IMAGE_ENV, raising=False)
    docker = REPO / "deploy" / "docker"

    def digest(files: tuple[str, ...]) -> str:
        return hashlib.sha256(b"".join((docker / f).read_bytes() for f in files)).hexdigest()[:12]

    py, _, _ = image_for(PackageSpec("pypi", "mcp-server-time", "1.0"))
    node, _, _ = image_for(PackageSpec("npm", "x", "1.0"))
    assert py == f"pydanticos-mcp-runner-python:{digest(PYTHON_FILES)}"
    assert node == f"pydanticos-mcp-runner-node:{digest(NODE_FILES)}"
    assert "mcp-runner-python.Dockerfile" in PYTHON_FILES


def test_up_sh_tags_the_runner_images_with_the_same_content_hash() -> None:
    up = (REPO / "deploy" / "scripts" / "up.sh").read_text()
    assert "mcp-runner-python.Dockerfile mcp-run-python.sh" in up and "shasum -a 256" in up
    assert 'runner_python_image="pydanticos-mcp-runner-python:${runner_python_tag}"' in up


def test_the_explicit_runner_tag_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PYTHON_IMAGE_ENV, raising=False)
    monkeypatch.setenv("EAP_MCP_RUNNER_TAG", "abc")
    assert image_for(PackageSpec("pypi", "mcp-server-time", "1.0"))[0] == "pydanticos-mcp-runner-python:abc"
