"""IAM design 8: an Architect run acts for the person who started it and never with more rights.

The API reads that person from the run (`principal` / `on_behalf_of`, recorded when the turn started), not from the token. A
viewer's turn gets the read tools; an editor's can change things; a run of a legacy turn (no recorded person) can only read."""

from __future__ import annotations

import dataclasses

import pytest

from eap_api.deps import iam_of
from eap_iam_apikey.provider import _requested_by

from .iam_model_engine import ModelServer
from .mcp_control_helpers import secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg
from .test_architect_control_seam import cleanup_agents, seed_architect, token  # noqa: F401 - fixtures

pytestmark = requires_pg


def test_the_person_is_read_from_the_runs_recorded_actor():
    assert _requested_by("user", "user:ann@example.com", None) == "ann@example.com"
    assert _requested_by("user", "user:zitadel:abc", "zitadel:abc") == "zitadel:abc"
    assert _requested_by("api_key", "apikey:k1", "ann@example.com") == "ann@example.com"  # a key acts for its owner
    assert _requested_by(None, None, None) is None  # a legacy run: nobody


def _use_model(world, roles):  # noqa: F811
    server = ModelServer()
    engine = server.policy()
    engine.roles = {k: frozenset(v) for k, v in roles.items()}
    world.app.state.iam = dataclasses.replace(iam_of(world.app), policy=engine)
    return engine


@pytest.mark.parametrize("person,roles,manage_allowed", [("vic", {"vic": {"viewer"}}, False), ("eve", {"eve": {"editor"}}, True)])
def test_a_turn_changes_things_only_when_its_person_may(world, secrets_key, cleanup_agents, person, roles, manage_allowed):  # noqa: F811
    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)
    world.sql("UPDATE runs SET principal = :p, on_behalf_of = :o, started_by_kind = 'user' WHERE id = :r",
              p=f"user:{person}", o=person, r=run_id)
    _use_model(world, roles)
    bearer = token(secrets_key, tenant, run_id)
    assert not world.call(bearer, "scorer.list").is_error  # every member reads
    made = world.call(bearer, "scorer.create", {"name": f"{person}-rule", "evaluator": {"kind": "regex", "pattern": "x"}})
    if manage_allowed:
        assert not made.is_error, made.data
    else:
        assert made.is_error and made.error["code"] == "forbidden", made.data


def test_a_legacy_run_with_no_recorded_person_cannot_change_anything(world, secrets_key, cleanup_agents):  # noqa: F811
    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)  # no actor recorded
    _use_model(world, {})
    bearer = token(secrets_key, tenant, run_id)
    made = world.call(bearer, "scorer.create", {"name": "legacy", "evaluator": {"kind": "regex", "pattern": "x"}})
    assert made.is_error and made.error["code"] in ("forbidden", "not_found"), made.data
