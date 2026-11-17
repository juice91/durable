"""AC.12 implementer tests: the evals API (suites, cases, import/export, eval runs, results, compare).

No worker: eval runs are enqueued into a fake DBOS client, and results are written with
EvalStore.record_case_result, as the eval runtime (AC.11) will.
"""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.eval_format import (
    EvalFormatError,
    dataset_dict,
    dump_dataset,
    evaluator_from_spec,
    evaluator_to_spec,
    parse_file,
    read_dataset,
)
from eap_api.launcher import EVAL_WORKFLOW_NAME, DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_run_store.evals import CaseResultWrite, EvalStore
from eap_schemas.evals import EvalCase

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_workflow_runs_api import FakeClient
from .timing import best_of_3, cap

API = "/api/v1"
AGENT_SPEC = {"name": "Evaluated", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}
SIDE_EFFECT_SPEC = {**AGENT_SPEC, "tools": ["tickets.create", "clock.now"]}

ALL_EVALUATORS = [
    {"kind": "equals_expected"},
    {"kind": "contains", "value": "hi", "case_sensitive": False, "name": "has_hi"},
    {"kind": "contains", "name": "has_expected"},
    {"kind": "regex", "pattern": "^h.*$"},
    {"kind": "schema_valid", "json_schema": {"type": "object", "properties": {"a": {"type": "string"}}}},
    {"kind": "field_equals", "path": "a.0", "value": 1, "name": "fe"},
    {"kind": "max_duration", "seconds": 2.5},
    {"kind": "max_cost", "usd": "0.0003", "name": "tiny"},
    {"kind": "max_cost", "usd": "0.5", "name": "half"},
    {"kind": "llm_judge", "rubric": "Polite. MUST_CONTAIN:hi", "model": {"provider": "test", "name": "judge"},
     "include_input": True, "include_expected": True, "pass_threshold": 0.5},
]


# ---------------------------------------------------------------------------- format (no database)


def _canon(evaluators):
    return [evaluator_from_spec(evaluator_to_spec(_model(e)), "x") for e in evaluators]


def _model(e):
    from pydantic import TypeAdapter

    from eap_schemas.evals import EvaluatorSpec

    return TypeAdapter(EvaluatorSpec).validate_python(e)


def test_every_evaluator_round_trips_through_the_spec_form():
    for e in ALL_EVALUATORS:
        model = _model(e)
        assert evaluator_from_spec(evaluator_to_spec(model), "x") == model.model_dump(mode="json", exclude_none=True)


def test_spec_forms_follow_pydantic_evals():
    assert evaluator_to_spec(_model({"kind": "equals_expected"})) == "EqualsExpected"
    assert evaluator_to_spec(_model({"kind": "equals_expected", "name": "eq"})) == {
        "EqualsExpected": {"evaluation_name": "eq"}
    }
    # A judge with a threshold is ours (stock LLMJudge has none); the name rides in `assertion`.
    judge = evaluator_to_spec(_model(ALL_EVALUATORS[-1]))
    assert judge == {"ThresholdLLMJudge": {"rubric": "Polite. MUST_CONTAIN:hi", "model": "test:judge",
                                           "include_input": True, "include_expected_output": True,
                                           "pass_threshold": 0.5}}
    plain_judge = {"kind": "llm_judge", "rubric": "r", "model": {"provider": "test", "name": "judge"}, "name": "j"}
    assert evaluator_to_spec(_model(plain_judge)) == {
        "LLMJudge": {"rubric": "r", "model": "test:judge", "assertion": {"evaluation_name": "j"}}
    }
    # Stock MaxDuration takes no name; a named one is ours. Contains without a value is ours.
    assert evaluator_to_spec(_model({"kind": "max_duration", "seconds": 2})) == {"MaxDuration": {"seconds": 2.0}}
    assert evaluator_to_spec(_model({"kind": "max_duration", "seconds": 2, "name": "fast"})) == {
        "NamedMaxDuration": {"seconds": 2.0, "evaluation_name": "fast"}
    }
    assert evaluator_to_spec(_model({"kind": "contains"})) == "ContainsExpected"
    assert evaluator_to_spec(_model({"kind": "max_cost", "usd": "0.5"})) == {"MaxCost": {"usd": 0.5}}
    # Short form: the first positional argument.
    assert evaluator_from_spec({"Contains": "x"}, "p") == {"kind": "contains", "value": "x", "case_sensitive": True}
    assert evaluator_from_spec({"MaxDuration": 3}, "p")["seconds"] == 3.0


@pytest.mark.parametrize(
    "spec, message",
    [
        ("IsInstance", "unknown evaluator 'IsInstance'"),
        ({"Contains": {"value": "x", "as_strings": True}}, "Contains"),
        ({"EqualsExpected": 1}, "takes no positional argument"),
        ({"A": 1, "B": 2}, "an evaluator is"),
        ({"LLMJudge": {"rubric": "r", "model": "nocolon"}}, "provider:name"),
        ({"Regex": {"pattern": "x", "kind": "contains"}}, "unknown argument 'kind'"),
    ],
)
def test_bad_evaluator_specs(spec, message):
    with pytest.raises(EvalFormatError) as info:
        evaluator_from_spec(spec, "evaluators[0]")
    assert message in str(info.value)
    assert "evaluators[0]" in str(info.value)


def test_yaml_safe_load_only_and_no_aliases():
    with pytest.raises(EvalFormatError, match="unsupported YAML tag"):
        parse_file("cases: !!python/object/apply:os.system ['true']\n", "yaml")
    with pytest.raises(EvalFormatError, match="aliases"):
        parse_file("a: &x [1, 2]\nb: *x\n", "yaml")
    with pytest.raises(EvalFormatError, match="not valid YAML"):
        parse_file("a: [1, 2\n", "yaml")
    with pytest.raises(EvalFormatError, match="not valid JSON"):
        parse_file("{", "json")


