"""The production image is built with `uv sync --no-dev`: every module the API or worker imports at
runtime must come from a runtime dependency, not a dev group. The 7dc057a deploy crash-looped the API
because eap_api.mcp_control imported the FastMCP server while `fastmcp-slim[server]` was dev-only."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_PYPROJECT = REPO_ROOT / "apps/api/pyproject.toml"
MCP_CONTROL_SERVER = REPO_ROOT / "apps/api/src/eap_api/mcp_control/server.py"


def _runtime_dependency_names(pyproject: Path) -> list[str]:
    data = tomllib.loads(pyproject.read_text())
    return [d.strip() for d in data["project"]["dependencies"]]


def test_api_runtime_dependencies_include_the_fastmcp_server_extra():
    assert re.search(r"^from fastmcp import FastMCP", MCP_CONTROL_SERVER.read_text(), re.M), (
        "eap_api.mcp_control.server no longer imports FastMCP; retarget this guard at whatever now needs the server extra"
    )
    names = _runtime_dependency_names(API_PYPROJECT)
    assert any(n.startswith("fastmcp-slim[server]") for n in names), names


def test_no_dev_export_resolves_the_fastmcp_server_runtime():
    """`uncalled_for` is the module whose absence raised the ImportError in production."""
    out = subprocess.run(
        ["uv", "export", "--all-packages", "--no-dev", "--frozen", "--no-hashes"],
        cwd=REPO_ROOT, check=True, capture_output=True, text=True,
    ).stdout
    pinned = {line.split("==", 1)[0].strip() for line in out.splitlines() if "==" in line and not line.startswith("#")}
    assert "uncalled-for" in pinned, sorted(p for p in pinned if "fastmcp" in p or "uncalled" in p)
