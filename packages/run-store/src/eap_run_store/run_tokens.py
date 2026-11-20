"""Short-lived bearer tokens that let a built-in agent's run call the API's MCP control endpoint.

The Architect's `control__*` tools run in the worker but their handlers live in the API process (the
MCP control plane). The worker calls that endpoint over HTTP; this is the credential it presents. A token
names one tenant and one run, expires after `TTL_SECONDS`, and is signed (HMAC-SHA256) with a key derived
from the platform secrets key (`EAP_SECRETS_KEY`, which the API and the worker already share), under its
own domain label so the derived key is good for nothing else. The worker mints one per call; nothing is
stored, nothing long-lived is on disk, and the API does not trust the token alone: it also checks that
the run still exists, belongs to the tenant and is a live run of the built-in Architect
(`eap_api.mcp_control.auth`).

Format: `eaprun.<base64url(json {t, r, e})>.<hex signature>`. Pure standard library; never logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

PREFIX = "eaprun."
TTL_SECONDS = 120
_DOMAIN = b"pydanticos/run-token/v1"


@dataclass(frozen=True)
class RunToken:
    tenant_id: str
    run_id: str
    expires_at: int


def signing_key(secrets_key: str) -> bytes:
    """The HMAC key for run tokens, derived from the platform secrets key (never the key itself)."""
    return hmac.new(secrets_key.encode("utf-8"), _DOMAIN, hashlib.sha256).digest()


def looks_like_run_token(credential: str) -> bool:
    return credential.startswith(PREFIX)


def _sign(key: bytes, body: str) -> str:
    return hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()


def mint_run_token(key: bytes, tenant_id: str, run_id: str, *, ttl_seconds: int = TTL_SECONDS, now: float | None = None) -> str:
    payload = {"t": tenant_id, "r": run_id, "e": int((time.time() if now is None else now) + ttl_seconds)}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{PREFIX}{body}.{_sign(key, body)}"


def verify_run_token(key: bytes, credential: str, *, now: float | None = None) -> RunToken | None:
    """The token's claims when the signature is valid and it has not expired, else None."""
    if not looks_like_run_token(credential):
        return None
    body, _, signature = credential[len(PREFIX):].partition(".")
    if not body or not signature or not hmac.compare_digest(_sign(key, body), signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        tenant_id, run_id, expires = str(payload["t"]), str(payload["r"]), int(payload["e"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if (time.time() if now is None else now) >= expires:
        return None
    return RunToken(tenant_id=tenant_id, run_id=run_id, expires_at=expires)


__all__ = ["PREFIX", "TTL_SECONDS", "RunToken", "looks_like_run_token", "mint_run_token", "signing_key", "verify_run_token"]
