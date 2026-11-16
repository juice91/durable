"""LP.16 acceptance (tester): the evaluate gate is open, the examples hold, the cost-cap warning.

Covers what the implementer's tests do not: evaluate at every depth through publish and run start
and through a real worker (alone and inside a loop and a foreach); what is still refused; mutated
examples; every guide snippet and field name checked against the schema; the judge cost-cap
warning matrix and the unchanged agent warnings; judge metrics, tenant-scoped.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from eap_api.routes import _cost_cap_warnings
from eap_schemas import AgentSpec
from eap_schemas.workflows import WorkflowSpec

from .run_helpers import requires_pg
from .test_runs import _no_hang as _hang, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures
from .test_workflow_loops_examples import (  # noqa: F401 - fixtures and helpers
    BASE,
    DEFAULT_PRICES,
    EXAMPLES,
    GUIDE,
    SNIPPET,
    TEST_PRICES,
    Stack,
    _models_for_the_stack,
    load_example,
    resolve_pointer,
    stack,
    with_agent_ids,
)
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures for run start

REPO = Path(__file__).resolve().parents[3]


def ref(path: str) -> dict[str, str]:
    return {"$ref": path}


def tool(sid: str, nxt: str | None = None) -> dict[str, Any]:
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": {}, "next": nxt}


def wf(*steps: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"name": "LP16", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps), **extra}


def ev(sid: str = "ev", nxt: str | None = None) -> dict[str, Any]:
    return {"id": sid, "type": "evaluate", "value": ref("input.text"),
            "evaluators": [{"kind": "contains", "value": "a"}], "next": nxt}


def loop(sid: str, body: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    kw.setdefault("until", {"op": "exists", "left": ref("input.a")})
    return {"id": sid, "type": "loop", "max_iterations": 3, "body": {"start": body[0]["id"], "steps": body}, **kw}


def foreach(sid: str, body: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": sid, "type": "foreach", "items": ref("input.items"), "max_items": 3,
            "body": {"start": body[0]["id"], "steps": body}}


# ------------------------------------------------------------------------ the evaluate gate is open

BRANCH_TO_EVAL = {"id": "br", "type": "branch", "default": "x",
                  "cases": [{"when": {"op": "exists", "left": ref("input.a")}, "next": "ev"}]}
# what the removed gate refused (an evaluate step at any depth), plus what it never covered
OPEN_SHAPES = {
    "top-level": wf(ev()),
    "in-loop": wf(loop("lp", [ev()])),
    "in-foreach": wf(foreach("fe", [ev()])),
    "nested-loop": wf(loop("lp", [loop("in", [ev()])])),
    "in-branch-in-loop": wf(loop("lp", [BRANCH_TO_EVAL, ev(), tool("x")])),
    "in-parallel-branch": wf({"id": "par", "type": "parallel", "branches": ["ev", "z"], "join": "j"},
                             ev("ev", "j"), tool("z", "j"), tool("j")),
    "loop-and-limits": wf(loop("lp", [ev()]), limits={"max_cost_usd": "1"}),
    "foreach-and-limits": wf(foreach("fe", [ev()]), limits={"max_cost_usd": "1"}),
}


@pytest.mark.parametrize("name", list(OPEN_SHAPES))
def test_evaluate_publishes_and_starts_at_every_depth(api, fake, name):
    client, _, tenant_of = api
    _, h = tenant_of()
    created = client.post(BASE, json={"spec": OPEN_SHAPES[name]}, headers=h)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    published = client.post(f"{BASE}/{wid}/versions", headers=h)
    assert published.status_code == 201, published.text
    started = client.post(f"{BASE}/{wid}/runs", json={"input": {"text": "a", "items": [1]}}, headers=h)
    assert started.status_code == 202, started.text
    assert fake.calls, "the run was enqueued"


def test_the_removed_gate_names_are_gone_from_the_code():
    needles = ("runtime_pending_problems", "LOOP_RUNTIME_PENDING")
    hits = []
    for root in ("apps", "packages", "scripts", "docs/workflows", "examples", "config"):
        for path in (REPO / root).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".md", ".json", ".sh", ".toml"}:
                continue
            if {"node_modules", ".next", ".venv", "__pycache__"} & set(path.parts):
                continue
            if path == Path(__file__):
                continue
            text = path.read_text(errors="ignore")
            for n in needles:
                for i, line in enumerate(text.splitlines(), 1):
                    # a test may assert the name is gone (hasattr), nothing may use it
                    if n in line and "hasattr" not in line:
                        hits.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
    assert hits == []


def test_evaluate_steps_save_as_drafts_and_the_gate_message_is_gone(client, auth_header):
    created = client.post(BASE, json={"spec": OPEN_SHAPES["in-foreach"]}, headers=auth_header)
    assert created.status_code == 201
    r = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert r.status_code == 201, r.text
    assert "is not available yet" not in r.text and "has not landed" not in r.text


# ------------------------------------------------------------------- still refused, with clear problems

APPROVAL = {"id": "g", "type": "approval", "prompt": "ok?", "timeout_hours": 1}
JUDGE = {"kind": "llm_judge", "rubric": "ok", "model": {"provider": "test", "name": "judge"}}


def with_evaluators(evaluators: list[dict[str, Any]]) -> dict[str, Any]:
    step = {**ev(), "evaluators": evaluators}
    return wf(step)


REFUSED_AT_SAVE = {
    "approval-in-body": (wf(loop("lp", [APPROVAL])), "loop_body_step_not_allowed"),
    "parallel-in-evaluate-loop": (
        wf(loop("lp", [{"id": "par", "type": "parallel", "branches": ["y", "z"], "join": "j"},
                       tool("y", "j"), tool("z", "j"), tool("j")])), "loop_body_step_not_allowed"),
    "loop-in-parallel": (
        wf({"id": "par", "type": "parallel", "branches": ["lp", "z"], "join": "j"},
           {**loop("lp", [ev()]), "next": "j"}, tool("z", "j"), tool("j")), "loop_in_parallel_not_allowed"),
    "three-deep": (wf(loop("a", [loop("b", [loop("c", [ev()])])])), "loops nest at most 2 deep"),
    "max_duration": (with_evaluators([{"kind": "max_duration", "seconds": 1}]), "there is no run duration"),
    "max_cost": (with_evaluators([{"kind": "max_cost", "usd": "1"}]), "there is no run cost"),
    "nine-evaluators": (with_evaluators([{"kind": "contains", "value": "a"}] * 9), "evaluators"),
    "no-evaluators": (with_evaluators([]), "evaluators"),
    "equals-without-expected": (with_evaluators([{"kind": "equals_expected"}]), "expected"),
    "value-not-a-ref": (wf({**ev(), "value": "plain"}), "value"),
    "pass-neither": (wf({**ev(), "pass": "most"}), "pass"),
}


@pytest.mark.parametrize("name", list(REFUSED_AT_SAVE))
def test_unsupported_shapes_are_refused_with_a_clear_problem(client, auth_header, name):
    spec, needle = REFUSED_AT_SAVE[name]
    r = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert r.status_code == 422, r.text
    assert needle in r.text, r.text


def test_a_loop_over_the_iteration_cap_is_refused_at_publish_and_run_start(api, fake, monkeypatch):
    client, _, tenant_of = api
    _, h = tenant_of()
    wid = client.post(BASE, json={"spec": OPEN_SHAPES["in-loop"]}, headers=h).json()["id"]
    assert client.post(f"{BASE}/{wid}/versions", headers=h).status_code == 201
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", "2")
    n = len(fake.calls)
    r = client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=h)
    assert r.status_code == 422 and "EAP_WORKFLOW_MAX_LOOP_ITERATIONS" in r.text, r.text
    assert len(fake.calls) == n
    other = client.post(BASE, json={"spec": OPEN_SHAPES["in-loop"]}, headers=h).json()["id"]
    p = client.post(f"{BASE}/{other}/versions", headers=h)
    assert p.status_code == 422 and "EAP_WORKFLOW_MAX_LOOP_ITERATIONS" in p.text, p.text


def test_a_judge_off_the_allowlist_inside_a_foreach_is_refused_at_publish(client, auth_header, monkeypatch):
    spec = wf(foreach("fe", [{**ev(), "evaluators": [JUDGE]}]))
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo")
    wid = client.post(BASE, json={"spec": spec}, headers=auth_header).json()["id"]
    r = client.post(f"{BASE}/{wid}/versions", headers=auth_header)
    assert r.status_code == 422, r.text
    assert "step 'ev': evaluators.0 (llm_judge): model 'test:judge' is not allowed" in r.json()["detail"]


# ------------------------------------------------------------------------------- mutated examples


def example_spec(name: str) -> dict[str, Any]:
    """The example's spec with placeholder agent ids that are valid UUIDs (save validates the shape)."""
    ex = load_example(name)
    ids = {a: "00000000-0000-4000-8000-000000000001" for a in ex["agents"]}
    return with_agent_ids(ex["spec"], ids)


