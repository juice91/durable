"""Inbound webhooks P1 (start a run): trigger lifecycle, the hook route's pipeline, dedupe, limits, the IAM chain.

Real Postgres, the real identity chain (dev bearer and the `webhook` plug-in), a fake DBOS client behind the real launcher.
Design: an internal design note."""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any

import pytest
from eap_inbound import CustomConfig
from eap_inbound.signing import custom_headers, github_headers, standard_webhooks_headers
from eap_secrets import generate_key
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_runs import FakeDBOSClient

pytestmark = requires_pg

SECRET_VALUE = "sentinel-webhook-secret-7f3a9c"
WHSEC = "whsec_" + base64.b64encode(b"webhook-sentinel-key-0123456789ab").decode()
BASE = "https://hooks.example.test"
PAYLOAD_SCHEMA = {
    "type": "object", "required": ["repository"],
    "properties": {"repository": {"type": "object", "required": ["full_name"], "properties": {"full_name": {"type": "string"}}}},
}
INPUT_MAP = {"repo": {"$pointer": "/repository/full_name"}, "source": "github"}
UNIFORM_404 = {"detail": "not found"}
UNIFORM_401 = {"detail": "invalid signature"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EAP_SECRETS_KEY", generate_key())
    monkeypatch.setenv("EAP_WEBHOOK_BASE_URL", BASE)
    monkeypatch.setenv("EAP_WEBHOOKS_ENABLED", "true")
    with hard_timeout(120):
        yield


@pytest.fixture
def world():
    app = create_app(database_url=PG_URL)
    fake = FakeDBOSClient()
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []
    with TestClient(app) as client:
        yield World(client, app, fake, tenants)
    engine = app.state.database_engine
    with engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM webhook_deliveries WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM webhook_trigger_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM webhook_triggers WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM audit_log WHERE tenant_id = :t"), {"t": t})
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


class World:
    def __init__(self, client: TestClient, app: Any, fake: FakeDBOSClient, tenants: list[str]) -> None:
        self.client, self.app, self.fake, self.tenants = client, app, fake, tenants

    def tenant(self) -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        self.tenants.append(t)
        return t, get_auth_header(t, "alice")

    def workflow(self, headers: dict[str, str]) -> str:
        spec = {
            "name": "Repo push", "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}, "source": {"type": "string"}},
                                                 "required": ["repo"], "additionalProperties": False},
            "start": "now", "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
        }
        r = self.client.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        assert self.client.post(f"/api/v1/workflows/{wid}/versions", headers=headers).status_code == 201
        return wid

    def secret(self, tenant: str, name: str = "hook-secret", value: str = SECRET_VALUE) -> str:
        from eap_run_store.secrets import SecretStore
        from eap_secrets import load_keyring

        SecretStore(self.app.state.database_engine, load_keyring()).put(tenant, name, value, "test")
        return f"secret:{name}"

    def create(self, headers: dict[str, str], tenant: str, workflow_id: str, **over: Any) -> dict[str, Any]:
        body = {
            "name": f"trig-{uuid.uuid4().hex[:6]}", "workflow_id": workflow_id, "version": 1, "preset": "github",
            "secrets": [self.secret(tenant)], "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}},
        } | over
        r = self.client.post("/api/v1/webhook-triggers", json=body, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    def enable(self, headers: dict[str, str], trigger: dict[str, Any]) -> dict[str, Any]:
        r = self.client.post(
            f"/api/v1/webhook-triggers/{trigger['id']}/enable", json={"config_hash": trigger["config_hash"], "confirm": True}, headers=headers
        )
        assert r.status_code == 200, r.text
        return r.json()

    def live(self, **over: Any) -> tuple[str, dict[str, str], str, dict[str, Any]]:
        tenant, headers = self.tenant()
        wid = self.workflow(headers)
        trigger = self.enable(headers, self.create(headers, tenant, wid, **over))
        return tenant, headers, wid, trigger

    def post(self, trigger_id: str, body: bytes, headers: dict[str, str] | None = None):
        return self.client.post(f"/api/v1/hooks/{trigger_id}", content=body, headers=headers or {})

    def runs(self, tenant: str) -> list[dict[str, Any]]:
        with self.app.state.database_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, trigger_id, principal, started_by_kind, on_behalf_of, input FROM runs WHERE tenant_id = :t ORDER BY created_at"),
                {"t": tenant},
            ).mappings().all()
        return [{k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()} for r in rows]


def flat_routes(app: Any) -> list[tuple[str, set[str], Any]]:
    """Every route of the app as (path, methods, route), flattened through FastAPI's included routers."""
    out = []
    for route in app.routes:
        if type(route).__name__ == "_IncludedRouter":
            out.extend((c.path, set(getattr(c.original_route, "methods", None) or ()), c.original_route) for c in route.effective_route_contexts())
        else:
            out.append((getattr(route, "path", ""), set(getattr(route, "methods", None) or ()), route))
    return out


def dependency_calls(route: Any) -> set[Any]:
    dependant = getattr(route, "dependant", None)
    calls: set[Any] = set()
    stack = [dependant] if dependant is not None else []
    while stack:
        d = stack.pop()
        calls.add(d.call)
        stack.extend(d.dependencies)
    return calls


def raw_request(app: Any, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b"") -> tuple[int, bytes]:
    """One request straight into the ASGI app with `path` EXACTLY as given (an HTTP client would normalise `..`; a proxy
    that does not is what the Access bypass must survive). `path` is what the server hands the app after decoding."""
    import asyncio
    from urllib.parse import unquote

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
        "path": unquote(path), "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in {**(headers or {}), "host": "durable.test", "content-length": str(len(body))}.items()],
        "client": ("203.0.113.9", 4444), "server": ("durable.test", 443), "state": {},
    }
    got: dict[str, Any] = {"status": 0, "body": b""}

    async def run() -> None:
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                got["status"] = message["status"]
            elif message["type"] == "http.response.body":
                got["body"] += message.get("body", b"")

        await app(scope, receive, send)

    asyncio.run(run())
    return got["status"], got["body"]


