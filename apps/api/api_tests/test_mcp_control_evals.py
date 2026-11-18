"""MCP control: scorers, test suites, eval runs and the rule suggester (tools_evals)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from eap_api.launcher import get_run_launcher
from eap_api.routes import scorer_suggestions
from eap_run_store import RunStore

from .mcp_control_helpers import world  # noqa: F401 - fixture
from .run_helpers import requires_pg
from .test_architect_api import ScriptedLauncher
from .test_scorer_suggestions_api import GOOD, flat, entry

pytestmark = requires_pg

READ = {"scorer.list", "eval.suite.list", "eval.run.get"}
MANAGE = {"scorer.create", "scorer.attach", "scorer.suggest", "eval.suite.create", "eval.case.add", "eval.run.start"}
REGEX = {"kind": "regex", "pattern": "refund", "negate": True}


@pytest.fixture
def evals_world(world):  # noqa: F811
    scorer_suggestions.clear_cache()
    yield world
    for t in world.tenants:
        world.sql("DELETE FROM eval_case_results WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM eval_suites WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM scorers WHERE tenant_id = :t", t=t)
    scorer_suggestions.clear_cache()


def keys(world):
    tenant, h = world.tenant_of()
    return tenant, h, world.make_key(h), world.make_key(h, scopes=["agui:run", "manage"])


def test_tools_are_listed_by_scope(evals_world):
    world = evals_world
    _, _, read_key, manage_key = keys(world)

    async def names(key):
        async with world.session(key) as c:
            return {t.name for t in await c.list_tools()}

    assert (READ | MANAGE) <= asyncio.run(names(manage_key))
    some = str(uuid.uuid4())
    args = {
        "scorer.create": {"name": "n", "evaluator": REGEX}, "scorer.attach": {"agent_id": some, "scorer_id": some},
        "scorer.suggest": {"agent_id": some}, "eval.suite.create": {"name": "n", "agent_id": some},
        "eval.case.add": {"suite_id": some, "name": "n", "input": "i"}, "eval.run.start": {"suite_id": some},
    }
    for name in MANAGE:
        reply = world.call(read_key, name, args[name])
        assert reply.is_error and reply.data["error"]["code"] == "scope_required", name


def test_scorer_create_list_and_attach(evals_world):
    world = evals_world
    _, h, read_key, manage_key = keys(world)
    agent_id = world.publish_agent(h)

    created = world.call(manage_key, "scorer.create", {"name": "No refunds", "evaluator": REGEX, "description": "d"})
    assert not created.is_error, created.data
    scorer = created.data
    assert scorer["evaluator"]["negate"] is True and scorer["agent_ids"] == []

    dup = world.call(manage_key, "scorer.create", {"name": "No refunds", "evaluator": REGEX})
    assert dup.is_error and dup.data["error"]["code"] == "conflict"
    bad = world.call(manage_key, "scorer.create", {"name": "Bad", "evaluator": {"kind": "llm_judge", "rubric": "x"}})
    assert bad.is_error and bad.data["error"]["code"] == "invalid"
    bad = world.call(manage_key, "scorer.create", {"name": "Bad2", "evaluator": {"kind": "regex", "pattern": "(?=a)b"}})
    assert bad.is_error and bad.data["error"]["code"] == "invalid"

    attached = world.call(manage_key, "scorer.attach", {"agent_id": agent_id, "scorer_id": scorer["id"], "sampling_rate": 0.5})
    assert not attached.is_error, attached.data
    assert attached.data["items"][0]["id"] == scorer["id"] and attached.data["items"][0]["sampling_rate"] == 0.5
    missing = world.call(manage_key, "scorer.attach", {"agent_id": agent_id, "scorer_id": str(uuid.uuid4())})
    assert missing.is_error and missing.data["error"]["code"] == "not_found"
    assert world.call(manage_key, "scorer.attach", {"agent_id": agent_id, "scorer_id": scorer["id"], "sampling_rate": 2}).is_error

    listed = world.call(read_key, "scorer.list")
    assert not listed.is_error and [s["name"] for s in listed.data["items"]] == ["No refunds"]
    assert listed.data["items"][0]["agent_ids"] == [agent_id]


def test_suites_cases_and_runs(evals_world):
    world = evals_world
    _, h, read_key, manage_key = keys(world)
    agent_id = world.publish_agent(h)

    suite = world.call(manage_key, "eval.suite.create", {"name": "Smoke", "agent_id": agent_id, "evaluators": [REGEX]})
    assert not suite.is_error, suite.data
    assert world.call(manage_key, "eval.suite.create", {"name": "x"}).data["error"]["code"] == "invalid"  # no target
    assert world.call(manage_key, "eval.suite.create", {"name": "x", "agent_id": agent_id, "workflow_id": agent_id}).is_error
    bad = world.call(manage_key, "eval.suite.create", {"name": "Bad", "agent_id": agent_id, "evaluators": [{"kind": "nope"}]})
    assert bad.is_error and bad.data["error"]["code"] == "invalid"
    suite_id = suite.data["id"]

    case = world.call(manage_key, "eval.case.add", {"suite_id": suite_id, "name": "c1", "input": "hello", "expected_output": "hello"})
    assert not case.is_error, case.data
    assert world.call(manage_key, "eval.case.add", {"suite_id": suite_id, "name": "c1", "input": "again"}).data["error"]["code"] == "conflict"
    assert world.call(manage_key, "eval.case.add", {"suite_id": str(uuid.uuid4()), "name": "c", "input": "i"}).data["error"]["code"] == "not_found"

    listed = world.call(read_key, "eval.suite.list", {"agent_id": agent_id})
    assert [(s["name"], s["case_count"]) for s in listed.data["items"]] == [("Smoke", 1)]
    assert world.call(read_key, "eval.suite.list", {}).is_error

    started = world.call(manage_key, "eval.run.start", {"suite_id": suite_id})
    assert not started.is_error, started.data
    assert started.data["cases"] == 1 and "run_id" in started.data
    again = world.call(manage_key, "eval.run.start", {"suite_id": suite_id, "run_id": started.data["run_id"]})
    assert not again.is_error and again.data["run_id"] == started.data["run_id"]
    got = world.call(read_key, "eval.run.get", {"run_id": started.data["run_id"], "results_limit": 10})
    assert not got.is_error, got.data
    assert got.data["run_id"] == started.data["run_id"] and got.data["results"] == []
    assert got.data["aggregates"]["cases"] == 0 and got.data["suite_id"] == suite_id
    assert world.call(read_key, "eval.run.get", {"run_id": str(uuid.uuid4())}).data["error"]["code"] == "not_found"


def test_workflow_suite_and_tenant_isolation(evals_world):
    world = evals_world
    _, h, _, manage_key = keys(world)
    _, h2, _, other_manage = keys(world)
    agent_id = world.publish_agent(h)
    workflow_id = world.publish_workflow(h, agent_id)
    suite = world.call(manage_key, "eval.suite.create", {"name": "wf", "workflow_id": workflow_id, "evaluators": [{"kind": "max_duration", "seconds": 30}]})
    assert not suite.is_error, suite.data
    case = world.call(manage_key, "eval.case.add", {"suite_id": suite.data["id"], "name": "c", "input": '{"q": "x"}'})
    assert not case.is_error, case.data
    # another tenant sees none of it
    assert world.call(other_manage, "eval.case.add", {"suite_id": suite.data["id"], "name": "c", "input": "{}"}).data["error"]["code"] == "not_found"
    assert world.call(other_manage, "eval.suite.list", {"workflow_id": workflow_id}).data["error"]["code"] == "not_found"
    assert world.call(other_manage, "scorer.attach", {"agent_id": agent_id, "scorer_id": str(uuid.uuid4())}).data["error"]["code"] == "not_found"
    assert world.call(other_manage, "scorer.suggest", {"agent_id": agent_id}).data["error"]["code"] == "not_found"


def test_scorer_suggest_runs_the_suggester_and_validates(evals_world):
    world = evals_world
    tenant, h, _, manage_key = keys(world)
    agent_id = world.publish_agent(h)
    launcher = ScriptedLauncher(RunStore(world.app.state.database_engine))
    launcher.script = lambda _in: {"suggestions": [*GOOD, entry(flat("regex"))]}
    world.app.dependency_overrides[get_run_launcher] = lambda: launcher

    reply = world.call(manage_key, "scorer.suggest", {"agent_id": agent_id, "max": 5})
    assert not reply.is_error, reply.data
    assert [s["title"] for s in reply.data["suggestions"]] == ["No refund promises", "Stay cheap", "Tone"]
    assert reply.data["dropped"] == 1
    again = world.call(manage_key, "scorer.suggest", {"agent_id": agent_id, "max": 5})
    assert again.data == reply.data and len(launcher.inputs) == 1  # cached

    assert world.call(manage_key, "scorer.suggest", {}).data["error"]["code"] == "invalid"
    assert world.call(manage_key, "scorer.suggest", {"agent_id": agent_id, "max": 99}).data["error"]["code"] == "invalid"
    workflow_id = world.publish_workflow(h, agent_id)
    wf = world.call(manage_key, "scorer.suggest", {"workflow_id": workflow_id})
    assert not wf.is_error and {s["scope"] for s in wf.data["suggestions"]} == {"evaluator"}


def test_a_restricted_key_only_reaches_its_agents(evals_world):
    world = evals_world
    _, h, _, manage_key = keys(world)
    mine, other = world.publish_agent(h), world.publish_agent(h)
    suite = world.call(manage_key, "eval.suite.create", {"name": "theirs", "agent_id": other}).data["id"]
    restricted = world.make_key(h, scopes=["agui:run", "manage"], agent_ids=[mine])
    assert world.call(restricted, "eval.case.add", {"suite_id": suite, "name": "c", "input": "i"}).data["error"]["code"] == "not_found"
    assert world.call(restricted, "eval.suite.list", {"agent_id": other}).data["error"]["code"] == "not_found"
    assert world.call(restricted, "scorer.suggest", {"agent_id": other}).data["error"]["code"] == "not_found"
    assert world.call(restricted, "scorer.create", {"name": "n", "evaluator": REGEX}).data["error"]["code"] == "forbidden"
    assert not world.call(restricted, "eval.suite.create", {"name": "mine", "agent_id": mine}).is_error
