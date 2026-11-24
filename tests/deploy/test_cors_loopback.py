"""The SSH-tunnel path: Studio at a loopback origin calls the API's own port-forward cross-origin. The API allows exactly
the configured loopback Studio origins, without credentials, with only the headers Studio needs."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from eap_api.cors import (
    DEFAULT_LOOPBACK_ORIGINS,
    LOOPBACK_ALLOWED_HEADERS,
    CorsConfigurationError,
    PathScopedCORSMiddleware,
    loopback_cors_origins_from_env,
)

ORIGIN = "http://127.0.0.1:13000"


def client(loopback=DEFAULT_LOOPBACK_ORIGINS) -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/api/v1/agents", ok, methods=["GET", "PUT", "OPTIONS"])])
    return TestClient(PathScopedCORSMiddleware(app, loopback_origins=loopback))


def preflight(c, origin, headers="authorization", method="GET"):
    return c.options(
        "/api/v1/agents",
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers},
    )


def test_default_origins_are_the_two_tunnel_studio_origins():
    assert loopback_cors_origins_from_env({}) == ("http://127.0.0.1:13000", "http://localhost:13000")


def test_a_loopback_studio_origin_is_allowed_without_credentials():
    c = client()
    r = preflight(c, ORIGIN, "authorization,content-type,if-match")
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in r.headers
    got = c.get("/api/v1/agents", headers={"Origin": ORIGIN})
    assert got.headers["access-control-allow-origin"] == ORIGIN and "access-control-allow-credentials" not in got.headers
    assert "ETag" in got.headers["access-control-expose-headers"]


def test_only_the_headers_studio_needs_are_allowed():
    c = client()
    assert preflight(c, ORIGIN, "x-evil").status_code == 400
    assert preflight(c, ORIGIN, "cookie").status_code == 400
    assert set(h.lower() for h in LOOPBACK_ALLOWED_HEADERS) == {
        "authorization", "content-type", "accept", "if-match", "last-event-id",
    }


@pytest.mark.parametrize("origin", ["https://durable.example", "http://127.0.0.1:18000", "http://localhost:13001", "null"])
def test_other_origins_get_no_cors_headers(origin):
    c = client()
    assert preflight(c, origin).headers.get("access-control-allow-origin") in (None, "")
    assert c.get("/api/v1/agents", headers={"Origin": origin}).headers.get("access-control-allow-origin") in (None, "")


def test_the_origin_list_is_configurable_and_replaces_the_default():
    origins = loopback_cors_origins_from_env({"EAP_CORS_LOOPBACK_ORIGINS": "http://localhost:14000, http://[::1]:14000"})
    assert origins == ("http://localhost:14000", "http://[::1]:14000")
    c = client(origins)
    assert preflight(c, "http://localhost:14000").headers["access-control-allow-origin"] == "http://localhost:14000"
    assert preflight(c, ORIGIN).headers.get("access-control-allow-origin") in (None, "")


@pytest.mark.parametrize("bad", ["*", "https://studio.example.com", "http://10.0.0.5:13000", "http://127.0.0.1.evil.test", "nope"])
def test_a_non_loopback_entry_refuses_to_start(bad):
    with pytest.raises(CorsConfigurationError):
        loopback_cors_origins_from_env({"EAP_CORS_LOOPBACK_ORIGINS": bad})