def find_step(spec: dict[str, Any], sid: str) -> dict[str, Any]:
    def walk(steps):
        for s in steps:
            yield s
            if "body" in s:
                yield from walk(s["body"]["steps"])
    return next(s for s in walk(spec["steps"]) if s["id"] == sid)


def publish_422(client, headers, spec: dict[str, Any], agent_id: str | None = None) -> str:
    """The 422 detail of saving (or, if the draft is accepted, publishing) the spec."""
    r = client.post(BASE, json={"spec": spec}, headers=headers)
    if r.status_code == 201:
        r = client.post(f"{BASE}/{r.json()['id']}/versions", headers=headers)
    assert r.status_code == 422, r.text
    return r.text


def mutate(name: str, fn) -> dict[str, Any]:
    spec = copy.deepcopy(example_spec(name))
    fn(spec)
    return spec


def _del(sid: str, key: str):
    return lambda s: find_step(s, sid).pop(key)


def _set(sid: str, key: str, value: Any):
    return lambda s: find_step(s, sid).__setitem__(key, value)


def _judge(spec: dict[str, Any]) -> dict[str, Any]:
    return find_step(spec, "judge")["evaluators"][0]


MUTATIONS = {
    "rubric-missing": ("refine.json", lambda s: _judge(s).pop("rubric"), "llm_judge.rubric: Field required"),
    "rubric-empty": ("refine.json", lambda s: _judge(s).__setitem__("rubric", ""), "rubric: String should have at least 1"),
    "judge-kind-typo": ("refine.json", lambda s: _judge(s).__setitem__("kind", "llm_judger"), "llm_judger"),
    "loop-without-max-iterations": ("refine.json", _del("refine", "max_iterations"), "max_iterations: Field required"),
    "poll-without-max-iterations": ("until-polling.json", _del("poll", "max_iterations"), "max_iterations: Field required"),
    "foreach-without-max-items": ("foreach-triage.json", _del("each", "max_items"), "max_items: Field required"),
    "foreach-concurrency-9": ("foreach-triage.json", _set("each", "concurrency", 9), "concurrency: Input should be less than or equal to 8"),
    "until-unknown-op": ("refine.json", _set("refine", "until", {"op": "approx", "left": ref("input.a"), "right": 1}),
                         "until.op: Input should be"),
    "until-reads-unknown-step": ("refine.json", _set("refine", "until", {"op": "exists", "left": ref("steps.nope.output")}),
                                 "refers to unknown step 'nope'"),
    "until-and-while": ("refine.json", _set("refine", "while", {"op": "exists", "left": ref("input.a")}),
                        "exactly one of 'while' and 'until'"),
    "no-condition": ("refine.json", _del("refine", "until"), "exactly one of 'while' and 'until'"),
    "cap-zero": ("refine.json", lambda s: s.__setitem__("limits", {"max_cost_usd": "0"}), "max_cost_usd must be between"),
    "cap-too-precise": ("refine.json", lambda s: s.__setitem__("limits", {"max_cost_usd": "0.00001"}), "max_cost_usd"),
}


