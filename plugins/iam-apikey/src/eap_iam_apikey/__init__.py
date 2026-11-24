"""Tenant API keys and the Architect run token behind `eap_iam.IdentityProvider` (design 1.5)."""

from __future__ import annotations

from eap_iam import PluginContext

from eap_iam_apikey.provider import ApiKeyIdentityProvider

__all__ = ["ApiKeyIdentityProvider", "create"]


def create(ctx: PluginContext) -> ApiKeyIdentityProvider:
    from eap_run_store import ApiKeyStore

    return ApiKeyIdentityProvider(ApiKeyStore(ctx.engine), ctx.engine, ctx.env, ctx.membership)
