"""Webhook trigger tools (design section 10): the functional namespace `webhook.*`.

Reads need any API key of the tenant; mutations need `manage`. Every body calls the service the REST routes call.
What MCP never does: return a secret value (a trigger holds `secret:<name>` references; `generate` is Studio-only), return a
raw body or a signature (none is stored), add a secret to an enabled trigger, and ENABLE a trigger. `webhook.trigger.request_enable` moves it to
`pending_enable` and a person decides in Studio; there is no tool for the decision, and `approvals.decide` does not
take this kind. A key restricted to some agents or workflows cannot manage triggers (a trigger starts workflows) and sees only the
triggers of its own workflows in the list."""

from __future__ import annotations

from typing import Any

from eap_api.deps import webhook_store_of
from eap_api.inbound_guard import webhook_base_url
from eap_api.mcp_control.helpers import require_unrestricted, uid
from eap_api.mcp_control.routing import call_service
from eap_api.mcp_control.server import current_ctx
from eap_api.services.secrets import sealed_secret_store
from eap_api.services.webhook_triggers import WebhookTriggerService


def _service(ctx: Any) -> Any:
    return lambda db: WebhookTriggerService(webhook_store_of(ctx.app), db, ctx.engine, sealed_secret_store, webhook_base_url())


def _own(ctx: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if ctx.workflow_allowed(str(r.get("workflow_id")))]


