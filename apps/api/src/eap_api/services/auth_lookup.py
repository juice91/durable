"""Credential lookups for the entry points that authenticate a bearer (the MCP control endpoint).

Which kind of credential a bearer is, the tenant API key behind it, and the live Architect run behind
a run token. Deciding what a principal may do (scopes, origins, the HTTP refusal) stays with the entry
point; this only answers what the data says. It never imports a web framework.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_api.ports import TERMINAL_RUN_STATUSES
from eap_run_store import looks_like_api_key
from eap_run_store.run_tokens import looks_like_run_token, signing_key, verify_run_token

ARCHITECT_SLUG = "architect"


@dataclass(frozen=True)
class ArchitectRun:
    """A live run of the built-in Architect, named by a valid run token."""

    tenant_id: str
    run_id: str


class AuthLookupService:
    def __init__(self, api_keys: Any, engine: Engine) -> None:
        self._api_keys = api_keys
        self._engine = engine

    @staticmethod
    def is_run_token(credential: str) -> bool:
        return looks_like_run_token(credential)

    @staticmethod
    def is_api_key(credential: str) -> bool:
        return looks_like_api_key(credential)

    def api_key(self, credential: str) -> Any | None:
        """The key record of an API key credential (its last use is not touched), or None when it is unknown or revoked."""
        return self._api_keys.authenticate(credential, touch=False)

    def touch_api_key(self, key_id: str) -> None:
        self._api_keys.touch_last_used(key_id)

    def architect_run(self, credential: str) -> ArchitectRun | None:
        """The live Architect run a run token names: a signed, unexpired token AND a run row that still exists in
        the tenant, belongs to the built-in `architect` agent and has not ended. Anything else is None."""
        secret = os.environ.get("EAP_SECRETS_KEY")
        token = verify_run_token(signing_key(secret), credential) if secret else None
        if token is None:
            return None
        try:
            uuid.UUID(token.run_id)
        except ValueError:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT r.status, a.slug, a.builtin FROM runs r JOIN agents a ON a.id = r.agent_id "
                    "WHERE r.id = :r AND r.tenant_id = :t"
                ),
                {"r": token.run_id, "t": token.tenant_id},
            ).first()
        if row is None or row[0] in TERMINAL_RUN_STATUSES or not row[2] or row[1] != ARCHITECT_SLUG:
            return None
        return ArchitectRun(tenant_id=token.tenant_id, run_id=token.run_id)
