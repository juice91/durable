"""Exceptions the eap.tool_run steps can raise survive DBOS's step-error serialization (pickle) with
their fields intact, so a replayed failure is classified with the right code and tool id."""

from __future__ import annotations

import pickle
import uuid

import pytest
from dbos._serialization import DBOSDefaultSerializer

from eap_run_store import RunIdConflict, RunNotFound
from eap_runtime import SideEffectOutcomeUnknown, ToolNotAllowedError, UnknownToolError
from eap_worker.errors import ToolInputInvalid, classify

RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000042")

CASES = [
    (ToolInputInvalid("tickets.create"), {"tool_id": "tickets.create"}, "input does not match tool 'tickets.create'"),
    (UnknownToolError("no.such"), {"tool_id": "no.such"}, "Unknown tool: no.such"),
    (ToolNotAllowedError("tickets.create"), {"tool_id": "tickets.create"}, "Tool not allowed: tickets.create"),
    (SideEffectOutcomeUnknown(f"{RUN_ID}:tool"), {"key": f"{RUN_ID}:tool"}, f"Side effect outcome unknown for key: {RUN_ID}:tool"),
    (RunNotFound("t1", RUN_ID), {"tenant_id": "t1", "run_id": RUN_ID}, f"Run not found for tenant 't1': {RUN_ID}"),
    (RunIdConflict(RUN_ID), {"run_id": RUN_ID}, f"Run ID {RUN_ID} already exists under a different tenant"),
]


def _round_trips(exc: BaseException) -> list[BaseException]:
    return [
        pickle.loads(pickle.dumps(exc)),
        DBOSDefaultSerializer.deserialize(DBOSDefaultSerializer.serialize(exc)),
    ]


@pytest.mark.parametrize("exc, fields, message", CASES, ids=[type(c[0]).__name__ for c in CASES])
def test_step_exceptions_round_trip(exc, fields, message):
    for restored in _round_trips(exc):
        assert type(restored) is type(exc)
        assert {k: getattr(restored, k) for k in fields} == fields
        # The constructor path alone (args) is right too, not only the restored __dict__.
        assert {k: getattr(type(exc)(*restored.args), k) for k in fields} == fields
        assert str(restored) == message


def test_replayed_rejections_classify_with_the_right_tool_id():
    for exc, code, error in [
        (ToolInputInvalid("tickets.create"), "tool_input_invalid",
         "ToolInputInvalid: the input does not match tool 'tickets.create'"),
        (ToolNotAllowedError("tickets.create"), "tool_not_allowed",
         "ToolNotAllowedError: tool 'tickets.create' is not allowed on this worker"),
        (UnknownToolError("no.such"), "unknown_tool", "UnknownToolError: tool 'no.such' is not registered"),
        (SideEffectOutcomeUnknown("r:tool"), "side_effect_outcome_unknown", None),
    ]:
        for restored in _round_trips(exc):
            failure = classify(restored)
            assert failure.code == code
            if error is not None:
                assert failure.error == error