@pytest.mark.parametrize("name", list(MUTATIONS))
def test_a_mutated_example_fails_with_the_documented_problem(client, auth_header, name):
    example, fn, needle = MUTATIONS[name]
    spec = mutate(example, fn)
    with pytest.raises(ValidationError) as raised:
        WorkflowSpec.model_validate(spec)
    assert needle in str(raised.value)
    assert needle in publish_422(client, auth_header, spec)


def test_polling_until_over_a_step_a_path_skips_is_refused(client, auth_header):
    """`until` may read only body steps on every path (loop_ref_not_on_every_path)."""
    body = [
        {"id": "br", "type": "branch", "default": "skip",
         "cases": [{"when": {"op": "exists", "left": ref("input.a")}, "next": "only"}]},
        tool("only"), tool("skip"),
    ]
    spec = wf(loop("lp", body, until={"op": "exists", "left": ref("steps.only.output")}))
    assert "loop_ref_not_on_every_path" in publish_422(client, auth_header, spec)


def test_a_capped_loop_that_fails_validation_never_leaves_a_draft_to_publish(client, auth_header):
    spec = mutate("refine.json", _del("refine", "max_iterations"))
    assert client.post(BASE, json={"spec": spec}, headers=auth_header).status_code == 422


# ------------------------------------------------------------------------------------- the guide

