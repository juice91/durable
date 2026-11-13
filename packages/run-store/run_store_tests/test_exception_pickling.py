"""Run-store exceptions survive pickling (DBOS pickles errors raised inside steps): args are the
constructor arguments, so unpickling rebuilds the same fields and the same message."""

from __future__ import annotations

import pickle
from uuid import uuid4

import pytest

from eap_run_store import ConversationIdConflict, ConversationNotFound, RunIdConflict, RunNotFound, TurnInProgress

RUN_ID = uuid4()


@pytest.mark.parametrize("exc,fields", [
    (RunNotFound("t1", RUN_ID), ("tenant_id", "run_id")),
    (RunIdConflict(RUN_ID), ("run_id",)),
    (ConversationNotFound("t1", RUN_ID), ("tenant_id", "conversation_id")),
    (ConversationIdConflict(RUN_ID), ("conversation_id",)),
    (TurnInProgress("c1", "r1", "running"), ("conversation_id", "run_id", "status")),
])
def test_round_trip(exc, fields):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert {f: getattr(restored, f) for f in fields} == {f: getattr(exc, f) for f in fields}


def test_messages_are_unchanged():
    assert str(ConversationNotFound("t1", "c1")) == "Conversation not found for tenant 't1': c1"
    assert str(ConversationIdConflict("c1")) == "Conversation ID c1 already exists under a different tenant"
    assert str(TurnInProgress("c1", "r1", "queued")) == "Conversation c1 is busy: turn run r1 is queued"
