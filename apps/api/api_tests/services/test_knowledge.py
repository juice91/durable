"""KnowledgeService, with fake stores: the refusals and their bodies, no database."""

from __future__ import annotations

import pytest
from eap_run_store.definition_errors import DefinitionError

from eap_api.services import knowledge as svc
from eap_api.services.knowledge import KnowledgeService

TENANT = "t1"
KB = {"id": "11111111-1111-1111-1111-111111111111", "pending_generation": None, "chunking": {}}


class FakeStore:
    def __init__(self, kb=KB, docs=()):
        self.kb, self.docs, self.deleted = kb, list(docs), []

    def get_knowledge_base(self, tenant, kb_id):
        return self.kb

    def delete_knowledge_base(self, tenant, kb_id):
        self.deleted.append(kb_id)
        return True

    def list_documents(self, tenant, kb_id, limit=50, after=None):
        return self.docs[:limit]

    def get_document(self, *args, **kwargs):
        return None

    def update_knowledge_base(self, tenant, kb_id, **fields):
        raise svc.KnowledgeBaseNotFound(kb_id)


def make(store=None):
    return KnowledgeService(store or FakeStore(), run_store=None, launcher=None)


def refusal_of(call) -> DefinitionError:
    with pytest.raises(DefinitionError) as caught:
        call()
    return caught.value


def test_an_unknown_knowledge_base_is_a_404_with_the_plain_body():
    err = refusal_of(lambda: make(FakeStore(kb=None)).get_knowledge_base(TENANT, "x"))
    assert (err.status_code, err.body) == (404, {"detail": "Knowledge base not found"})


def test_update_of_a_vanished_knowledge_base_is_a_404():
    err = refusal_of(lambda: make().update_knowledge_base(TENANT, KB["id"], {"name": "n"}))
    assert err.status_code == 404


def test_delete_is_refused_with_the_agents_unless_forced(monkeypatch):
    agents = [{"agent_id": "a", "agent_name": "Bot", "version": 1, "archived": False, "draft": False}]
    monkeypatch.setattr(svc, "referencing_agents", lambda *a: agents)
    store = FakeStore()
    err = refusal_of(lambda: make(store).delete_knowledge_base(TENANT, KB["id"]))
    assert err.status_code == 409 and err.body["agents"] == agents and "Bot" in err.body["detail"]
    assert store.deleted == []
    make(store).delete_knowledge_base(TENANT, KB["id"], force=True)
    assert store.deleted == [KB["id"]]


def test_a_cursor_this_api_did_not_issue_is_a_422():
    err = refusal_of(lambda: make().list_documents(TENANT, KB["id"], 10, "not-a-cursor"))
    assert err.status_code == 422 and "cursor" in err.body["detail"]


def test_list_documents_pages_with_an_opaque_cursor():
    docs = [{"document_key": "a", "mime_type": "text/plain"}, {"document_key": "b", "mime_type": "text/plain"}]
    page = make(FakeStore(docs=docs)).list_documents(TENANT, KB["id"], 2, None)
    assert [d["document_key"] for d in page["items"]] == ["a", "b"]
    assert svc.decode_document_cursor(page["next_after"]) == "b"


def test_a_body_document_key_must_match_the_path():
    class Body:
        document_key = "other"
        mime_type = "text/plain"

    err = refusal_of(lambda: make().put_document(TENANT, KB["id"], "doc", Body(), 10))
    assert err.status_code == 422 and "must match the path" in err.body["detail"]


def test_a_failed_hand_off_is_a_503_with_the_run_id():
    err = svc.HandOffFailed("run-1")
    assert (err.status_code, err.body) == (503, {"detail": svc.ENQUEUE_FAILED_DETAIL, "run_id": "run-1"})


def test_search_view_is_finished_only_for_a_terminal_run():
    base = {"id": "r", "status": "running", "display_status": "running", "knowledge_base_id": "k"}
    assert svc.search_view(base)[0] is False
    done, view = svc.search_view({**base, "status": "completed", "output": '{"hits": []}'})
    assert done and view["result"] == {"hits": []}
    done, view = svc.search_view({**base, "status": "failed", "error": "boom"})
    assert done and view["error"] == "boom"
