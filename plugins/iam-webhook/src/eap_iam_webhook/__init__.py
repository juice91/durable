"""The `webhook` identity provider (see the README)."""

from __future__ import annotations

from eap_iam import PluginContext

from eap_iam_webhook.provider import (
    INVALID_SIGNATURE_DETAIL,
    NOT_FOUND_DETAIL,
    WebhookSignatureProvider,
)

__all__ = ["INVALID_SIGNATURE_DETAIL", "NOT_FOUND_DETAIL", "WebhookSignatureProvider", "create"]


def create(ctx: PluginContext) -> WebhookSignatureProvider:
    from eap_run_store import WebhookStore

    return WebhookSignatureProvider(WebhookStore(ctx.engine), ctx.engine, ctx.env)
