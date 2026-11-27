"""IAM P0b through the API: the Cloudflare Access JWT end to end, the real actor in created_by / published_by /
decided_by / the run, the audit log (rows, filters, retention, no secrets), and the implied-permissions report."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import text

import eap_iam_cfaccess
from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_approvals_api import add_tool_wait, tool_url, waiting_step
from .test_workflow_runs_api import FakeClient, publish_demo

pytestmark = requires_pg

TEAM, AUD = "example-org.cloudflareaccess.com", "aud-p0b"


def _forged(alg: str, claims: dict) -> str:
    """A token that claims `alg` in its header, signed with a random throwaway key (HS256) or not at all (none).

    Built by hand so the negative tests carry no literal `alg=none` encode call or hard-coded signing secret."""

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    signing_input = b64(json.dumps({"alg": alg, "typ": "JWT", "kid": "k1"}).encode()) + "." + b64(json.dumps(claims).encode())
    signature = hmac.new(os.urandom(32), signing_input.encode(), hashlib.sha256).digest() if alg == "HS256" else b""
    return signing_input + "." + b64(signature)


EMAIL = "user@example.com"
AGENT_SPEC = {"name": "Gated", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(90):
        yield


@pytest.fixture
def cf(monkeypatch):
    """An app whose chain is apikey,cfaccess,devloopback with a throw-away Access key, reached from a pod IP."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    tenant = f"t{uuid.uuid4().hex[:10]}"
    jwks = {"keys": [{**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}]}
    monkeypatch.setattr(eap_iam_cfaccess, "fetch_jwks", lambda url: jwks)
    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    for name, value in {
        "EAP_IAM_IDENTITY": "apikey,cfaccess,devloopback", "EAP_CF_ACCESS_TEAM_DOMAIN": TEAM,
        "EAP_CF_ACCESS_AUD": AUD, "EAP_CF_ACCESS_TENANT": tenant,
    }.items():
        monkeypatch.setenv(name, value)
    fake = FakeClient()
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )

    def mint(email: str | None = EMAIL, signer=key, **claims) -> str:
        body = {"iss": f"https://{TEAM}", "aud": [AUD], "exp": int(time.time()) + 300, "iat": int(time.time()), "email": email}
        body.update(claims)
        return jwt.encode({k: v for k, v in body.items() if v is not None}, signer, algorithm="RS256", headers={"kid": "k1"})

    def headers(email: str = EMAIL) -> dict[str, str]:
        return {"Cf-Access-Jwt-Assertion": mint(email)}

    with TestClient(app, client=("10.42.0.9", 40000)) as client:
        yield SimpleNamespace(client=client, app=app, tenant=tenant, mint=mint, headers=headers, fake=fake, key=key)
    engine = app.state.database_engine
    cleanup_tenant(engine, tenant)
    with engine.begin() as conn:
        for table in ("workflow_versions", "workflows", "audit_log", "tenant_secrets", "api_keys"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
    engine.dispose()


def rows(cf, sql: str, **params):
    with cf.app.state.database_engine.connect() as conn:
        return conn.execute(text(sql), {"t": cf.tenant, **params}).mappings().all()


# --- authentication -------------------------------------------------------------------------------------


def test_a_valid_access_jwt_authenticates_with_the_email_as_the_subject(cf):
    r = cf.client.get("/api/v1/iam/capabilities", headers=cf.headers())
    assert r.status_code == 200
    body = r.json()
    assert [p["name"] for p in body["identity"]] == ["apikey", "cfaccess", "devloopback", "webhook"]
    assert body["me"] == {
        "subject": EMAIL, "email": EMAIL, "tenant_id": cf.tenant, "kind": "user", "roles": [], "provider": "cf-access",
    }
    assert body["membership"] is None  # Members and Access stay hidden: no membership source, no grants feature
    assert "grants" not in body["policy"]["features"]
    assert cf.client.get("/api/v1/agents", headers=cf.headers()).status_code == 200


def test_every_invalid_access_credential_is_a_401(cf):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    bad = {
        "wrong key": cf.mint(signer=other),
        "wrong aud": cf.mint(aud=["someone-else"]),
        "wrong iss": cf.mint(iss="https://evil.example"),
        "expired": cf.mint(exp=now - 600),
        "no email": cf.mint(email=None),
        "alg none": _forged("none", {"iss": f"https://{TEAM}", "aud": [AUD], "exp": now + 60, "email": EMAIL}),
        "hs256": _forged("HS256", {"iss": f"https://{TEAM}", "aud": [AUD], "exp": now + 60, "email": EMAIL}),
    }
    for label, token in bad.items():
        r = cf.client.get("/api/v1/agents", headers={"Cf-Access-Jwt-Assertion": token})
        assert r.status_code == 401, label
    # A bare e-mail header, alone or beside the dev bearer from a pod IP: nothing.
    mail = {"Cf-Access-Authenticated-User-Email": EMAIL}
    assert cf.client.get("/api/v1/agents", headers=mail).status_code == 401
    assert cf.client.get("/api/v1/agents", headers={**mail, **get_auth_header(cf.tenant, "alice")}).status_code == 401


def test_a_valid_jwt_beside_a_bad_bearer_is_not_rescued_and_the_bearer_alone_stays_loopback_only(cf):
    assert cf.client.get("/api/v1/agents", headers=get_auth_header(cf.tenant, "alice")).status_code == 401
    with TestClient(cf.app, client=("127.0.0.1", 40000)) as local:
        assert local.get("/api/v1/agents", headers=get_auth_header(cf.tenant, "alice")).status_code == 200


# --- the actor and the audit log --------------------------------------------------------------------------


def test_an_agent_edit_a_publish_a_run_and_an_approval_record_the_subject_everywhere(cf):
    client, h = cf.client, cf.headers()
    created = client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=h)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    etag = client.get(f"/api/v1/agents/{agent_id}", headers=h).headers["ETag"]
    edited = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**AGENT_SPEC, "instructions": "Help more."}},
                          headers={**h, "If-Match": etag})
    assert edited.status_code == 200, edited.text
    published = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
    assert published.status_code == 201
    assert published.json()["created_by"] == EMAIL and published.json()["published_by"] == EMAIL
    version = rows(cf, "SELECT created_by, published_by FROM agent_versions WHERE tenant_id = :t")[0]
    assert (version["created_by"], version["published_by"]) == (EMAIL, EMAIL)

    run_id = uuid.uuid4()
    started = client.post(f"/api/v1/agents/{agent_id}/runs", json={"input": "hi", "run_id": str(run_id)}, headers=h)
    assert started.status_code == 201, started.text
    run = rows(cf, "SELECT principal, on_behalf_of, started_by_kind FROM runs WHERE id = :r", r=str(run_id))[0]
    assert dict(run) == {"principal": f"user:{EMAIL}", "on_behalf_of": EMAIL, "started_by_kind": "user"}

    # An approval decided by the user: the event's decided_by is the e-mail.
    waiting_run = uuid.uuid4()
    run_store, event_log = cf.app.state.run_stores
    run_store.create_run(cf.tenant, uuid.UUID(agent_id), 1, "hi", waiting_run)
    assert run_store.mark_running(cf.tenant, waiting_run)
    add_tool_wait(cf.app, cf.tenant, waiting_run, "call_1")
    decided = client.post(tool_url(waiting_run, "call_1", "approve"), headers=h)
    assert decided.status_code == 200, decided.text
    assert decided.json()["decided_by"] == EMAIL
    assert event_log.get_event(cf.tenant, waiting_run, "tool:call_1:approval:decision").data["decided_by"] == EMAIL

    # The workflow approval step too.
    step_run = waiting_step(client, cf.app, cf.tenant, h)
    approved = client.post(f"/api/v1/workflows/runs/{step_run}/steps/approve/approve", json={"comment": "ok"}, headers=h)
    assert approved.status_code == 200, approved.text
    assert approved.json()["decided_by"] == EMAIL

    # One audit row per mutating call, each naming the subject, the action, the resource and the request id.
    listing = client.get("/api/v1/audit", params={"actor": EMAIL, "limit": 200}, headers=h)
    assert listing.status_code == 200
    items = listing.json()["items"]
    by_action = {}
    for item in items:
        by_action.setdefault(item["action"], []).append(item)
    for action in ("create_agent", "patch_agent", "publish_version", "approve_tool_call"):
        assert action in by_action, (action, sorted(by_action))
    publish = next(i for i in by_action["publish_version"] if i["resource_id"] == agent_id)
    assert publish["actor_subject"] == EMAIL and publish["actor_kind"] == "user" and publish["on_behalf_of"] is None
    assert publish["resource_kind"] == "agents" and publish["resource_id"] == agent_id
    assert publish["outcome"] == "success" and publish["request_id"] and publish["detail"]["status"] == 201
    approve = by_action["approve_tool_call"][0]
    assert approve["run_id"] == str(waiting_run)
    assert {i["action"] for i in items if i["action"].startswith("approve")} >= {"approve_tool_call"}


