"""AGUI.9 acceptance: router sweep (mode x credential), AG-UI CORS, per-key origins, startup, no leaks."""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from eap_run_store import parse_api_key
from fastapi.testclient import TestClient

from eap_api.app import create_app
from eap_api.cors import is_agui_path

from .conftest import create_agent, get_auth_header
from .run_helpers import PG_URL

KEYS = "/api/v1/api-keys"
EXTRA = "https://app.example.com"
STUDIO = "http://localhost:3000"
DEV_OFF = {"detail": "dev bearer authentication is not enabled"}
INVALID = {"detail": "Invalid API key"}
FORBIDDEN_ORIGIN = {"detail": "Origin not allowed for this API key"}


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _dev(tenant, user="alice"):
    return get_auth_header(tenant, user)


def _create(c, tenant, **body):
    r = c.post(KEYS, json={"name": "k", **body}, headers=_dev(tenant))
    assert r.status_code == 201, r.text
    return r.json()["key"], r.json()["id"]


def _thread(agent_id):
    return f"/api/v1/ag-ui/agents/{agent_id}/threads/t1"


def _routes(app):
    out = []
    for template, item in app.openapi()["paths"].items():
        for method, op in item.items():
            if method.upper() in ("HEAD", "OPTIONS") or not isinstance(op, dict):
                continue
            path = template
            for p in op.get("parameters", []):
                if p.get("in") == "path":
                    v = "1" if p.get("schema", {}).get("type") == "integer" else str(uuid.uuid4())
                    path = path.replace("{" + p["name"] + "}", v)
            out.append((method.upper(), path))
    return out


OTLP = ("POST", "/api/v1/otlp/v1/traces")


HOOKS_PREFIX = "/api/v1/hooks/"
# The built-in admin's setup and sign-in come before there is any credential and read none (swept by
# test_agui9_hardening.test_the_local_sign_in_routes_take_no_key and test_local_auth_api).
LOCAL_AUTH_PATH = "/api/v1/auth/local"


def _outside(app):
    # The inbound webhook route is public by design (its HMAC signature is its only gate, see test_webhooks_p1): it
    # is swept separately below, where no credential of any kind may make it answer anything but its uniform refusals.
    return [
        (m, p) for m, p in _routes(app)
        if p != "/healthz" and not is_agui_path(p) and not p.startswith((HOOKS_PREFIX, LOCAL_AUTH_PATH))
    ]


def _statuses(c, headers):
    return {(m, p): c.request(m, p, headers=headers).status_code for m, p in _outside(c.app)}


@pytest.fixture
def c(client):
    return client


@pytest.fixture
def world(c, tenant, monkeypatch):
    """A tenant with an agent, a valid key, a revoked key, and a key of another tenant."""
    agent_id = create_agent(c, tenant, "alice")
    valid, _ = _create(c, tenant)
    revoked, rid = _create(c, tenant)
    assert c.delete(f"{KEYS}/{rid}", headers=_dev(tenant)).status_code == 204
    other_tenant = f"o{uuid.uuid4().hex[:10]}"
    foreign, _ = _create(c, other_tenant)
    return {"agent": agent_id, "valid": valid, "revoked": revoked, "foreign": foreign, "tenant": tenant}


@pytest.mark.parametrize("mode", ["dev", "api_key"])
def test_sweep_outside_ag_ui_no_key_is_ever_accepted(c, world, monkeypatch, mode):
    monkeypatch.setenv("EAP_AUTH_MODE", mode)
    assert len(_outside(c.app)) > 50
    for name in ("valid", "revoked", "foreign"):
        # The OTLP endpoint takes keys too, but only with the `otlp` scope: an AG-UI key there is 403, not accepted.
        bad = {k: s for k, s in _statuses(c, _bearer(world[name])).items()
               if s != (403 if (name != "revoked" and k == OTLP) else 401)}
        assert bad == {}, (mode, name, bad)
    none = {k: s for k, s in _statuses(c, {}).items() if s != 401}
    assert none == {}, (mode, none)


@pytest.mark.parametrize("mode", ["dev", "api_key"])
def test_the_public_hook_route_accepts_no_key_and_no_bearer(c, world, monkeypatch, mode):
    monkeypatch.setenv("EAP_AUTH_MODE", mode)
    hook = f"{HOOKS_PREFIX}{uuid.uuid4()}"
    assert [m for m, p in _routes(c.app) if p.startswith(HOOKS_PREFIX)] == ["POST"]  # exactly one route, POST only
    for headers in ({}, _bearer(world["valid"]), _bearer(world["revoked"]), _bearer(world["foreign"]), _dev(world["tenant"])):
        r = c.post(hook, content=b"{}", headers=headers)
        assert (r.status_code, r.json()) == (404, {"detail": "not found"}), headers  # never 2xx, never a 401 that names a credential