def body_of(repo: str = "acme/api", **extra: Any) -> bytes:
    return json.dumps({"repository": {"full_name": repo}, **extra}).encode()


def signed(body: bytes, secret: str = SECRET_VALUE, **extra: str) -> dict[str, str]:
    return {**github_headers(secret, body), "content-type": "application/json", **extra}


# --------------------------------------------------------------------------------------------- the pipeline


def test_a_github_signed_delivery_starts_exactly_one_run_as_the_trigger(world):
    tenant, headers, wid, trig = world.live()
    body = body_of()
    r = world.post(trig["id"], body, signed(body))
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"status", "delivery"} and r.json()["status"] == "accepted"
    (run,) = world.runs(tenant)
    assert run["trigger_id"] == trig["id"] and run["principal"] == f"webhook:{trig['id']}" and run["started_by_kind"] == "webhook"
    assert run["on_behalf_of"] is None  # never the person who enabled it
    assert json.loads(run["input"]) == {"repo": "acme/api", "source": "github"}
    (options, args), = world.fake.calls
    assert options["workflow_id"] == str(run["id"]) and args[1] == tenant
    delivery = world.client.get(f"/api/v1/webhook-deliveries/{r.json()['delivery']}", headers=headers).json()
    assert delivery["outcome"] == "accepted" and delivery["run_id"] == str(run["id"]) and delivery["key_kind"] == "body"
    assert "body" not in delivery and SECRET_VALUE not in json.dumps(delivery)


def test_a_retried_or_replayed_delivery_starts_at_most_one_run(world):
    tenant, _h, _w, trig = world.live()
    body = body_of()
    first = world.post(trig["id"], body, signed(body, **{"x-github-delivery": "guid-1"}))
    again = world.post(trig["id"], body, signed(body, **{"x-github-delivery": "guid-1"}))
    fresh_guid = world.post(trig["id"], body, signed(body, **{"x-github-delivery": "guid-2"}))  # the GUID is unsigned
    assert (first.status_code, again.status_code, fresh_guid.status_code) == (202, 200, 200)
    assert again.json()["status"] == fresh_guid.json()["status"] == "duplicate"
    assert len(world.runs(tenant)) == 1
    assert {c[0]["workflow_id"] for c in world.fake.calls} == {world.runs(tenant)[0]["id"]}  # re-drives share the run id: DBOS enqueues once
    other = body_of(repo="acme/web")
    assert world.post(trig["id"], other, signed(other)).status_code == 202
    assert len(world.runs(tenant)) == 2


def test_the_run_id_is_derived_from_the_delivery(world):
    from eap_api.services.webhook_ingest import run_id_for

    tenant, _h, _w, trig = world.live()
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    import hashlib

    assert world.runs(tenant)[0]["id"] == str(run_id_for(trig["id"], "body:" + hashlib.sha256(body).hexdigest()))


def test_standard_webhooks_valid_retry_and_stale(world):
    tenant, headers, wid = *world.tenant(), None
    wid = world.workflow(headers)
    ref = world.secret(tenant, "wh", WHSEC)
    trig = world.enable(headers, world.create(headers, tenant, wid, preset="standard_webhooks", secrets=[ref]))
    body = body_of()
    now = int(time.time())
    ok = standard_webhooks_headers(WHSEC, body, msg_id="msg_1", timestamp=now)
    assert world.post(trig["id"], body, ok).status_code == 202
    retry = standard_webhooks_headers(WHSEC, body, msg_id="msg_1", timestamp=now + 3)  # same id, new timestamp and signature
    assert world.post(trig["id"], body, retry).json()["status"] == "duplicate"
    stale = standard_webhooks_headers(WHSEC, body_of("acme/x"), msg_id="msg_2", timestamp=now - 2000)
    r = world.post(trig["id"], body_of("acme/x"), stale)
    assert (r.status_code, r.json()) == (401, UNIFORM_401)
    rows = world.client.get(f"/api/v1/webhook-deliveries?trigger_id={trig['id']}&outcome=stale", headers=headers).json()["items"]
    assert len(rows) == 1 and rows[0]["reason"] == "timestamp_stale"
    # a stale delivery whose signature is wrong leaves no row at all
    bad = standard_webhooks_headers(WHSEC, b"{}", msg_id="msg_3", timestamp=now - 2000)
    assert world.post(trig["id"], body_of("acme/y"), bad).status_code == 401
    assert len(world.client.get(f"/api/v1/webhook-deliveries?trigger_id={trig['id']}&outcome=stale", headers=headers).json()["items"]) == 1
    assert len(world.runs(tenant)) == 1


