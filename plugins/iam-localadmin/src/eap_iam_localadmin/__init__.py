"""`eap-iam-localadmin`: exactly one built-in admin account (username and password) for deployments without an identity
provider. People who need several users or roles use ZITADEL instead.

`create_identity` (group `eap.iam.identity`) signs the admin in and verifies the tokens it issued; `create_membership`
(group `eap.iam.membership`) answers that the admin holds the role `admin` in its tenant; `MIGRATIONS` (group
`eap.db.migrations`) is the plug-in's own Alembic branch (`eap-db plugins upgrade`)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from eap_iam import PluginContext

from eap_iam_localadmin.membership import LocalAdminMembership
from eap_iam_localadmin.passwords import Passwords
from eap_iam_localadmin.provider import LocalAdminProvider
from eap_iam_localadmin.settings import load_account_settings, load_settings
from eap_iam_localadmin.store import AdminStore, utcnow

MIGRATIONS = Path(__file__).parent / "migrations"


def create_identity(
    ctx: PluginContext, *, passwords: Passwords | None = None, clock: Callable[[], datetime] = utcnow
) -> LocalAdminProvider:
    """Refuses to start (IamConfigError) when half configured; with a database, also when the tables are missing or the
    env values cannot make the account. With no account and no env values it prints a one-time setup code. Without a
    database (a caller outside an app) it authenticates and signs in nobody."""
    settings = load_settings(ctx.env)
    store = None if ctx.engine is None else AdminStore(ctx.engine, settings.tenant_id, clock)
    provider = LocalAdminProvider(settings, store, passwords or Passwords(), clock)
    if store is not None:
        provider.provision(production=ctx.env.get("EAP_AUTH_MODE") == "production")
    return provider


def create_membership(ctx: PluginContext) -> LocalAdminMembership:
    settings = load_account_settings(ctx.env)
    store = None if ctx.engine is None else AdminStore(ctx.engine, settings.tenant_id)
    return LocalAdminMembership(settings, store)


__all__ = ["MIGRATIONS", "LocalAdminMembership", "LocalAdminProvider", "create_identity", "create_membership"]
