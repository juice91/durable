"""`requires_k3s`: contract tests that need the local `colima-pydanticos` cluster and operator."""

from __future__ import annotations

import os
import subprocess

import pytest

CONTEXT = "colima-pydanticos"
CRD = "mcpservers.toolhive.stacklok.dev"


def k3s_unavailable_reason() -> str | None:
    try:
        r = subprocess.run(
            ["kubectl", "--context", CONTEXT, "get", "crd", CRD, "-o", "name"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"kubectl is not usable: {e}"
    if r.returncode != 0:
        return f"cluster {CONTEXT} or CRD {CRD} is unreachable: {r.stderr.strip()[:200]}"
    return None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "requires_k3s: needs the local colima-pydanticos k3s with the ToolHive operator"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marked = [i for i in items if i.get_closest_marker("requires_k3s")]
    if not marked:
        return
    reason = k3s_unavailable_reason()
    if reason is None:
        return
    if os.environ.get("EAP_REQUIRE_K3S") == "1":
        for item in marked:
            item.add_marker(pytest.mark.usefixtures("_k3s_required_but_unreachable"))
        return
    skip = pytest.mark.skip(reason=f"{reason} (set EAP_REQUIRE_K3S=1 to fail instead)")
    for item in marked:
        item.add_marker(skip)


@pytest.fixture
def _k3s_required_but_unreachable() -> None:
    pytest.fail(f"EAP_REQUIRE_K3S=1 and {k3s_unavailable_reason()}")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
