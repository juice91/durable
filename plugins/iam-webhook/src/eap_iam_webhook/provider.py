"""The `webhook` identity provider.

Order of checks (design 4.2, steps 2, 5, 6 and 6b; the route did the per-IP limit, the per-trigger limit and the capped
body read before asking the chain):

1. the trigger: the feature flag, the id, the row, `enabled`, and `approved_config_hash == config_hash` (a human approved
   exactly this grant). Anything else is `not found`.
2. the secrets: `secret:<name>` references resolved in the trigger's tenant (a missing one is a `None` slot).
3. the signature, over the raw bytes, every secret and every candidate compared (`eap_inbound.verify`).
4. the timestamp window, after the signature: a stale delivery with a valid signature is recorded (it proves the sender)
   and refused with the same 401.

Unknown and disabled triggers run the same HMAC computation over a fixed dummy secret, so their answer costs the same.
Nothing here logs or returns a secret, a signature, a header value or the body."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

from eap_inbound import PRESETS, REASON_STALE, CustomConfig, Verified, body_sha256, dedupe_key, verify
from eap_iam import ACTION_INGEST_WEBHOOK, AuthError, Principal, RequestView
from eap_run_store import WebhookStore
from eap_run_store.secrets import SecretStore
from eap_schemas.secrets import is_secret_ref
from eap_secrets import SecretsError, load_keyring

log = logging.getLogger("eap_iam_webhook")

PROVIDER = "webhook-hmac"
NOT_FOUND_DETAIL = "not found"
INVALID_SIGNATURE_DETAIL = "invalid signature"
FLAG = "EAP_WEBHOOKS_ENABLED"
_DUMMY_SECRET = "eap-webhook-dummy-secret"
_TRUE = frozenset({"1", "true", "yes", "on"})


def webhooks_enabled(env: Mapping[str, str]) -> bool:
    return env.get(FLAG, "").strip().lower() in _TRUE


class WebhookSignatureProvider:
    name = "webhook"
    kinds = frozenset({"webhook"})

    def __init__(self, store: WebhookStore, engine: Any, env: Mapping[str, str]) -> None:
        self._store = store
        self._engine = engine
        self._env = env

    async def authenticate(self, request: RequestView) -> Principal | None:
        if request.action != ACTION_INGEST_WEBHOOK:
            return None
        trigger_id = request.route_params.get("trigger_id")
        if trigger_id is None or request.body is None:
            return None
        return await asyncio.to_thread(self._authenticate, request, trigger_id, request.body)

    # -- everything below is synchronous (database and AES-GCM) and runs in a worker thread

    def _dummy(self, body: bytes) -> None:
        """The same HMAC work as a real trigger, so an unknown or disabled one costs the same as a bad signature."""
        verify("github", None, {"x-hub-signature-256": "sha256=" + "0" * 64}, body, [_DUMMY_SECRET], time.time())
        hmac.compare_digest(b"dummy", b"dummy")

    def _row(self, trigger_id: str) -> dict[str, Any] | None:
        try:
            uuid.UUID(trigger_id)
        except ValueError:
            return None
        row = self._store.get_trigger_by_id(trigger_id)
        if row is None or row["status"] != "enabled" or row["approved_config_hash"] != row["config_hash"]:
            return None
        return row

    def _secret_values(self, row: dict[str, Any]) -> list[str | None]:
        try:
            store = SecretStore(self._engine, load_keyring(self._env))
        except SecretsError:
            return [None for _ in row["secrets"]]
        values: list[str | None] = []
        for entry in row["secrets"]:
            name = is_secret_ref(entry.get("ref"))
            try:
                values.append(store.resolve(row["tenant_id"], name) if name else None)
            except Exception:  # noqa: BLE001 - a value that does not decrypt is a missing secret; the message is never kept
                values.append(None)
        return values

    def _authenticate(self, request: RequestView, trigger_id: str, body: bytes) -> Principal:
        if not webhooks_enabled(self._env):
            self._dummy(body)
            raise AuthError(NOT_FOUND_DETAIL, 404, www_authenticate=False, reason="feature_off")
        row = self._row(trigger_id)
        if row is None:
            self._dummy(body)
            raise AuthError(NOT_FOUND_DETAIL, 404, www_authenticate=False, reason="not_enabled")
        secrets = self._secret_values(row)
        preset = row["preset"]
        if preset not in PRESETS:  # a hand-edited row
            raise AuthError(INVALID_SIGNATURE_DETAIL, 401, www_authenticate=False, reason="preset_invalid")
        config = CustomConfig.model_validate(row["preset_config"]) if preset == "custom" else None
        result: Verified = verify(preset, config, request.headers, body, secrets, time.time(), row["tolerance_seconds"])
        if not result.signature_ok:
            raise AuthError(INVALID_SIGNATURE_DETAIL, 401, www_authenticate=False, reason=result.reason)
        if result.reason == REASON_STALE:
            # A valid signature proves the sender: the replay (or very late retry) is worth a row.
            self._store.record_delivery(
                row["tenant_id"], row["id"], "stale", REASON_STALE, body_sha256=body_sha256(body), body_bytes=len(body),
                timestamp_skew_seconds=result.skew_seconds, secret_slot=result.secret_slot,
            )
            raise AuthError(INVALID_SIGNATURE_DETAIL, 401, www_authenticate=False, reason=REASON_STALE)
        if result.secret_slot is not None:
            self._store.touch_secret_matched(row["tenant_id"], row["id"], result.secret_slot)
        key, kind = dedupe_key(preset, result, body)
        return Principal(
            tenant_id=row["tenant_id"],
            kind="webhook",
            subject=row["id"],
            provider=PROVIDER,
            # No `on_behalf_of`: a webhook run is the trigger's, never the person's who enabled it.
            extra={"verified": result, "dedupe_key": key, "key_kind": kind, "config_hash": row["config_hash"]},
        )
