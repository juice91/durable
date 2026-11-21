"""ConversationService with fake stores: refusals, idempotent turns, the clone title and delete."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.services.conversations import (
    ConversationService,
    TurnBusyRefusal,
    canonical_uuid_or_404,
    clone_title,
    default_resource_id,
)
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_run_store import ConversationIdConflict, ConversationNotFound, RunIdConflict, TurnInProgress

TENANT = "t1"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def conv(**over):
    base = {
        "id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4()), "resource_id": None, "title": "T", "turn_count": 0,
        "last_turn_run_id": None, "last_turn_status": None, "last_turn_display_status": None,
        "created_at": NOW, "updated_at": NOW,
    }
    return {**base, **over}


def service(run_store=None, memory=None, db=None):
    return ConversationService(db or MagicMock(), run_store or MagicMock(), MagicMock(), memory)


def test_clone_title_fits_the_limit_and_names_an_untitled_thread():
    assert clone_title(None) == "Untitled conversation (copy)"
    assert clone_title("  Plan ") == "Plan (copy)"
    assert len(clone_title("x" * 500)) == 200 and clone_title("x" * 500).endswith(" (copy)")


def test_default_resource_id_is_none_for_an_id_that_does_not_fit():
    assert default_resource_id("alice") == "alice"
    assert default_resource_id("not valid!") is None


def test_malformed_ids_are_not_found():
    with pytest.raises(NotFound):
        canonical_uuid_or_404("nope", "Conversation not found")


def test_refusals_carry_the_plain_body_the_routes_always_had():
    store = MagicMock()
    store.get_conversation.return_value = None
    with pytest.raises(NotFound) as info:
        service(store).get(TENANT, uuid.uuid4())
    assert info.value.detail_object == "Conversation not found"


def test_create_refuses_another_agents_conversation_and_an_id_in_use():
    store = MagicMock()
    agent_id = uuid.uuid4()
    db = MagicMock()
    db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = SimpleNamespace(
        archived=False
    )
    store.create_conversation.return_value = (conv(), True)
    with pytest.raises(Conflict, match="another agent"):
        service(store, db=db).create(TENANT, "alice", agent_id, title=None, conversation_id=None, resource_id=None)
    store.create_conversation.side_effect = ConversationIdConflict("c")
    with pytest.raises(Conflict, match="already in use"):
        service(store, db=db).create(TENANT, "alice", agent_id, title=None, conversation_id=None, resource_id=None)


def test_create_defaults_the_resource_to_the_user_and_refuses_an_archived_agent():
    store = MagicMock()
    agent_id = uuid.uuid4()
    db = MagicMock()
    query = db.query.return_value.filter.return_value.with_for_update.return_value
    query.first.return_value = SimpleNamespace(archived=False)
    store.create_conversation.return_value = (conv(agent_id=str(agent_id)), True)
    body, created = service(store, db=db).create(
        TENANT, "alice", agent_id, title="x", conversation_id=None, resource_id=None
    )
    assert created and body["agent_id"] == str(agent_id)
    assert store.create_conversation.call_args.kwargs["resource_id"] == "alice"
    query.first.return_value = SimpleNamespace(archived=True)
    with pytest.raises(Conflict):
        service(store, db=db).create(TENANT, "alice", agent_id, title="x", conversation_id=None, resource_id=None)


def test_list_pages_with_an_opaque_cursor_and_refuses_a_foreign_one():
    store = MagicMock()
    store.list_conversations.return_value = [conv()]
    out = service(store).list(TENANT, agent_id=None, q=None, limit=1, before=None)
    assert out["next_before"] is not None and len(out["items"]) == 1
    with pytest.raises(InvalidInput):
        service(store).list(TENANT, agent_id=None, q=None, limit=1, before="garbage")


def test_rename_of_an_unknown_conversation_is_not_found():
    store = MagicMock()
    store.rename_conversation.side_effect = ConversationNotFound(TENANT, "c")
    with pytest.raises(NotFound):
        service(store).rename(TENANT, uuid.uuid4(), "x")


def test_record_and_launch_turn_replays_a_recorded_run_and_refuses_a_different_request(monkeypatch):
    from eap_api.services import conversations as module

    launched = []
    monkeypatch.setattr(module, "launch_recorded_run", lambda *a: launched.append(a) or a[4])
    store = MagicMock()
    recorded = {"id": str(uuid.uuid4()), "conversation_id": "c"}
    store.get_run.return_value = recorded
    svc = service(store)
    run, created = svc.record_and_launch_turn(
        TENANT, "c", run_id=uuid.uuid4(), new_options=lambda: pytest.fail("no options for a recorded turn"),
        input_text="hi", same_request=lambda r: True,
    )
    assert run is recorded and created is False and len(launched) == 1
    with pytest.raises(Conflict, match="different"):
        svc.record_and_launch_turn(
            TENANT, "c", run_id=uuid.uuid4(), new_options=lambda: None, input_text="hi", same_request=lambda r: False,
        )


def test_record_and_launch_turn_maps_store_refusals(monkeypatch):
    options = SimpleNamespace(target=SimpleNamespace(version=1), store_kwargs=lambda: {})
    store = MagicMock()
    store.get_run.return_value = None
    store.create_turn.side_effect = ConversationNotFound(TENANT, "c")
    with pytest.raises(NotFound):
        service(store).record_and_launch_turn(
            TENANT, "c", run_id=uuid.uuid4(), new_options=options, input_text="hi", same_request=lambda r: True
        )
    store.create_turn.side_effect = RunIdConflict(uuid.uuid4())
    with pytest.raises(Conflict, match="already in use"):
        service(store).record_and_launch_turn(
            TENANT, "c", run_id=uuid.uuid4(), new_options=options, input_text="hi", same_request=lambda r: True
        )
    store.create_turn.side_effect = TurnInProgress("c", "r1", "running")
    with pytest.raises(TurnInProgress):  # not a ServiceError: the AG-UI route decides
        service(store).record_and_launch_turn(
            TENANT, "c", run_id=uuid.uuid4(), new_options=options, input_text="hi", same_request=lambda r: True
        )


def test_delete_needs_memory_and_refuses_while_a_turn_runs():
    with pytest.raises(Unavailable):
        service().delete(TENANT, str(uuid.uuid4()))
    memory = MagicMock()
    memory.delete_conversation.side_effect = TurnInProgress("c", "r1", "running")
    with pytest.raises(TurnBusyRefusal, match="still running"):
        service(memory=memory).delete(TENANT, str(uuid.uuid4()))
    memory.delete_conversation.side_effect = None
    memory.delete_conversation.return_value = False
    with pytest.raises(NotFound):
        service(memory=memory).delete(TENANT, str(uuid.uuid4()))
    memory.delete_conversation.return_value = True
    service(memory=memory).delete(TENANT, str(uuid.uuid4()))