GUIDE_TEXT = GUIDE.read_text()
SOURCE_SUFFIXES = {".py", ".ts", ".tsx"}


def schema_field_names() -> set[str]:
    import inspect
    import pkgutil

    import eap_schemas
    from pydantic import BaseModel

    names: set[str] = set()
    for info in pkgutil.iter_modules(eap_schemas.__path__):
        module = __import__(f"eap_schemas.{info.name}", fromlist=["x"])
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseModel):
                for key, field in cls.model_fields.items():
                    names.update({key, field.alias} - {None})
    return names


def source_text() -> str:
    chunks = []
    for root in ("apps", "packages"):
        for path in (REPO / root).rglob("*"):
            if (path.suffix in SOURCE_SUFFIXES and path.is_file() and "api_tests" not in path.parts
                    and not {"node_modules", ".venv", ".next"} & set(path.parts)):
                chunks.append(path.read_text(errors="ignore"))
    return "\n".join(chunks)


def test_every_guide_snippet_parses_and_validates_in_place():
    snippets = list(SNIPPET.finditer(GUIDE_TEXT))
    assert len(snippets) >= 5
    for s in snippets:
        body = json.loads(s["body"])  # parses as JSON
        example = load_example(s["file"])
        ids = {a: "00000000-0000-4000-8000-000000000001" for a in example["agents"]}
        spec = with_agent_ids(example["spec"], ids)
        *parents, key = s["pointer"].removeprefix("/spec/").split("/")
        holder = spec
        for token in parents:
            holder = holder[int(token)] if isinstance(holder, list) else holder[token]
        holder[int(key) if isinstance(holder, list) else key] = with_agent_ids(body, ids)
        WorkflowSpec.model_validate(spec)


def test_every_guide_snippet_is_valid_json_and_no_other_fence_is_unparsed():
    fences = re.findall(r"```(\w*)\n(.*?)\n```", GUIDE_TEXT, re.DOTALL)
    assert fences, "the guide has code blocks"
    for lang, body in fences:
        assert lang == "json", f"unexpected {lang!r} fence"
        json.loads(body)


def test_every_field_name_the_guide_mentions_exists_in_the_schema():
    fields = schema_field_names()
    src = source_text()
    tokens = set(re.findall(r"`([a-z][a-z_]*)`", GUIDE_TEXT))
    keys = set(re.findall(r'"([a-z_]+)":', "\n".join(m["body"] for m in SNIPPET.finditer(GUIDE_TEXT))))
    data_words = {"after", "priority", "file", "spec", "agents", "last", "judge"}  # example data, not schema
    # a non-field token must appear in the code as a whole quoted literal ("x", 'x' or `x`), such as
    # an error code, an event type or a step kind, not merely as a substring of other source text
    def quoted_in_src(t: str) -> bool:
        return re.search(r"[\"'`]" + re.escape(t) + r"[\"'`]", src) is not None

    unknown = sorted(t for t in tokens - fields - data_words if not quoted_in_src(t))
    assert unknown == [], f"mentioned in the guide but neither a schema field nor a literal in the code: {unknown}"
    assert not quoted_in_src("no_such_loop_field_xyz")  # the check can fail
    for needed in ("max_items", "concurrency", "on_item_error", "max_iterations", "on_max_iterations", "state",
                   "carry", "while", "until", "evaluators", "pass", "expected", "value", "items", "max_cost_usd"):
        assert needed in tokens | keys and needed in fields, needed