def test_read_dataset_rules():
    with pytest.raises(EvalFormatError, match="unknown key 'extra'"):
        read_dataset({"cases": [], "extra": 1})
    with pytest.raises(EvalFormatError, match="needs a name"):
        read_dataset({"cases": [{"inputs": "x"}]})
    with pytest.raises(EvalFormatError, match="inputs must be text"):
        read_dataset({"cases": [{"name": "a", "inputs": {"q": 1}}]})
    with pytest.raises(EvalFormatError, match="report evaluators"):
        read_dataset({"cases": [], "report_evaluators": ["ConfusionMatrix"]})
    cases, evaluators = read_dataset({"$schema": "x", "name": "n", "cases": [{"name": "a", "inputs": "q"}]})
    assert evaluators is None
    assert cases == [{"name": "a", "input": "q", "expected_output": None, "metadata": {}, "evaluators": []}]


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_dataset_file_round_trip(fmt):
    cases = [
        EvalCase(name="one", input="Say hi", expected_output={"a": ["hi", 1.5, None]}, metadata={"k": "v", "n": 2},
                 evaluators=[ALL_EVALUATORS[1]]),
        EvalCase(name="two: tricky", input="multi\nline ünïcode", expected_output="hi"),
    ]
    suite_evaluators = [_model(e) for e in ALL_EVALUATORS[:1] + ALL_EVALUATORS[2:]]
    content = dump_dataset(dataset_dict("S", suite_evaluators, cases), fmt)
    got_cases, got_evaluators = read_dataset(parse_file(content, fmt))
    assert [EvalCase.model_validate(c) for c in got_cases] == cases
    assert got_evaluators == [e.model_dump(mode="json", exclude_none=True) for e in suite_evaluators]


# ---------------------------------------------------------------------------- API (Postgres)


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def api(fake):
    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("needs Postgres")
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    with hard_timeout(120), TestClient(app) as client:
        yield client, app, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM eval_case_results WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM eval_suites WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
    engine.dispose()


def _agent(client, headers, spec=AGENT_SPEC, publish=True) -> str:
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    if publish:
        assert client.post(f"{API}/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def _suite(client, headers, agent_id, name="smoke", evaluators=None, cases=("a", "b")) -> str:
    r = client.post(
        f"{API}/agents/{agent_id}/eval-suites",
        json={"name": name, "evaluators": evaluators if evaluators is not None else [{"kind": "equals_expected"}]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    for c in cases:
        r = client.post(
            f"{API}/eval-suites/{suite_id}/cases", json={"name": c, "input": f"in {c}", "expected_output": c},
            headers=headers,
        )
        assert r.status_code == 201, r.text
    return suite_id


pg = requires_pg


@pg
def test_suite_and_case_crud(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, cases=())

    r = client.get(f"{API}/agents/{agent_id}/eval-suites", headers=h)
    assert [s["id"] for s in r.json()["items"]] == [suite_id]
    assert r.json()["items"][0]["case_count"] == 0

    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "smoke"}, headers=h)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]

    r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c1", "input": "hi", "metadata": {"k": 1}},
                    headers=h)
    assert r.status_code == 201
    case = r.json()
    assert case["position"] == 0 and case["metadata"] == {"k": 1} and case["expected_output"] is None
    r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c1", "input": "x"}, headers=h)
    assert r.status_code == 409
    # Evaluator names clash across the suite's and the case's.
    client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": [{"kind": "equals_expected", "name": "e"}]},
                 headers=h)
    r = client.post(f"{API}/eval-suites/{suite_id}/cases",
                    json={"name": "c2", "input": "x", "evaluators": [{"kind": "regex", "pattern": "a", "name": "e"}]},
                    headers=h)
    assert r.status_code == 422 and "duplicate evaluator name" in r.json()["detail"]
    r = client.post(f"{API}/eval-suites/{suite_id}/cases",
                    json={"name": "c2", "input": "x", "evaluators": [{"kind": "nope"}]}, headers=h)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)

    r = client.patch(f"{API}/eval-suites/{suite_id}/cases/{case['id']}", json={"expected_output": {"a": 1}}, headers=h)
    assert r.status_code == 200 and r.json()["expected_output"] == {"a": 1} and r.json()["input"] == "hi"
    r = client.get(f"{API}/eval-suites/{suite_id}", headers=h)
    assert r.json()["case_count"] == 1 and r.json()["cases"][0]["expected_output"] == {"a": 1}

    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"archived": True}, headers=h)
    assert r.json()["archived"] is True
    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"name": "x"}, headers=h)
    assert r.status_code == 409 and "archived" in r.json()["detail"]
    assert client.get(f"{API}/agents/{agent_id}/eval-suites", headers=h).json()["items"] == []
    assert len(client.get(f"{API}/agents/{agent_id}/eval-suites?include_archived=true", headers=h).json()["items"]) == 1
    client.patch(f"{API}/eval-suites/{suite_id}", json={"archived": False}, headers=h)

    assert client.delete(f"{API}/eval-suites/{suite_id}/cases/{case['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/eval-suites/{suite_id}/cases/{case['id']}", headers=h).status_code == 404
    assert client.delete(f"{API}/eval-suites/{suite_id}", headers=h).status_code == 204
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).status_code == 404


@pg
def test_suite_id_idempotency(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, h2 = tenant_of()
    agent_id = _agent(client, h)
    sid = str(uuid.uuid4())
    body = {"suite_id": sid, "name": "s"}
    assert client.post(f"{API}/agents/{agent_id}/eval-suites", json=body, headers=h).status_code == 201
    assert client.post(f"{API}/agents/{agent_id}/eval-suites", json=body, headers=h).status_code == 200
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={**body, "name": "t"}, headers=h)
    assert r.status_code == 409
    other_agent = _agent(client, h2)
    r = client.post(f"{API}/agents/{other_agent}/eval-suites", json=body, headers=h2)
    assert r.status_code == 409 and r.json()["detail"] == "suite_id is already in use"


