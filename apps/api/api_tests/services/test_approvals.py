"""ApprovalService: inbox reads and the tool-decision refusals, with fake collaborators."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from eap_api.services.approvals import ApprovalService, approval_json
from eap_run_store import InvalidRunCursor
from eap_run_store.definition_errors import DefinitionError


class _Store:
    def __init__(self, rows=(), error=None):
        self.rows, self.error = list(rows), error

    def list(self, tenant_id, **kwargs):
        if self.error:
            raise self.error
        return self.rows, None

    def get(self, tenant_id, run_id, kind, key):
        return self.rows[0] if self.rows else None


class _Events:
    def get_event(self, tenant_id, run_id, key):
        return SimpleNamespace(data={"summary": "send an email"}) if key.endswith("approval:waiting") else None


def _row(**over):
    row = {
        "run_id": uuid.uuid4(), "kind": "tool", "approval_key": "call_1", "root_run_id": uuid.uuid4(),
        "agent_id": None, "workflow_id": None, "tool_name": "mail.send", "args": {}, "prompt": None,
        "status": "pending", "requested_at": None, "deadline": None, "decided_at": None, "decision": None,
        "decided_by": None, "run_status": "running",
    }
    return {**row, **over}


def test_a_tool_item_carries_its_summary_and_ids():
    row = _row()
    item = ApprovalService(_Store([row]), _Events(), SimpleNamespace()).get("t", row["run_id"], "tool", "call_1")
    assert item["tool_call_id"] == "call_1" and item["step_id"] is None and item["summary"] == "send an email"
    assert item == {**approval_json(row), "summary": "send an email"}


def test_unknown_approval_and_bad_cursor_are_refused():
    service = ApprovalService(_Store(), _Events(), SimpleNamespace())
    with pytest.raises(DefinitionError) as missing:
        service.get("t", uuid.uuid4(), "tool", "x")
    assert missing.value.body == {"detail": "Approval not found"}
    service = ApprovalService(_Store(error=InvalidRunCursor("bad")), _Events(), SimpleNamespace())
    with pytest.raises(DefinitionError) as bad:
        service.list("t", cursor="zzz")
    assert bad.value.status_code == 422
