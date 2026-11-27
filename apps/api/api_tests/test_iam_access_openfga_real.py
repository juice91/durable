"""The Access tab's API and "why can't I?" over a REAL OpenFGA, for people whose subjects are `zitadel:<sub>` (which the
server refuses unless the plug-in encodes them). The model double of the other IAM tests cannot show that.

Needs Postgres and an OpenFGA: skipped unless `EAP_TEST_OPENFGA_URL` or `EAP_TEST_OPENFGA_DOCKER=1` (the plug-in's own
server fixture, loaded from its test directory)."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from eap_iam import Member, Principal, RequestView
from eap_iam_openfga.bootstrap import run as bootstrap_run
from eap_iam_openfga.client import OpenFgaClient
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.model import model_text
from eap_iam_openfga.settings import OpenFgaSettings
from eap_secrets import KEY_ENV, generate_key

from eap_api.deps import iam_of

from .run_helpers import requires_pg
from .test_agent_grants_api import _audit, _workflow
from .test_agent_grants_api import world as _grants_world  # noqa: F401
from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401

_SERVER = Path(__file__).resolve().parents[3] / "plugins/iam-openfga/iam_openfga_tests/server.py"
_spec = importlib.util.spec_from_file_location("openfga_test_server", _SERVER)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
server = _module.server

pytestmark = requires_pg

IAM = f"{API}/iam"
# The dev bearer's user -> the ZITADEL user id and the roles its token would carry.
PEOPLE = {"ann": ("400000000000000001", "admin"), "eve": ("400000000000000002", "editor"), "vic": ("400000000000000003", "viewer")}


def subject(user: str) -> str:
    return f"zitadel:{PEOPLE[user][0]}"


class Members:
    name, mutable, configured = "zitadel", True, True

    async def list_members(self, tenant_id: str) -> list[Member]:
        return [Member(id=i, email=f"{u}@example.test", roles=(role,)) for u, (i, role) in PEOPLE.items()]

    async def roles_of(self, tenant_id: str, subject: str) -> frozenset[str]:
        return frozenset()


class AsZitadel:
    """The app's identity chain, answering with the principal ZITADEL would: `zitadel:<sub>` and its roles."""

    def __init__(self, chain: Any) -> None:
        self.chain = chain

    async def authenticate(self, view: RequestView) -> Principal:
        principal = await self.chain.authenticate(view)
        role = PEOPLE[principal.subject][1]
        return principal.model_copy(update={"subject": subject(principal.subject), "roles": frozenset({role})})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())


@pytest.fixture
def world(server, _grants_world):  # noqa: F811
    url, key = server
    base = OpenFgaSettings(url=url, store_id="", preshared_key=key, timeout=10.0)
    result = asyncio.run(
        bootstrap_run(base, store_name=f"api-{uuid.uuid4().hex[:8]}", model_text_=model_text(), store_id=None, dry_run=False)
    )
    settings = dataclasses.replace(base, store_id=result.store_id, model_id=result.model_id)
    w = _grants_world
    iam = iam_of(w.app)
    w.app.state.iam = dataclasses.replace(
        iam, chain=AsZitadel(iam.chain), membership=Members(), policy=OpenFgaPolicy(OpenFgaClient(settings))
    )
    yield w


def test_register_list_grant_explain_and_revoke_for_zitadel_people(world: World):
    one, two = _workflow(world), _workflow(world)
    url = f"{IAM}/access/workflow/{one}"
    body = world.client.get(url, headers=world.h("ann"))
    assert body.status_code == 200, body.text
    assert body.json()["grants"] == [{"relation": "owner", "subject_type": "user", "subject": subject("ann")}]
    for user in PEOPLE:
        listed = world.client.get(f"{API}/workflows", headers=world.h(user))
        assert listed.status_code == 200 and {w["id"] for w in listed.json()} >= {one, two}, listed.text

    def explain(user: str, wid: str) -> dict:
        r = world.client.get(f"{IAM}/explain?kind=workflow&id={wid}&action=run", headers=world.h(user))
        assert r.status_code == 200, r.text
        return r.json()["decisions"][0]

    assert explain("vic", one)["allowed"] is False
    grant = {"relation": "runner", "member_id": PEOPLE["vic"][0]}
    made = world.client.post(f"{url}/grants", json=grant, headers=world.h("ann"))
    assert made.status_code == 200 and made.json()["subject"] == subject("vic"), made.text
    assert explain("vic", one)["allowed"] is True and explain("vic", two)["allowed"] is False
    grants = world.client.get(url, headers=world.h("vic")).json()["grants"]
    assert {"relation": "runner", "subject_type": "user", "subject": subject("vic")} in grants
    gone = world.client.delete(f"{url}/grants?relation=runner&subject_type=user&subject={subject('vic')}", headers=world.h("ann"))
    assert gone.status_code == 204, gone.text
    assert explain("vic", one)["allowed"] is False
    assert [r["detail"]["name"] for r in _audit(world, "revoke_access")] == [f"runner user:{subject('vic')}"]
