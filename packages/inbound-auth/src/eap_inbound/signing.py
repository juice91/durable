"""Sign a body the way a sender does (tests, docs, sample senders). Never used to verify."""

from __future__ import annotations

import base64
import hashlib
import hmac

from eap_inbound.presets import CustomConfig, _decode_key, standard_webhooks_key


def github_headers(secret: str, body: bytes) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={digest}"}


def standard_webhooks_headers(secret: str, body: bytes, *, msg_id: str, timestamp: int) -> dict[str, str]:
    key = standard_webhooks_key(secret)
    assert key is not None, "not a whsec_<base64> secret"
    signed = f"{msg_id}.{timestamp}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": str(timestamp), "webhook-signature": f"v1,{sig}"}


def custom_headers(
    config: CustomConfig, secret: str, body: bytes, *, timestamp: int | None = None, msg_id: str | None = None
) -> dict[str, str]:
    ts_text = ""
    headers: dict[str, str] = {}
    if config.timestamp is not None:
        assert timestamp is not None
        ts_text = str(timestamp * 1000 if config.timestamp.unit == "milliseconds" else timestamp)
        headers[config.timestamp.header] = ts_text
    if config.id is not None and msg_id is not None:
        headers[config.id.header] = msg_id
    message = (
        config.signed_payload.encode().replace(b"{id}", (msg_id or "").encode()).replace(b"{timestamp}", ts_text.encode())
        .replace(b"{body}", body)
    )
    key = _decode_key(secret, config.key_encoding)
    assert key is not None
    digest = hmac.new(key, message, getattr(hashlib, config.algorithm)).digest()
    encoded = digest.hex() if config.encoding == "hex" else base64.b64encode(digest).decode()
    headers[config.header] = f"{config.prefix}{encoded}"
    return headers