def test_a_refused_call_is_audited_as_failed_and_an_unauthenticated_one_is_not(cf):
    h = cf.headers()
    assert cf.client.post(f"/api/v1/agents/{uuid.uuid4()}/versions", headers=h).status_code == 404
    assert cf.client.post("/api/v1/agents", json={"spec": AGENT_SPEC}).status_code == 401
    items = cf.client.get("/api/v1/audit", headers=h).json()["items"]
    assert [(i["action"], i["outcome"]) for i in items if i["action"] == "publish_version"] == [("publish_version", "failed")]
    assert not [i for i in items if i["action"] == "create_agent"]


def test_the_audit_listing_filters_pages_and_is_for_people_only(cf):
    h = cf.headers()
    for _ in range(3):
        assert cf.client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=h).status_code == 201
    other = cf.headers("someone.else@example.com")
    assert cf.client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=other).status_code == 201
    page = cf.client.get("/api/v1/audit", params={"action": "create_agent", "limit": 2}, headers=h).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    rest = cf.client.get("/api/v1/audit", params={"action": "create_agent", "limit": 2, "cursor": page["next_cursor"]}, headers=h).json()
    assert len(rest["items"]) == 2 and rest["next_cursor"] is None
    only = cf.client.get("/api/v1/audit", params={"actor": "someone.else@example.com", "action": "create_agent"}, headers=h).json()
    assert [i["actor_subject"] for i in only["items"]] == ["someone.else@example.com"]
    assert cf.client.get("/api/v1/audit", params={"outcome": "denied"}, headers=h).json()["items"] == []
    assert cf.client.get("/api/v1/audit", params={"cursor": "nonsense"}, headers=h).status_code == 422
    assert cf.client.get("/api/v1/audit", params={"outcome": "bogus"}, headers=h).status_code == 422
    assert cf.client.get("/api/v1/audit").status_code == 401
    assert page["retention_days"] == 400