@pg
def test_cross_tenant_reads_as_not_found(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    case_id = client.get(f"{API}/eval-suites/{suite_id}/cases", headers=h).json()["items"][0]["id"]
    run = client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id},
                      headers=h).json()
    checks = [
        ("get", f"/agents/{agent_id}/eval-suites", None),
        ("post", f"/agents/{agent_id}/eval-suites", {"name": "x"}),
        ("get", f"/eval-suites/{suite_id}", None),
        ("patch", f"/eval-suites/{suite_id}", {"name": "x"}),
        ("delete", f"/eval-suites/{suite_id}", None),
        ("get", f"/eval-suites/{suite_id}/cases", None),
        ("post", f"/eval-suites/{suite_id}/cases", {"name": "z", "input": "z"}),
        ("get", f"/eval-suites/{suite_id}/cases/{case_id}", None),
        ("patch", f"/eval-suites/{suite_id}/cases/{case_id}", {"input": "z"}),
        ("delete", f"/eval-suites/{suite_id}/cases/{case_id}", None),
        ("get", f"/eval-suites/{suite_id}/export", None),
        ("post", f"/agents/{agent_id}/evals/runs", {"run_id": str(uuid.uuid4()), "suite_id": suite_id}),
        ("get", f"/agents/{agent_id}/evals/runs", None),
        ("get", f"/agents/{agent_id}/evals/runs/{run['run_id']}", None),
        ("get", f"/agents/{agent_id}/evals/runs/{run['run_id']}/results", None),
        ("get", f"/agents/{agent_id}/evals/compare?base={run['run_id']}&target={run['run_id']}", None),
        ("get", f"/agents/{agent_id}/evals/warnings", None),
    ]
    for method, path, body in checks:
        r = client.request(method, API + path, json=body, headers=other)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=b"cases: []\n", headers=other)
    assert r.status_code == 404
    # Nothing changed for the owner.
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["case_count"] == 2


@pg
def test_export_import_round_trip(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, evaluators=[e for e in ALL_EVALUATORS if e.get("name") != "has_hi"],
                      cases=())
    for i, name in enumerate(["first", "second", "third"]):
        r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={
            "name": name, "input": f"input {i}\nünï", "expected_output": {"n": i, "l": [True, None]},
            "metadata": {"i": i, "tag": "x"}, "evaluators": [ALL_EVALUATORS[1]] if i == 1 else [],
        }, headers=h)
        assert r.status_code == 201, r.text
    original = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()

    for fmt in ("yaml", "json"):
        r = client.get(f"{API}/eval-suites/{suite_id}/export?format={fmt}", headers=h)
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f'attachment; filename="smoke.{fmt}"'
        data = yaml.safe_load(r.text) if fmt == "yaml" else json.loads(r.text)
        assert data["name"] == "smoke" and [c["name"] for c in data["cases"]] == ["first", "second", "third"]
        assert data["cases"][1]["evaluators"] == [{"Contains": {"value": "hi", "case_sensitive": False,
                                                                "evaluation_name": "has_hi"}}]

        target_id = _suite(client, h, agent_id, name=f"copy-{fmt}", evaluators=[], cases=())
        ctype = "application/json" if fmt == "json" else "application/yaml"
        r = client.post(f"{API}/eval-suites/{target_id}/import", content=r.content, headers={**h, "content-type": ctype})
        assert r.status_code == 200, r.text
        assert (r.json()["created"], r.json()["updated"]) == (3, 0)
        copy = client.get(f"{API}/eval-suites/{target_id}", headers=h).json()
        assert copy["evaluators"] == original["evaluators"]
        strip = lambda cs: [{k: c[k] for k in ("name", "input", "expected_output", "metadata", "evaluators", "position")}
                            for c in cs]  # noqa: E731
        assert strip(copy["cases"]) == strip(original["cases"])

    # Re-import: upsert by name keeps ids and positions, adds new ones at the end.
    r = client.post(
        f"{API}/eval-suites/{suite_id}/import?format=yaml",
        content="cases:\n  - name: second\n    inputs: changed\n  - name: fourth\n    inputs: new\n".encode(),
        headers=h,
    )
    assert r.status_code == 200 and (r.json()["created"], r.json()["updated"]) == (1, 1)
    cases = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["cases"]
    assert [(c["name"], c["position"]) for c in cases] == [("first", 0), ("second", 1), ("third", 2), ("fourth", 3)]
    assert cases[1]["id"] == original["cases"][1]["id"] and cases[1]["input"] == "changed"
    # No `evaluators` key in the file: the suite's are kept.
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["evaluators"] == original["evaluators"]