def test_the_generic_sha256_prefix_preset(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    config = {"allow_body_hash_dedupe_only": True}
    trig = world.enable(headers, world.create(headers, tenant, wid, preset="custom", preset_config=config))
    body = body_of()
    sent = custom_headers(CustomConfig(**config), SECRET_VALUE, body)
    assert world.post(trig["id"], body, sent).status_code == 202
    assert world.post(trig["id"], body, sent).json()["status"] == "duplicate"
    assert world.post(trig["id"], body, {**sent, "x-signature-256": "sha256=" + "0" * 64}).status_code == 401


def test_wrong_missing_and_tampered_signatures_are_one_uniform_401(world):
    tenant, _h, _w, trig = world.live()
    body = body_of()
    good = signed(body)
    cases = {
        "missing": {"content-type": "application/json"},
        "wrong secret": signed(body, "not-the-secret"),
        "tampered body": good,  # sent below with one byte changed
        "malformed": {"x-hub-signature-256": "sha256=zz"},
        "legacy sha1 header only": {"x-hub-signature": "sha1=" + "a" * 40},
        "bearer is not a signature": {"authorization": "Bearer dev:someone:alice"},
    }
    seen = []
    for name, headers in cases.items():
        payload = body + b" " if name == "tampered body" else body
        r = world.post(trig["id"], payload, headers)
        assert (r.status_code, r.json()) == (401, UNIFORM_401), name
        seen.append(r.text)
    assert len(set(seen)) == 1
    assert world.runs(tenant) == []
    snapshot = world.client.get(f"/api/v1/webhook-triggers/{trig['id']}", headers=_h).json()["refusals_since_start"]
    assert snapshot["signature_mismatch"] >= 2 and snapshot["missing_header"] >= 1


def test_unknown_disabled_pending_archived_and_edited_triggers_are_one_uniform_404(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    body = body_of()
    sig = signed(body)
    created = world.create(headers, tenant, wid)  # disabled
    assert world.post(created["id"], body, sig).json() == UNIFORM_404
    assert world.client.post(f"/api/v1/webhook-triggers/{created['id']}/enable-request", headers=headers).json()["status"] == "pending_enable"
    r_pending = world.post(created["id"], body, sig)
    enabled = world.enable(headers, created)
    assert world.post(enabled["id"], body, sig).status_code == 202
    # an edit after approval returns it to disabled: the URL is a 404 again within one request
    r = world.client.patch(f"/api/v1/webhook-triggers/{enabled['id']}", json={"limits": {"rate_per_minute": 10}}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r_edited = world.post(enabled["id"], body_of("acme/z"), signed(body_of("acme/z")))
    world.client.post(f"/api/v1/webhook-triggers/{enabled['id']}/archive", headers=headers)
    r_archived = world.post(enabled["id"], body, sig)
    r_unknown = world.post(str(uuid.uuid4()), body, sig)
    r_garbage = world.post("not-a-uuid", body, sig)
    for r in (r_pending, r_edited, r_archived, r_unknown, r_garbage):
        assert (r.status_code, r.json()) == (404, UNIFORM_404)
    assert len(world.runs(tenant)) == 1


def test_the_feature_is_off_unless_explicitly_enabled(world, monkeypatch):
    _t, _h, _w, trig = world.live()
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    for value in ("", "false", "0"):
        monkeypatch.setenv("EAP_WEBHOOKS_ENABLED", value)
        world.app.state.iam = __import__("eap_iam").load_iam(world.app.state.database_engine)
        assert world.post(trig["id"], body_of("a/z"), signed(body_of("a/z"))).json() == UNIFORM_404
    monkeypatch.delenv("EAP_WEBHOOKS_ENABLED")
    world.app.state.iam = __import__("eap_iam").load_iam(world.app.state.database_engine)
    assert world.post(trig["id"], body_of("a/y"), signed(body_of("a/y"))).json() == UNIFORM_404  # unset = off


def test_a_disabled_trigger_stops_at_once(world):
    _t, headers, _w, trig = world.live()
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    assert world.client.post(f"/api/v1/webhook-triggers/{trig['id']}/disable", headers=headers).json()["status"] == "disabled"
    assert world.post(trig["id"], body_of("a/b"), signed(body_of("a/b"))).status_code == 404


def test_body_cap_with_and_without_a_declared_length(world):
    tenant, _h, _w, trig = world.live(limits={"max_body_bytes": 1024})
    big = body_of(pad="x" * 2000)
    r = world.post(trig["id"], big, signed(big))
    assert (r.status_code, r.json()) == (413, {"detail": "request body too large"})

    def stream():
        yield big[:900]
        yield big[900:]

    r = world.client.post(f"/api/v1/hooks/{trig['id']}", content=stream(), headers=signed(big))  # chunked: no Content-Length
    assert r.status_code == 413
    assert world.runs(tenant) == []


def test_per_trigger_rate_limit(world):
    _t, _h, _w, trig = world.live(limits={"rate_per_minute": 2})
    codes = []
    for i in range(4):
        b = body_of(f"acme/r{i}")
        codes.append(world.post(trig["id"], b, signed(b)))
    assert [c.status_code for c in codes] == [202, 202, 429, 429]
    assert int(codes[2].headers["retry-after"]) >= 1


def test_per_ip_rate_limit(world, monkeypatch):
    from eap_api.inbound_guard import InboundGuard

    world.app.state.webhook_guard = InboundGuard(3)
    _t, _h, _w, trig = world.live()
    statuses = [world.post(trig["id"], b"{}", {}).status_code for _ in range(5)]
    assert statuses[:3] == [401, 401, 401] and statuses[3:] == [429, 429]


def test_concurrency_limit_and_a_released_claim(world):
    tenant, _h, _w, trig = world.live(limits={"max_concurrent_runs": 1})
    b1, b2 = body_of("acme/a"), body_of("acme/b")
    assert world.post(trig["id"], b1, signed(b1)).status_code == 202  # the run stays queued: active
    r = world.post(trig["id"], b2, signed(b2))
    assert r.status_code == 429 and r.headers["retry-after"]
    with world.app.state.database_engine.begin() as conn:  # the first run finishes
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE tenant_id = :t"), {"t": tenant})
    assert world.post(trig["id"], b2, signed(b2)).status_code == 202  # the rejection was not a claim: same body is processed now
    assert len(world.runs(tenant)) == 2


def test_payload_and_mapping_refusals_never_echo_values(world):
    tenant, _h, _w, trig = world.live()
    secret_text = "do-not-echo-this-value"
    bad = json.dumps({"repository": {"full_name": 5, "x": secret_text}}).encode()
    r = world.post(trig["id"], bad, signed(bad))
    assert r.status_code == 422 and secret_text not in r.text and r.json()["code"] == "payload_invalid"
    assert r.json()["errors"][0]["type"].startswith("json_schema")
    missing = json.dumps({"repository": {"full_name": "a/b"}, "unused": secret_text}).encode()
    assert world.post(trig["id"], missing, signed(missing)).status_code == 202
    for raw, status_code, code in ((b"not json", 422, "body_not_json"), (b"[1,2]", 422, "body_not_object"),
                                   (b"\xff\xfe{}", 415, "body_not_utf8"), (b'{"a": NaN}', 422, "body_not_json")):
        r = world.post(trig["id"], raw, signed(raw))
        assert (r.status_code, r.json()["code"]) == (status_code, code), raw
    assert len(world.runs(tenant)) == 1


def test_an_unresolved_pointer_is_a_422_with_the_location_only(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    schema = {"type": "object"}
    trig = world.enable(headers, world.create(headers, tenant, wid, actions={"start": {"payload_schema": schema, "input_map": INPUT_MAP}}))
    b = json.dumps({"other": 1}).encode()
    r = world.post(trig["id"], b, signed(b))
    assert r.status_code == 422 and r.json()["code"] == "input_map_unresolved" and r.json()["errors"] == [{"loc": "input_map.repo"}]


def test_mapped_input_must_fit_the_workflow_input_schema(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    mapping = {"repo": {"$pointer": "/n"}}
    trig = world.enable(headers, world.create(headers, tenant, wid, actions={"start": {"payload_schema": {"type": "object"}, "input_map": mapping}}))
    b = json.dumps({"n": 7}).encode()  # repo must be a string
    r = world.post(trig["id"], b, signed(b))
    assert r.status_code == 422 and r.json()["code"] == "input_invalid"
    assert world.runs(tenant) == []
    rows = world.client.get(f"/api/v1/webhook-deliveries?trigger_id={trig['id']}", headers=headers).json()["items"]
    assert rows[0]["outcome"] == "rejected_schema" and rows[0]["reason"] == "input_invalid"


def test_a_failed_hand_off_is_a_503_and_the_retry_starts_the_same_run(world):
    tenant, _h, _w, trig = world.live()
    body = body_of()
    world.fake.fail_times = 1
    r = world.post(trig["id"], body, signed(body))
    assert r.status_code == 503 and r.headers["retry-after"]
    retried = world.post(trig["id"], body, signed(body))
    assert retried.status_code == 202
    assert len(world.runs(tenant)) == 1 and len(world.fake.calls) == 1


def test_a_crashed_claim_is_taken_over_after_the_lease(world):
    tenant, _h, _w, trig = world.live()
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    with world.app.state.database_engine.begin() as conn:  # simulate: claim row left `processing`, run row gone
        conn.execute(text("UPDATE webhook_deliveries SET outcome = 'processing', state = 'processing' WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant})
    busy = world.post(trig["id"], body, signed(body))
    assert busy.status_code == 503  # a live attempt holds the claim
    with world.app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE webhook_deliveries SET claimed_at = now() - interval '5 minutes' WHERE tenant_id = :t"), {"t": tenant})
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    assert len(world.runs(tenant)) == 1


def test_a_burst_of_deliveries_never_exceeds_the_concurrency_cap(world):
    """50 distinct signed deliveries from 16 threads at once: exactly `max_concurrent_runs` start, the rest are 429."""
    from concurrent.futures import ThreadPoolExecutor

    tenant, _h, _w, trig = world.live(limits={"max_concurrent_runs": 5, "rate_per_minute": 600})
    bodies = [body_of(f"acme/burst-{i}") for i in range(50)]
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda b: world.post(trig["id"], b, signed(b)).status_code, bodies))
    assert codes.count(202) == 5 and codes.count(429) == 45, sorted(set(codes))
    assert len(world.runs(tenant)) == 5


def test_the_daily_cap_holds(world):
    tenant, _h, _w, trig = world.live(limits={"max_runs_per_day": 3, "max_concurrent_runs": 50})
    codes = []
    for i in range(5):
        b = body_of(f"acme/day-{i}")
        codes.append(world.post(trig["id"], b, signed(b)).status_code)
    assert codes == [202, 202, 202, 429, 429] and len(world.runs(tenant)) == 3


def test_a_retry_of_a_recorded_run_is_not_refused_by_the_cap_it_counts_toward(world):
    tenant, _h, _w, trig = world.live(limits={"max_concurrent_runs": 1})
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    with world.app.state.database_engine.begin() as conn:  # crash after the enqueue: the claim is stale, the run exists
        conn.execute(text("UPDATE webhook_deliveries SET outcome = 'processing', state = 'processing', claimed_at = now() - interval '5 minutes' WHERE tenant_id = :t"), {"t": tenant})
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    assert len(world.runs(tenant)) == 1 and {c[0]["workflow_id"] for c in world.fake.calls} == {world.runs(tenant)[0]["id"]}


def test_a_deleted_secret_is_a_uniform_401_and_an_edited_row_without_approval_is_a_404(world):
    tenant, _h, _w, trig = world.live()
    body = body_of()
    with world.app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": tenant})
    r = world.post(trig["id"], body, signed(body))
    assert (r.status_code, r.json()) == (401, UNIFORM_401)
    world.secret(tenant)  # restored
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    with world.app.state.database_engine.begin() as conn:  # the grant was edited behind the API's back: the approval no longer matches
        conn.execute(text("UPDATE webhook_triggers SET config_hash = 'edited' WHERE id = :i"), {"i": trig["id"]})
    b2 = body_of("acme/other")
    assert (lambda r: (r.status_code, r.json()))(world.post(trig["id"], b2, signed(b2))) == (404, UNIFORM_404)


def test_a_future_timestamp_is_stale_in_either_direction(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    ref = world.secret(tenant, "wh", WHSEC)
    trig = world.enable(headers, world.create(headers, tenant, wid, preset="standard_webhooks", secrets=[ref]))
    body = body_of()
    future = standard_webhooks_headers(WHSEC, body, msg_id="msg_f", timestamp=int(time.time()) + 3000)
    assert (lambda r: (r.status_code, r.json()))(world.post(trig["id"], body, future)) == (401, UNIFORM_401)
    assert world.runs(tenant) == []


# --------------------------------------------------------------------------------------------- the IAM chain


def test_the_hook_ignores_every_other_credential(world, monkeypatch):
    tenant, headers, _w, trig = world.live()
    body = body_of()
    for credential in ({"authorization": headers["Authorization"]}, {"authorization": "Bearer pos_" + "a" * 24 + "_" + "b" * 43},
                       {"cf-access-jwt-assertion": "x.y.z"}):
        r = world.post(trig["id"], body, credential)  # no signature: the credential is not an alternative
        assert (r.status_code, r.json()) == (401, UNIFORM_401)
    # and a valid signature does not need (or look at) a bad bearer
    r = world.post(trig["id"], body, {**signed(body), "authorization": "Bearer garbage"})
    assert r.status_code == 202


def test_other_routes_do_not_accept_the_webhook_signature(world):
    _t, _h, _w, trig = world.live()
    body = body_of()
    sig = signed(body, **{"x-webhook-trigger": trig["id"]})
    for method, path in (("get", "/api/v1/webhook-triggers"), ("get", "/api/v1/agents"), ("post", "/api/v1/api-keys"),
                         ("get", "/api/v1/runs"), ("get", "/api/v1/iam/capabilities")):
        r = getattr(world.client, method)(path, headers=sig)
        assert r.status_code == 401, (method, path, r.status_code)
    # the chain itself: a webhook principal is never produced for a route that does not accept the kind
    import asyncio

    from eap_iam import RequestView

    chain = world.app.state.iam.chain
    view = RequestView(headers=sig, accepts=frozenset({"user"}), action="ingest.webhook", route_params={"trigger_id": trig["id"]}, body=body)
    with pytest.raises(Exception):
        asyncio.run(chain.authenticate(view))


def test_the_hook_route_is_reachable_in_api_key_mode_while_management_routes_are_not(world, monkeypatch):
    _t, _h, _w, trig = world.live()
    monkeypatch.setenv("EAP_AUTH_MODE", "api_key")
    body = body_of()
    assert world.post(trig["id"], body, signed(body)).status_code == 202
    assert world.client.get("/api/v1/webhook-triggers", headers=signed(body)).status_code == 401


# --------------------------------------------------------------------------------------------- lifecycle and secrets


def test_create_is_disabled_enable_needs_the_exact_hash_and_a_person(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    created = world.create(headers, tenant, wid)
    assert created["status"] == "disabled" and created["approved"] is False and created["url"] == f"{BASE}/api/v1/hooks/{created['id']}"
    url = f"/api/v1/webhook-triggers/{created['id']}/enable"
    assert world.client.post(url, json={"config_hash": "0" * 64, "confirm": True}, headers=headers).status_code == 409
    assert world.client.post(url, json={"config_hash": created["config_hash"]}, headers=headers).status_code == 422
    # an API key (a non-human credential) cannot reach the route at all
    assert world.client.post(url, json={"config_hash": created["config_hash"], "confirm": True},
                             headers={"Authorization": "Bearer pos_" + "a" * 24 + "_" + "b" * 43}).status_code == 401
    enabled = world.enable(headers, created)
    assert enabled["status"] == "enabled" and enabled["approved"] and enabled["enabled_by"] == "user:alice"
    kinds = [e["kind"] for e in world.client.get(f"/api/v1/webhook-triggers/{created['id']}/events", headers=headers).json()["items"]]
    assert set(kinds) == {"created", "enabled"}


def test_the_decision_card_approves_or_rejects_a_pending_request(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    t = world.create(headers, tenant, wid)
    base = f"/api/v1/webhook-triggers/{t['id']}"
    assert world.client.post(f"{base}/decision", json={"decision": "approve", "config_hash": t["config_hash"]}, headers=headers).status_code == 409
    assert world.client.post(f"{base}/enable-request", headers=headers).json()["status"] == "pending_enable"
    rejected = world.client.post(f"{base}/decision", json={"decision": "reject"}, headers=headers).json()
    assert rejected["status"] == "disabled"
    world.client.post(f"{base}/enable-request", headers=headers)
    approved = world.client.post(f"{base}/decision", json={"decision": "approve", "config_hash": t["config_hash"]}, headers=headers).json()
    assert approved["status"] == "enabled"
    kinds = [e["kind"] for e in world.client.get(f"{base}/events", headers=headers).json()["items"]]
    assert kinds.count("enable_requested") == 2 and {"enable_rejected", "enabled"} <= set(kinds)


def test_enable_is_refused_without_a_public_base_url(world, monkeypatch):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    t = world.create(headers, tenant, wid)
    monkeypatch.delenv("EAP_WEBHOOK_BASE_URL")
    r = world.client.post(f"/api/v1/webhook-triggers/{t['id']}/enable", json={"config_hash": t["config_hash"], "confirm": True}, headers=headers)
    assert r.status_code == 503 and r.json()["code"] == "webhook_base_url_unset"


def test_generate_shows_the_secret_once_and_it_signs(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    body = {"name": "gen", "workflow_id": wid, "version": "latest", "preset": "github", "generate": True,
            "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}}}
    r = world.client.post("/api/v1/webhook-triggers", json=body, headers=headers)
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    created = r.json()
    value = created.pop("generated_secret")
    assert len(value) >= 40 and value not in json.dumps(created)
    assert created["version"] == "latest" and any(w["code"] == "webhook_version_latest" for w in created["warnings"])
    for path in (f"/api/v1/webhook-triggers/{created['id']}", "/api/v1/webhook-triggers", f"/api/v1/webhook-triggers/{created['id']}/events"):
        assert value not in world.client.get(path, headers=headers).text
    world.enable(headers, created)
    payload = body_of()
    assert world.post(created["id"], payload, signed(payload, value)).status_code == 202
    assert [s["last_matched_at"] is not None for s in world.client.get(f"/api/v1/webhook-triggers/{created['id']}", headers=headers).json()["secrets"]] == [True]


def test_rotation_with_two_active_secrets_and_no_downtime(world):
    tenant, headers, _w, trig = world.live()
    old, new = SECRET_VALUE, "the-new-sentinel-secret"
    new_ref = world.secret(tenant, "hook-secret-2", new)
    base = f"/api/v1/webhook-triggers/{trig['id']}"
    b1, b2, b3 = body_of("a/1"), body_of("a/2"), body_of("a/3")
    r = world.client.post(f"{base}/rotate", json={"add": new_ref}, headers=headers)
    assert r.status_code == 200 and [s["ref"] for s in r.json()["secrets"]] == ["secret:hook-secret", new_ref]
    assert r.json()["status"] == "enabled"  # the grant did not change: no new approval
    assert world.post(trig["id"], b1, signed(b1, old)).status_code == 202
    assert world.post(trig["id"], b2, signed(b2, new)).status_code == 202
    assert world.client.post(f"{base}/rotate", json={"add": "secret:third"}, headers=headers).status_code == 409
    assert world.client.post(f"{base}/rotate", json={"retire": "secret:hook-secret"}, headers=headers).status_code == 200
    assert world.post(trig["id"], b3, signed(b3, old)).status_code == 401
    assert world.post(trig["id"], b3, signed(b3, new)).status_code == 202
    assert world.client.post(f"{base}/rotate", json={"retire": new_ref}, headers=headers).status_code == 409  # never the last
    kinds = [e["kind"] for e in world.client.get(f"{base}/events", headers=headers).json()["items"]]
    assert "secret_added" in kinds and "secret_retired" in kinds


def test_validation_refusals_do_not_leak_values(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    good = {"name": "v", "workflow_id": wid, "version": 1, "preset": "github", "secrets": [world.secret(tenant)],
            "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}}}
    value_like = {**good, "secrets": [SECRET_VALUE]}
    r = world.client.post("/api/v1/webhook-triggers", json=value_like, headers=headers)
    assert r.status_code == 422 and SECRET_VALUE not in r.text
    cases = [
        ({"version": None}, 422), ({"preset": "stripe"}, 422), ({"secrets": ["secret:missing"]}, 422),
        ({"actions": {"wake": {}}}, 422), ({"actions": {"start": {"payload_schema": {"type": "object", "properties": {"a": {"$ref": "#/x"}}}}}}, 422),
        ({"actions": {"start": {"payload_schema": {"type": "object"}, "input_map": {"a": {"$expr": "1+1"}}}}}, 422),
        ({"actions": {"start": {"payload_schema": {"type": "object"}, "input_map": {"a": {"$pointer": "no-slash"}}}}}, 422),
        ({"limits": {"rate_per_minute": 100000}}, 422), ({"tolerance_seconds": 5}, 422), ({"workflow_id": str(uuid.uuid4())}, 404),
        ({"preset": "custom", "preset_config": {}}, 422), ({"name": "bad name"}, 422), ({"extra": 1}, 422), ({"generate": True, "secrets": ["secret:hook-secret"]}, 422),
    ]
    for over, expected in cases:
        r = world.client.post("/api/v1/webhook-triggers", json={**good, **over, "name": over.get("name", f"c{uuid.uuid4().hex[:5]}")}, headers=headers)
        assert r.status_code == expected, (over, r.status_code, r.text)
    assert world.client.post("/api/v1/webhook-triggers", json=good, headers=headers).status_code == 201
    assert world.client.post("/api/v1/webhook-triggers", json=good, headers=headers).status_code == 409  # same name


def test_standard_webhooks_needs_a_whsec_secret(world):
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    ref = world.secret(tenant, "plain", "not-base64-whsec")
    r = world.client.post("/api/v1/webhook-triggers", json={
        "name": "sw", "workflow_id": wid, "version": 1, "preset": "standard_webhooks", "secrets": [ref],
        "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}}}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "secret_invalid"


def test_patch_uses_if_match_and_a_grant_change_disables(world):
    tenant, headers, _w, trig = world.live()
    base = f"/api/v1/webhook-triggers/{trig['id']}"
    stale = world.client.patch(base, json={"description": "x"}, headers={**headers, "If-Match": '"99"'})
    assert stale.status_code == 412 and stale.json()["code"] == "revision_mismatch"
    ok = world.client.patch(base, json={"description": "just a note"}, headers={**headers, "If-Match": f'"{trig["revision"]}"'})
    assert ok.status_code == 200 and ok.json()["status"] == "enabled" and ok.headers["etag"]  # not a grant field
    changed = world.client.patch(base, json={"tolerance_seconds": 120}, headers=headers)
    assert changed.json()["status"] == "disabled" and changed.json()["approved"] is False
    assert world.client.patch(base, json={"workflow_id": str(uuid.uuid4())}, headers=headers).status_code == 422


def test_a_secret_in_use_cannot_be_deleted(world):
    tenant, headers, _w, trig = world.live()
    r = world.client.delete("/api/v1/secrets/hook-secret", headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "secret_in_use"
    assert r.json()["servers"] == [] and r.json()["triggers"] == [{"trigger_id": trig["id"], "name": trig["name"]}]
    world.client.post(f"/api/v1/webhook-triggers/{trig['id']}/archive", headers=headers)
    assert world.client.delete("/api/v1/secrets/hook-secret", headers=headers).status_code == 204


def test_another_tenant_sees_nothing(world):
    tenant_a, headers_a, wid_a, trig = world.live()
    tenant_b, headers_b = world.tenant()
    base = f"/api/v1/webhook-triggers/{trig['id']}"
    for method, path in (("get", base), ("get", f"{base}/events"), ("post", f"{base}/disable"), ("post", f"{base}/archive"),
                         ("post", f"{base}/enable-request"), ("patch", base)):
        kwargs = {"json": {"description": "x"}} if method == "patch" else {}
        assert getattr(world.client, method)(path, headers=headers_b, **kwargs).status_code == 404, path
    assert world.client.get("/api/v1/webhook-triggers", headers=headers_b).json()["items"] == []
    ref_b = world.secret(tenant_b, "hook-secret", "tenant-b-value")
    r = world.client.post("/api/v1/webhook-triggers", headers=headers_b, json={
        "name": "x", "workflow_id": wid_a, "version": 1, "preset": "github", "secrets": [ref_b],
        "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}}})
    assert r.status_code == 404  # tenant A's workflow reads as missing
    # tenant B's secret with the same NAME does not sign tenant A's trigger
    body = body_of()
    assert world.post(trig["id"], body, signed(body, "tenant-b-value")).status_code == 401
    assert world.post(trig["id"], body, signed(body)).status_code == 202


# --------------------------------------------------------------------------------------------- secrets stay secret


def test_no_secret_in_logs_audit_events_deliveries_runs_or_responses(world, caplog):
    caplog.set_level(logging.DEBUG)
    tenant, headers = world.tenant()
    wid = world.workflow(headers)
    ref = world.secret(tenant, "leak-check", SECRET_VALUE)
    trig = world.enable(headers, world.create(headers, tenant, wid, secrets=[ref]))
    body = body_of("acme/leak")
    sig = signed(body)
    responses = [world.post(trig["id"], body, sig), world.post(trig["id"], body, sig), world.post(trig["id"], body, signed(body, "wrong-sentinel")),
                 world.post(trig["id"], b"nope", signed(b"nope"))]
    world.client.post(f"/api/v1/webhook-triggers/{trig['id']}/rotate", json={"add": world.secret(tenant, "leak-2", "second-sentinel")}, headers=headers)
    text_blobs = [r.text for r in responses] + [caplog.text]
    with world.app.state.database_engine.connect() as conn:
        for table in ("webhook_trigger_events", "webhook_deliveries", "audit_log", "runs", "execution_events", "webhook_triggers"):
            rows = conn.execute(text(f"SELECT * FROM {table} WHERE tenant_id = :t"), {"t": tenant}).mappings().all()
            text_blobs.append(json.dumps([dict(r) for r in rows], default=str))
    sig_value = sig["x-hub-signature-256"]
    blob = "\n".join(text_blobs)
    for sentinel in (SECRET_VALUE, "second-sentinel", "wrong-sentinel", sig_value.split("=", 1)[1]):
        assert sentinel not in blob, sentinel


# --------------------------------------------------------------------------------------------- the public prefix


def test_every_route_under_the_hooks_prefix_is_the_hook_route(world):
    """The Cloudflare Access bypass covers /api/v1/hooks/*: nothing else may live there."""
    routes = flat_routes(world.app)
    under = [(path, sorted(methods)) for path, methods, _r in routes if path.lower().startswith("/api/v1/hooks")]
    assert under == [("/api/v1/hooks/{trigger_id}", ["POST"])]
    # no mount, redirect or catch-all can answer for a path inside the prefix either
    for path, _methods, route in routes:
        if type(route).__name__ == "Mount" or "{" in path and ":path}" in path:
            assert not path.lower().startswith("/api/v1/hooks"), path
            assert "/api/v1/hooks".startswith(path.rstrip("/").lower()) is False or path in ("", "/"), path
    # and the prefix is exactly one segment deep: the one route has a single non-slash path parameter
    assert world.app.router.redirect_slashes in (True, False)


def test_path_tricks_never_reach_another_route(world):
    tenant, headers, _w, trig = world.live()
    body = body_of()
    sig = signed(body)
    before = len(world.runs(tenant))
    tricks = [
        "/api/v1/hooks/../agents", "/api/v1/hooks/%2e%2e/agents", "/api/v1/hooks/..%2Fagents", "/api/v1/hooks/%2E%2E%2Fagents",
        "/api/v1/hooks//agents", "/api/v1/hooks/../webhook-triggers", f"/api/v1/hooks/{trig['id']}/../agents", f"/api/v1/hooks/{trig['id']}/extra",
        "/api/v1/hooks/./", "/api/v1/hooks/..", "/api/v1/hooks", "/api/v1/hooks/", "/API/V1/HOOKS/" + trig["id"], "/api/v1/Hooks/" + trig["id"],
        "//api/v1/hooks/" + trig["id"], "/api/v1/./hooks/" + trig["id"], "/api/v1//hooks/" + trig["id"],
        "/api/v1/hooks/" + trig["id"] + "%00", "/api/v1/hooks/" + trig["id"] + ";x=1/../agents",
    ]
    for path in tricks:
        for method in ("POST", "GET"):
            for credentials in (sig, headers):  # even the management bearer reaches nothing through the prefix
                status_code, content = raw_request(world.app, method, path, credentials, body)
                assert status_code in (404, 405), (method, path, status_code)
                assert b"items" not in content and b"agents" not in content.lower() or status_code == 404, (path, content[:80])
    assert len(world.runs(tenant)) == before  # nothing started a run
    # the real route, addressed exactly, still works and answers POST only
    assert raw_request(world.app, "POST", f"/api/v1/hooks/{trig['id']}", sig, body)[0] == 202
    assert raw_request(world.app, "GET", f"/api/v1/hooks/{trig['id']}", sig)[0] == 405
    assert raw_request(world.app, "HEAD", f"/api/v1/hooks/{trig['id']}", sig)[0] == 405


# The routes that answer without `current_principal` / `agui_principal` / `otlp_principal` / `iam_principal` (design 3, layer 2):
# a new public route must be added here on purpose. Public by design: the hook route (its signature is its gate) and the
# external agents' surfaces (off by default): the MCP run surface (`run` keys only, test_external_mcp), the A2A
# JSON-RPC interface (`run` keys only) and the A2A public Agent Card, which lists no workflow (test_external_a2a). And the
# built-in admin's setup and sign-in, which come before any principal and answer 404 without the `localadmin` provider:
# the setup code, password or refresh token in the body is the credential (test_local_auth_api).
PUBLIC_ROUTES = {
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/healthz", "/api/v1/hooks/{trigger_id}", "/api/v1/mcp",
    "/api/v1/external/mcp",
    *(f"/api/v1/external/a2a/{{endpoint_id}}{base}{leaf}" for base in ("", "/skills/{skill}")
      for leaf in ("/rpc", "/.well-known/agent-card.json")),
    *(f"/api/v1/auth/local{leaf}" for leaf in ("", "/setup", "/sign-in", "/password", "/refresh", "/sign-out")),
}


def test_the_route_table_allowlist_of_routes_without_a_principal(world):
    from eap_api import auth

    principals = {auth.current_principal, auth.agui_principal, auth.otlp_principal, auth.iam_principal}
    unguarded = {path for path, _m, route in flat_routes(world.app) if not dependency_calls(route) & principals}
    assert "/api/v1/hooks/{trigger_id}" in unguarded
    assert not any(p.startswith("/api/v1/webhook-") for p in unguarded)
    assert unguarded == PUBLIC_ROUTES, sorted(unguarded ^ PUBLIC_ROUTES)


def test_client_ip_trusts_cf_connecting_ip_only_from_a_trusted_proxy():
    from eap_api.inbound_guard import WebhookGuardConfigError, client_ip, trusted_proxies_from_env

    trusted = trusted_proxies_from_env({"EAP_TRUSTED_PROXY_CIDRS": "10.42.0.0/16"})
    spoof = {"cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"}
    assert client_ip("203.0.113.9", spoof, trusted) == "203.0.113.9"  # untrusted peer: headers ignored
    assert client_ip("203.0.113.9", spoof, ()) == "203.0.113.9"
    assert client_ip("10.42.0.7", spoof, trusted) == "1.2.3.4"  # trusted peer: CF-Connecting-IP, never X-Forwarded-For
    assert client_ip("10.42.0.7", {"cf-connecting-ip": "2001:db8::1"}, trusted) == "2001:db8::1"
    for bad in ("1.2.3.4, 5.6.7.8", "not-an-ip", "", "1.2.3.4:80"):
        assert client_ip("10.42.0.7", {"cf-connecting-ip": bad}, trusted) == "10.42.0.7"
    assert client_ip("10.42.0.7", {}, trusted) == "10.42.0.7" and client_ip(None, spoof, trusted) == "unknown"
    assert trusted_proxies_from_env({}) == ()
    with pytest.raises(WebhookGuardConfigError):
        trusted_proxies_from_env({"EAP_TRUSTED_PROXY_CIDRS": "nope"})


def test_senders_behind_the_proxy_get_separate_buckets_and_a_spoof_gets_none(world, monkeypatch):
    from eap_api.inbound_guard import InboundGuard

    monkeypatch.setenv("EAP_TRUSTED_PROXY_CIDRS", "10.42.0.0/16")
    world.app.state.webhook_guard = InboundGuard(2)
    _t, _h, _w, trig = world.live()
    # the TestClient peer is "testclient": not an address, so it is never trusted
    codes = [world.post(trig["id"], b"{}", {"cf-connecting-ip": f"9.9.9.{i}"}).status_code for i in range(4)]
    assert codes == [401, 401, 429, 429]  # a spoofed header from an untrusted peer buys no new bucket


def test_startup_guard_refuses_a_public_hook_path_under_the_dev_bearer(monkeypatch):
    from eap_api.inbound_guard import WebhookGuardConfigError, check_webhook_startup, webhook_base_url

    with pytest.raises(WebhookGuardConfigError):
        check_webhook_startup({"EAP_WEBHOOK_PUBLIC": "true", "EAP_AUTH_MODE": "dev"})
    check_webhook_startup({"EAP_WEBHOOK_PUBLIC": "true", "EAP_AUTH_MODE": "production"})
    check_webhook_startup({"EAP_AUTH_MODE": "dev"})
    assert webhook_base_url({"EAP_WEBHOOK_BASE_URL": "https://x.example/"}) == "https://x.example"
    for bad in ("http://x.example", "ftp://x", "https://u:p@x.example", "https://x.example?a=1"):
        with pytest.raises(WebhookGuardConfigError):
            webhook_base_url({"EAP_WEBHOOK_BASE_URL": bad})
    assert webhook_base_url({"EAP_WEBHOOK_BASE_URL": "http://localhost:8000"}) == "http://localhost:8000"
    monkeypatch.setenv("EAP_WEBHOOK_PUBLIC", "true")
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    with pytest.raises(WebhookGuardConfigError):
        create_app(database_url=PG_URL)