def test_guide_env_variables_exist_in_the_code():
    src = source_text()
    for var in sorted(set(re.findall(r"`(EAP_[A-Z_]+)`", GUIDE_TEXT))):
        assert var in src, var


# ------------------------------------------------------------------------------ the cost-cap warning

PERIOD = [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "4"}]
ZERO = [{"effective_from": "2026-01-01", "input_per_mtok": "0", "output_per_mtok": "0"}]


def judge_workflow(model: str = "judge", cap: str | None = "1") -> dict[str, Any]:
    step = {"id": "ev", "type": "evaluate", "value": ref("input.text"), "evaluators": [
        {"kind": "contains", "value": "a"},
        {"kind": "llm_judge", "rubric": "ok", "model": {"provider": "test", "name": model}}]}
    return wf(step, **({"limits": {"max_cost_usd": cap}} if cap else {}))


def prices(tmp_path, monkeypatch, models: dict[str, Any] | None, raw: str | None = None) -> None:
    path = tmp_path / "prices.json"
    path.write_text(raw if raw is not None else json.dumps({"models": models}))
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(path))


def publish_warnings_of(client, headers, spec: dict[str, Any]) -> list[dict[str, str]]:
    created = client.post(BASE, json={"spec": spec}, headers=headers)
    r = client.post(f"{BASE}/{created.json()['id']}/versions", headers=headers)
    assert r.status_code == 201, r.text
    return [w for w in r.json()["warnings"] if w["code"] == "cost_cap_unpriced_model"]


def test_a_priced_judge_gives_no_warning(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {"test:judge": PERIOD})
    assert publish_warnings_of(client, auth_header, judge_workflow()) == []


def test_a_wildcard_priced_judge_gives_no_warning(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {"test:*": PERIOD})
    assert publish_warnings_of(client, auth_header, judge_workflow("anything")) == []


def test_an_unpriced_judge_without_a_cap_gives_no_warning(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {})
    assert publish_warnings_of(client, auth_header, judge_workflow(cap=None)) == []


@pytest.mark.parametrize("models, raw, needle", [
    ({}, None, "'test:judge' has no price in the model price override file"),
    ({"test:judge": ZERO}, None, "'test:judge' is priced at $0, so its requests count as $0"),
    (None, "{not json", "the model price override file is unreadable"),
])
def test_an_unpriced_judge_with_a_cap_warns_once_at_its_path(client, auth_header, tmp_path, monkeypatch,
                                                            models, raw, needle):
    prices(tmp_path, monkeypatch, models, raw)
    [warning] = publish_warnings_of(client, auth_header, judge_workflow())
    assert warning["path"] == "steps[0].evaluators[1]"
    assert needle in warning["message"] and "toward limits.max_cost_usd" in warning["message"]
    assert warning["message"].startswith("step 'ev': evaluators.1 (llm_judge): ")


def test_two_unpriced_judges_in_a_loop_and_a_foreach_warn_each(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {})
    judge = {"kind": "llm_judge", "rubric": "ok", "model": {"provider": "test", "name": "judge"}}
    inner = {**ev("e1"), "evaluators": [judge, judge]}
    spec = wf(foreach("fe", [inner]), limits={"max_cost_usd": "1"})
    paths = [w["path"] for w in publish_warnings_of(client, auth_header, spec)]
    assert paths == ["steps[0].body.steps[0].evaluators[0]", "steps[0].body.steps[0].evaluators[1]"]


def test_non_judge_evaluators_and_the_warning_never_block_the_publish(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {})
    spec = wf(ev(), limits={"max_cost_usd": "1"})
    assert publish_warnings_of(client, auth_header, spec) == []


def published_agent(client, headers, **spec: Any) -> str:
    body = {"name": "W", "instructions": "x", "model": {"provider": "test", "name": "echo"}, **spec}
    created = client.post("/api/v1/agents", json={"spec": body}, headers=headers)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def agent_workflow(agent_id: str, cap: str | None = "1", version: int | None = 1) -> dict[str, Any]:
    step = {"id": "w", "type": "agent", "agent_id": agent_id, "input": {}}
    if version is not None:
        step["version"] = version
    return wf(loop("lp", [step]), **({"limits": {"max_cost_usd": cap}} if cap else {}))