@pg
def test_import_rejections(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    url = f"{API}/eval-suites/{suite_id}/import"

    r = client.post(url, content=b"cases:\n  - name: x\n    inputs: y\n    evaluators: [IsInstance]\n", headers=h)
    assert r.status_code == 422 and "unknown evaluator 'IsInstance'" in r.json()["detail"]
    assert "cases[0].evaluators[0]" in r.json()["detail"]
    big = b"cases: []\n#" + b"x" * (1024 * 1024)
    r = client.post(url, content=big, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "the suite file must be at most 1 MiB"
    r = client.post(url, content=b"cases: !!python/object/apply:os.system ['true']\n", headers=h)
    assert r.status_code == 422 and "unsupported YAML tag" in r.json()["detail"]
    r = client.post(url, content=b"\xff\xfe", headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "the suite file must be UTF-8 text"
    r = client.post(url, content=b'cases:\n  - name: "n\\0"\n    inputs: y\n', headers=h)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    # A bad case anywhere rejects the whole file (all or nothing).
    r = client.post(url, content=b"cases:\n  - name: new\n    inputs: y\n  - name: bad\n    inputs: ''\n", headers=h)
    assert r.status_code == 422
    names = [c["name"] for c in client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["cases"]]
    assert names == ["a", "b"]
    # Just under 1 MiB is accepted by the size check (the plain route, not the 256 KiB JSON guard).
    padded = b"cases: []\n#" + b"x" * (600 * 1024) + b"\n"
    assert client.post(url, content=padded, headers=h).status_code == 200


@pg
def test_eval_run_creation_is_idempotent_and_enqueues(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h, spec=SIDE_EFFECT_SPEC)
    suite_id = _suite(client, h, agent_id)
    run_id = str(uuid.uuid4())
    body = {"run_id": run_id, "suite_id": suite_id, "version": 1, "max_concurrency": 2}

    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["run_id"] == run_id and out["version"] == 1 and out["draft"] is False
    assert out["suite_id"] == suite_id and out["cases"] == 2 and out["max_concurrency"] == 2
    assert out["status"] == "queued" and len(out["suite_hash"]) == 64
    assert out["warnings"] == [{"code": "tool_side_effect", "tool": "tickets.create", "kind": "builtin",
                                "message": "Tool tickets.create has side effects; every case that calls it runs it for real"}]
    options, args = fake.calls[-1]
    assert options["workflow_name"] == EVAL_WORKFLOW_NAME and options["workflow_id"] == run_id
    assert options["queue_name"] == "eap-agent-runs"
    assert args[0] == run_id and args[1] == tenant
    snapshot = json.loads(args[2])
    assert snapshot["suite_id"] == suite_id and [c["name"] for c in snapshot["cases"]] == ["a", "b"]

    # The same request again: 200 and a second enqueue (DBOS attaches to the same workflow).
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
    assert r.status_code == 200 and r.json()["run_id"] == run_id and len(fake.calls) == 2
    # A suite edit after creation never changes the recorded run.
    client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c", "input": "x"}, headers=h)
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
    assert r.status_code == 200 and r.json()["cases"] == 2
    for change in ({"max_concurrency": 3}, {"version": "draft"}, {"suite_id": _suite(client, h, agent_id, name="o")}):
        r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={**body, **change}, headers=h)
        assert r.status_code == 409 and r.json()["detail"] == "run_id is already used by a different run request"
    # A run id used by an ordinary agent run.
    run_store, _ = app.state.run_stores
    other = uuid.uuid4()
    run_store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", other)
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={**body, "run_id": str(other)}, headers=h)
    assert r.status_code == 409


@pg
def test_eval_run_targets_and_errors(api, fake):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h, publish=False)
    suite_id = _suite(client, h, agent_id)
    url = f"{API}/agents/{agent_id}/evals/runs"
    new = lambda **kw: {"run_id": str(uuid.uuid4()), "suite_id": suite_id, **kw}  # noqa: E731

    r = client.post(url, json=new(), headers=h)
    assert r.status_code == 409 and r.json()["detail"] == "Agent has no published version"
    r = client.post(url, json=new(version="draft"), headers=h)
    assert r.status_code == 201 and r.json()["draft"] is True and r.json()["version"] is None
    assert fake.calls[-1][0]["workflow_name"] == EVAL_WORKFLOW_NAME
    r = client.post(url, json=new(version=7), headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "Version not found"
    r = client.post(url, json={**new(version="draft"), "suite_id": str(uuid.uuid4())}, headers=h)
    assert r.status_code == 404 and r.json()["detail"].startswith("Test suite not found")
    r = client.post(url, json=new(version="draft", max_concurrency=9), headers=h)
    assert r.status_code == 422

    # Another agent's suite is a 404.
    other_agent = _agent(client, h, publish=False)
    r = client.post(f"{API}/agents/{other_agent}/evals/runs", json=new(version="draft"), headers=h)
    assert r.status_code == 404 and r.json()["detail"].startswith("Test suite not found")
    # An empty suite, an archived suite.
    empty = _suite(client, h, agent_id, name="empty", cases=())
    r = client.post(url, json={**new(version="draft"), "suite_id": empty}, headers=h)
    assert r.status_code == 422 and "no cases" in r.json()["detail"]
    client.patch(f"{API}/eval-suites/{suite_id}", json={"archived": True}, headers=h)
    r = client.post(url, json=new(version="draft"), headers=h)
    assert r.status_code == 409 and "archived" in r.json()["detail"]


@pg
def test_judge_model_allowlist(api, monkeypatch):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    judge = {"kind": "llm_judge", "rubric": "r", "model": {"provider": "openai", "name": "gpt-5"}}
    suite_id = _suite(client, h, agent_id, evaluators=[judge])
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id},
                    headers=h)
    assert r.status_code == 422 and r.json()["detail"].startswith("judge model at evaluators:")


