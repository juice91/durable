"""AttachmentService: the attachment and feedback refusals, no database."""

from __future__ import annotations

import types

import pytest
from eap_run_store.definition_errors import DefinitionError

from eap_api.services.attachments import AttachmentService


class Attachments:
    def get(self, tenant, attachment_id):
        return None


class Runs:
    def __init__(self, conversation=True):
        self.conversation = conversation

    def get_conversation(self, tenant, cid):
        return {"id": cid} if self.conversation else None

    def list_turns(self, tenant, cid):
        return [{"id": "turn-1"}]


class Feedback:
    def __init__(self):
        self.set_calls, self.deleted = [], []

    def set(self, tenant, run_id, message_id, **kw):
        self.set_calls.append((run_id, message_id, kw))
        return types.SimpleNamespace(as_json=lambda: {"message_id": message_id, **kw})

    def delete(self, tenant, message_id, created_by):
        self.deleted.append(message_id)

    def for_runs(self, *a, **kw):
        return []


def make(conversation=True):
    feedback = Feedback()
    return AttachmentService(Attachments(), feedback, Runs(conversation), engine=None), feedback


def test_an_unknown_attachment_is_a_404():
    with pytest.raises(DefinitionError) as err:
        make()[0].get_attachment("t", "a")
    assert (err.value.status_code, err.value.body) == (404, {"detail": "Attachment not found"})


@pytest.mark.parametrize("message_id", ["no-suffix", "x"])
def test_a_malformed_message_id_is_a_404(message_id):
    service, _ = make()
    with pytest.raises(DefinitionError) as err:
        service.set_feedback("t", "u", "c", message_id, rating="up", comment=None)
    assert err.value.body == {"detail": "Message not found"}


def test_feedback_on_a_turn_message_is_stored_with_a_trimmed_comment():
    service, feedback = make()
    out = service.set_feedback("t", "u", "c", "turn-1-m2", rating="down", comment="  too long ")
    assert feedback.set_calls[0][0] == "turn-1" and out["comment"] == "too long"
    service.clear_feedback("t", "u", "c", "turn-1-m2")
    assert feedback.deleted == ["turn-1-m2"]


def test_the_feedback_of_a_viewer_less_principal_is_empty_and_an_unknown_thread_a_404():
    service, _ = make()
    assert service.list_feedback("t", "c", None) == {"feedback": {}}
    with pytest.raises(DefinitionError):
        make(conversation=False)[0].list_feedback("t", "c", "u")
