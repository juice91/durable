"""AgUiService, AgUiWorkflowService and the thread history builder with fake stores."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.ag_ui.thread import build_thread_view
from eap_api.services.ag_ui import AgUiService, ThreadData, TurnBusy, TurnRun, compact_error
from eap_api.services.ag_ui_workflows import AgUiWorkflowService, history_of, input_errors
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_run_store import AgUiScopeMismatch, ConversationIdConflict, ConversationResourceConflict

TENANT = "t1"
KEY = {"scope_kind": "agent", "scope_id": "a1", "external_thread_id": "th", "external_run_id": "r1"}


def make(cls=AgUiService, **over):
    parts = dict(engine=MagicMock(), run_store=MagicMock(), event_log=MagicMock(), ag_ui=MagicMock(),
                 approvals=MagicMock(), api_keys=MagicMock(), attachments=MagicMock(), feedback=MagicMock(),
                 launcher=MagicMock())
    parts.update(over)
    return cls(*parts.values())


def test_compact_error_is_the_one_item_detail_of_an_input_error():
    assert compact_error("x", ["a", 1], "m" * 300, extra=1) == [
        {"loc": ["body", "a", 1], "msg": "m" * 200, "type": "x", "extra": 1}
    ]


def test_refusals_answer_the_plain_body_without_a_code():
    svc = make()
    svc.run_store.create_conversation.side_effect = ConversationIdConflict("x")
    with pytest.raises(Conflict) as err:
        svc.ensure_conversation(TENANT, "a1", "c1", "th", None)
    assert err.value.detail_object == "threadId is already in use"
    svc.run_store.create_conversation.side_effect = ConversationResourceConflict("x")
    with pytest.raises(Conflict) as err:
        svc.ensure_conversation(TENANT, "a1", "c1", "th", None)
    assert err.value.detail_object[0]["type"] == "resource_mismatch"


def test_key_is_active_reads_the_key_row():
    svc = make()
    svc.api_keys.get.return_value = SimpleNamespace(status=lambda: "active")
    assert svc.key_is_active(TENANT, "k") is True
    svc.api_keys.get.return_value = SimpleNamespace(status=lambda: "revoked")
    assert svc.key_is_active(TENANT, "k") is False
    svc.api_keys.get.return_value = None
    assert svc.key_is_active(TENANT, "k") is False


def test_record_resume_run_refuses_another_agents_thread_and_a_different_input():
    svc = make()
    svc.ag_ui.record_ag_ui_run.side_effect = AgUiScopeMismatch(TENANT, "r", "agent", "a1")
    with pytest.raises(Conflict) as err:
        svc.record_resume_run(TENANT, KEY, turn_id="t", fingerprint="f", from_seq=3)
    assert err.value.detail_object == "threadId belongs to another agent"
    svc.ag_ui.record_ag_ui_run.side_effect = None
    svc.ag_ui.record_ag_ui_run.return_value = (SimpleNamespace(fingerprint="other"), False)
    with pytest.raises(Conflict):
        svc.record_resume_run(TENANT, KEY, turn_id="t", fingerprint="f", from_seq=3)
    svc.ag_ui.record_ag_ui_run.return_value = (SimpleNamespace(fingerprint="f"), True)
    assert svc.record_resume_run(TENANT, KEY, turn_id="t", fingerprint="f", from_seq=3)[1] is True


def test_adopt_turn_gives_none_when_the_row_cannot_be_written():
    svc = make()
    svc.ag_ui.record_ag_ui_run.side_effect = AgUiScopeMismatch(TENANT, "r", "agent", "a1")
    assert svc.adopt_turn(TENANT, KEY, run_id="t", fingerprint="f", input_message_ids=()) is None


def test_attachment_for_download_is_404_for_an_unknown_attachment_and_scopes_a_key():
    svc = make()
    svc.attachments.get_for_key.return_value = None
    with pytest.raises(NotFound):
        svc.attachment_for_download(TENANT, "x", created_by="key:k", via_api_key=True, agent_ids=["a1"])
    svc.attachments.get_for_key.assert_called_once_with(TENANT, "x", "key:k", ("a1",))
    svc.attachments.get.return_value = "meta"
    assert svc.attachment_for_download(TENANT, "x", created_by="u", via_api_key=False, agent_ids=None) == "meta"


def test_link_attachments_reports_a_foreign_upload_as_not_found():
    svc = make()
    svc.attachments.link.return_value = []
    with pytest.raises(InvalidInput) as err:
        svc.link_attachments(TENANT, "u", [("m1", ["att1"])], "run")
    assert err.value.detail_object[0]["type"] == "attachment_not_found"


def test_check_attachments_needs_nothing_for_a_turn_without_files():
    assert make().check_attachments(TENANT, "u", [], "test:m") is None


def test_record_turn_translates_a_busy_thread():
    from eap_run_store import TurnInProgress

    svc = make()
    svc.engine = MagicMock()
    import eap_api.services.ag_ui as module

    class Locked:
        conversations = MagicMock()

        def new_turn_options(self, **kw):  # pragma: no cover - not reached
            raise AssertionError

    from contextlib import contextmanager

    @contextmanager
    def locked(tenant, agent):
        yield Locked()

    svc.locked_agent = locked
    Locked.conversations.record_and_launch_turn.side_effect = TurnInProgress("c", "run9", "running")
    with pytest.raises(TurnBusy) as busy:
        svc.record_turn(
            TENANT, "a1", conversation_id="c", run_id="r", key=KEY, fingerprint="f", input_message_ids=("u1",),
            input_text="hi", version=None, request_context=None, model=None, model_settings=None,
            attachment_capabilities=None, client_tools=None, client_tool_results=None,
        )
    assert busy.value.run_id == "run9" and module.TurnBusy is TurnBusy


def turn(n, run_id, status="completed"):
    return {"id": run_id, "turn": n, "status": status, "display_status": status, "input": f"q{n}", "output": f"a{n}"}


def test_thread_view_supersedes_a_regenerated_turn_and_reports_the_active_run():
    turns = [turn(0, "t0"), turn(1, "t1", "running")]
    records = {"t0": TurnRun("r0", ("u1",)), "t1": TurnRun("r1", ("u1",))}
    view = build_thread_view(ThreadData(turns, {}, records), lambda ids: {}, via_api_key=False)
    assert [r["superseded"] for r in view.runs] == [True, False]
    assert view.active_run_id == "r1"
    assert [m["role"] for m in view.messages] == ["user"]  # t1 has no answer yet; t0 is left out


def test_thread_view_of_an_empty_thread_is_empty():
    view = build_thread_view(ThreadData([], {}, {}), lambda ids: {}, via_api_key=False)
    assert view.messages == [] and view.runs == [] and view.active_run_id is None


def test_attachment_parts_use_the_route_of_the_principal_kind():
    turns = [turn(0, "t0")]
    records = {"t0": TurnRun("r0", ("u1",))}
    file = SimpleNamespace(id="f1", media_type="image/png", name="a.png", size=3)
    files = lambda ids: {m: [file] for m in ids}  # noqa: E731
    view = build_thread_view(ThreadData(turns, {}, records), files, via_api_key=True)
    part = view.messages[0]["content"][-1]
    assert part["source"]["value"] == "/api/v1/ag-ui/attachments/f1" and part["type"] == "image"
    view = build_thread_view(ThreadData(turns, {}, records), files, via_api_key=False)
    assert view.messages[0]["content"][-1]["source"]["value"] == "/api/v1/attachments/f1"


def test_history_of_a_workflow_thread_is_its_starter_rows_input():
    rows = [SimpleNamespace(from_seq=2, input_messages=None),
            SimpleNamespace(from_seq=-1, input_messages=[{"id": "m1", "content": "go"}])]
    assert history_of(rows) == [{"id": "m1", "role": "user", "content": "go"}]
    assert history_of([SimpleNamespace(from_seq=0, input_messages=None)]) == []


def test_workflow_input_errors_name_the_location_not_the_value():
    spec = SimpleNamespace(input_schema={"type": "object", "required": ["x"]})
    assert input_errors("nope", spec)[0]["type"] == "workflow_input_invalid"
    assert input_errors({}, spec)  # a required key is missing
    assert input_errors({"x": 1}, spec) == []


def test_workflow_service_refuses_an_unknown_workflow_and_records_end_order():
    svc = make(AgUiWorkflowService)
    session = MagicMock()
    session.__enter__.return_value.execute.return_value.scalar_one_or_none.return_value = None
    import eap_api.services.ag_ui_workflows as module

    module_session = module.Session
    module.Session = lambda engine: session
    try:
        with pytest.raises(NotFound):
            svc.load_workflow(TENANT, str(uuid.uuid4()))
    finally:
        module.Session = module_session
    from sqlalchemy.exc import NoResultFound

    svc.ag_ui.set_end_order.side_effect = NoResultFound()
    record = SimpleNamespace(scope_id="w", external_thread_id="th", external_run_id="r")
    assert svc.record_end_order(TENANT, record, 7) == 7
    svc.ag_ui.set_end_order.side_effect = None
    svc.ag_ui.set_end_order.return_value = 5
    assert svc.record_end_order(TENANT, record, 7) == 5


def test_workflow_record_thread_run_refuses_another_workflows_thread():
    svc = make(AgUiWorkflowService)
    svc.ag_ui.record_ag_ui_run.side_effect = AgUiScopeMismatch(TENANT, "r", "workflow", "w")
    with pytest.raises(Conflict) as err:
        svc.record_thread_run(TENANT, KEY, run_id="r", fingerprint="f", from_seq=-1)
    assert err.value.detail_object == "threadId belongs to another workflow"
