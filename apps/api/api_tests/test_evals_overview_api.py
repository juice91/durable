"""Tenant-wide eval lists for the Datasets, Scorers and Experiments pages: shape, pagination and
tenant isolation. Same harness as test_evals_api (fake DBOS client, results written to the store)."""

from __future__ import annotations

import uuid

from .test_evals_api import API, _agent, _record, _suite, api, fake, pg  # noqa: F401

SPEC_B = {"name": "Other", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


def _run(client, h, agent_id, suite_id) -> str:
    r = client.post(
        f"{API}/agents/{agent_id}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id}, headers=h
    )
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


@pg
def test_suites_across_agents_with_last_run(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    a1, a2 = _agent(client, h), _agent(client, h, spec=SPEC_B)
    s1 = _suite(client, h, a1, name="alpha", cases=("a", "b"))
    s2 = _suite(client, h, a2, name="beta", cases=("c",))
    run = _run(client, h, a1, s1)
    _record(app, tenant, run, ["passed", "failed"])

    r = client.get(f"{API}/eval-suites", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["next_cursor"] is None
    first, second = body["items"]
    assert (first["name"], first["agent_id"], first["agent_name"], first["case_count"]) == ("alpha", a1, "Evaluated", 2)
    assert first["last_run"]["run_id"] == run
    assert first["last_run"]["aggregates"] == {"cases": 2, "passed": 1, "failed": 1, "errors": 0, "pass_rate": 0.5}
    assert (second["id"], second["agent_name"], second["last_run"]) == (s2, "Other", None)

    only = client.get(f"{API}/eval-suites", params={"agent_id": a2}, headers=h).json()["items"]
    assert [x["id"] for x in only] == [s2]
    # An archived suite is left out unless asked for.
    assert client.patch(f"{API}/eval-suites/{s2}", json={"archived": True}, headers=h).status_code == 200
    assert [x["id"] for x in client.get(f"{API}/eval-suites", headers=h).json()["items"]] == [s1]
    assert len(client.get(f"{API}/eval-suites", params={"include_archived": True}, headers=h).json()["items"]) == 2


@pg
def test_suites_paginate_and_reject_bad_cursors(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent = _agent(client, h)
    ids = [_suite(client, h, agent, name=f"s{i}", cases=()) for i in range(5)]
    seen, cursor = [], None
    while True:
        r = client.get(f"{API}/eval-suites", params={"limit": 2, **({"after": cursor} if cursor else {})}, headers=h)
        assert r.status_code == 200
        seen += [x["id"] for x in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == ids
    assert client.get(f"{API}/eval-suites", params={"after": "nope"}, headers=h).status_code == 422
    assert client.get(f"{API}/eval-suites", params={"limit": 101}, headers=h).status_code == 422


@pg
def test_runs_across_agents_newest_first_and_paged(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    a1, a2 = _agent(client, h), _agent(client, h, spec=SPEC_B)
    s1, s2 = _suite(client, h, a1, name="one"), _suite(client, h, a2, name="two")
    runs = [_run(client, h, a1, s1), _run(client, h, a2, s2), _run(client, h, a1, s1)]
    _record(app, tenant, runs[0], ["passed", "passed"])

    items = client.get(f"{API}/evals/runs", headers=h).json()["items"]
    assert [x["run_id"] for x in items] == runs[::-1]
    by_id = {x["run_id"]: x for x in items}
    assert by_id[runs[1]]["agent_name"] == "Other" and by_id[runs[1]]["suite_name"] == "two"
    assert by_id[runs[0]]["aggregates"]["pass_rate"] == 1.0 and "input" not in by_id[runs[0]]

    seen, cursor = [], None
    while True:
        r = client.get(f"{API}/evals/runs", params={"limit": 2, **({"before": cursor} if cursor else {})}, headers=h)
        seen += [x["run_id"] for x in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert seen == runs[::-1]
    assert [x["run_id"] for x in client.get(f"{API}/evals/runs", params={"suite_id": s2}, headers=h).json()["items"]] == [runs[1]]
    assert client.get(f"{API}/evals/runs", params={"before": "nope"}, headers=h).status_code == 422


@pg
def test_scorers_group_evaluators_with_recent_values(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent = _agent(client, h)
    s1 = _suite(client, h, agent, name="one", evaluators=[{"kind": "equals_expected", "name": "eq"}, {"kind": "regex", "pattern": "x"}])
    _suite(client, h, agent, name="two", evaluators=[{"kind": "equals_expected", "name": "eq"}])
    run = _run(client, h, agent, s1)
    _record(app, tenant, run, ["passed", "failed"], scores={"a": 1.0, "b": 0.0})

    body = client.get(f"{API}/evals/scorers", headers=h).json()
    assert body["next_cursor"] is None
    by = {(x["kind"], x["name"]): x for x in body["items"]}
    assert set(by) == {("equals_expected", "eq"), ("regex", "regex")}
    eq = by[("equals_expected", "eq")]
    assert eq["suite_count"] == 2 and [s["name"] for s in eq["suites"]] == ["one", "two"]
    assert eq["suites"][0]["agent_name"] == "Evaluated"
    assert eq["history"] == [{
        "run_id": run, "suite_id": s1, "agent_id": agent, "created_at": eq["history"][0]["created_at"],
        "metric": "pass_rate", "value": 0.5,
    }]
    assert by[("regex", "regex")]["history"] == []

    first = client.get(f"{API}/evals/scorers", params={"limit": 1}, headers=h).json()
    assert len(first["items"]) == 1 and first["next_cursor"]
    rest = client.get(f"{API}/evals/scorers", params={"limit": 1, "after": first["next_cursor"]}, headers=h).json()
    assert [x["kind"] for x in first["items"] + rest["items"]] == ["equals_expected", "regex"]
    assert client.get(f"{API}/evals/scorers", params={"after": "!"}, headers=h).status_code == 422


@pg
def test_overview_is_tenant_scoped(api):
    client, app, tenant_of = api
    t1, h1 = tenant_of("alice")
    _, h2 = tenant_of("bob")
    agent = _agent(client, h1)
    suite = _suite(client, h1, agent, name="mine")
    _run(client, h1, agent, suite)
    for path in ("/eval-suites", "/evals/runs", "/evals/scorers"):
        assert client.get(f"{API}{path}", headers=h2).json() == {"items": [], "next_cursor": None}
    # Another tenant's agent id is a 404, never data.
    assert client.get(f"{API}/eval-suites", params={"agent_id": agent}, headers=h2).status_code == 404
    assert client.get(f"{API}/evals/runs", params={"agent_id": agent}, headers=h2).status_code == 404
    assert len(client.get(f"{API}/eval-suites", headers=h1).json()["items"]) == 1


from .test_evals_workflow_api import agent_suite, workflow_suite, wf_api  # noqa: E402,F401
from .test_workflow_runs_api import publish_demo  # noqa: E402


@pg
def test_overview_rows_carry_target_kind_and_name(wf_api):
    client, app, _, tenant_of = wf_api
    tenant, h = tenant_of()
    wf = publish_demo(client, h)
    ws = workflow_suite(client, h, wf, name="wfs", cases=('{"q": "a"}',))
    agent_id, as_ = agent_suite(client, h, name="ags")
    wf_run = client.post(
        f"{API}/workflows/{wf}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": ws}, headers=h
    ).json()["run_id"]
    ag_run = _run(client, h, agent_id, as_)
    _record(app, tenant, wf_run, ["passed"])

    suites = {s["id"]: s for s in client.get(f"{API}/eval-suites", headers=h).json()["items"]}
    assert (suites[ws]["target_kind"], suites[ws]["target_id"], suites[ws]["target_name"]) == ("workflow", wf, "Triage")
    assert (suites[ws]["agent_id"], suites[ws]["agent_name"], suites[ws]["workflow_id"]) == (None, None, wf)
    assert suites[ws]["last_run"]["run_id"] == wf_run
    assert (suites[as_]["target_kind"], suites[as_]["target_id"], suites[as_]["target_name"]) == (
        "agent", agent_id, "Evaluated")
    assert (suites[as_]["agent_id"], suites[as_]["agent_name"]) == (agent_id, "Evaluated")

    runs = {r["run_id"]: r for r in client.get(f"{API}/evals/runs", headers=h).json()["items"]}
    assert (runs[wf_run]["target_kind"], runs[wf_run]["target_id"], runs[wf_run]["target_name"]) == (
        "workflow", wf, "Triage")
    assert (runs[wf_run]["agent_id"], runs[wf_run]["agent_name"], runs[wf_run]["workflow_id"]) == (None, None, wf)
    assert runs[wf_run]["suite_name"] == "wfs" and runs[wf_run]["aggregates"]["passed"] == 1
    assert (runs[ag_run]["target_kind"], runs[ag_run]["target_name"], runs[ag_run]["agent_name"]) == (
        "agent", "Evaluated", "Evaluated")
    # The agent filter keeps the workflow suites out.
    only = client.get(f"{API}/eval-suites", params={"agent_id": agent_id}, headers=h).json()["items"]
    assert [s["id"] for s in only] == [as_]
    # Scorers list a workflow suite's evaluator with its target.
    scorer = next(x for x in client.get(f"{API}/evals/scorers", headers=h).json()["items"] if x["kind"] == "contains")
    assert scorer["suites"][0]["target_kind"] == "workflow" and scorer["suites"][0]["target_name"] == "Triage"
