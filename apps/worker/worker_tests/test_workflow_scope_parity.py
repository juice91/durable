"""LP.2: eap_schemas.workflow_scope keeps the worker's top-level identifiers byte-identical, and its
loop error codes fit (and do not redefine) WORKFLOW_FAILURES. Pure; no database."""

import random
import uuid

from eap_schemas import workflow_scope as ws
from eap_worker import workflow_run as wr
from eap_worker.errors import WORKFLOW_FAILURES


def test_empty_scope_matches_worker_identifiers():
    assert ws.CHILD_RUN_NAMESPACE == wr.CHILD_RUN_NAMESPACE
    rng = random.Random(7)
    names = ["a", "fetch", "a_1_s", "b_0", "z" * 64, "s9_x"]
    for _ in range(200):
        run = str(uuid.UUID(int=rng.getrandbits(128)))
        step = rng.choice(names)
        assert ws.scoped_child_run_id(run, ws.EMPTY_SCOPE, step) == wr.child_run_id(run, step)
        assert ws.scoped_guard_key(run, ws.EMPTY_SCOPE, step) == wr.side_effect_key(run, step)
        assert ws.scoped_tool_call_id(ws.EMPTY_SCOPE, step) == wr.tool_call_id_for(step)


def test_loop_failures_fit_worker_table():
    sample = next(iter(WORKFLOW_FAILURES.values()))
    for code, value in ws.LOOP_WORKFLOW_FAILURES.items():
        assert type(value) is type(sample) and len(value) == len(sample)
        assert code not in WORKFLOW_FAILURES or WORKFLOW_FAILURES[code] == value
