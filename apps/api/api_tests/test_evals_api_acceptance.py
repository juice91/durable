"""AC.12 acceptance tests: caps, isolation, round trips, hostile imports, runs, compare, warnings.

Complements test_evals_api.py. No worker exists for eap.eval_run (AC.11): runs are enqueued into a
fake DBOS client and results are seeded with EvalStore.record_case_result.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

import pytest
import yaml

from eap_api.eval_format import EvalFormatError, parse_file
from eap_api.routes.evals import side_effect_warnings
from eap_run_store.evals import CaseResultWrite, EvalStore
from eap_schemas import AgentSpec

from .test_evals_api import (  # noqa: F401  (fixtures)
    AGENT_SPEC,
    API,
    SIDE_EFFECT_SPEC,
    _agent,
    _suite,
    api,
    fake,
    pg,
)


def _ev(n: int) -> list[dict]:
    return [{"kind": "regex", "pattern": f"p{i}", "name": f"r{i}"} for i in range(n)]


@pg
def test_caps_on_cases_and_evaluators(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    # Suite evaluator cap: 10 ok, 11 rejected, on create and on patch.
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "ten", "evaluators": _ev(10)}, headers=h)
    assert r.status_code == 201, r.text
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "eleven", "evaluators": _ev(11)}, headers=h)
    assert r.status_code == 422, r.text
    suite_id = client.get(f"{API}/agents/{agent_id}/eval-suites", headers=h).json()["items"][0]["id"]
    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": _ev(11)}, headers=h)
    assert r.status_code == 422
    assert len(client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["evaluators"]) == 10
    # Case evaluator cap: 5 ok, 6 rejected.
    url = f"{API}/eval-suites/{suite_id}/cases"
    five = [{"kind": "regex", "pattern": "p", "name": f"k{i}"} for i in range(5)]
    assert client.post(url, json={"name": "five", "input": "x", "evaluators": five}, headers=h).status_code == 201
    r = client.post(url, json={"name": "six", "input": "x", "evaluators": [
        {"kind": "regex", "pattern": "p", "name": f"c{i}"} for i in range(6)]}, headers=h)
    assert r.status_code == 422, r.text


@pg
def test_500_case_cap(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, evaluators=[], cases=())
    lines = "".join(f"  - name: c{i}\n    inputs: q{i}\n" for i in range(500))
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=("cases:\n" + lines).encode(), headers=h)
    assert r.status_code == 200 and r.json()["created"] == 500, r.text
    r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c500", "input": "x"}, headers=h)
    assert r.status_code == 422 and "at most 500 cases" in r.json()["detail"]
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=b"cases:\n  - name: extra\n    inputs: x\n", headers=h)
    assert r.status_code == 422, r.text
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["case_count"] == 500
    # Deleting one makes room again; updating an existing case by import still works at the cap.
    first = client.get(f"{API}/eval-suites/{suite_id}/cases", headers=h).json()["items"][0]["id"]
    assert client.delete(f"{API}/eval-suites/{suite_id}/cases/{first}", headers=h).status_code == 204
    assert client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c500", "input": "x"}, headers=h).status_code == 201


@pg
def test_cross_agent_isolation_on_every_route(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_a, agent_b = _agent(client, h), _agent(client, h)
    suite_id = _suite(client, h, agent_a)
    run = client.post(f"{API}/agents/{agent_a}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id},
                      headers=h).json()
    rid = run["run_id"]
    # Another agent of the same tenant never sees agent A's suite or runs.
    assert client.get(f"{API}/agents/{agent_b}/eval-suites", headers=h).json()["items"] == []
    assert client.get(f"{API}/agents/{agent_b}/evals/runs", headers=h).json()["items"] == []
    for path in (f"/evals/runs/{rid}", f"/evals/runs/{rid}/results"):
        assert client.get(f"{API}/agents/{agent_b}{path}", headers=h).status_code == 404, path
    r = client.post(f"{API}/agents/{agent_b}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id,
                                                                 "version": "draft"}, headers=h)
    assert r.status_code == 404
    # Reusing agent A's run id from agent B is a conflict, never a read or a relaunch.
    r = client.post(f"{API}/agents/{agent_b}/evals/runs", json={"run_id": rid, "suite_id": suite_id}, headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "run_id is already used by a different run request"
    # Unknown agent: 404 on every agent-scoped route.
    ghost = uuid.uuid4()
    for method, path, body in (
        ("get", f"/agents/{ghost}/eval-suites", None),
        ("post", f"/agents/{ghost}/eval-suites", {"name": "x"}),
        ("get", f"/agents/{ghost}/evals/runs", None),
        ("get", f"/agents/{ghost}/evals/warnings", None),
        ("get", f"/agents/{ghost}/evals/runs/{rid}", None),
        ("get", f"/agents/{ghost}/evals/compare?base={rid}&target={rid}", None),
        ("post", f"/agents/{ghost}/evals/runs", {"run_id": str(uuid.uuid4()), "suite_id": suite_id}),
    ):
        r = client.request(method, API + path, json=body, headers=h)
        assert r.status_code == 404, (method, path, r.status_code)


@pg
def test_cross_tenant_run_id_conflict_and_no_leak(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    rid = str(uuid.uuid4())
    assert client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": rid, "suite_id": suite_id},
                       headers=h).status_code == 201
    o_agent = _agent(client, other)
    o_suite = _suite(client, other, o_agent)
    r = client.post(f"{API}/agents/{o_agent}/evals/runs", json={"run_id": rid, "suite_id": o_suite}, headers=other)
    assert r.status_code == 409 and r.json()["detail"] == "run_id is already in use"
    # The other tenant cannot import into or run against the owner's suite via its own agent.
    r = client.post(f"{API}/agents/{o_agent}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id},
                    headers=other)
    assert r.status_code == 404


@pg
def test_export_import_round_trip_both_formats_and_field_equals_null(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    fe_null = {"kind": "field_equals", "path": "x", "value": None, "name": "nullfe"}
    suite_id = _suite(client, h, agent_id, evaluators=[fe_null, {"kind": "max_duration", "seconds": 3}], cases=())
    sent = [
        {"name": "k1", "input": "a\nb", "expected_output": {"x": None, "y": [1, "2"]}, "metadata": {"m": None},
         "evaluators": [{"kind": "field_equals", "path": "x", "value": None}]},
        {"name": "k2: colon", "input": "t", "expected_output": None, "metadata": {}, "evaluators": []},
    ]
    for c in sent:
        assert client.post(f"{API}/eval-suites/{suite_id}/cases", json=c, headers=h).status_code == 201
    src = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()
    keep = ("name", "input", "expected_output", "metadata", "evaluators", "position")
    for fmt in ("yaml", "json"):
        exp = client.get(f"{API}/eval-suites/{suite_id}/export?format={fmt}", headers=h)
        assert exp.status_code == 200 and fmt in exp.headers["content-type"]
        dst = _suite(client, h, agent_id, name=f"dst-{fmt}", evaluators=[], cases=())
        r = client.post(f"{API}/eval-suites/{dst}/import?format={fmt}", content=exp.content, headers=h)
        assert r.status_code == 200 and r.json()["created"] == 2, r.text
        got = client.get(f"{API}/eval-suites/{dst}", headers=h).json()
        assert [{k: c[k] for k in keep} for c in got["cases"]] == [{k: c[k] for k in keep} for c in src["cases"]]
        assert got["evaluators"] == src["evaluators"]
        # Re-import into the same suite is a pure update: nothing created, same ids.
        again = client.post(f"{API}/eval-suites/{dst}/import?format={fmt}", content=exp.content, headers=h).json()
        assert (again["created"], again["updated"]) == (0, 2)
        assert [c["id"] for c in client.get(f"{API}/eval-suites/{dst}", headers=h).json()["cases"]] == [
            c["id"] for c in got["cases"]]
    # Cross-format: a YAML export is readable as JSON when it is JSON, and vice versa by format param.
    j = client.get(f"{API}/eval-suites/{suite_id}/export?format=json", headers=h).text
    r = client.post(f"{API}/eval-suites/{suite_id}/import?format=yaml", content=j.encode(), headers=h)
    assert r.status_code == 200  # JSON is valid YAML


@pg
def test_unsafe_yaml_is_rejected_and_nothing_executes(api, tmp_path: Path):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    marker = tmp_path / "pwned"
    hostile = [
        f"cases: !!python/object/apply:os.system ['touch {marker}']\n",
        f"cases:\n  - !!python/object/apply:subprocess.call [[touch, {marker}]]\n",
        f"cases:\n  - name: x\n    inputs: !!python/name:os.system\n",
        f"!!python/object/new:os.system [touch {marker}]\n",
        "cases:\n  - name: x\n    inputs: !!binary aGk=\n",
        "a: &a [1]\ncases: *a\n",
    ]
    for body in hostile:
        r = client.post(f"{API}/eval-suites/{suite_id}/import", content=body.encode(), headers=h)
        assert r.status_code == 422, (body, r.status_code, r.text)
        for fmt in ("yaml", "json"):
            r = client.post(f"{API}/eval-suites/{suite_id}/import?format={fmt}", content=body.encode(), headers=h)
            assert r.status_code == 422, (fmt, body)
    assert not marker.exists()
    assert [c["name"] for c in client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["cases"]] == ["a", "b"]
    for body in hostile[:4]:
        with pytest.raises(EvalFormatError):
            parse_file(body, "yaml")
    assert not marker.exists()


@pg
def test_import_rejection_edges(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    url = f"{API}/eval-suites/{suite_id}/import"
    for body, needle in (
        (b"cases:\n  - name: n\n    inputs: i\n    evaluators:\n      - Nope: {a: 1}\n", "unknown evaluator 'Nope'"),
        (b"evaluators: [LLMJudgeX]\ncases: []\n", "unknown evaluator"),
        (b"cases: []\nbogus: 1\n", "unknown key 'bogus'"),
        (b"[1, 2]\n", "invalid"),
    ):
        r = client.post(url, content=body, headers=h)
        assert r.status_code == 422 and needle in str(r.json()["detail"]), (body, r.text)
    # Exactly over the cap, with each content type.
    over = b"cases: []\n#" + b"y" * (1024 * 1024 + 1)
    for ctype in ("application/json", "application/yaml", "text/plain"):
        r = client.post(url, content=over, headers={**h, "content-type": ctype})
        assert r.status_code == 422 and "1 MiB" in r.json()["detail"], ctype
    assert [c["name"] for c in client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["cases"]] == ["a", "b"]


@pg
def test_concurrent_double_submit_creates_one_run(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    body = {"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}
    out: list = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        out.append(client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    codes = sorted(r.status_code for r in out)
    assert len(codes) == 6 and codes.count(201) == 1 and set(codes) <= {200, 201}, codes
    assert {r.json()["run_id"] for r in out} == {body["run_id"]}
    assert {r.json()["suite_hash"] for r in out} == {out[0].json()["suite_hash"]}
    assert all(o["workflow_id"] == body["run_id"] for o, _ in fake.calls)
    runs = client.get(f"{API}/agents/{agent_id}/evals/runs", headers=h).json()["items"]
    assert [r["run_id"] for r in runs] == [body["run_id"]]


@pg
def test_run_against_version_and_against_draft(api, fake):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    r = client.patch(f"{API}/agents/{agent_id}", json={"spec": {**AGENT_SPEC, "instructions": "Draft."}}, headers=h)
    assert r.status_code == 200
    url = f"{API}/agents/{agent_id}/evals/runs"
    latest = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id}, headers=h).json()
    pinned = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}, headers=h).json()
    draft = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": "draft"}, headers=h).json()
    assert (latest["version"], latest["draft"]) == (1, False) and (pinned["version"], pinned["draft"]) == (1, False)
    assert (draft["version"], draft["draft"]) == (None, True)
    for run in (latest, pinned, draft):
        got = client.get(f"{url}/{run['run_id']}", headers=h).json()
        assert got["draft"] == run["draft"] and got["version"] == run["version"] and got["suite_id"] == suite_id
    # Every run was handed to the workflow under its own id, once.
    assert sorted(o["workflow_id"] for o, _ in fake.calls) == sorted(r["run_id"] for r in (latest, pinned, draft))


@pg
def test_side_effect_warning_list_builtin_and_mcp(api):
    client, _, tenant_of = api
    tenant, h = tenant_of()
    base = {**AGENT_SPEC, "tools": ["tickets.create", "clock.now"]}
    agent_id = _agent(client, h, spec=base)
    ws = client.get(f"{API}/agents/{agent_id}/evals/warnings", headers=h).json()["warnings"]
    assert [(w["code"], w["tool"], w["kind"]) for w in ws] == [("tool_side_effect", "tickets.create", "builtin")]
    # An MCP tool: warned unless under approval (draft only: no server registry needed for the unit below).
    spec = AgentSpec.model_validate({**base, "mcp_tools": [{"server": "orders", "tool": "refund"},
                                                           {"server": "orders", "tool": "lookup"}]})
    got = side_effect_warnings(spec)
    assert [(w["code"], w["tool"], w["kind"]) for w in got] == [
        ("tool_side_effect", "tickets.create", "builtin"),
        ("mcp_tool_side_effect", "orders/refund", "mcp"),
        ("mcp_tool_side_effect", "orders/lookup", "mcp"),
    ]
    gated = AgentSpec.model_validate({**base, "mcp_tools": spec.model_dump()["mcp_tools"],
                                      "approvals": {"tools": ["tickets.create", "orders/refund"]}})
    assert [w["tool"] for w in side_effect_warnings(gated)] == ["orders/lookup"]
    assert side_effect_warnings(AgentSpec.model_validate(AGENT_SPEC)) == []


@pg
def test_mcp_tool_warning_through_the_api(api):
    from sqlalchemy import text

    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    with app.state.database_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at) "
            "VALUES (:id, :t, 'orders', 'http://127.0.0.1:1/mcp', 'streamable_http', true, now(), now())"),
            {"id": str(uuid.uuid4()), "t": tenant})
    spec = {**AGENT_SPEC, "mcp_tools": [{"server": "orders", "tool": "refund"}]}
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": spec}, headers=h).status_code == 200
    r = client.get(f"{API}/agents/{agent_id}/evals/warnings?version=draft", headers=h)
    assert [(w["code"], w["tool"]) for w in r.json()["warnings"]] == [("mcp_tool_side_effect", "orders/refund")]
    suite_id = _suite(client, h, agent_id)
    run = client.post(f"{API}/agents/{agent_id}/evals/runs",
                      json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": "draft"}, headers=h).json()
    assert [w["tool"] for w in run["warnings"]] == ["orders/refund"]
    gated = {**spec, "approvals": {"tools": ["orders/refund"]}}
    assert client.patch(f"{API}/agents/{agent_id}", json={"spec": gated}, headers=h).status_code == 200
    assert client.get(f"{API}/agents/{agent_id}/evals/warnings?version=draft", headers=h).json()["warnings"] == []


@pg
def test_compare_404_across_agents_and_hash_warning(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    a, b = _agent(client, h), _agent(client, h)
    sa, sb = _suite(client, h, a), _suite(client, h, b)
    start = lambda ag, s: client.post(f"{API}/agents/{ag}/evals/runs", headers=h, json={  # noqa: E731
        "run_id": str(uuid.uuid4()), "suite_id": s}).json()["run_id"]
    ra1, ra2, rb = start(a, sa), start(a, sa), start(b, sb)
    cmp = lambda ag, x, y: client.get(f"{API}/agents/{ag}/evals/compare?base={x}&target={y}", headers=h)  # noqa: E731
    assert cmp(a, ra1, rb).status_code == 404 and cmp(a, rb, ra1).status_code == 404
    assert cmp(b, ra1, rb).status_code == 404
    assert cmp(a, ra1, str(uuid.uuid4())).status_code == 404
    same = cmp(a, ra1, ra2).json()
    assert same["same_suite"] is True and same["warnings"] == []
    client.post(f"{API}/eval-suites/{sa}/cases", json={"name": "z", "input": "z"}, headers=h)
    ra3 = start(a, sa)
    diff = cmp(a, ra1, ra3).json()
    assert diff["same_suite"] is False
    assert diff["warnings"] == ["The runs used different test suites (their suite hashes differ)."]


@pg
def test_acceptance_suite_run_against_version_results(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, name="acceptance", cases=("one", "two", "three"))
    run = client.post(f"{API}/agents/{agent_id}/evals/runs",
                      json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}, headers=h)
    assert run.status_code == 201, run.text
    rid = run.json()["run_id"]
    assert fake.calls[-1][0]["workflow_id"] == rid
    # Seed what the runtime will write.
    snap = json.loads(app.state.run_stores[0].get_run(tenant, uuid.UUID(rid))["input"])
    store = EvalStore(app.state.run_stores[0].engine)
    for case, st in zip(snap["cases"], ["passed", "failed", "error"]):
        store.record_case_result(tenant, rid, CaseResultWrite(
            case_id=case["id"], case_name=case["name"], position=case["position"], status=st,
            assertions={"eq": {"value": st == "passed", "reason": None}}, duration_ms=10,
            error_code="agent_failed" if st == "error" else None))
    got = client.get(f"{API}/agents/{agent_id}/evals/runs/{rid}", headers=h).json()
    assert got["run_id"] == rid and got["suite_id"] == suite_id and got["version"] == 1
    assert (got["aggregates"]["cases"], got["aggregates"]["passed"], got["aggregates"]["failed"],
            got["aggregates"]["errors"]) == (3, 1, 1, 1)
    items = client.get(f"{API}/agents/{agent_id}/evals/runs/{rid}/results", headers=h).json()["items"]
    assert [(i["case_name"], i["status"], i["position"]) for i in items] == [
        ("one", "passed", 0), ("two", "failed", 1), ("three", "error", 2)]
    assert items[2]["error_code"] == "agent_failed" and items[0]["assertions"]["eq"]["value"] is True
    assert [r["run_id"] for r in client.get(f"{API}/agents/{agent_id}/evals/runs", headers=h).json()["items"]] == [rid]
