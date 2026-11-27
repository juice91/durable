"""The built-in admin under a REAL OpenFGA: the role `admin` travels with its principal, so it is the tenant admin with
nothing stored; the backfill gives `localadmin:<username>` ownership of what existed before the switch.

Needs Postgres and an OpenFGA: skipped unless `EAP_TEST_OPENFGA_URL` or `EAP_TEST_OPENFGA_DOCKER=1` (CI Heavy)."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import uuid
from pathlib import Path

import pytest
from eap_iam_openfga.bootstrap import run as bootstrap_run
from eap_iam_openfga.model import model_text
from eap_iam_openfga.reconcile import amain
from eap_iam_openfga.settings import OpenFgaSettings
from eap_run_store.iam_catalog import RunStoreCatalog
from eap_secrets import KEY_ENV, generate_key

from .local_auth_helpers import API, admin_app, bearer, clean, first_sign_in, plugin_tables
from .run_helpers import hard_timeout, requires_pg
from .test_workflow_runs_api import AGENT_SPEC

_SERVER = Path(__file__).resolve().parents[3] / "plugins/iam-openfga/iam_openfga_tests/server.py"
_spec = importlib.util.spec_from_file_location("openfga_test_server_local", _SERVER)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
server = _module.server

pytestmark = requires_pg


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(120):
        yield


@pytest.fixture
def openfga(server):
    url, key = server
    base = OpenFgaSettings(url=url, store_id="", preshared_key=key, timeout=10.0)
    result = asyncio.run(bootstrap_run(
        base, store_name=f"local-{uuid.uuid4().hex[:8]}", model_text_=model_text(), store_id=None, dry_run=False
    ))
    env = {"EAP_OPENFGA_URL": url, "EAP_OPENFGA_STORE_ID": result.store_id, "EAP_OPENFGA_MODEL_ID": result.model_id}
    if key:
        env["EAP_OPENFGA_PRESHARED_KEY"] = key
    return env


@pytest.fixture
def tenants(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())
    created: list[str] = []
    with plugin_tables():
        yield created
        clean(created)


def test_the_admin_is_the_tenant_admin_under_openfga(monkeypatch, tenants, openfga):
    with admin_app(monkeypatch, tenants, EAP_IAM_POLICY="openfga", EAP_IAM_ALLOW_TRANSITIONAL="", **openfga) as made:
        _, client, _tenant = made
        h = bearer(first_sign_in(client))
        body = client.get(f"{API}/iam/capabilities", headers=h).json()
        assert body["policy"]["name"] == "openfga"
        assert {"tenant.manage_members", "tenant.read_audit"} <= set(body["actions"])
        agent = client.post(f"{API}/agents", json={"spec": AGENT_SPEC}, headers=h)
        assert agent.status_code == 201, agent.text
        agent_id = agent.json()["id"]
        assert [a["id"] for a in client.get(f"{API}/agents", headers=h).json()] == [agent_id]
        assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
        secret = client.put(f"{API}/secrets/crm", json={"value": "fake-value"}, headers=h)
        assert secret.status_code in (200, 201), secret.text
        assert client.get(f"{API}/audit", headers=h).status_code == 200
        access = client.get(f"{API}/iam/access/agent/{agent_id}", headers=h).json()
        assert access["grants"] == [{"relation": "owner", "subject_type": "user", "subject": "localadmin:admin"}]
        granted = client.post(
            f"{API}/iam/access/agent/{agent_id}/grants", json={"relation": "viewer", "member_id": "admin"}, headers=h
        )
        assert granted.status_code == 200 and granted.json()["subject"] == "localadmin:admin", granted.text


def test_the_backfill_links_what_existed_before_the_switch(monkeypatch, tenants, openfga):
    with admin_app(monkeypatch, tenants) as (app, client, tenant):  # openaccess: nothing is registered
        h = bearer(first_sign_in(client))
        agent_id = client.post(f"{API}/agents", json={"spec": AGENT_SPEC}, headers=h).json()["id"]
        out = io.StringIO()
        code = asyncio.run(amain(
            ["--tenant", tenant, "--owner", "localadmin:admin", "--apply"], backfill=True, env=openfga, out=out,
            catalog=RunStoreCatalog(app.state.database_engine),
        ))
        assert code == 0, out.getvalue()
        assert "owner:agent: 1" in out.getvalue() and "link:agent: 1" in out.getvalue()
    monkeypatch.setenv("EAP_IAM_ALLOW_TRANSITIONAL", "")
    from eap_api.app import create_app
    from fastapi.testclient import TestClient

    from .run_helpers import PG_URL

    for name, value in {"EAP_IAM_POLICY": "openfga", **openfga}.items():
        monkeypatch.setenv(name, value)
    switched = create_app(database_url=PG_URL)
    with TestClient(switched, client=("192.0.2.11", 40000)) as client:
        signed = client.post(f"{API}/auth/local/sign-in", json={"username": "admin", "password": "chosen-password-0002"})
        h = bearer(signed.json())
        assert client.get(f"{API}/agents/{agent_id}", headers=h).status_code == 200
        access = client.get(f"{API}/iam/access/agent/{agent_id}", headers=h).json()
        assert "localadmin:admin" in [g["subject"] for g in access["grants"]]
    switched.state.database_engine.dispose()
