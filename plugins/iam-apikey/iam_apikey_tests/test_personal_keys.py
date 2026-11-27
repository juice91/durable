"""The apikey provider with a personal key: owner as the actor, scopes capped by the owner's current roles (cached
briefly), a removed member rejected, a failing membership source failing closed. A fake store and source: no database."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from eap_iam import AuthError, RequestView

from eap_iam_apikey.provider import OWNER_ROLES_TTL_SECONDS, ApiKeyIdentityProvider

KEY = "pos_" + "a" * 24 + "_" + "b" * 43


def record(owner="zitadel:u1", scopes=("mcp", "manage")):
    return SimpleNamespace(
        id="a" * 24, tenant_id="t1", scopes=scopes, agent_ids=None, workflow_ids=None, allowed_origins=None,
        created_by=owner or "admin", owner_subject=owner, owner_email="u1@example.test" if owner else None,
        prefix="pos_" + "a" * 24 + "_",
    )


class Store:
    def __init__(self, rec):
        self.rec = rec

    def authenticate(self, token, touch=True):
        return self.rec

    def touch_last_used(self, key_id):
        return None


class Source:
    configured = True

    def __init__(self, roles):
        self.roles, self.calls, self.fail = roles, 0, False

    async def roles_of(self, tenant_id, subject):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return frozenset(self.roles)


def auth(provider, action=None):
    view = RequestView(headers={"authorization": f"Bearer {KEY}"}, accepts=frozenset({"api_key"}), action=action)
    return asyncio.run(provider.authenticate(view))


def provider(rec, source, clock=lambda: 0.0):
    return ApiKeyIdentityProvider(Store(rec), None, {}, source, clock=clock)


def test_owner_is_the_actor_and_roles_cap_the_scopes():
    p = auth(provider(record(), Source(["viewer"])))
    assert p.kind == "api_key" and p.subject == "a" * 24 and p.on_behalf_of == "zitadel:u1"
    assert p.scopes == {"mcp"} and p.roles == {"viewer"}
    assert p.extra["owner"] == "zitadel:u1" and p.extra["via_api_key"] == "pos_" + "a" * 24 + "_"


def test_a_scope_the_role_lost_is_refused_403():
    with pytest.raises(AuthError) as exc:
        auth(provider(record(scopes=("manage",)), Source(["viewer"])), action="mcp.manage")
    assert exc.value.status == 403


def test_a_removed_member_is_rejected_and_the_cache_bounds_the_staleness():
    now = [0.0]
    source = Source(["editor"])
    p = provider(record(), source, clock=lambda: now[0])
    assert auth(p).scopes == {"mcp", "manage"}
    source.roles = []
    assert auth(p).scopes == {"mcp", "manage"} and source.calls == 1  # still inside the window
    now[0] = OWNER_ROLES_TTL_SECONDS + 1
    with pytest.raises(AuthError) as exc:
        auth(p)
    assert exc.value.status == 401 and KEY not in exc.value.detail


def test_a_failing_membership_source_fails_closed():
    source = Source(["admin"])
    source.fail = True
    with pytest.raises(AuthError) as exc:
        auth(provider(record(), source))
    assert exc.value.status == 503 and KEY not in exc.value.detail


def test_without_a_membership_source_the_stored_scopes_stand_and_a_tenant_key_is_unchanged():
    assert auth(provider(record(), None)).scopes == {"mcp", "manage"}
    tenant = auth(provider(record(owner=None), Source([])))
    assert tenant.on_behalf_of == "admin" and "owner" not in tenant.extra and tenant.scopes == {"mcp", "manage"}


# --- the Architect's run token carries the CURRENT roles of the person it acts for (a policy engine reads them) ---------


def run_principal(provider, requested_by="zitadel:u1"):
    provider._architect_run = lambda credential: ("t1", "00000000-0000-0000-0000-000000000001", requested_by)
    return asyncio.run(provider._run_token("run-token"))


def test_a_run_token_carries_the_persons_current_roles_cached_briefly():
    now = [0.0]
    source = Source(["editor"])
    p = provider(record(), source, clock=lambda: now[0])
    run = run_principal(p)
    assert run.kind == "run" and run.on_behalf_of == "zitadel:u1" and run.roles == {"editor"}
    assert run.scopes == {"mcp", "manage"}  # the policy engine narrows it, as before
    source.roles = ["viewer"]
    assert run_principal(p).roles == {"editor"} and source.calls == 1
    now[0] = OWNER_ROLES_TTL_SECONDS + 1
    assert run_principal(p).roles == {"viewer"}


def test_a_run_token_has_no_roles_without_a_person_a_source_or_an_answer():
    assert run_principal(provider(record(), Source(["admin"])), requested_by=None).roles == frozenset()
    assert run_principal(provider(record(), None)).roles == frozenset()
    source = Source(["admin"])
    source.fail = True
    # Not a 503: with the transitional engine the Architect keeps working; with openfga no role means no right.
    assert run_principal(provider(record(), source)).roles == frozenset()