def test_actor_me_lists_only_the_callers_own_rows_in_their_tenant(cf):
    h = cf.headers()
    assert cf.client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=h).status_code == 201
    mine = cf.client.get("/api/v1/audit", params={"actor": "me"}, headers=h)
    assert mine.status_code == 200, mine.text
    items = mine.json()["items"]
    assert items and {i["actor_subject"] for i in items} == {EMAIL}
    # Reading your own activity is not itself an audit.read row.
    assert not [i for i in cf.client.get("/api/v1/audit", params={"actor": "me"}, headers=h).json()["items"] if i["action"] == "audit.read"]
    # Another person sees none of those rows under `me`.
    other = cf.client.get("/api/v1/audit", params={"actor": "me"}, headers=cf.headers("someone.else@example.com")).json()
    assert other["items"] == []
    assert cf.client.get("/api/v1/audit", params={"actor": "me"}).status_code == 401


def test_no_secret_or_key_value_reaches_audit_log_or_the_logs(cf, caplog):
    caplog.set_level(logging.DEBUG)
    h = cf.headers()
    canary = "CANARY-secret-value-9d41e7"
    assert cf.client.put("/api/v1/secrets/crm_token", json={"value": canary}, headers=h).status_code in (200, 201, 204)
    made = cf.client.post("/api/v1/api-keys", json={"name": "ci", "scopes": ["agui:run"]}, headers=h)
    assert made.status_code == 201, made.text
    plaintext = made.json()["key"]
    assert plaintext.startswith("pos_")
    assert cf.client.delete(f"/api/v1/api-keys/{made.json()['id']}", headers=h).status_code == 204
    token = h["Cf-Access-Jwt-Assertion"]
    dump = json.dumps([dict(r) for r in rows(cf, "SELECT * FROM audit_log WHERE tenant_id = :t")], default=str)
    listing = cf.client.get("/api/v1/audit", headers=h).text
    for blob in (dump, listing, caplog.text):
        assert canary not in blob and plaintext not in blob and token not in blob
    assert "create_api_key" in dump and "put_secret" in dump  # they were audited, as events with no values


