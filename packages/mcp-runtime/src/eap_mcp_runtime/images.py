"""Map a catalog package to the image, args and env a managed server runs with.

`npm` and `pypi` packages run on two platform runner images that install the package at pod start
(the operator cannot take `npx://` packages). The package reference travels only as the env
variable `MCP_PACKAGE`; the runner entrypoint passes it to `npx`/`uvx` as one argv element, so it is
never parsed by a shell. Identifiers and versions are validated here as well, so that a hostile
catalog entry cannot become an option (`--index-url=...`) or a second argument.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

NODE_IMAGE_ENV = "EAP_MCP_RUNNER_NODE_IMAGE"
PYTHON_IMAGE_ENV = "EAP_MCP_RUNNER_PYTHON_IMAGE"
TAG_ENV = "EAP_MCP_RUNNER_TAG"
PACKAGE_ENV = "MCP_PACKAGE"
NODE_IMAGE_NAME = "pydanticos-mcp-runner-node"
PYTHON_IMAGE_NAME = "pydanticos-mcp-runner-python"
# What each image is built from, relative to deploy/docker; the default tag hashes these in order.
NODE_FILES = ("mcp-runner-node.Dockerfile",)
PYTHON_FILES = ("mcp-runner-python.Dockerfile", "mcp-run-python.sh")

# npm: optional @scope/, lowercase name. PyPI: PEP 508 name (no extras). Neither may start with "-".
_NPM_NAME = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
_PYPI_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_~-]*$")  # semver, PEP 440, tags; no whitespace or shell syntax
_OCI_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


class UnsupportedPackage(ValueError):
    """The package cannot be run safely (unknown registry or a malformed identifier or version)."""


@dataclass(frozen=True)
class PackageSpec:
    registry_type: str  # npm | pypi | oci
    identifier: str
    version: str
    image: str | None = None  # oci: the entry's image, ideally already pinned by digest


def _runner_files_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "deploy" / "docker").is_dir():
            return parent / "deploy" / "docker"
    return None


def _tag(files: tuple[str, ...]) -> str:
    """Tag of a default runner image: `EAP_MCP_RUNNER_TAG`, else a hash of the files the image is built
    from (`deploy/scripts/up.sh` computes the same), so it changes only when the image does and a code
    commit does not force a rebuild. `local` when the files are not there (an installed package)."""
    explicit = os.environ.get(TAG_ENV)
    if explicit:
        return explicit
    base = _runner_files_dir()
    if base is None or not all((base / f).is_file() for f in files):
        return "local"
    return hashlib.sha256(b"".join((base / f).read_bytes() for f in files)).hexdigest()[:12]


def _runner(env_name: str, image_name: str, files: tuple[str, ...]) -> str:
    return os.environ.get(env_name) or f"{image_name}:{_tag(files)}"


def _check(pattern: re.Pattern[str], value: str, what: str) -> str:
    if not value or len(value) > 214 or not pattern.fullmatch(value):
        raise UnsupportedPackage(f"invalid {what}")
    return value


def image_for(package: PackageSpec) -> tuple[str, tuple[str, ...], dict[str, str]]:
    """Return `(image, args, env)` for a catalog package."""
    kind = package.registry_type
    if kind == "npm":
        ident = _check(_NPM_NAME, package.identifier, "npm package name")
        version = _check(_VERSION, package.version, "npm package version")
        return _runner(NODE_IMAGE_ENV, NODE_IMAGE_NAME, NODE_FILES), (), {PACKAGE_ENV: f"{ident}@{version}"}
    if kind == "pypi":
        ident = _check(_PYPI_NAME, package.identifier, "pypi package name")
        version = _check(_VERSION, package.version, "pypi package version")
        return _runner(PYTHON_IMAGE_ENV, PYTHON_IMAGE_NAME, PYTHON_FILES), (), {PACKAGE_ENV: f"{ident}=={version}"}
    if kind == "oci":
        ref = package.image or f"{package.identifier}:{package.version}"
        return _check(_OCI_REF, ref, "image reference"), (), {}
    raise UnsupportedPackage(f"unsupported package registry: {kind!r}")