def test_an_unpriced_body_agent_warns_per_model_of_its_chain(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {"test:echo": ZERO})
    agent_id = published_agent(client, auth_header, fallback_models=[{"provider": "test", "name": "other"}])
    warnings = publish_warnings_of(client, auth_header, agent_workflow(agent_id))
    assert warnings == [
        {"code": "cost_cap_unpriced_model", "path": "steps[0].body.steps[0]",
         "message": f"step 'w': agent '{agent_id}' version 1 model: 'test:echo' is priced at $0, so its requests "
                    "count as $0 toward limits.max_cost_usd, so the cost limit may not stop the run"},
        {"code": "cost_cap_unpriced_model", "path": "steps[0].body.steps[0]",
         "message": f"step 'w': agent '{agent_id}' version 1 fallback_models[0]: 'test:other' has no price in the "
                    "model price override file; its requests count as $0 toward limits.max_cost_usd, so the "
                    "cost limit may not stop the run"},
    ]


def test_an_unpinned_body_agent_warns_for_its_latest_version(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {})
    agent_id = published_agent(client, auth_header)
    [warning] = publish_warnings_of(client, auth_header, agent_workflow(agent_id, version=None))
    assert warning["message"].startswith(f"step 'w': agent '{agent_id}' version 1 model: 'test:echo' has no price")


def test_a_priced_or_uncapped_body_agent_gives_no_warning(client, auth_header, tmp_path, monkeypatch):
    prices(tmp_path, monkeypatch, {"test:*": PERIOD})
    agent_id = published_agent(client, auth_header)
    assert publish_warnings_of(client, auth_header, agent_workflow(agent_id)) == []
    prices(tmp_path, monkeypatch, {})
    assert publish_warnings_of(client, auth_header, {**agent_workflow(agent_id, cap=None), "name": "U"}) == []


AGENT = {"name": "A", "instructions": "x", "model": {"provider": "test", "name": "echo"},
         "limits": {"max_cost_usd": "1"}}


@pytest.mark.parametrize("models, raw, message", [
    ({}, None, "model: 'test:echo' has no price in the model price override file; its requests count as $0 "
               "toward limits.max_cost_usd, so the cost limit may not stop the run"),
    ({"test:echo": ZERO}, None, "model: 'test:echo' is priced at $0, so its requests count as $0 toward "
                                "limits.max_cost_usd, so the cost limit may not stop the run"),
    (None, "{not json", "model: the model price override file is unreadable, invalid or missing, so no model has "
                        "a price; its requests count as $0 toward limits.max_cost_usd, so the cost limit may "
                        "not stop the run"),
])
def test_agent_cost_cap_warnings_keep_their_exact_text(tmp_path, monkeypatch, models, raw, message):
    prices(tmp_path, monkeypatch, models, raw)
    warnings = _cost_cap_warnings(AgentSpec.model_validate(AGENT), date(2026, 11, 15))
    assert [(w["code"], w["path"], w["message"]) for w in warnings] == [("cost_cap_unpriced_model", "model", message)]
    prices(tmp_path, monkeypatch, {"test:echo": PERIOD})
    assert _cost_cap_warnings(AgentSpec.model_validate(AGENT), date(2026, 11, 15)) == []


# ------------------------------------------------------------- evaluate runs through the API (worker)

JUDGE_PASS = {"kind": "llm_judge", "rubric": 'MUST_CONTAIN:"attempt 3"', "model": {"provider": "test", "name": "judge"}}


def standalone_spec() -> dict[str, Any]:
    return wf({"id": "ev", "type": "evaluate", "value": ref("input.text"),
               "evaluators": [JUDGE_PASS, {"kind": "contains", "value": "hello"}]})


def foreach_spec() -> dict[str, Any]:
    each = foreach("fe", [{"id": "ev", "type": "evaluate", "value": ref("loop.fe.item"), "evaluators": [JUDGE_PASS]}])
    return wf(each)


