"""The run-list cursor: a position in the (created_at DESC, id DESC) order, as an opaque token.

Runs can share a created_at (one transaction, a clock that does not advance, imported rows), so the
position is the pair (created_at, id), and the next page is `(created_at, id) < cursor` under the
same ordering. The token is URL-safe base64 (no padding) of `{"t": <ISO created_at>, "id": <uuid>}`;
clients pass it back verbatim and must not parse it. A token only names a position; every query is
still tenant-scoped, so a token from one tenant never reveals another tenant's rows.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

MAX_CURSOR_LENGTH = 256


class InvalidRunCursor(ValueError):
    """The token is not a cursor this API issued (malformed, truncated, or the old timestamp form)."""


@dataclass(frozen=True)
class RunCursor:
    created_at: datetime
    id: str

    @classmethod
    def after(cls, run: dict[str, Any]) -> RunCursor:
        """The cursor positioned at `run` (a list_runs row): the next page starts after it."""
        return cls(run["created_at"], str(run["id"]))

    def encode(self) -> str:
        raw = json.dumps({"t": self.created_at.isoformat(), "id": self.id}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> RunCursor:
        if not token or len(token) > MAX_CURSOR_LENGTH:
            raise InvalidRunCursor("cursor is empty or too long")
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            data = json.loads(raw)
        except (binascii.Error, ValueError) as exc:
            raise InvalidRunCursor("cursor is not valid") from exc
        if not isinstance(data, dict) or set(data) != {"t", "id"}:
            raise InvalidRunCursor("cursor is not valid")
        t, run_id = data["t"], data["id"]
        if not isinstance(t, str) or not isinstance(run_id, str):
            raise InvalidRunCursor("cursor is not valid")
        try:
            created_at = datetime.fromisoformat(t)
            canonical_id = str(UUID(run_id))
        except ValueError as exc:
            raise InvalidRunCursor("cursor is not valid") from exc
        if created_at.tzinfo is None:
            raise InvalidRunCursor("cursor is not valid")
        return cls(created_at, canonical_id)


__all__ = ["InvalidRunCursor", "MAX_CURSOR_LENGTH", "RunCursor"]
