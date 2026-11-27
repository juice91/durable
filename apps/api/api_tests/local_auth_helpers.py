"""The built-in admin account behind the real API: the environment, the plug-in's own tables in the session schema, and
an app that signs people in with it."""

from __future__ import annotations

import secrets
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from eap_api.app import create_app

from .run_helpers import PG_URL, cleanup_tenant

API = "/api/v1"
AUTH = f"{API}/auth/local"
INITIAL = "initial-password-0001"
CHOSEN = "chosen-password-0002"
KEY = secrets.token_hex(32)  # made at run time: no key value lives in the repository


def local_env(tenant: str, **extra: str) -> dict[str, str]:
    return {
        "EAP_AUTH_MODE": "production",
        "EAP_IAM_IDENTITY": "localadmin,apikey",
        "EAP_IAM_POLICY": "openaccess",
        "EAP_IAM_ALLOW_TRANSITIONAL": "1",
        "EAP_IAM_MEMBERSHIP": "localadmin",
        "EAP_LOCAL_ADMIN_USERNAME": "admin",
        "EAP_LOCAL_ADMIN_INITIAL_PASSWORD": INITIAL,
        "EAP_LOCAL_AUTH_SIGNING_KEY": KEY,
        "EAP_LOCAL_ADMIN_TENANT": tenant,
        **extra,
    }


@contextmanager
def plugin_tables():
    """The plug-in's Alembic branch in the session schema, removed afterwards (core's schema checks do not know it)."""
    from eap_db.plugin_migrations import downgrade_branch, installed_branches, upgrade_branch

    branch = next(b for b in installed_branches() if b.name == "localadmin")
    engine = create_engine(PG_URL)
    with engine.begin() as connection:
        upgrade_branch(connection, branch)
    try:
        yield
    finally:
        with engine.begin() as connection:
            downgrade_branch(connection, branch)
            connection.execute(text(f"DROP TABLE {branch.version_table}"))
        engine.dispose()


@contextmanager
def admin_app(monkeypatch, tenants: list[str], **extra: str):
    """(app, client, tenant) with the built-in admin of a fresh tenant."""
    tenant = f"t{uuid.uuid4().hex[:10]}"
    tenants.append(tenant)
    for name, value in local_env(tenant, **extra).items():
        monkeypatch.setenv(name, value)
    app = create_app(database_url=PG_URL)
    with TestClient(app, client=("192.0.2.10", 40000)) as client:
        yield app, client, tenant
    app.state.database_engine.dispose()


def clean(tenants: list[str]) -> None:
    engine = create_engine(PG_URL)
    for tenant in tenants:
        with engine.begin() as conn:
            for table in ("audit_log", "workflow_versions", "workflows"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
        cleanup_tenant(engine, tenant)
    engine.dispose()


def bearer(body: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {body['access_token']}"}


def first_sign_in(client: TestClient) -> dict:
    """The forced change: the session body."""
    response = client.post(f"{AUTH}/password", json={"username": "admin", "current_password": INITIAL, "new_password": CHOSEN})
    assert response.status_code == 200, response.text
    return response.json()
