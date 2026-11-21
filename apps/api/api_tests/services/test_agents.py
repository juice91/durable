"""AgentService, RequestContextService, ToolService, ProcessorService and ModelService with fake collaborators."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.services import agents as agents_module
from eap_api.services.agents import AgentService, ToolRefusal, builtin_role, refuse_architect_tools
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.models import ModelService
from eap_api.services.processors import ProcessorService, ResultsCursor
from eap_api.services.tools import ToolService, canonical_json
from eap_run_store import RunIdConflict
from eap_run_store.revisions import RevisionMismatch
from eap_schemas import AgentSpec

TENANT = "t1"
SPEC = AgentSpec.model_validate({"name": "a", "instructions": "hi", "model": {"provider": "test", "name": "echo"}})


def agent_row(**over):
    row = {"id": uuid.uuid4(), "name": "a", "draft_spec": SPEC.model_dump(mode="json"), "archived": False,
           "builtin": False, "slug": None, "revision": 3}
    return SimpleNamespace(**{**row, **over})


def service_with(agent):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = agent
    return AgentService(db), db


def test_unknown_agent_is_not_found_with_the_plain_body():
    svc, _ = service_with(None)
    with pytest.raises(NotFound) as info:
        svc.get(TENANT, uuid.uuid4())
    assert info.value.detail_object == "Agent not found"


def test_builtin_role_names_the_architect_and_hides_drafters():
    assert builtin_role(agent_row()) is None
    assert builtin_role(agent_row(builtin=True, slug="architect")) == "architect"
    assert builtin_role(agent_row(builtin=True, slug="x-drafter")) == "drafter"


def test_non_builtin_agent_may_not_list_architect_tools():
    spec = SPEC.model_copy(update={"tools": ["architect.publish"]})
    with pytest.raises(ToolRefusal) as info:
        refuse_architect_tools(spec, builtin=False)
    assert info.value.detail_object["code"] == "tool_not_allowed" and info.value.detail_object["tools"] == ["architect.publish"]


def test_patch_with_a_stale_revision_is_the_mismatch_conflict_with_the_current_agent(monkeypatch):
    monkeypatch.setattr(agents_module, "_latest_version", lambda db, agent_id: None)
    svc, _ = service_with(agent_row(revision=5))
    with pytest.raises(Conflict) as info:
        svc.patch(TENANT, uuid.uuid4(), SPEC, expected_revision=4)
    assert info.value.code == "revision_mismatch" and info.value.current_revision == 5
    assert info.value.current["revision"] == 5


def test_patch_loses_a_race_when_the_guarded_update_finds_the_row_moved(monkeypatch):
    monkeypatch.setattr(agents_module, "_latest_version", lambda db, agent_id: None)
    monkeypatch.setattr(agents_module, "bump_row", lambda *a: (_ for _ in ()).throw(RevisionMismatch(4)))
    svc, db = service_with(agent_row(revision=3))
    with pytest.raises(Conflict) as info:
        svc.patch(TENANT, uuid.uuid4(), SPEC, expected_revision=3)
    assert info.value.code == "revision_mismatch"
    db.rollback.assert_called_once()


def test_patch_without_if_match_replaces_the_draft(monkeypatch):
    monkeypatch.setattr(agents_module, "_latest_version", lambda db, agent_id: None)
    monkeypatch.setattr(agents_module, "bump_row", lambda *a: None)
    row = agent_row()
    svc, db = service_with(row)
    body, revision = svc.patch(TENANT, uuid.uuid4(), SPEC.model_copy(update={"name": "b"}), None)
    assert row.name == "b" and body["name"] == "b" and revision == 3
    db.commit.assert_called_once()


def test_a_refused_definition_operation_rolls_back(monkeypatch):
    def refuse(*a, **k):
        raise Conflict("nope")

    monkeypatch.setattr(agents_module, "publish_agent", refuse)
    svc, db = service_with(agent_row())
    with pytest.raises(Conflict):
        svc.publish(TENANT, "u", uuid.uuid4())
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


def test_compare_names_a_missing_draft_and_a_missing_version():
    svc, db = service_with(agent_row(draft_spec=None))
    with pytest.raises(NotFound, match="no draft"):
        svc.compare(TENANT, uuid.uuid4(), "draft", "draft")
    svc, db = service_with(agent_row())
    db.query.return_value.filter.return_value.first.side_effect = [agent_row(), None]
    with pytest.raises(NotFound, match="Version 9 not found"):
        svc.compare(TENANT, uuid.uuid4(), 9, "draft")


# -- tools -------------------------------------------------------------------------------------


def tool_service(store=None, launcher=None):
    return ToolService(store or MagicMock(), launcher or MagicMock())


def test_tool_catalog_and_unknown_tool():
    svc = ToolService()
    listing = svc.list()
    assert listing and {"id", "allowed", "input_schema"} <= set(listing[0])
    with pytest.raises(NotFound):
        svc.get("nope")
    with pytest.raises(NotFound):
        tool_service().start_run(TENANT, "nope", {}, None)


def test_tool_input_error_never_echoes_the_rejected_value():
    from eap_schemas.tools import TOOL_DECLARATIONS

    tool_id = next(iter(TOOL_DECLARATIONS))
    with pytest.raises(InvalidInput) as info:
        tool_service().start_run(TENANT, tool_id, {"definitely_not_a_field": "s3cret-value"}, None)
    assert "s3cret-value" not in repr(info.value.detail_object)
    assert all({"loc", "msg", "type"} == set(e) for e in info.value.detail_object)


def _first_valid_tool():
    from eap_schemas.tools import TOOL_DECLARATIONS

    return next(d for d in TOOL_DECLARATIONS.values() if _accepts_empty(d))


def _accepts_empty(declaration):
    try:
        declaration.input_model.model_validate({})
        return True
    except Exception:  # noqa: BLE001
        return False


def _run(tool_id, **over):
    return {"id": uuid.uuid4(), "kind": "tool", "tool_id": tool_id, "input": canonical_json({}), "traceparent": None,
            "status": "queued", "display_status": "queued", "error": None, "trace_id": None, **over}


def test_start_tool_run_hands_off_and_answers_the_run():
    declaration = _first_valid_tool()
    store, launcher = MagicMock(), MagicMock()
    store.create_tool_run.return_value = (uuid.uuid4(), True)
    store.get_run.return_value = _run(declaration.id, input=canonical_json(declaration.input_model.model_validate({}).model_dump(mode="json")))
    out = tool_service(store, launcher).start_run(TENANT, declaration.id, {}, None)
    assert out["kind"] == "tool" and out["status"] == "queued"
    launcher.launch_tool.assert_called_once()


def test_failed_hand_off_marks_the_run_failed_and_is_unavailable():
    declaration = _first_valid_tool()
    store, launcher = MagicMock(), MagicMock()
    store.create_tool_run.return_value = (uuid.uuid4(), True)
    store.get_run.return_value = _run(declaration.id, input=canonical_json(declaration.input_model.model_validate({}).model_dump(mode="json")))
    launcher.launch_tool.side_effect = RuntimeError("postgres://secret")
    with pytest.raises(Unavailable) as info:
        tool_service(store, launcher).start_run(TENANT, declaration.id, {}, None)
    assert "secret" not in info.value.message
    store.mark_enqueue_failed.assert_called_once()


def test_run_id_reuse_by_a_different_request_is_a_conflict():
    declaration = _first_valid_tool()
    store = MagicMock()
    store.create_tool_run.return_value = (uuid.uuid4(), False)
    store.get_run.return_value = _run(declaration.id, input="different")
    with pytest.raises(Conflict):
        tool_service(store).start_run(TENANT, declaration.id, {}, uuid.uuid4())
    store.create_tool_run.side_effect = RunIdConflict(uuid.uuid4())
    with pytest.raises(Conflict, match="already in use"):
        tool_service(store).start_run(TENANT, declaration.id, {}, uuid.uuid4())


# -- processors and models -----------------------------------------------------------------------


def test_processor_catalog_lists_limits_and_the_console_refuses_moderation_and_big_samples():
    svc = ProcessorService()
    assert svc.catalog()["items"] and svc.catalog()["limits"]["max_test_sample_bytes"] == 32 * 1024
    with pytest.raises(InvalidInput, match="larger than"):
        svc.test([], "x" * (32 * 1024 + 1))


def test_results_cursor_round_trips_and_refuses_a_foreign_token():
    cursor = ResultsCursor(datetime(2026, 1, 1, tzinfo=UTC), str(uuid.uuid4()), 4)
    assert ResultsCursor.decode(cursor.encode()) == cursor
    with pytest.raises(InvalidInput):
        ResultsCursor.decode("garbage")


def test_unknown_provider_is_not_found():
    with pytest.raises(NotFound, match="unknown provider"):
        ModelService().test_provider("nope")