def test_sweep_dev_bearer_is_accepted_outside_ag_ui_only_in_dev_mode(c, world, monkeypatch):
    dev = _dev(world["tenant"])
    assert all(s != 401 for s in _statuses(c, dev).values())
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    assert all(s == 401 for s in _statuses(c, dev).values())
    r = c.get("/api/v1/agents", headers=dev)
    assert r.json() == DEV_OFF and r.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("mode", ["dev", "api_key"])
def test_ag_ui_matrix_per_credential(c, world, monkeypatch, mode):
    monkeypatch.setenv("EAP_AUTH_MODE", mode)
    url = _thread(world["agent"])
    r = c.get(url)
    assert r.status_code == 401 and r.json() == {"detail": "Missing Authorization header"}
    assert c.get(url, headers=_bearer(world["valid"])).status_code == 200
    r = c.get(url, headers=_bearer(world["revoked"]))
    assert r.status_code == 401 and r.json() == INVALID
    # A key of another tenant never sees this tenant's agent: the same 404 as a missing agent.
    r = c.get(url, headers=_bearer(world["foreign"]))
    assert r.status_code == 404
    assert r.json() == c.get(_thread(str(uuid.uuid4())), headers=_bearer(world["foreign"])).json()
    dev = c.get(url, headers=_dev(world["tenant"]))
    if mode == "dev":
        assert dev.status_code == 200
    else:
        assert dev.status_code == 401 and dev.json() == DEV_OFF


@pytest.mark.parametrize("mode", ["dev", "api_key"])
def test_every_ag_ui_route_takes_a_key_and_refuses_a_revoked_one(c, world, monkeypatch, mode):
    monkeypatch.setenv("EAP_AUTH_MODE", mode)
    agui = [(m, p) for m, p in _routes(c.app) if is_agui_path(p)]
    assert len(agui) >= 3
    for m, p in agui:
        assert c.request(m, p, headers=_bearer(world["revoked"])).status_code == 401, p
        assert c.request(m, p).status_code == 401, p
        # A valid key is past auth: whatever comes back is not an auth failure.
        assert c.request(m, p, headers=_bearer(world["valid"])).status_code not in (401, 403), p


