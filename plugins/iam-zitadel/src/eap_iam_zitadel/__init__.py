"""`eap-iam-zitadel`: ZITADEL access tokens as the identity, the tenant's org as the membership source (design 1.7).

Two entry points share one configuration (`EAP_ZITADEL_*`, see `settings.py`): `create_identity` (group
`eap.iam.identity`) and `create_membership` (group `eap.iam.membership`). Without a project id and org map both are
dormant, so a deployment can list them before the bootstrap has run."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
from eap_iam import PluginContext

from eap_iam_zitadel.jwks import JwksCache, fetch_jwks
from eap_iam_zitadel.management import ManagementClient
from eap_iam_zitadel.membership import ZitadelMembership
from eap_iam_zitadel.provider import ZitadelProvider
from eap_iam_zitadel.settings import ZitadelSettings, load_settings


def create_identity(ctx: PluginContext, *, fetcher: Callable[[], Any] | None = None) -> ZitadelProvider:
    settings = load_settings(ctx.env)
    if settings is None:
        return ZitadelProvider(None)
    cache = JwksCache(
        fetcher or (lambda: fetch_jwks(settings.jwks_url, settings.issuer_host)),
        ttl=settings.jwks_ttl,
        min_refresh=settings.jwks_min_refresh,
        grace=settings.jwks_grace,
    )
    return ZitadelProvider(settings, jwks=cache)


def _client(settings: ZitadelSettings, http: httpx2.AsyncClient | None) -> ManagementClient | None:
    if settings.machine_key is None:
        return None
    return ManagementClient(
        api_url=settings.api_url, issuer=settings.issuer, host=settings.issuer_host, key=settings.machine_key, http=http
    )


def create_membership(ctx: PluginContext, *, http: httpx2.AsyncClient | None = None) -> ZitadelMembership:
    settings = load_settings(ctx.env)
    if settings is None:
        return ZitadelMembership(None, None)
    return ZitadelMembership(settings, _client(settings, http))


__all__ = ["ZitadelMembership", "ZitadelProvider", "create_identity", "create_membership"]