def test_the_audit_log_is_append_only(cf):
    cf.client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=cf.headers())
    with cf.app.state.database_engine.begin() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE audit_log SET action = 'x' WHERE tenant_id = :t"), {"t": cf.tenant})


def test_audit_pruning_deletes_only_rows_older_than_the_cutoff(cf):
    from datetime import datetime, timedelta, timezone

    from eap_run_store import AuditEntry, AuditStore

    store = AuditStore(cf.app.state.database_engine)
    store.record(AuditEntry(tenant_id=cf.tenant, actor_kind="user", actor_subject=EMAIL, action="old", outcome="success"))
    with cf.app.state.database_engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only"))
        conn.execute(text("UPDATE audit_log SET at = now() - interval '500 days' WHERE tenant_id = :t"), {"t": cf.tenant})
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only"))
    store.record(AuditEntry(tenant_id=cf.tenant, actor_kind="user", actor_subject=EMAIL, action="new", outcome="success"))
    assert store.prune_batch(datetime.now(timezone.utc) - timedelta(days=400)) >= 1
    assert [r["action"] for r in rows(cf, "SELECT action FROM audit_log WHERE tenant_id = :t")] == ["new"]


# --- the implied-permissions report (read only) ---------------------------------------------------------------


def test_the_implied_permissions_report_is_read_only_and_lists_what_references_imply(cf):
    client, h = cf.client, cf.headers()
    workflow_id = publish_demo(client, h)  # an agent with no refs and a workflow with an agent step and tools
    before = client.get(f"/api/v1/workflows/{workflow_id}", headers=h).json()
    r = client.get("/api/v1/iam/implied-permissions", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enforced"] is False
    workflow = next(i for i in body["items"] if i["kind"] == "workflow")
    assert workflow["implied"]["tools"] == ["clock.now"] and len(workflow["implied"]["agents"]) == 1
    assert workflow["declared"] is False and workflow["dynamic"] is False
    assert body["summary"]["total"] == len(body["items"]) == 2
    # Reading changes nothing: the workflow is exactly as it was, and the report is not an audited mutation.
    assert client.get(f"/api/v1/workflows/{workflow_id}", headers=h).json() == before
    assert client.get("/api/v1/iam/implied-permissions").status_code == 401


# --- the MCP control endpoint -----------------------------------------------------------------------------------

from .mcp_control_helpers import world  # noqa: E402,F401 - fixture


def test_a_mutating_mcp_tool_call_is_audited_without_its_arguments_and_a_read_is_not(world):
    tenant, h = world.tenant_of()
    key = world.make_key(h, scopes=["agui:run", "manage"])
    canary = "CANARY-instructions-6f1b"
    spec = {"name": "Drafted", "instructions": canary, "model": {"provider": "test", "name": "echo"}}
    saved = world.call(key, "agent.save_draft", {"spec": spec})
    assert not saved.is_error, saved
    world.call(key, "agent.list", {})
    got = world.sql("SELECT actor_kind, actor_subject, action, outcome, detail FROM audit_log WHERE tenant_id = :t "
                    "AND action LIKE 'mcp.%'", t=tenant)
    assert [(r[0], r[2], r[3]) for r in got] == [("api_key", "mcp.agent.save_draft", "success")]
    assert got[0][1] and "pos_" not in got[0][1]  # the key id, never the key
    assert canary not in json.dumps([list(map(str, r)) for r in got])
