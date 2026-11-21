"""The refusal of a write whose `If-Match` revision is stale (eap_api.concurrency's 412, as a service's refusal)."""

from __future__ import annotations

from typing import Any

from eap_api.services.errors import Conflict

MISMATCH_CODE = "revision_mismatch"


def stale_revision(expected: int | None, current: dict[str, Any], revision: int) -> Conflict:
    """412 `revision_mismatch` carrying the resource as a read returns it and its current revision."""
    return Conflict(
        f"this resource changed since you read it (revision {revision}, you sent {expected}); "
        "re-read it or send the current ETag to overwrite",
        code=MISMATCH_CODE,
        current=current,
        current_revision=revision,
    )
