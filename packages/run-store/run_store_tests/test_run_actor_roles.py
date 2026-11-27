"""A run records who started it, with the roles they held (`runs.actor_roles`, 0041): conversation turns too, and a
child run acts with exactly its parent's roles."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import RunActor, RunStore, bind_actor


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    bind_actor(None)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def store(engine) -> RunStore:
    return RunStore(engine)


def _turn(store: RunStore, tenant: str, conversation_id: str):
    run_id = uuid4()
    store.create_turn(tenant, conversation_id, 1, "hi", run_id)
    return run_id


def test_a_conversation_turn_records_the_requests_actor_and_roles(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    bind_actor(RunActor("user", "vic", on_behalf_of="vic", roles=("viewer",)))
    run_id = _turn(store, tenant, conv["id"])
    actor = store.get_run_actor(tenant, run_id)
    assert actor == RunActor("user", "vic", on_behalf_of="vic", roles=("viewer",))
    assert store.conversation_starters(tenant, conv["id"]) == {"user:vic"}


def test_an_actor_without_roles_and_no_actor_store_null_which_is_not_an_empty_role_list(store, tenant):
    key_run, nobody_run, roleless_run = uuid4(), uuid4(), uuid4()
    store.create_run(tenant, uuid4(), 1, "hi", key_run, actor=RunActor("api_key", "k1"))
    assert store.get_run_actor(tenant, key_run).roles is None
    store.create_run(tenant, uuid4(), 1, "hi", nobody_run)
    assert store.get_run_actor(tenant, nobody_run) is None
    store.create_run(tenant, uuid4(), 1, "hi", roleless_run, actor=RunActor("user", "dev", on_behalf_of="dev", roles=()))
    assert store.get_run_actor(tenant, roleless_run).roles == ()


def test_a_turn_with_no_actor_has_no_starter(store, tenant):
    conv, _ = store.create_conversation(tenant, uuid4())
    _turn(store, tenant, conv["id"])
    assert store.conversation_starters(tenant, conv["id"]) == {None}
    assert store.conversation_starters(tenant, uuid4()) == set()


def test_a_child_run_inherits_its_parents_roles(store, tenant):
    parent, child = uuid4(), uuid4()
    store.create_run(tenant, uuid4(), 1, "hi", parent, actor=RunActor("user", "vic", on_behalf_of="vic", roles=("viewer",)))
    store.create_child_agent_run(tenant, uuid4(), 1, "x", child, parent, "step")
    actor = store.get_run_actor(tenant, child)
    assert (actor.kind, actor.on_behalf_of, actor.roles) == ("run", "vic", ("viewer",))
