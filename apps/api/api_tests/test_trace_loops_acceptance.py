"""LP.9 tester: GET /api/v1/runs/{id}/trace of a workflow loop run, and tenant isolation."""

from __future__ import annotations

import json
import uuid

import pytest
from .run_helpers import requires_pg
from .test_traces_acceptance import Ctx, api, ctx, fake, _no_hang  # noqa: F401 - fixtures

pytestmark = requires_pg


def _seed_loop(ctx, tenant=None, iterations=3):
    t = tenant or ctx.tenant
    run = uuid.uuid4()
    ctx.store.create_workflow_run(t, uuid.uuid4(), 1, json.dumps({}), run)
    ap = lambda key, typ, data: ctx.log.append(t, run, key, typ, data)  # noqa: E731
    ap("start", "run_started", {"kind": "workflow", "input": {}})
    ap("step:refine:started", "step_started", {"step_id": "refine", "step_type": "loop"})
    for i in range(iterations):
        ap(f"loop:refine.{i}:started", "loop_iteration_started",
           {"step_id": "refine", "scope": [], "index": i, "state": {"n": i}})
        ap(f"loop:refine.{i}:step:work:started", "step_started",
           {"step_id": "work", "step_type": "tool", "scope": [{"loop": "refine", "index": i}]})
        ap(f"loop:refine.{i}:step:work:completed", "step_completed",
           {"step_id": "work", "output": i, "scope": [{"loop": "refine", "index": i}]})
        ap(f"loop:refine.{i}:completed", "loop_iteration_completed",
           {"step_id": "refine", "scope": [], "index": i, "last_key": "k", "state": {"n": i + 1}})
    ap("step:refine:completed", "step_completed",
       {"step_id": "refine", "output": {"iterations": iterations, "exited": "max_iterations"}})
    return run


def test_trace_api_returns_loop_spans(ctx):
    run = _seed_loop(ctx)
    body = ctx.trace(run)
    kinds = [s["kind"] for s in body["spans"]]
    assert kinds.count("loop") == 1 and kinds.count("loop_iteration") == 3
    spans = {s["id"]: s for s in body["spans"]}
    loop = spans[f"{run}:step:refine"]
    assert loop["attributes"]["iterations_completed"] == 3 and loop["attributes"]["exited"] == "max_iterations"
    for i in range(3):
        it = spans[f"{run}:loop:refine.{i}"]
        assert it["parent_id"] == loop["id"] and it["name"] == f"refine #{i}" and it["status"] == "ok"
        assert spans[f"{run}:step:refine.{i}~work"]["parent_id"] == it["id"]
    assert body["truncated"]["iterations"] == 0 and body["limits"]["max_loop_iteration_spans"] == 100


def test_trace_api_loop_run_is_tenant_isolated(ctx):
    run = _seed_loop(ctx)
    _, h2 = ctx.tenant_of()
    r = ctx.client.get(f"/api/v1/runs/{run}/trace", headers=h2)
    assert r.status_code == 404 and r.json()["detail"] == "Run not found"
    other_tenant, h3 = ctx.tenant_of()
    mine = _seed_loop(ctx, tenant=other_tenant, iterations=1)
    body = ctx.trace(mine, h3)
    assert str(run) not in json.dumps(body)
    assert sum(1 for s in body["spans"] if s["kind"] == "loop_iteration") == 1


def test_trace_api_caps_loop_iterations(ctx):
    run = _seed_loop(ctx, iterations=103)
    body = ctx.trace(run)
    assert sum(1 for s in body["spans"] if s["kind"] == "loop_iteration") == 100
    assert body["truncated"]["iterations"] == 3
