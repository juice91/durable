"""Viewers and the Architect over a REAL OpenFGA, for people whose subjects are `zitadel:<sub>`: who may converse, and
the `write` decision on the roles a run recorded (what the worker asks to decide whether the run is read-only).

Needs Postgres and an OpenFGA: skipped unless `EAP_TEST_OPENFGA_URL` or `EAP_TEST_OPENFGA_DOCKER=1` (the fixtures of
test_iam_access_openfga_real)."""

from __future__ import annotations

import asyncio
import uuid

from eap_iam import Principal, ResourceRef
from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.engine import OpenFgaPolicy

from eap_api.deps import iam_of

from .run_helpers import requires_pg
from .test_agent_grants_api import world as _grants_world  # noqa: F401
from .test_architect_viewer import actor, agui, converse, finish, make_agent, turn
from .test_iam_access_openfga_real import _env, server, subject, world  # noqa: F401 - fixtures
from .test_iam_p2b_wave1 import World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401

pytestmark = requires_pg


def _may_write(w: World, run_id: str) -> bool:
    who = actor(w, run_id)
    principal = Principal(tenant_id=w.tenant, kind="user", subject=who.on_behalf_of, provider="run", roles=frozenset(who.roles))
    # A client of its own, as the worker has: the app's pooled connections belong to the app's event loop.
    policy = OpenFgaPolicy(OpenFgaClient(iam_of(w.app).policy.client.settings))
    return asyncio.run(policy.check(principal, "write", ResourceRef("tenant", None, w.tenant))).allowed


def test_a_viewer_converses_with_the_architect_only(world: World):  # noqa: F811
    architect = make_agent(world, "Architect", slug="architect")
    drafter = make_agent(world, "Drafter", slug="architect-drafter")
    plain = make_agent(world, "Plain")

    conversation = converse(world, "vic", architect)
    assert conversation.status_code == 201, conversation.text
    first = turn(world, "vic", conversation.json()["id"])
    assert first.status_code == 201, first.text
    finish(world, first.json()["run_id"])
    assert turn(world, "vic", conversation.json()["id"]).status_code == 201
    assert agui(world, "vic", architect, uuid.uuid4().hex) == 200
    who = actor(world, first.json()["run_id"])
    assert (who.on_behalf_of, who.roles) == (subject("vic"), ("viewer",))
    assert _may_write(world, first.json()["run_id"]) is False

    for agent_id in (drafter, plain):
        assert converse(world, "vic", agent_id).status_code == 403
        assert agui(world, "vic", agent_id, uuid.uuid4().hex) == 403

    for user in ("eve", "ann"):
        made = turn(world, user, converse(world, user, plain).json()["id"])
        assert made.status_code == 201, made.text
        own = turn(world, user, converse(world, user, architect).json()["id"])
        assert own.status_code == 201 and _may_write(world, own.json()["run_id"]) is True