def publish_run(stack: Stack, spec: dict[str, Any], input_value: Any) -> tuple[str, dict[str, Any]]:
    created = stack.client.post(BASE, json={"spec": spec}, headers=stack.h)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    p = stack.client.post(f"{BASE}/{wid}/versions", headers=stack.h)
    assert p.status_code == 201 and p.json()["warnings"] == [], p.text
    return stack.run(wid, input_value)


def judge_rows(stack: Stack, run_id: str) -> list[tuple[str, str]]:
    with stack.engine.connect() as conn:
        rows = conn.execute(text("SELECT request_key, operation FROM run_model_requests WHERE run_id = :r "
                                 "ORDER BY request_key"), {"r": run_id}).all()
    return [(r.request_key, r.operation) for r in rows]


@requires_pg
def test_a_standalone_evaluate_runs_and_a_failing_verdict_does_not_fail_the_run(stack):
    run_id, run = publish_run(stack, standalone_spec(), {"text": "hello attempt 3"})
    assert run["status"] == "completed", run
    out = json.loads(run["output"])
    assert out["passed"] is True and len(out["results"]) == 2
    assert judge_rows(stack, run_id) == [("judge:ev:0", "chat")]
    run_id2, run2 = publish_run(stack, standalone_spec(), {"text": "hello nothing"})
    assert run2["status"] == "completed", run2
    assert json.loads(run2["output"])["passed"] is False


@requires_pg
def test_evaluate_inside_a_foreach_runs_per_item_with_scoped_judge_rows(stack):
    run_id, run = publish_run(stack, foreach_spec(), {"items": ["attempt 3", "nope", "attempt 3!"]})
    assert run["status"] == "completed", run
    out = json.loads(run["output"])
    assert [item["passed"] for item in out] == [True, False, True]
    rows = judge_rows(stack, run_id)
    assert [k for k, _ in rows] == [f"judge:fe.{i}:ev:0" for i in range(3)]
    assert {op for _, op in rows} == {"chat"}


@requires_pg
def test_judge_rows_of_the_run_count_as_judge_in_this_tenants_metrics_only(stack):
    other_tenant = f"t{uuid.uuid4().hex[:10]}"
    other = Stack(stack.client, stack.engine, other_tenant)
    run_id, _ = publish_run(stack, standalone_spec(), {"text": "attempt 3 hello"})
    window = {"from": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
              "to": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    mine = stack.client.get("/api/v1/metrics/breakdown", params={**window, "by": "operation"}, headers=stack.h).json()
    assert "judge" in {i["key"] for i in mine["items"]} and run_id
    theirs = stack.client.get("/api/v1/metrics/breakdown", params={**window, "by": "operation"}, headers=other.h).json()
    assert theirs["items"] == []
    summary = stack.client.get("/api/v1/metrics/summary", params={**window, "operation": "judge"}, headers=other.h).json()
    assert summary["current"]["requests"] == 0


@requires_pg
def test_seeded_judge_rows_of_two_tenants_never_mix(pg, tenant_of):
    from decimal import Decimal

    from .test_metrics_operations_api import WINDOW, _seed_run

    client, app = pg
    a, ha = tenant_of()
    b, hb = tenant_of()
    with app.state.database_engine.begin() as conn:
        _seed_run(conn, a, [("judge:ev:0", "chat", 100, 10, Decimal("0.001"))])
        _seed_run(conn, b, [("judge:fe.0:ev:0", "chat", 7, 1, Decimal("0.5")),
                            ("judge:fe.1:ev:0", "chat", 7, 1, Decimal("0.5"))])
    for h, requests, cost in ((ha, 1, "0.0010000000"), (hb, 2, "1.0000000000")):
        judged = client.get("/api/v1/metrics/summary", params={**WINDOW, "operation": "judge"}, headers=h).json()
        assert judged["current"]["requests"] == requests
        judge = client.get("/api/v1/metrics/summary", params=WINDOW, headers=h).json()["current"]["operations"]["judge"]
        assert (judge["requests"], judge["cost_usd"]) == (requests, cost)
        items = client.get("/api/v1/metrics/breakdown", params={**WINDOW, "by": "operation"}, headers=h).json()["items"]
        assert [i["key"] for i in items] == ["judge"]