def test_malformed_credentials_are_401_in_both_modes(c, world, monkeypatch):
    url = _thread(world["agent"])
    for mode in ("dev", "api_key"):
        monkeypatch.setenv("EAP_AUTH_MODE", mode)
        for h in ({"Authorization": "Bearer pos_nope"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            assert c.get(url, headers=h).status_code == 401, (mode, h)


# ----------------------------------------------------------------------------- CORS


@pytest.fixture
def cc(monkeypatch):
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", f"{EXTRA}, https://Other.test:443")
    app = create_app(database_url=PG_URL)
    with TestClient(app) as t:
        yield t
    app.state.database_engine.dispose()


def _pre(c, path, origin, method="POST", headers="authorization,content-type"):
    return c.options(path, headers={"Origin": origin, "Access-Control-Request-Method": method,
                                    "Access-Control-Request-Headers": headers})


AG = "/api/v1/ag-ui/agents/x"


def test_preflight_allow_list_credentials_and_vary(cc):
    for origin in (EXTRA, "https://other.test", STUDIO, "http://127.0.0.1:3000"):
        r = _pre(cc, AG, origin)
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
        assert "access-control-allow-credentials" not in r.headers
        assert "origin" in r.headers["vary"].lower()
        assert r.headers["access-control-max-age"] == "600"
    allowed = {h.strip().lower() for h in _pre(cc, AG, EXTRA).headers["access-control-allow-headers"].split(",")}
    assert {"authorization", "content-type", "accept", "last-event-id"} <= allowed
    for bad in ("https://evil.test", "https://app.example.com.evil.test", "http://app.example.com", "null"):
        r = _pre(cc, AG, bad)
        assert r.status_code == 400 and "access-control-allow-origin" not in r.headers, bad


def test_preflight_refuses_other_methods_and_headers(cc):
    for method in ("DELETE", "PUT", "PATCH"):
        assert _pre(cc, AG, EXTRA, method=method).status_code == 400
    assert _pre(cc, AG, EXTRA, headers="x-api-key").status_code == 400
    assert _pre(cc, AG, EXTRA, method="GET", headers="last-event-id").status_code == 200


def test_actual_requests_expose_only_retry_after_and_vary_origin(cc, world, monkeypatch):
    r = cc.get(_thread(world["agent"]), headers=_bearer(world["valid"]) | {"Origin": EXTRA})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == EXTRA
    assert r.headers["access-control-expose-headers"] == "Retry-After"
    assert "access-control-allow-credentials" not in r.headers
    assert "origin" in r.headers["vary"].lower()
    # Errors carry the CORS headers too, so a browser can read the 401 / 403 body.
    r = cc.get(_thread(world["agent"]), headers={"Origin": EXTRA})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == EXTRA
    r = cc.get(_thread(world["agent"]), headers=_bearer(world["valid"]) | {"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r.headers


def test_no_cors_headers_at_all_outside_ag_ui_for_agui_origins(cc, world):
    for path in ("/api/v1/agents", KEYS, "/api/v1/ag-uix", "/healthz", "/api/v1/ag-ui-other"):
        for origin in (EXTRA, "https://other.test"):
            pre = _pre(cc, path, origin)
            # Starlette's refused preflight (400) still lists methods/headers, but never ACAO, so
            # the browser blocks it.
            assert pre.status_code == 400 and "access-control-allow-origin" not in pre.headers, path
            got = cc.get(path, headers=_dev(world["tenant"]) | {"Origin": origin})
            # (Starlette's legacy allow-credentials on a non-matching origin is inert without ACAO.)
            assert "access-control-allow-origin" not in got.headers, path
            assert "access-control-expose-headers" not in got.headers, path


def test_studio_policy_outside_ag_ui_is_unchanged(cc, world):
    pre = _pre(cc, "/api/v1/agents", STUDIO, method="DELETE", headers="x-anything")
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == STUDIO
    assert pre.headers["access-control-allow-credentials"] == "true"
    got = cc.get("/api/v1/agents", headers=_dev(world["tenant"]) | {"Origin": STUDIO})
    assert got.headers["access-control-allow-origin"] == STUDIO
    assert "origin" in got.headers["vary"].lower()


def test_default_config_has_no_extra_origin_anywhere(c):
    assert "access-control-allow-origin" not in _pre(c, AG, EXTRA).headers
    assert _pre(c, AG, STUDIO).headers["access-control-allow-origin"] == STUDIO


# ----------------------------------------------------------------------------- per-key origins


def _post_body():
    return {"threadId": "t1", "runId": "r1", "messages": [{"id": "u1", "role": "user", "content": "hi"}]}


def test_per_key_origin_match_mismatch_and_missing(c, world):
    tenant, agent = world["tenant"], world["agent"]
    key, _ = _create(c, tenant, allowed_origins=["https://ok.test", "http://localhost:5173"])
    url = _thread(agent)
    for origin in ("https://ok.test", "HTTPS://OK.test:443", "http://localhost:5173"):
        assert c.get(url, headers=_bearer(key) | {"Origin": origin}).status_code == 200, origin
    assert c.get(url, headers=_bearer(key)).status_code == 200  # no Origin: server-side caller (plan)
    for origin in ("https://evil.test", "https://ok.test.evil.test", "http://ok.test", "https://ok.test:8443",
                   "null", "*", "", "garbage"):
        r = c.get(url, headers=_bearer(key) | {"Origin": origin})
        assert r.status_code == 403 and r.json() == FORBIDDEN_ORIGIN, origin


def test_key_auth_precedes_the_per_key_origin_check(c, world):
    key, _ = _create(c, world["tenant"], allowed_origins=["https://ok.test"])
    bad = {"Origin": "https://evil.test"}
    # A revoked / unknown key is 401 whatever the origin; a mismatch never turns a bad key into 403.
    assert c.get(_thread(world["agent"]), headers=_bearer(world["revoked"]) | bad).json() == INVALID
    # 403 comes before the unknown-thread/run 404 and before a POST records anything.
    r = c.get(f"{_thread(world['agent'])}/runs/nope/stream", headers=_bearer(key) | bad)
    assert r.status_code == 403
    r = c.post(f"/api/v1/ag-ui/agents/{world['agent']}", json=_post_body(), headers=_bearer(key) | bad)
    assert r.status_code == 403 and r.json() == FORBIDDEN_ORIGIN
    assert c.get(_thread(world["agent"]), headers=_bearer(key)).json()["runs"] == []


def test_empty_list_allows_no_browser_origin_and_null_list_allows_all(c, world):
    empty, _ = _create(c, world["tenant"], allowed_origins=[])
    anyone, _ = _create(c, world["tenant"])
    url = _thread(world["agent"])
    for origin in ("https://ok.test", "null"):
        assert c.get(url, headers=_bearer(empty) | {"Origin": origin}).status_code == 403
        assert c.get(url, headers=_bearer(anyone) | {"Origin": origin}).status_code == 200
    assert c.get(url, headers=_bearer(empty)).status_code == 200


def test_wildcards_cannot_be_stored_as_an_allowed_origin(c, world):
    for bad in ("*", "https://*.example.com", "https://ok.test/path", "ok.test"):
        r = c.post(KEYS, json={"name": "k", "allowed_origins": [bad]}, headers=_dev(world["tenant"]))
        assert r.status_code == 422, (bad, r.status_code)


def test_origin_is_enforced_in_api_key_mode_too(c, world, monkeypatch):
    key, _ = _create(c, world["tenant"], allowed_origins=["https://ok.test"])
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    url = _thread(world["agent"])
    assert c.get(url, headers=_bearer(key) | {"Origin": "https://ok.test"}).status_code == 200
    assert c.get(url, headers=_bearer(key) | {"Origin": "https://evil.test"}).status_code == 403


def test_dev_bearer_is_never_origin_checked(c, world):
    r = c.get(_thread(world["agent"]), headers=_dev(world["tenant"]) | {"Origin": "https://evil.test"})
    assert r.status_code == 200


# ----------------------------------------------------------------------------- startup / config

DB = "postgresql+psycopg://unused@localhost:1/unused"


@pytest.mark.parametrize("mode", [None, "", " dev", "prod", "Dev", "api-key", "API_KEY", "both"])
def test_invalid_auth_mode_fails_at_startup(monkeypatch, mode):
    if mode is None:
        monkeypatch.delenv("EAP_AUTH_MODE", raising=False)
    else:
        monkeypatch.setenv("EAP_AUTH_MODE", mode)
    with pytest.raises(RuntimeError, match=re.escape("EAP_AUTH_MODE")):
        create_app(database_url=DB)


@pytest.mark.parametrize("raw", ["*", "https://ok.test,*", "ok.test", "https://a.test/x", "ftp://a.test",
                                 "https://*.a.test", "null", "https://a.test:99999"])
def test_invalid_cors_origins_fail_at_startup(monkeypatch, raw):
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", raw)
    with pytest.raises(RuntimeError, match="EAP_AGUI_CORS_ORIGINS"):
        create_app(database_url=DB)


@pytest.mark.parametrize("raw", ["", "  ", " , ,"])
def test_blank_cors_origins_are_none_not_an_error(monkeypatch, raw):
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", raw)
    app = create_app(database_url=PG_URL)
    app.state.database_engine.dispose()


def test_keys_can_only_be_created_in_dev_mode(c, tenant, monkeypatch):
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    for headers in (_dev(tenant), {}):
        r = c.post(KEYS, json={"name": "k"}, headers=headers)
        assert r.status_code == 401
    assert c.get(KEYS, headers=_dev(tenant)).status_code == 401
    assert c.delete(f"{KEYS}/{uuid.uuid4().hex[:24]}", headers=_dev(tenant)).status_code == 401
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    assert c.post(KEYS, json={"name": "k"}, headers=_dev(tenant)).status_code == 201


def test_healthz_is_open_in_both_modes(c, monkeypatch):
    for mode in ("dev", "api_key"):
        monkeypatch.setenv("EAP_AUTH_MODE", mode)
        assert c.get("/healthz").status_code == 200


# ----------------------------------------------------------------------------- no leaks


def test_no_key_value_in_any_error_body_header_or_log(c, world, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    key, _ = _create(c, world["tenant"], allowed_origins=["https://ok.test"])
    secrets = [world[n] for n in ("valid", "revoked", "foreign")] + [key]
    secrets += [parse_api_key(k)[1] for k in secrets]  # the whole secret (it may contain "_")
    seen = []
    for mode in ("dev", "api_key"):
        monkeypatch.setenv("EAP_AUTH_MODE", mode)
        for name in ("valid", "revoked", "foreign"):
            for origin in (None, "https://evil.test"):
                h = _bearer(world[name]) | ({"Origin": origin} if origin else {})
                seen.append(c.get(_thread(world["agent"]), headers=h))
                seen.append(c.get("/api/v1/agents", headers=h))
                seen.append(c.post(KEYS, json={"name": "x"}, headers=h))
        seen.append(c.get(_thread(world["agent"]), headers=_bearer(key) | {"Origin": "https://evil.test"}))
    assert any(r.status_code in (401, 403) for r in seen)
    for r in seen:
        blob = r.text + str(dict(r.headers))
        assert not any(s in blob for s in secrets)
    assert not any(s in caplog.text for s in secrets)


def test_startup_cors_error_does_not_echo_anything_but_the_entry(monkeypatch):
    monkeypatch.setenv("EAP_AGUI_CORS_ORIGINS", "https://ok.test,bad entry")
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    with pytest.raises(RuntimeError) as info:
        create_app(database_url=DB)
    assert "bad entry" in str(info.value) and "ok.test" not in str(info.value)
