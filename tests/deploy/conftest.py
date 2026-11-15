"""Shared paths for deploy acceptance tests."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"
K8S_DIR = DEPLOY_DIR / "k8s"
SCRIPTS_DIR = DEPLOY_DIR / "scripts"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
PROFILE = "pydanticos"
EXPECTED_CONTEXT = "colima-pydanticos"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