@pg
def test_enqueue_failure_then_retry(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    body = {"run_id": str(uuid.uuid4()), "suite_id": suite_id}
    fake.fail_times = 1
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
    assert r.status_code == 503
    run_store, _ = app.state.run_stores
    assert run_store.get_run(tenant, uuid.UUID(body["run_id"]))["status"] == "failed"
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "queued"


@pg
def test_warnings_endpoint(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    gated = {**SIDE_EFFECT_SPEC, "approvals": {"tools": ["tickets.create"]}}
    agent_id = _agent(client, h, spec=SIDE_EFFECT_SPEC)
    r = client.get(f"{API}/agents/{agent_id}/evals/warnings", headers=h)
    assert r.status_code == 200 and [w["tool"] for w in r.json()["warnings"]] == ["tickets.create"]
    assert r.json()["version"] == 1
    gated_id = _agent(client, h, spec=gated)
    assert client.get(f"{API}/agents/{gated_id}/evals/warnings?version=draft", headers=h).json()["warnings"] == []
    assert client.get(f"{API}/agents/{agent_id}/evals/warnings?version=x", headers=h).status_code == 422


def _record(app, tenant, run_id, statuses, scores=None):
    store = EvalStore(app.state.run_stores[0].engine)
    snapshot = json.loads(app.state.run_stores[0].get_run(tenant, uuid.UUID(run_id))["input"])
    for case, st in zip(snapshot["cases"], statuses):
        store.record_case_result(tenant, run_id, CaseResultWrite(
            case_id=case["id"], case_name=case["name"], position=case["position"], status=st,
            assertions={"eq": {"value": st == "passed", "reason": None}},
            scores={"s": {"value": (scores or {}).get(case["name"], 0.5), "reason": None}},
            duration_ms=100, error_code="agent_failed" if st == "error" else None,
        ))


@pg
def test_results_list_and_compare(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    r = client.patch(f"{API}/agents/{agent_id}", json={"spec": {**AGENT_SPEC, "instructions": "Help more."}}, headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201  # v2
    suite_id = _suite(client, h, agent_id)
    url = f"{API}/agents/{agent_id}/evals/runs"
    base = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}, headers=h).json()
    target = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 2}, headers=h).json()
    _record(app, tenant, base["run_id"], ["passed", "failed"])
    _record(app, tenant, target["run_id"], ["failed", "passed"], scores={"a": 1.0})

    r = client.get(f"{url}/{base['run_id']}/results", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["case_name"], i["status"]) for i in items] == [("a", "passed"), ("b", "failed")]
    assert items[0]["cost_usd"] == "0.0000000000"
    r = client.get(f"{url}/{base['run_id']}", headers=h)
    assert r.json()["aggregates"]["passed"] == 1 and r.json()["aggregates"]["pass_rate"] == 0.5
    assert "input" not in r.json() and r.json()["suite_hash"] == base["suite_hash"]
    runs = client.get(f"{url}?suite_id={suite_id}", headers=h).json()["items"]
    assert [x["run_id"] for x in runs] == [target["run_id"], base["run_id"]]
    assert runs[0]["aggregates"]["cases"] == 2

    r = client.get(f"{API}/agents/{agent_id}/evals/compare?base={base['run_id']}&target={target['run_id']}", headers=h)
    assert r.status_code == 200, r.text
    cmp = r.json()
    assert cmp["same_suite"] is True and cmp["warnings"] == []
    assert (cmp["base_version"], cmp["target_version"]) == (1, 2)
    assert [(c["name"], c["change"]) for c in cmp["cases"]] == [("a", "regressed"), ("b", "fixed")]
    assert cmp["cases"][0]["score_deltas"] == {"s": 0.5}

    # A different suite: a warning. Another agent's run: 404.
    client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c", "input": "x"}, headers=h)
    third = client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id}, headers=h).json()
    r = client.get(f"{API}/agents/{agent_id}/evals/compare?base={base['run_id']}&target={third['run_id']}", headers=h)
    assert r.json()["same_suite"] is False and r.json()["warnings"]
    other_agent = _agent(client, h)
    other_suite = _suite(client, h, other_agent)
    foreign = client.post(f"{API}/agents/{other_agent}/evals/runs",
                          json={"run_id": str(uuid.uuid4()), "suite_id": other_suite}, headers=h).json()
    for a, b in ((base["run_id"], foreign["run_id"]), (foreign["run_id"], base["run_id"])):
        r = client.get(f"{API}/agents/{agent_id}/evals/compare?base={a}&target={b}", headers=h)
        assert r.status_code == 404
    assert client.get(f"{url}/{foreign['run_id']}", headers=h).status_code == 404
    assert client.get(f"{url}/{foreign['run_id']}/results", headers=h).status_code == 404


def test_field_equals_null_value_survives_the_reader():
    # `value: null` is a value to compare with (FieldEquals.value is required).
    out = evaluator_from_spec({"FieldEquals": {"path": "a", "value": None}}, "x")
    assert out == {"kind": "field_equals", "path": "a", "value": None}
    assert _model(out).value is None


@pg
def test_json_routes_keep_the_body_guard(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "x" * (300 * 1024)}, headers=h)
    assert r.status_code == 413
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "a\u0000b"}, headers=h)
    assert r.status_code == 422


@pg
def test_field_equals_null_value_round_trips_through_the_api(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    fe = {"kind": "field_equals", "path": "a", "value": None, "name": "fe"}
    suite_id = _suite(client, h, agent_id, evaluators=[fe], cases=())
    r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={
        "name": "c", "input": "x", "evaluators": [{"kind": "field_equals", "path": "b", "value": None}]}, headers=h)
    assert r.status_code == 201, r.text
    suite = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()
    assert suite["evaluators"] == [fe]
    assert suite["cases"][0]["evaluators"] == [{"kind": "field_equals", "path": "b", "value": None}]
    exported = client.get(f"{API}/eval-suites/{suite_id}/export", headers=h)
    assert yaml.safe_load(exported.text)["evaluators"] == [{"FieldEquals": {"path": "a", "value": None,
                                                                            "evaluation_name": "fe"}}]
    copy_id = _suite(client, h, agent_id, name="copy", evaluators=[], cases=())
    r = client.post(f"{API}/eval-suites/{copy_id}/import", content=exported.content, headers=h)
    assert r.status_code == 200, r.text
    copy = client.get(f"{API}/eval-suites/{copy_id}", headers=h).json()
    assert copy["evaluators"] == suite["evaluators"]
    assert copy["cases"][0]["evaluators"] == suite["cases"][0]["evaluators"]
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite_id},
                    headers=h)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------- review fixes (AC.12)

import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

