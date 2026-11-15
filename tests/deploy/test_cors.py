"""Port 13000 is a Studio origin only; other origins stay refused."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from eap_api.cors import STUDIO_ORIGINS, PathScopedCORSMiddleware

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "deploy" / "k8s"

TUNNEL = ("http://localhost:13000", "http://127.0.0.1:13000")
DEV = ("http://localhost:3000", "http://127.0.0.1:3000")
REFUSED = (
    "http://localhost:3001",
    "http://localhost:8000",
    "http://localhost:3002",
    "http://127.0.0.1:18000",
    "https://evil.example",
)


def test_studio_origins_add_only_the_private_tunnel_ports():
    assert STUDIO_ORIGINS == DEV + TUNNEL


def test_next_config_does_not_declare_cors_origins():
    text = (REPO_ROOT / "apps/studio/next.config.ts").read_text()
    assert "13000" not in text
    assert "output: \"standalone\"" in text or "output: 'standalone'" in text


def test_deploy_configmap_lists_tunnel_origins_on_agui_allowlist():
    text = (K8S_DIR / "configmap.yaml").read_text()
    assert "EAP_AGUI_CORS_ORIGINS: http://localhost:13000,http://127.0.0.1:13000" in text


def _client() -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/healthz", ok),
            Route("/api/v1/agents", ok),
            Route("/api/v1/ag-ui/agents/x", ok, methods=["GET", "POST", "OPTIONS"]),
        ]
    )
    return TestClient(PathScopedCORSMiddleware(app, agui_origins=()))


def _preflight(client: TestClient, path: str, origin: str):
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_tunnel_origin_is_allowed_on_studio_and_agui_paths():
    client = _client()
    for origin in TUNNEL + DEV:
        for path in ("/healthz", "/api/v1/agents", "/api/v1/ag-ui/agents/x"):
            r = _preflight(client, path, origin)
            assert r.status_code == 200, (origin, path, r.status_code)
            assert r.headers.get("access-control-allow-origin") == origin
            got = client.get(path, headers={"Origin": origin})
            assert got.headers.get("access-control-allow-origin") == origin


def test_other_origins_are_still_refused():
    client = _client()
    for origin in REFUSED:
        for path in ("/healthz", "/api/v1/agents", "/api/v1/ag-ui/agents/x"):
            r = _preflight(client, path, origin)
            assert r.headers.get("access-control-allow-origin") in (None, "")
            got = client.get(path, headers={"Origin": origin})
            assert got.headers.get("access-control-allow-origin") in (None, "")
            assert got.status_code == 200
