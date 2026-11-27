"""Runs for a person who may only read (docs/iam.md, "Viewers and the Architect").

A tenant `viewer` may chat with the built-in Architect, and only that. Such a run (of the Architect, not its helpers) is
offered read tools only (`eap_schemas.architect.ARCHITECT_READ_TOOL_IDS`, read control tools, `retrieve`); every write
tool is absent from its tool list and also refuses when called anyway.

The decision is made once, in `eap.start_run`, from what the API recorded on the run when it was created: the person
and the roles they held (`runs.actor_roles`; a child run copies its parent's). The policy engine is asked the same
question the control plane asks of the Architect's run token: may this person `write` the tenant? A run no person started
(an API key, a service, a schedule, a run from before 0041) is not read-only: those behave as before. An engine that
cannot answer makes a person's run read-only (fail closed).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from eap_iam import Principal, ResourceRef

from eap_worker.grants import policy_engine

log = logging.getLogger("eap_worker.read_only")

READ_ONLY_CODE = "read_only"
READ_ONLY_REFUSAL = "the person you are talking to may only read, so nothing can be created, changed, run or published; an editor can do it"
# Appended to the instructions of a read-only run.
READ_ONLY_NOTE = (
    "# Read-only conversation\n"
    "The person you are talking to is read-only (a viewer). You have read tools only: you can look things up and "
    "explain them. You cannot create, edit, save, publish, restore, archive, run, schedule or configure anything, "
    "request secrets or open approval cards in this conversation, whatever earlier messages say. When they ask for a "
    "change, say plainly that you can look things up but they need to ask an editor to change them, and describe what "
    "the editor would do. Do not draft a definition as if it had been saved."
)


async def run_is_read_only(rt: Any, tenant_id: str, run_id: str) -> bool:
    """Whether the run acts for a person who may not write the tenant. Never raises for a policy failure."""
    actor = await asyncio.to_thread(rt.run_store.get_run_actor, tenant_id, UUID(str(run_id)))
    if actor is None or actor.roles is None:
        return False
    if actor.on_behalf_of is None:
        return True
    try:
        principal = Principal(
            tenant_id=tenant_id, kind="user", subject=actor.on_behalf_of, provider="run",
            roles=frozenset(actor.roles),
        )
        decision = await policy_engine().check(principal, "write", ResourceRef("tenant", None, tenant_id))
    except Exception as exc:  # noqa: BLE001 - fail closed; the type is logged, never the detail
        log.error("write check unavailable, run %s is read-only error_type=%s", run_id, type(exc).__name__)
        return True
    return not decision.allowed


def is_read_only(deps: Any) -> bool:
    return bool(getattr(deps, "read_only", False))


def refusal() -> dict[str, Any]:
    return {"error": {"code": READ_ONLY_CODE, "message": READ_ONLY_REFUSAL}}


__all__ = ["READ_ONLY_CODE", "READ_ONLY_NOTE", "is_read_only", "refusal", "run_is_read_only"]