MiB = 1024 * 1024
# The reviewer's hostile files (AC.12 review): each cost 10-76 s with the pure-Python loader.
HOSTILE = {
    "flow nesting 4 KB": "cases: " + "[" * 2000 + "]" * 2000,
    "flow nesting 40 KB": "cases: " + "[" * 20000 + "]" * 20000,
    "flow nesting 1 MiB": "cases: " + "[" * (MiB // 2 - 8) + "]" * (MiB // 2 - 8),
    "indent nesting": "".join(" " * i + f"k{i}:\n" for i in range(200)) + " " * 200 + "x\n",
    "dash nesting": "cases:\n" + "- " * 500 + "x\n",
    "flat flow 1 MiB": "cases: [" + ",".join(["1"] * (MiB // 2 - 8)) + "]",
    "dense nested flow 1 MiB": "cases: [" + ",".join(["[1,2,[3,[4]]]"] * 70_000) + "]",
    "flat block 1 MiB": "cases:\n" + "- 1\n" * (MiB // 4 - 8),
    "alias bomb": "a: &a [1]\n" + "".join(f"{c}: &{c} [*{p},*{p},*{p},*{p},*{p}]\n"
                                         for p, c in zip("abcdefgh", "bcdefghi")),
    "many aliases": "a: &x [1]\nb: [" + ",".join(["*x"] * 50_000) + "]",
}


def _cpu(fn):
    start = time.process_time()
    try:
        return fn(), time.process_time() - start
    except EvalFormatError as exc:
        return exc, time.process_time() - start


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_yaml_fails_fast(name):
    result, cpu = _cpu(lambda: parse_file(HOSTILE[name], "yaml"))
    assert isinstance(result, EvalFormatError), name
    assert cpu < 0.5, (name, cpu)


def test_worst_case_yaml_within_the_caps_parses_under_two_seconds():
    """Files just inside every prescan cap (the most expensive ones the import will parse)."""
    n = 99_000
    worst = {
        "flow mapping": "cases: []\nx: {" + ",".join(f"k{i}: 1" for i in range(n)) + "}",
        "flow list": "cases: [" + ",".join(['"' + "a" * 6 + '"'] * n) + "]",
        "block mapping": "cases: []\nx:\n" + "".join(f"  k{i}: {'v' * 10}\n" for i in range(49_000)),
        "block flow maps": "cases: []\nx:\n" + "".join(f"  - {{a: {i}}}\n" for i in range(40_000)),
    }
    for name, text in worst.items():
        assert len(text.encode()) <= MiB, name
        result, cpu = best_of_3(lambda: _cpu(lambda: parse_file(text, "yaml"))[0])
        assert not isinstance(result, EvalFormatError), (name, result)
        assert cpu < cap(2.0), (name, cpu)


def _suite_file(n_cases: int = 500, prompt_chars: int = 1800) -> str:
    case = (
        "  - name: case{i}\n    inputs: \"{p}, with commas, [brackets] and *stars*\"\n"
        "    expected_output:\n      answer: hi\n      n: {i}\n      tags: [a, b, c]\n"
        "    metadata: {{area: chat, i: {i}}}\n"
        "    evaluators:\n      - Contains: {{value: hi, evaluation_name: c{i}}}\n"
    )
    return "evaluators: [EqualsExpected]\ncases:\n" + "".join(
        case.format(i=i, p="x" * prompt_chars) for i in range(n_cases)
    )


def test_normal_500_case_suite_parses_quickly():
    text = _suite_file()
    assert 0.8 * MiB < len(text.encode()) < MiB
    result, cpu = _cpu(lambda: read_dataset(parse_file(text, "yaml")))
    assert not isinstance(result, EvalFormatError), result
    assert len(result[0]) == 500 and cpu < 1.0


def test_prescan_counts_only_flow_commas():
    # Prose with many commas in block context is fine; the same count inside a flow list is not.
    prose = "cases:\n  - name: a\n    inputs: \"" + "word, " * 170_000 + "\"\n"
    assert parse_file(prose, "yaml")["cases"][0]["name"] == "a"
    with pytest.raises(EvalFormatError, match="more than 100000 values"):
        parse_file("cases: [" + ",".join(["w"] * 170_000) + "]", "yaml")


@pytest.mark.parametrize(
    "spec, expected",
    [
        # Stock LLMJudge files: assertion.evaluation_name is our name; score and include_reason ignored.
        ({"LLMJudge": {"rubric": "r", "model": "test:judge", "assertion": {"evaluation_name": "j",
                                                                           "include_reason": True},
                       "score": {"evaluation_name": "s"}}},
         {"kind": "llm_judge", "rubric": "r", "model": {"provider": "test", "name": "judge"}, "name": "j",
          "include_input": False, "include_expected": False}),
        ({"LLMJudge": {"rubric": "r", "model": "test:judge", "score": False}},
         {"kind": "llm_judge", "rubric": "r", "model": {"provider": "test", "name": "judge"},
          "include_input": False, "include_expected": False}),
        ({"Contains": {"value": "x", "as_strings": False}}, {"kind": "contains", "value": "x", "case_sensitive": True}),
        ({"ContainsExpected": {"case_sensitive": False}}, {"kind": "contains", "case_sensitive": False}),
        ({"NamedMaxDuration": {"seconds": 1, "evaluation_name": "fast"}},
         {"kind": "max_duration", "seconds": 1.0, "name": "fast"}),
    ],
)
def test_stock_pydantic_evals_forms_import(spec, expected):
    assert evaluator_from_spec(spec, "x") == expected


@pytest.mark.parametrize(
    "spec, message",
    [
        ({"LLMJudge": {"rubric": "r"}}, "model is required"),
        ({"LLMJudge": {"rubric": "r", "model": "test:judge", "model_settings": {"temperature": 0}}},
         "model_settings is not supported"),
        ({"LLMJudge": {"rubric": "r", "model": "test:judge", "assertion": False}}, "assertion: false"),
        ({"LLMJudge": {"rubric": "r", "model": "test:judge", "pass_threshold": 0.5}}, "use ThresholdLLMJudge"),
        ({"Contains": {"value": "x", "as_strings": True}}, "as_strings: true"),
        ({"MaxDuration": {"seconds": 1, "evaluation_name": "n"}}, "use NamedMaxDuration"),
        ({"ContainsExpected": {"value": "x"}}, "takes no value"),
    ],
)
def test_unsupported_stock_forms_are_clear_errors(spec, message):
    with pytest.raises(EvalFormatError, match=message):
        evaluator_from_spec(spec, "x")


LOADER = Path(__file__).with_name("pydantic_evals_load.py")


def _real_pydantic_evals(path: Path) -> dict:
    """Load `path` with the real pydantic_evals 2.67.0 (offline, from the uv cache, outside the
    project environment). Skips when uv or the cached wheel is not available."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    proc = subprocess.run(
        [uv, "run", "--no-project", "--offline", "--with", "pydantic-evals==2.67.0", "python", str(LOADER), str(path)],
        capture_output=True, text=True, timeout=120, cwd=path.parent,
    )
    if proc.returncode != 0 and ("No solution found" in proc.stderr or "offline" in proc.stderr.lower()):
        pytest.skip("pydantic-evals 2.67.0 is not in the uv cache")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


EXPORT_EVALUATORS = [
    {"kind": "equals_expected"},
    {"kind": "equals_expected", "name": "eq"},
    {"kind": "contains", "value": "hi", "case_sensitive": False, "name": "has_hi"},
    {"kind": "contains", "name": "has_expected"},
    {"kind": "regex", "pattern": "^h.*$", "name": "rx"},
    {"kind": "schema_valid", "json_schema": {"type": "object"}},
    {"kind": "field_equals", "path": "a.0", "value": None, "name": "fe"},
    {"kind": "max_duration", "seconds": 2.5},
    {"kind": "max_duration", "seconds": 3, "name": "fast"},
    {"kind": "max_cost", "usd": "0.0003", "name": "tiny"},
]
EXPORT_JUDGES = [
    {"kind": "llm_judge", "rubric": "r", "model": {"provider": "test", "name": "judge"}, "name": "polite",
     "include_input": True, "include_expected": True},
    {"kind": "llm_judge", "rubric": "r2", "model": {"provider": "test", "name": "judge"}, "pass_threshold": 0.5,
     "name": "t"},
    {"kind": "llm_judge", "rubric": "r3", "model": {"provider": "test", "name": "judge"}},
]


@pytest.mark.parametrize("fmt", ["yaml", "json"])
def test_export_loads_with_real_pydantic_evals(fmt, tmp_path):
    """Every evaluator form we write loads with pydantic_evals 2.67.0's Dataset loader (stock
    classes as stock, ours through custom_evaluator_types as the eval_format docstring defines)."""
    suite_evaluators = [_model(e) for e in EXPORT_EVALUATORS]
    cases = [EvalCase(name="c1", input="x", evaluators=EXPORT_JUDGES),
             EvalCase(name="c2", input="y", expected_output={"a": 1}, metadata={"k": "v"})]
    path = tmp_path / f"suite.{fmt}"
    path.write_text(dump_dataset(dataset_dict("S", suite_evaluators, cases), fmt), encoding="utf-8")
    loaded = _real_pydantic_evals(path)
    assert [name for name, _ in loaded["evaluators"]] == [
        "EqualsExpected", "EqualsExpected", "Contains", "ContainsExpected", "Regex", "SchemaValid", "FieldEquals",
        "MaxDuration", "NamedMaxDuration", "MaxCost",
    ]
    judges = loaded["cases"][0]["evaluators"]
    assert [name for name, _ in judges] == ["LLMJudge", "ThresholdLLMJudge", "LLMJudge"]
    assert judges[0][1]["assertion"] == {"evaluation_name": "polite"}
    assert judges[0][1]["include_expected_output"] is True
    assert [c["name"] for c in loaded["cases"]] == ["c1", "c2"]
    # And our reader gets back exactly what we wrote.
    got_cases, got_evaluators = read_dataset(parse_file(path.read_text(encoding="utf-8"), fmt))
    assert got_evaluators == [e.model_dump(mode="json", exclude_none=True) | (
        {"value": None} if e.kind == "field_equals" else {}) for e in suite_evaluators]
    assert [EvalCase.model_validate(c) for c in got_cases] == cases


@pg
def test_import_of_a_500_case_suite_and_a_hostile_file(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, evaluators=[], cases=())
    url = f"{API}/eval-suites/{suite_id}/import"
    start = time.monotonic()
    r = client.post(url, content=_suite_file().encode(), headers=h)
    assert r.status_code == 200, r.text[:500]
    assert (r.json()["created"], r.json()["updated"]) == (500, 0)
    assert r.json()["suite"]["case_count"] == 500
    assert time.monotonic() - start < 30  # includes the database writes on the shared dev Postgres
    for name in ("flow nesting 40 KB", "flat flow 1 MiB", "alias bomb"):
        start = time.monotonic()
        r = client.post(url, content=HOSTILE[name].encode(), headers=h)
        assert r.status_code == 422, (name, r.text[:300])
        assert time.monotonic() - start < 2, name
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["case_count"] == 500


@pg
def test_import_checks_the_suite_before_parsing(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    bad = HOSTILE["flow nesting 40 KB"].encode()
    assert client.post(f"{API}/eval-suites/{uuid.uuid4()}/import", content=bad, headers=h).status_code == 404
    assert client.post(f"{API}/eval-suites/{suite_id}/import", content=bad, headers=other).status_code == 404
    client.patch(f"{API}/eval-suites/{suite_id}", json={"archived": True}, headers=h)
    r = client.post(f"{API}/eval-suites/{suite_id}/import", content=bad, headers=h)
    assert r.status_code == 409 and "archived" in r.json()["detail"]


@pg
def test_patch_null_evaluators_or_name_is_422(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, evaluators=[{"kind": "equals_expected", "name": "keep"}])
    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": None}, headers=h)
    assert r.status_code == 422 and r.json()["detail"].startswith("evaluators cannot be null")
    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"name": None}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "name cannot be null"
    assert client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["evaluators"] == [
        {"kind": "equals_expected", "name": "keep"}]
    r = client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": []}, headers=h)
    assert r.status_code == 200 and r.json()["evaluators"] == []


@pg
def test_concurrent_identical_suite_creates_answer_the_suite(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    body = {"suite_id": str(uuid.uuid4()), "name": "race"}
    barrier, codes = threading.Barrier(6), []

    def go():
        barrier.wait()
        codes.append(client.post(f"{API}/agents/{agent_id}/eval-suites", json=body, headers=h).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(codes) == [200] * 5 + [201], codes


@pg
def test_run_list_and_results_paging(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id, cases=("a", "b", "c", "d", "e"))
    url = f"{API}/agents/{agent_id}/evals/runs"
    run_ids = [client.post(url, json={"run_id": str(uuid.uuid4()), "suite_id": suite_id}, headers=h).json()["run_id"]
               for _ in range(5)]
    _record(app, tenant, run_ids[0], ["passed", "failed", "passed", "error", "passed"])

    seen, cursor = [], None
    while True:
        r = client.get(url, params={"limit": 2, **({"before": cursor} if cursor else {})}, headers=h)
        assert r.status_code == 200
        seen += [x["run_id"] for x in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(run_ids) and len(seen) == 5
    first = next(x for x in client.get(url, params={"limit": 100}, headers=h).json()["items"]
                 if x["run_id"] == run_ids[0])
    assert first["aggregates"]["cases"] == 5 and first["aggregates"]["errors"] == 1
    assert client.get(url, params={"before": "nope"}, headers=h).status_code == 422

    names, cursor = [], None
    while True:
        r = client.get(f"{url}/{run_ids[0]}/results", params={"limit": 2, **({"after": cursor} if cursor else {})},
                       headers=h)
        assert r.status_code == 200
        names += [x["case_name"] for x in r.json()["items"]]
        cursor = r.json()["next_cursor"]
        if cursor is None:
            break
    assert names == ["a", "b", "c", "d", "e"]
    assert client.get(f"{url}/{run_ids[0]}/results", params={"after": "!!"}, headers=h).status_code == 422
    assert client.get(f"{url}/{run_ids[0]}/results", params={"limit": 501}, headers=h).status_code == 422


@pg
def test_invalid_regex_is_refused_on_every_write_without_echoing_the_pattern(api):
    """AC.11 review: a regex RE2 refuses is a 422 naming the evaluator path (fixed message, the
    pattern never echoed) on suite create and update, case add and update, and import."""
    from eap_api.routes.evals import REGEX_INVALID

    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    bad = [{"kind": "equals_expected"}, {"kind": "regex", "pattern": "zq("}]

    def refused(r, path: str) -> None:
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail == f"the test suite is invalid at {path}: {REGEX_INVALID}"
        assert "zq(" not in r.text

    refused(client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "s", "evaluators": bad}, headers=h),
            "evaluators.1.pattern")
    suite_id = _suite(client, h, agent_id, cases=("a",))
    refused(client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": bad}, headers=h), "evaluators.1.pattern")
    refused(client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c", "input": "x", "evaluators": bad[1:]},
                        headers=h), "case.evaluators.0.pattern")
    case_id = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()["cases"][0]["id"]
    refused(client.patch(f"{API}/eval-suites/{suite_id}/cases/{case_id}", json={"evaluators": bad[1:]}, headers=h),
            "case.evaluators.0.pattern")
    url = f"{API}/eval-suites/{suite_id}/import?format=yaml"
    refused(client.post(url, content=b"cases:\n  - name: n\n    inputs: y\n    evaluators: [{Regex: 'zq('}]\n", headers=h),
            "cases.n.evaluators.0.pattern")
    refused(client.post(url, content=b"cases: []\nevaluators: [{Regex: 'zq('}]\n", headers=h), "evaluators.0.pattern")
    # Nothing was written, and valid patterns (an empty-matching one included) are accepted.
    suite = client.get(f"{API}/eval-suites/{suite_id}", headers=h).json()
    assert [c["name"] for c in suite["cases"]] == ["a"] and suite["evaluators"] == [{"kind": "equals_expected"}]
    ok = [{"kind": "regex", "pattern": "^h.*$"}, {"kind": "regex", "pattern": "x*", "name": "empty_ok"}]
    assert client.patch(f"{API}/eval-suites/{suite_id}", json={"evaluators": ok}, headers=h).status_code == 200


@pg
def test_archived_agent_refuses_new_eval_runs_but_answers_a_recorded_one(api, fake):
    """As the run routes (eap_api.agent_archive): a new eval run of an archived agent is a 409
    AGENT_ARCHIVED and enqueues nothing; a retry of a run recorded before the archive is answered."""
    from eap_api.agent_archive import AGENT_ARCHIVED

    client, _, tenant_of = api
    _, h = tenant_of()
    agent_id = _agent(client, h)
    suite_id = _suite(client, h, agent_id)
    recorded = {"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}
    assert client.post(f"{API}/agents/{agent_id}/evals/runs", json=recorded, headers=h).status_code == 201
    assert client.post(f"{API}/agents/{agent_id}/archive", headers=h).status_code == 200
    before = len(fake.calls)
    for version in (1, "draft", None):
        body = {"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": version}
        r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h)
        assert r.status_code == 409 and r.json()["detail"] == AGENT_ARCHIVED
        assert client.get(f"{API}/runs/{body['run_id']}", headers=h).status_code == 404
    assert len(fake.calls) == before
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json=recorded, headers=h)
    assert r.status_code == 200 and r.json()["run_id"] == recorded["run_id"]
    assert client.post(f"{API}/agents/{agent_id}/unarchive", headers=h).status_code == 200
    body = {"run_id": str(uuid.uuid4()), "suite_id": suite_id, "version": 1}
    assert client.post(f"{API}/agents/{agent_id}/evals/runs", json=body, headers=h).status_code == 201
