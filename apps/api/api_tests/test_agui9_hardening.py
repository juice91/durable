"""AGUI.9: auth modes (Q2), AG-UI CORS (D17), the per-key origin check, and the router sweep proving
API keys work only on /api/v1/ag-ui/*. Implementer's focused tests; the acceptance suite adds more."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app
from eap_api.auth import require_allowed_origin
from eap_api.cors import STUDIO_ORIGINS, CorsConfigurationError, agui_cors_origins_from_env, is_agui_path

from .conftest import create_agent, get_auth_header

KEYS = "/api/v1/api-keys"
EXTRA = "https://app.example.com"
STUDIO = "http://localhost:3000"
DEV_OFF = {"detail": "dev bearer authentication is not enabled"}


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _create_key(client, tenant, **body) -> str:
    r = client.post(KEYS, json={"name": "k", **body}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _thread_url(agent_id: str) -> str:
    return f"/api/v1/ag-ui/agents/{agent_id}/threads/t1"


# --------------------------------------------------------------------------- pure


def test_cors_origins_parse_normalize_and_dedupe():
    env = {"EAP_AGUI_CORS_ORIGINS": " https://App.Example.com:443 , http://x.test:8080,,https://app.example.com"}
    assert agui_cors_origins_from_env(env) == ("https://app.example.com", "http://x.test:8080")
    assert agui_cors_origins_from_env({}) == ()
    assert agui_cors_origins_from_env({"EAP_AGUI_CORS_ORIGINS": "  "}) == ()


@pytest.mark.parametrize(
    "bad", ["*", "https://*.a.test", "https://a*.test", "http://a.test:*", "app.example.com", "https://a.test/path", "ftp://a.test", "null"]
)
def test_cors_origins_refuse_wildcards_and_non_origins(bad):
    with pytest.raises(CorsConfigurationError) as info:
        agui_cors_origins_from_env({"EAP_AGUI_CORS_ORIGINS": f"https://ok.test,{bad}"})
    assert "EAP_AGUI_CORS_ORIGINS" in str(info.value) and repr(bad) in str(info.value)


def test_is_agui_path():
    assert is_agui_path("/api/v1/ag-ui") and is_agui_path("/api/v1/ag-ui/agents/x")
    assert not is_agui_path("/api/v1/ag-uix") and not is_agui_path("/api/v1/agents")


def test_private_deployment_tunnel_is_a_studio_origin():
    assert "http://localhost:13000" in STUDIO_ORIGINS
    assert "http://127.0.0.1:13000" in STUDIO_ORIGINS


def test_require_allowed_origin_rules():
    from fastapi import HTTPException

    require_allowed_origin(None, "https://evil.test")  # NULL list: any origin
    require_allowed_origin(("https://a.test",), None)  # no Origin: a server-side caller
    require_allowed_origin(("https://a.test",), "HTTPS://A.test:443")  # compared normalized
    for origin in ("https://b.test", "null", "https://a.test.evil.test", ""):
        with pytest.raises(HTTPException) as info:
            require_allowed_origin(("https://a.test",), origin)
        assert info.value.status_code == 403 and info.value.detail == "Origin not allowed for this API key"
    with pytest.raises(HTTPException):
        require_allowed_origin((), "https://a.test")  # an empty list allows no browser origin


# --------------------------------------------------------------------------- startup (Q2)


@pytest.mark.parametrize("mode", [None, "production", "DEV", "API_KEY", "apikey"])
def test_create_app_refuses_without_a_known_auth_mode(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("EAP_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("EAP_AUTH_MODE", mode)
    with pytest.raises(RuntimeError, match=re.escape("EAP_AUTH_MODE=api_key")):
        create_app(database_url="postgresql+psycopg://unused@localhost:1/unused")


def test_create_app_refuses_a_malformed_cors_allowlist(monkeypatch):
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", "*")
    with pytest.raises(CorsConfigurationError):
        create_app(database_url="postgresql+psycopg://unused@localhost:1/unused")


def test_api_key_mode_starts_and_serves_only_keys_on_ag_ui(client, tenant, monkeypatch):
    agent_id = create_agent(client, tenant, "alice")
    key = _create_key(client, tenant)
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    from .run_helpers import PG_URL

    app = create_app(database_url=PG_URL)
    try:
        with TestClient(app) as prod:
            assert prod.get("/healthz").status_code == 200
            assert prod.get(_thread_url(agent_id), headers=_bearer(key)).status_code == 200
            dev = prod.get(_thread_url(agent_id), headers=get_auth_header(tenant, "alice"))
            assert dev.status_code == 401 and dev.json() == DEV_OFF
            for headers in (get_auth_header(tenant, "alice"), _bearer(key)):
                r = prod.get("/api/v1/agents", headers=headers)
                assert r.status_code == 401 and r.json() == DEV_OFF
                assert prod.get(KEYS, headers=headers).status_code == 401
    finally:
        app.state.database_engine.dispose()


# --------------------------------------------------------------------------- CORS (D17)


@pytest.fixture
def cors_client(monkeypatch):
    from .run_helpers import PG_URL

    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("the API needs Postgres")
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", EXTRA)
    app = create_app(database_url=PG_URL)
    with TestClient(app) as test_client:
        yield test_client
    app.state.database_engine.dispose()


def _preflight(c, path, origin, method="POST", headers="authorization,content-type"):
    return c.options(
        path,
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers},
    )


def test_preflight_from_the_agui_allowlist_passes_on_ag_ui_only(cors_client):
    agui = _preflight(cors_client, "/api/v1/ag-ui/agents/x", EXTRA)
    assert agui.status_code == 200
    assert agui.headers["access-control-allow-origin"] == EXTRA
    assert "access-control-allow-credentials" not in agui.headers
    assert set(agui.headers["access-control-allow-methods"].replace(" ", "").split(",")) >= {"GET", "POST"}
    resume = _preflight(cors_client, "/api/v1/ag-ui/agents/x/threads/t/runs/r/stream", EXTRA, "GET", "last-event-id")
    assert resume.status_code == 200 and resume.headers["access-control-allow-origin"] == EXTRA
    # The same origin elsewhere: no CORS headers (Starlette answers a disallowed preflight 400).
    for path in ("/api/v1/agents", KEYS, "/api/v1/ag-uix"):
        other = _preflight(cors_client, path, EXTRA)
        assert "access-control-allow-origin" not in other.headers, path
    # A simple GET elsewhere from that origin: no CORS header either, so the browser blocks the read.
    assert "access-control-allow-origin" not in cors_client.get("/healthz", headers={"Origin": EXTRA}).headers


def test_a_disallowed_origin_gets_no_cors_headers(cors_client):
    for path in ("/api/v1/ag-ui/agents/x", "/api/v1/agents"):
        r = _preflight(cors_client, path, "https://evil.test")
        assert "access-control-allow-origin" not in r.headers
    r = cors_client.get("/api/v1/ag-ui/agents/x/threads/t", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers


def test_studio_keeps_cors_everywhere(cors_client):
    for path in ("/api/v1/agents", "/api/v1/ag-ui/agents/x"):
        r = _preflight(cors_client, path, STUDIO)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == STUDIO
    assert _preflight(cors_client, "/api/v1/agents", STUDIO).headers["access-control-allow-credentials"] == "true"
    r = cors_client.get("/api/v1/ag-ui/agents/x/threads/t", headers={"Origin": STUDIO})
    assert r.headers["access-control-allow-origin"] == STUDIO
    assert r.headers["access-control-expose-headers"] == "Retry-After"


def test_an_agui_preflight_refuses_other_methods_and_headers(cors_client):
    assert _preflight(cors_client, "/api/v1/ag-ui/agents/x", EXTRA, method="DELETE").status_code == 400
    assert _preflight(cors_client, "/api/v1/ag-ui/agents/x", EXTRA, headers="x-custom").status_code == 400


# --------------------------------------------------------------------------- per-key origin


def test_per_key_origin_check_on_every_agui_route(client, tenant):
    agent_id = create_agent(client, tenant, "alice")
    pinned = _create_key(client, tenant, allowed_origins=["https://ok.test"])
    anyorigin = _create_key(client, tenant)
    url = _thread_url(agent_id)
    assert client.get(url, headers=_bearer(pinned) | {"Origin": "https://ok.test"}).status_code == 200
    assert client.get(url, headers=_bearer(pinned) | {"Origin": "https://OK.test:443"}).status_code == 200
    assert client.get(url, headers=_bearer(pinned)).status_code == 200  # no Origin: server-side
    assert client.get(url, headers=_bearer(anyorigin) | {"Origin": "https://evil.test"}).status_code == 200
    forbidden = {"detail": "Origin not allowed for this API key"}
    bad = {"Origin": "https://evil.test"}
    r = client.get(url, headers=_bearer(pinned) | bad)
    assert r.status_code == 403 and r.json() == forbidden
    # The attach route: 403 before the unknown-run 404, and before any stream.
    r = client.get(f"{url}/runs/r1/stream", headers=_bearer(pinned) | bad | {"Accept": "text/event-stream"})
    assert r.status_code == 403 and r.json() == forbidden
    # The POST: 403 before the body is looked at, so no run is recorded.
    body = {"threadId": "t1", "runId": "r1", "messages": [{"id": "u1", "role": "user", "content": "hi"}]}
    r = client.post(f"/api/v1/ag-ui/agents/{agent_id}", json=body, headers=_bearer(pinned) | bad)
    assert r.status_code == 403 and r.json() == forbidden
    assert client.get(url, headers=_bearer(pinned)).json()["runs"] == []
    # The dev bearer has no origin list.
    assert client.get(url, headers=get_auth_header(tenant, "alice") | bad).status_code == 200


# --------------------------------------------------------------------------- router sweep


def _api_routes(app) -> list[tuple[str, str]]:
    """Every (method, sample path) of the app, from its OpenAPI schema (no route sets
    include_in_schema=False; FastAPI 0.162 keeps included routers nested, so app.routes is not
    flat). Path parameters get a random UUID, or 1 for an integer."""
    out = []
    for template, item in app.openapi()["paths"].items():
        for method, op in item.items():
            if method.upper() in ("HEAD", "OPTIONS") or not isinstance(op, dict):
                continue
            path = template
            for param in op.get("parameters", []):
                if param.get("in") == "path":
                    value = "1" if param.get("schema", {}).get("type") == "integer" else str(uuid.uuid4())
                    path = path.replace("{" + param["name"] + "}", value)
            assert "{" not in path, path
            out.append((method.upper(), path))
    return out


# Routes that answer without credentials on purpose.
PUBLIC = {"/healthz"}


def _non_agui_api_routes(app) -> list[tuple[str, str]]:
    return [(m, p) for m, p in _api_routes(app) if p not in PUBLIC and not is_agui_path(p)]


def _sweep(c, headers) -> dict[tuple[str, str], int]:
    failures = {}
    for method, path in _non_agui_api_routes(c.app):
        r = c.request(method, path, headers=headers)
        if r.status_code != 401:
            failures[(method, path)] = r.status_code
    return failures


def test_router_sweep_a_key_works_on_no_route_outside_ag_ui(client, tenant, monkeypatch):
    agent_id = create_agent(client, tenant, "alice")
    key = _create_key(client, tenant)
    routes = _non_agui_api_routes(client.app)
    assert len(routes) > 50 and ("POST", "/api/v1/api-keys") in routes  # the sweep is not empty
    # The key is valid: it works on /ag-ui/*.
    assert client.get(_thread_url(agent_id), headers=_bearer(key)).status_code == 200
    assert _sweep(client, _bearer(key)) == {}  # dev mode: a key is not a dev bearer
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    assert client.get(_thread_url(agent_id), headers=_bearer(key)).status_code == 200
    assert _sweep(client, _bearer(key)) == {}
    assert _sweep(client, get_auth_header(tenant, "alice")) == {}  # api_key mode: no dev bearer either
    assert _sweep(client, {}) == {}  # and no route is open without credentials


def test_every_agui_route_authenticates(client):
    """Only /ag-ui/* and /healthz are outside the sweep, and every /ag-ui/* route asks for
    credentials (agui_principal) before anything else."""
    agui = [(m, p) for m, p in _api_routes(client.app) if is_agui_path(p)]
    assert {m for m, _ in agui} == {"GET", "POST", "DELETE"} and len(agui) >= 3
    for method, path in agui:
        r = client.request(method, path)
        assert r.status_code == 401 and r.json() == {"detail": "Missing Authorization header"}, path


def test_normalize_origin_refuses_wildcards_anywhere():
    from eap_run_store import normalize_origin

    for bad in ("*", "https://*", "https://*.example.com", "https://a.*.test", "*://a.test", "http://a.test:*"):
        with pytest.raises(ValueError, match="wildcards are not allowed"):
            normalize_origin(bad)


def test_the_key_api_refuses_a_wildcard_origin_with_422(client, tenant):
    r = client.post(KEYS, json={"name": "k", "allowed_origins": ["https://*.example.com"]},
                    headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 422 and "wildcards are not allowed" in r.text
    assert client.get(KEYS, headers=get_auth_header(tenant, "alice")).json()["items"] == []


# --------------------------------------------------------------------------- AGUI.9 review fixes

DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")


def _all_routes(routes, prefix: str = "", in_schema: bool = True):
    """(full path, route, in schema) for every route of an app, walking the routers FastAPI 0.162
    keeps nested: an `_IncludedRouter` holds its `original_router` and an include context with the
    include prefix and include_in_schema."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = route.include_context
            yield from _all_routes(inner.routes, prefix + ctx.prefix, in_schema and ctx.include_in_schema)
        else:
            yield prefix + route.path, route, in_schema and getattr(route, "include_in_schema", True)


def test_the_sweep_sees_every_route(client):
    """The OpenAPI-based sweep cannot miss a route: every APIRoute is in the schema, and the only
    other routes are the docs pages (dev mode). A future include_in_schema=False route or a mount
    fails here."""
    from fastapi.routing import APIRoute
    from starlette.routing import Mount

    routes = list(_all_routes(client.app.routes))
    assert not any(isinstance(r, Mount) for _, r, _ in routes)
    api = [(path, r) for path, r, _ in routes if isinstance(r, APIRoute)]
    assert [path for path, r, shown in routes if isinstance(r, APIRoute) and not shown] == []
    schema_ops = {(m, p) for p, item in client.app.openapi()["paths"].items() for m in item}
    # OpenAPI drops a path converter: `/x/{name:path}` is `/x/{name}` in the schema.
    route_ops = {(m.lower(), re.sub(r"\{(\w+):\w+\}", r"{\1}", path)) for path, r in api for m in r.methods - {"HEAD", "OPTIONS"}}
    assert route_ops == schema_ops and len(route_ops) > 50
    assert {path for path, r, _ in routes if not isinstance(r, APIRoute)} <= set(DOC_PATHS)


def test_api_key_mode_serves_no_docs_or_schema(client, monkeypatch):
    from .run_helpers import PG_URL

    for path in DOC_PATHS[:2] + DOC_PATHS[3:]:
        assert client.get(path).status_code == 200, path  # dev mode keeps them
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    app = create_app(database_url=PG_URL)
    try:
        with TestClient(app) as prod:
            for path in DOC_PATHS:
                assert prod.get(path).status_code == 404, path
            assert {path for path, _, _ in _all_routes(app.routes) if path in DOC_PATHS} == set()
            assert prod.get("/healthz").status_code == 200
    finally:
        app.state.database_engine.dispose()


def test_normalize_origin_encodes_internationalized_hosts():
    from eap_run_store import normalize_origin

    assert normalize_origin("https://bücher.example") == "https://xn--bcher-kva.example"
    assert normalize_origin("https://BÜCHER.example:8443") == "https://xn--bcher-kva.example:8443"
    require_allowed_origin((normalize_origin("https://bücher.example"),), "https://xn--bcher-kva.example")


def test_a_request_refused_for_its_origin_does_not_touch_last_used(client, tenant):
    from sqlalchemy import text

    agent_id = create_agent(client, tenant, "alice")
    key = _create_key(client, tenant, allowed_origins=["https://ok.test"])
    key_id = key.split("_", 2)[1]

    def last_used():
        with client.app.state.database_engine.connect() as conn:
            return conn.execute(text("SELECT last_used_at FROM api_keys WHERE id = :i"), {"i": key_id}).scalar()

    url = _thread_url(agent_id)
    assert client.get(url, headers=_bearer(key) | {"Origin": "https://evil.test"}).status_code == 403
    assert last_used() is None
    assert client.get(url, headers=_bearer(key) | {"Origin": "https://ok.test"}).status_code == 200
    assert last_used() is not None