def register_tools(tool: Any) -> None:
    @tool("webhook.trigger.list")
    async def webhook_trigger_list(workflow_id: str | None = None, include_archived: bool = False) -> dict[str, Any]:
        """The tenant's inbound webhook triggers (name, workflow, preset, status, URL, secret REFERENCE names, limits,
        last delivery). Never a secret value."""
        ctx = current_ctx()
        wid = None if workflow_id is None else str(uid(workflow_id, "workflow_id"))
        rows = await call_service(
            ctx, _service(ctx), lambda s: s.list(ctx.tenant_id, include_archived=include_archived, workflow_id=wid))
        return {"items": _own(ctx, rows)}

    @tool("webhook.trigger.get")
    async def webhook_trigger_get(trigger_id: str) -> dict[str, Any]:
        """One trigger with its configuration, status, `config_hash` and publish-style warnings (side effects without an
        approval step, webhook text reaching an agent prompt, following `latest`)."""
        ctx = current_ctx()
        require_unrestricted(ctx, "read webhook triggers")
        found = await call_service(ctx, _service(ctx), lambda s: s.get(ctx.tenant_id, uid(trigger_id, "trigger_id")))
        return found

    @tool("webhook.trigger.events")
    async def webhook_trigger_events(trigger_id: str, limit: int = 50) -> dict[str, Any]:
        """The trigger's lifecycle audit rows (created, enabled, secret_added, ...), newest first. Never a secret."""
        ctx = current_ctx()
        require_unrestricted(ctx, "read webhook triggers")
        items = await call_service(
            ctx, _service(ctx), lambda s: s.events(ctx.tenant_id, uid(trigger_id, "trigger_id"), max(1, min(limit, 100))))
        return {"items": items}

    @tool("webhook.delivery.list")
    async def webhook_delivery_list(
        trigger_id: str | None = None, outcome: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Authenticated deliveries (outcome, reason, size, timestamp skew, secret slot, the run it started), newest
        first, at most 100. No body, signature or header value is stored or returned."""
        ctx = current_ctx()
        require_unrestricted(ctx, "read webhook deliveries")
        tid = None if trigger_id is None else uid(trigger_id, "trigger_id")
        items = await call_service(
            ctx, _service(ctx), lambda s: s.deliveries(ctx.tenant_id, trigger_id=tid, outcome=outcome, limit=max(1, min(limit, 100))))
        return {"items": items}

    @tool("webhook.delivery.get")
    async def webhook_delivery_get(delivery_id: str) -> dict[str, Any]:
        """One delivery."""
        ctx = current_ctx()
        require_unrestricted(ctx, "read webhook deliveries")
        return await call_service(ctx, _service(ctx), lambda s: s.delivery(ctx.tenant_id, str(uid(delivery_id, "delivery_id"))))

    @tool("webhook.trigger.create", manage=True)
    async def webhook_trigger_create(
        name: str, workflow_id: str, version: int | str, preset: str, secrets: list[str], actions: dict[str, Any],
        description: str = "", preset_config: dict[str, Any] | None = None, tolerance_seconds: int = 300,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a DISABLED trigger that starts `workflow_id` (a version number or "latest") from a signed delivery.
        `preset`: github | standard_webhooks | custom. `secrets`: one or two EXISTING references `secret:<name>` (create the
        secret in Studio, or with secrets.request: a value is never an argument). `actions`: {start: {payload_schema,
        input_map}}. A person enables it in Studio after you call webhook.trigger.request_enable."""
        ctx = current_ctx()
        require_unrestricted(ctx, "create webhook triggers")
        payload = {
            "name": name, "workflow_id": workflow_id, "version": version, "preset": preset, "secrets": secrets,
            "actions": actions, "description": description, "tolerance_seconds": tolerance_seconds,
        }
        if preset_config is not None:
            payload["preset_config"] = preset_config
        if limits is not None:
            payload["limits"] = limits
        return await call_service(ctx, _service(ctx), lambda s: s.create(ctx.tenant_id, ctx.actor.user_id, payload))

    @tool("webhook.trigger.update", manage=True)
    async def webhook_trigger_update(trigger_id: str, changes: dict[str, Any], if_revision: int | None = None) -> dict[str, Any]:
        """Change description, name, version, preset_config, tolerance_seconds, actions or limits. A change to the grant
        returns an enabled trigger to `disabled`: a person approves it again."""
        ctx = current_ctx()
        require_unrestricted(ctx, "change webhook triggers")
        return await call_service(
            ctx, _service(ctx), lambda s: s.update(ctx.tenant_id, ctx.actor.user_id, uid(trigger_id, "trigger_id"), changes, if_revision))

    @tool("webhook.trigger.request_enable", manage=True)
    async def webhook_trigger_request_enable(trigger_id: str) -> dict[str, Any]:
        """Ask for the trigger to be enabled: it becomes `pending_enable` and a card appears in Studio > Webhooks. Only a
        person can enable it; this tool cannot open the URL."""
        ctx = current_ctx()
        require_unrestricted(ctx, "request enabling a webhook trigger")
        return await call_service(ctx, _service(ctx), lambda s: s.request_enable(ctx.tenant_id, ctx.actor.user_id, uid(trigger_id, "trigger_id")))

    @tool("webhook.trigger.disable", manage=True)
    async def webhook_trigger_disable(trigger_id: str) -> dict[str, Any]:
        """Stop the trigger's URL (it answers 404 from the next request)."""
        ctx = current_ctx()
        require_unrestricted(ctx, "disable webhook triggers")
        return await call_service(ctx, _service(ctx), lambda s: s.disable(ctx.tenant_id, ctx.actor.user_id, uid(trigger_id, "trigger_id")))

    @tool("webhook.trigger.rotate", manage=True)
    async def webhook_trigger_rotate(trigger_id: str, add: str | None = None, retire: str | None = None) -> dict[str, Any]:
        """Rotate without downtime: `add` a second active secret reference (an existing `secret:<name>`), update the
        provider, then `retire` the old reference. Exactly one of add or retire. Never takes or returns a value. Adding a
        secret to an ENABLED trigger opens it to that secret at once, so only a person does that, in Studio."""
        ctx = current_ctx()
        require_unrestricted(ctx, "rotate webhook secrets")
        payload = {k: v for k, v in (("add", add), ("retire", retire)) if v is not None}
        return await call_service(
            ctx, _service(ctx), lambda s: s.rotate(ctx.tenant_id, ctx.actor.user_id, uid(trigger_id, "trigger_id"), payload))

    @tool("webhook.trigger.archive", manage=True)
    async def webhook_trigger_archive(trigger_id: str) -> dict[str, Any]:
        """Retire the trigger for good (its row stays for the log; the URL answers 404)."""
        ctx = current_ctx()
        require_unrestricted(ctx, "archive webhook triggers")
        return await call_service(ctx, _service(ctx), lambda s: s.archive(ctx.tenant_id, ctx.actor.user_id, uid(trigger_id, "trigger_id")))
