"""The MCP run surface `POST /api/v1/external/mcp` (inbound design 5.1, sections 4, 6, 7 and 8) over the real app on
Postgres, driven by the official `mcp` client in both protocol revisions."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid

import pytest
from eap_run_store import ExternalAccessStore

from .conftest import get_auth_header
from .external_mcp_helpers import LEGACY, MCP_PATH, MODERN, MODES, Served, call, external_env, served  # noqa: F401
from .external_world import BASE, INPUT_SCHEMA, NOISY_STEPS, Live
from .run_helpers import requires_pg

pytestmark = requires_pg

FIXED_TOOLS = ["exports.list", "runs.get", "runs.wait", "runs.list", "runs.cancel"]
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw", "version": "1"}}}


def second_client(served: Served, live: Live) -> Live:
    other = served.client_record(live.headers)
    key = served.key(live.headers, other["id"], [live.workflow])["key"]
    return Live(served, live.tenant, live.headers, live.workflow, live.export, other, key)


def start(served: Served, live: Live, q: str = "hello", **args) -> dict:
    args = {"input": {"q": q}, "idempotency_key": "k-1", "wait_seconds": 0} | args
    return served.tool(live.key, "run_run_report", args).ok


def later(seconds: float, fn, *args, **kwargs) -> threading.Timer:
    timer = threading.Timer(seconds, fn, args, kwargs)
    timer.start()
    return timer


# -------------------------------------------------------------------------------------------------- acceptance


@pytest.mark.parametrize("mode", MODES)
def test_the_official_client_lists_runs_waits_and_cancels(served: Served, mode: str):
    live = served.live()

    async def go() -> None:
        async with served.session(live.key, mode) as client:
            assert (client.protocol_version == MODERN) is (mode == MODERN)
            listed = await client.list_tools()
            assert [t.name for t in listed.tools] == [*FIXED_TOOLS, "run_run_report"]

            started = (await call(client, "run_run_report", {"input": {"q": "x"}, "idempotency_key": "a",
                                                             "wait_seconds": 0})).ok
            assert started["state"] == "queued" and started["done"] is False
            run_id = started["run_id"]
            served.running(live, run_id)
            timer = later(0.5, served.finish, live, run_id, "completed", output=json.dumps({"total": 3}),
                          output_format="json")
            waited = (await call(client, "runs.wait", {"run_id": run_id, "timeout_seconds": 30})).ok
            timer.join()
            assert waited["state"] == "completed" and waited["result"] == {"total": 3}
            assert (await call(client, "runs.get", {"run_id": run_id})).ok["result"] == {"total": 3}

            second = (await call(client, "run_run_report", {"input": {"q": "y"}, "idempotency_key": "b",
                                                            "wait_seconds": 0})).ok
            cancelled = (await call(client, "runs.cancel", {"run_id": second["run_id"]})).ok
            assert cancelled["state"] == "cancelled" and cancelled["done"] is True
            again = (await call(client, "runs.cancel", {"run_id": second["run_id"]})).ok
            assert again["state"] == "cancelled"
            items = (await call(client, "runs.list", {})).ok["items"]
            assert {i["run_id"] for i in items} == {run_id, second["run_id"]}
            assert all("result" not in i for i in items)

    asyncio.run(go())
    assert [r["principal"] for r in live.runs()] == [f"api_key:{live.key.split('_')[1]}"] * 2
    assert {r["on_behalf_of"] for r in live.runs()} == {None}


def test_the_client_negotiates_the_newest_revision_by_itself(served: Served):
    live = served.live()

    async def go() -> None:
        async with served.session(live.key, "auto") as client:
            assert client.protocol_version == MODERN
            assert (await call(client, "exports.list")).ok["exports"][0]["name"] == "run_report"

    asyncio.run(go())


# -------------------------------------------------------------------------------------------------- the closed surface


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_the_route_answers_like_an_unknown_one_while_the_switch_is_off(served: Served, external_env, method):
    live = served.live()
    external_env.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "false")
    unknown = served.client.request(method, "/api/v1/external/nothing", headers={"Authorization": f"Bearer {live.key}"})
    for key in (live.key, None):
        r = served.post(key, INITIALIZE, method=method)
        assert r.status_code == 404 == unknown.status_code and r.json() == unknown.json()
    external_env.delenv("EAP_EXTERNAL_ACCESS_ENABLED")
    assert served.post(live.key, INITIALIZE).status_code == 404


def test_this_is_the_only_mcp_route_under_the_external_prefix(served: Served):
    def paths(routes, prefix=""):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from paths(inner.routes, prefix + route.include_context.prefix)
            elif hasattr(route, "path"):
                yield prefix + route.path

    external = [p for p in paths(served.app.router.routes) if p.startswith("/api/v1/external")]
    assert [p for p in external if not p.startswith("/api/v1/external/a2a/")] == [MCP_PATH]


def test_only_a_run_key_is_let_in(served: Served):
    live = served.live()
    assert served.post(live.key, INITIALIZE).status_code == 200
    dev = get_auth_header(live.tenant, "alice")["Authorization"].removeprefix("Bearer ")
    for key in (None, "nonsense", "pos_0123456789abcdef_" + "0" * 43, dev):
        r = served.post(key, INITIALIZE)
        assert r.status_code == 401, (key, r.text)
        assert r.json() == {"error": {"code": "unauthorized", "message": "a run key is required"}}
        assert r.headers["www-authenticate"] == "Bearer"
    for scope in ("agui:run", "mcp", "manage", "otlp"):
        made = served.client.post("/api/v1/api-keys", json={"name": scope, "scopes": [scope]}, headers=live.headers)
        assert made.status_code == 201, made.text
        r = served.post(made.json()["key"], INITIALIZE)
        assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden", (scope, r.text)
    assert live.runs() == []


def test_a_run_key_reaches_no_other_surface_and_no_control_tool(served: Served):
    live = served.live()
    bearer = {"Authorization": f"Bearer {live.key}"}
    r = served.client.post("/api/v1/mcp", json=INITIALIZE,
                           headers=bearer | {"Accept": "application/json, text/event-stream"})
    assert r.status_code == 403, r.text
    for path in ("/api/v1/workflows", f"/api/v1/workflows/{live.workflow}", "/api/v1/runs", "/api/v1/agents",
                 "/api/v1/secrets", f"{BASE}/clients"):
        assert served.client.get(path, headers=bearer).status_code in (401, 403), path
    for tool in ("workflow.list", "workflow.get", "agent.list", "workflow.run.start", "approvals.approve",
                 "approvals.list", "runs.answer", "settings.get"):
        reply = served.tool(live.key, tool, {})
        assert reply.is_error and "Unknown tool" in json.dumps(reply.data), tool

    async def go() -> None:
        async with served.session(live.key) as client:
            assert (await client.list_resources()).resources == []
            assert (await client.list_resource_templates()).resource_templates == []
            assert (await client.list_prompts()).prompts == []

    asyncio.run(go())


def test_a_revoked_key_a_disabled_client_and_a_foreign_origin_stop_at_once(served: Served):
    live = served.live()
    patch = f"{BASE}/clients/{live.client['id']}"
    assert served.client.patch(patch, json={"enabled": False}, headers=live.headers).status_code == 200
    r = served.post(live.key, INITIALIZE)
    assert r.status_code == 403 and r.json()["error"]["code"] == "client_disabled", r.text
    assert served.client.patch(patch, json={"enabled": True}, headers=live.headers).status_code == 200
    assert served.post(live.key, INITIALIZE).status_code == 200
    limited = served.key(live.headers, live.client["id"], [live.workflow],
                         allowed_origins=["https://agents.example.test"])["key"]
    assert served.post(limited, INITIALIZE, headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert served.post(limited, INITIALIZE, headers={"Origin": "https://agents.example.test"}).status_code == 200
    served.sql("UPDATE api_keys SET revoked_at = now() WHERE tenant_id = :t", t=live.tenant)
    assert served.post(live.key, INITIALIZE).status_code == 401
    refused = served.sql("SELECT detail FROM audit_log WHERE tenant_id = :t AND outcome = 'denied'", t=live.tenant)
    assert [r["detail"]["reason"] for r in refused] == ["client_disabled"]


# -------------------------------------------------------------------------------------------------- tools and exports


def test_tools_describe_only_the_exports_this_key_reaches(served: Served):
    live = served.live()
    hidden = served.workflow(live.headers)
    assert served.enable(live.headers, served.export(live.headers, hidden, name="not_allowed")).status_code == 200
    served.export(live.headers, live.workflow, name="drafted_only")
    a2a = served.export(live.headers, live.workflow, name="a2a_only", protocols=["a2a"])
    assert served.enable(live.headers, a2a).status_code == 200
    early = served.export(live.headers, live.workflow, name="another")
    assert served.enable(live.headers, early).status_code == 200
    served.live(name="other_tenants")

    listed = served.tools(live.key)
    assert [t.name for t in listed.tools] == [*FIXED_TOOLS, "run_another", "run_run_report"]
    assert [t.name for t in served.tools(live.key, LEGACY).tools] == [t.name for t in listed.tools]
    assert listed.cache_scope == "private" and 0 < listed.ttl_ms <= 60_000
    run = next(t for t in listed.tools if t.name == "run_run_report")
    assert run.title == "Run the report" and "Builds the report." in run.description and "runs.wait" in run.description
    schema = run.input_schema
    assert schema["properties"]["input"] == INPUT_SCHEMA and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"input", "idempotency_key", "deadline_seconds", "wait_seconds"}
    assert schema["required"] == ["input"]
    assert schema["properties"]["wait_seconds"] == {
        "type": "integer", "minimum": 0, "maximum": 25, "default": 10,
        "description": schema["properties"]["wait_seconds"]["description"]}
    assert live.workflow not in json.dumps([t.model_dump(mode="json") for t in listed.tools])

    exports = served.tool(live.key, "exports.list").ok["exports"]
    assert [e["name"] for e in exports] == ["another", "run_report"]
    assert set(exports[0]) == {"name", "title", "description", "tags", "examples", "input_schema", "result",
                               "require_idempotency_key", "version"}


def test_a_required_idempotency_key_is_in_the_schema_and_enforced(served: Served):
    live = served.live(steps=NOISY_STEPS)
    run = served.tools(live.key).tools[-1]
    assert run.input_schema["required"] == ["input", "idempotency_key"]
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "x"}})
    assert reply.error["code"] == "idempotency_key_required" and live.runs() == []


def test_without_a_required_key_every_call_is_a_new_run_and_says_so(served: Served):
    live = served.live()
    assert "new run" in served.tools(live.key).tools[-1].description
    ids = {served.tool(live.key, "run_run_report", {"input": {"q": "x"}, "wait_seconds": 0}).ok["run_id"]
           for _ in range(2)}
    assert len(ids) == 2


def test_references_inside_the_input_schema_still_resolve_in_the_tool_schema(served: Served):
    import jsonschema

    schema = {
        "type": "object", "$defs": {"name": {"type": "string", "minLength": 2}},
        "properties": {"q": {"$ref": "#/$defs/name"}, "again": {"$ref": "#/properties/q"}},
        "required": ["q"],
    }
    tenant, headers = served.tenant()
    workflow = served.workflow(headers, input_schema=schema)
    export = served.export(headers, workflow, name="refs")
    client = served.client_record(headers)
    key = served.key(headers, client["id"], [workflow])["key"]
    assert served.enable(headers, export).status_code == 200
    tool = served.tools(key).tools[-1].input_schema
    check = jsonschema.Draft202012Validator(tool)
    assert check.is_valid({"input": {"q": "ab", "again": "cd"}})
    assert not check.is_valid({"input": {"q": "a"}})
    assert not check.is_valid({"input": {"q": "ab", "again": "c"}})


@pytest.mark.parametrize("name", ["hidden", "drafted", "missing", "a2a_only", "theirs"])
def test_an_export_the_key_may_not_start_does_not_exist(served: Served, name: str):
    live = served.live()
    hidden = served.workflow(live.headers)
    assert served.enable(live.headers, served.export(live.headers, hidden, name="hidden")).status_code == 200
    served.export(live.headers, live.workflow, name="drafted")
    a2a = served.export(live.headers, live.workflow, name="a2a_only", protocols=["a2a"])
    assert served.enable(live.headers, a2a).status_code == 200
    served.live(name="theirs")
    reply = served.tool(live.key, f"run_{name}", {"input": {"q": "x"}, "idempotency_key": "k"})
    assert reply.error == {"code": "export_not_found", "message": "export not found"}
    assert live.runs() == []


def test_disabling_an_export_removes_its_tool_and_cancels_nothing(served: Served):
    live = served.live()
    run = start(served, live)
    served.client.post(f"{BASE}/exports/{live.export['id']}/disable", headers=live.headers)
    assert [t.name for t in served.tools(live.key).tools] == FIXED_TOOLS
    assert start(served, live)["run_id"] == run["run_id"]  # the retry of a started run
    assert served.tool(live.key, "run_run_report", {"input": {"q": "x"}, "idempotency_key": "new"}).error["code"] \
        == "export_not_found"
    assert served.tool(live.key, "runs.get", {"run_id": run["run_id"]}).ok["state"] == "queued"


def test_bad_input_is_refused_without_echoing_it(served: Served):
    live = served.live()
    reply = served.tool(live.key, "run_run_report", {"input": {"q": 5, "secret": "do-not-echo"}, "idempotency_key": "k"})
    assert reply.error["code"] == "input_invalid" and reply.error["details"]
    assert "do-not-echo" not in json.dumps(reply.data)
    big = served.tool(live.key, "run_run_report", {"input": {"q": "x" * (33 * 1024)}, "idempotency_key": "k"})
    assert big.error["code"] == "input_too_large" and live.runs() == []


@pytest.mark.parametrize("tool, args", [
    ("run_run_report", {}),
    ("run_run_report", {"input": "text"}),
    ("run_run_report", {"input": {"q": "x"}, "wait_seconds": 26}),
    ("run_run_report", {"input": {"q": "x"}, "wait_seconds": -1}),
    ("run_run_report", {"input": {"q": "x"}, "wait_seconds": True}),
    ("run_run_report", {"input": {"q": "x"}, "model": "other"}),
    ("run_run_report", {"input": {"q": "x"}, "version": 1}),
    ("runs.get", {}),
    ("runs.get", {"run_id": 5}),
    ("runs.wait", {"run_id": "x", "timeout_seconds": 61}),
    ("runs.list", {"limit": 51}),
    ("runs.list", {"limit": 0}),
    ("runs.list", {"status": 3}),
    ("runs.cancel", {"run_id": "x", "force": True}),
    ("exports.list", {"tenant_id": "other"}),
])
def test_arguments_outside_the_contract_are_refused(served: Served, tool: str, args: dict):
    live = served.live()
    reply = served.tool(live.key, tool, args)
    assert reply.error["code"] == "invalid_arguments", reply.data
    assert live.runs() == []


# -------------------------------------------------------------------------------------------------- idempotency, quotas


def test_a_retry_is_the_same_run_and_another_input_conflicts(served: Served):
    live = served.live()
    first = start(served, live)
    assert start(served, live)["run_id"] == first["run_id"]
    assert start(served, live) and len(live.runs()) == 1
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "other"}, "idempotency_key": "k-1"})
    assert reply.error["code"] == "idempotency_conflict" and len(live.runs()) == 1


def test_a_retry_after_a_failed_hand_off_starts_the_same_run(served: Served):
    live = served.live()
    served.fake.fail_times = 1
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "hello"}, "idempotency_key": "k-1"})
    assert reply.error["code"] == "start_unavailable"
    assert start(served, live)["state"] == "queued" and len(live.runs()) == 1


def test_a_quota_refusal_says_when_to_come_back(served: Served, external_env):
    live = served.live()
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT", "1")
    start(served, live)
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "x"}, "idempotency_key": "k-2"})
    assert reply.error["code"] == "quota_exceeded" and reply.error["retry_after_seconds"] > 0
    assert len(live.runs()) == 1


def test_grants_that_stopped_being_enforced_refuse_a_start(served: Served, external_env):
    live = served.live()
    external_env.setenv("EAP_AGENT_GRANTS_ENFORCE", "0")
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "x"}, "idempotency_key": "k"})
    assert reply.error["code"] == "grants_not_enforced" and live.runs() == []


# -------------------------------------------------------------------------------------------------- ownership


def test_nothing_of_another_client_tenant_or_person_is_visible(served: Served):
    live = served.live()
    mine = start(served, live)
    stranger = second_client(served, live)
    foreign = served.live()
    person = served.client.post(f"/api/v1/workflows/{live.workflow}/runs", json={"input": {"q": "x"}},
                                headers=live.headers)
    assert person.status_code == 202, person.text
    for key, run_id in (
        (stranger.key, mine["run_id"]), (foreign.key, mine["run_id"]), (live.key, person.json()["run_id"]),
        (live.key, str(uuid.uuid4())), (live.key, "nonsense"),
    ):
        for tool in ("runs.get", "runs.wait", "runs.cancel"):
            reply = served.tool(key, tool, {"run_id": run_id})
            assert reply.error == {"code": "run_not_found", "message": "run not found"}, (tool, run_id)
    assert served.tool(stranger.key, "runs.list").ok == {"items": [], "next_cursor": None}
    assert served.tool(foreign.key, "runs.list").ok["items"] == []
    assert [i["run_id"] for i in served.tool(live.key, "runs.list").ok["items"]] == [mine["run_id"]]
    assert [r["status"] for r in live.runs()] == ["queued", "queued"]


def test_runs_list_pages_and_filters(served: Served):
    live = served.live()
    ids = [start(served, live, idempotency_key=f"k-{n}")["run_id"] for n in range(3)]
    served.finish(live, ids[0], output="done")
    first = served.tool(live.key, "runs.list", {"limit": 2}).ok
    rest = served.tool(live.key, "runs.list", {"limit": 2, "cursor": first["next_cursor"]}).ok
    assert {i["run_id"] for i in [*first["items"], *rest["items"]]} == set(ids) and rest["next_cursor"] is None
    done = served.tool(live.key, "runs.list", {"status": "completed"}).ok["items"]
    assert [i["run_id"] for i in done] == [ids[0]]
    assert served.tool(live.key, "runs.list", {"status": "nope"}).error["code"] == "state_invalid"
    assert served.tool(live.key, "runs.list", {"cursor": "nope"}).error["code"] == "cursor_invalid"


# -------------------------------------------------------------------------------------------------- waiting


def test_a_start_waits_for_a_short_run_and_answers_its_result(served: Served):
    live = served.live()
    run_id = str(uuid.uuid5(uuid.UUID(live.client["id"]), f"{live.export['id']}:k-1"))

    def work() -> None:
        end = time.monotonic() + 20
        while not live.runs() and time.monotonic() < end:
            time.sleep(0.05)
        served.finish(live, run_id, output=json.dumps([1]), output_format="json")

    worker = threading.Thread(target=work)
    worker.start()
    view = start(served, live, wait_seconds=25)
    worker.join()
    assert view["state"] == "completed" and view["result"] == [1]


def test_a_wait_ends_at_its_timeout_with_the_run_still_going(served: Served):
    live = served.live()
    run_id = start(served, live)["run_id"]
    served.running(live, run_id)
    before = time.monotonic()
    view = served.tool(live.key, "runs.wait", {"run_id": run_id, "timeout_seconds": 1}).ok
    assert view["state"] == "running" and view["done"] is False and time.monotonic() - before >= 1


def test_a_wait_returns_when_a_person_is_needed_and_nothing_here_can_decide(served: Served):
    from datetime import datetime, timedelta, timezone

    live = served.live(client_inputs=True)
    run_id = start(served, live)["run_id"]
    served.running(live, run_id)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    served.app.state.run_stores[1].append_approval_waiting(
        live.tenant, uuid.UUID(run_id), "step", "ok_gate",
        {"step_id": "ok_gate", "prompt": "internal prompt", "deadline": deadline, "workflow_id": live.workflow})
    view = served.tool(live.key, "runs.wait", {"run_id": run_id, "timeout_seconds": 60}).ok
    assert view["state"] == "input_required" and view["waiting"] == {"kind": "approval", "answerable": False}
    assert "ok_gate" not in json.dumps(view) and "internal prompt" not in json.dumps(view)
    names = [t.name for t in served.tools(live.key).tools]
    assert not [n for n in names if any(w in n for w in ("approv", "answer", "decide", "reject"))]
    undecided = served.sql(
        "SELECT 1 FROM pending_approvals WHERE tenant_id = :t AND decided_at IS NULL", t=live.tenant)
    assert len(undecided) == 1


# -------------------------------------------------------------------------------------------------- deadlines


def test_the_deadline_is_stored_and_its_cancel_reads_as_a_failure(served: Served):
    from datetime import datetime, timezone

    live = served.live()
    view = start(served, live, deadline_seconds=60)
    left = datetime.fromisoformat(view["deadline"]) - datetime.now(timezone.utc)
    assert 40 < left.total_seconds() <= 60
    reply = served.tool(live.key, "run_run_report", {"input": {"q": "x"}, "idempotency_key": "d", "deadline_seconds": 0})
    assert reply.error["code"] == "invalid_arguments"
    # what the worker's sweep does with an overdue run
    assert ExternalAccessStore(served.engine).request_deadline_cancel(live.tenant, view["run_id"])
    served.app.state.run_stores[0].cancel_run_tree(live.tenant, view["run_id"])
    got = served.tool(live.key, "runs.get", {"run_id": view["run_id"]}).ok
    assert got["state"] == "failed" and got["error"]["code"] == "deadline_exceeded"


# -------------------------------------------------------------------------------------------------- audit


def test_audit_rows_name_the_client_and_never_the_input(served: Served):
    live = served.live()
    run_id = start(served, live, q="very-private-input")["run_id"]
    served.tool(live.key, "runs.cancel", {"run_id": run_id})
    served.tool(live.key, "run_missing", {"input": {"q": "very-private-input"}, "idempotency_key": "secret-key-1"})
    rows = served.sql(
        "SELECT action, outcome, actor_kind, actor_subject, detail FROM audit_log"
        " WHERE tenant_id = :t AND action LIKE 'external.run.%' ORDER BY at", t=live.tenant)
    assert [(r["action"], r["outcome"]) for r in rows] == [
        ("external.run.start", "success"), ("external.run.cancel", "success"), ("external.run.start", "failed")]
    for row in rows:
        assert row["actor_kind"] == "api_key" and row["actor_subject"] == live.key.split("_")[1]
        assert row["detail"]["client"] == live.client["name"]
        assert row["detail"]["agent"].startswith("mcp")  # the caller's self-declared name, a label only
    assert "very-private-input" not in json.dumps([dict(r) for r in rows], default=str)
    assert "secret-key-1" not in json.dumps([dict(r) for r in rows], default=str)
    # one row per change: the HTTP audit middleware adds none of its own
    assert served.sql("SELECT count(*) AS n FROM audit_log WHERE tenant_id = :t AND action NOT LIKE 'external.%'"
                      " AND actor_kind = 'api_key'", t=live.tenant)[0]["n"] == 0


def test_a_declared_name_is_bounded_and_printable(served: Served):
    from eap_api.external_mcp.guard import agent_label

    assert agent_label({"params": {"_meta": {"io.modelcontextprotocol/clientInfo": {"name": "a\n\x00b" + "x" * 500}}}},
                       "ua") == "ab" + "x" * 78
    assert agent_label({"method": "initialize", "params": {"clientInfo": {"name": "codex", "version": "1.0"}}}, "ua") \
        == "codex/1.0"
    assert agent_label(None, "claude-code/2") == "claude-code/2" and agent_label([], None) is None


# -------------------------------------------------------------------------------------------------- limits


def test_a_body_over_the_cap_is_refused(served: Served):
    live = served.live()
    r = served.post(live.key, INITIALIZE | {"padding": "x" * (64 * 1024)})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"

    def chunks():
        for _ in range(70):
            yield b"x" * 1024

    r = served.client.post(MCP_PATH, content=chunks(), headers={"Authorization": f"Bearer {live.key}"})
    assert r.status_code == 413


def test_the_rate_per_key_is_a_tool_error_on_calls_and_429_elsewhere(served: Served, external_env):
    live = served.live()
    other = second_client(served, live)
    external_env.setenv("EAP_EXTERNAL_REQUESTS_PER_KEY_PER_MINUTE", "3")
    for _ in range(3):
        assert served.post(live.key, INITIALIZE).status_code == 200
    r = served.post(live.key, INITIALIZE)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"]["code"] == "rate_limited"
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
        "name": "run_run_report", "arguments": {"input": {"q": "x"}, "idempotency_key": "k"}}}
    r = served.post(live.key, body, headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200, r.text
    assert '"rate_limited"' in r.text and '"retry_after_seconds"' in r.text and '"isError":true' in r.text
    assert live.runs() == []
    assert served.post(other.key, INITIALIZE).status_code == 200  # per key


def test_unauthenticated_noise_is_limited_per_address(served: Served, external_env):
    live = served.live()
    external_env.setenv("EAP_EXTERNAL_IP_RATE_PER_MINUTE", "2")
    served.app.state.external_guard = None
    assert [served.post("nonsense", INITIALIZE).status_code for _ in range(3)] == [401, 401, 429]
    r = served.post(live.key, INITIALIZE)
    assert r.status_code == 429 and r.headers["retry-after"]
    assert served.sql("SELECT count(*) AS n FROM audit_log WHERE tenant_id = :t AND outcome = 'denied'",
                      t=live.tenant)[0]["n"] == 0


def test_concurrent_calls_per_key_are_capped(served: Served, external_env):
    external_env.setenv("EAP_EXTERNAL_MAX_CONCURRENT_PER_KEY", "1")
    live = served.live()
    run_id = start(served, live)["run_id"]
    served.running(live, run_id)

    async def go() -> None:
        async with served.session(live.key) as client:
            waiting = asyncio.create_task(call(client, "runs.wait", {"run_id": run_id, "timeout_seconds": 30}))
            reply = None
            end = time.monotonic() + 20
            while time.monotonic() < end:
                await asyncio.sleep(0.2)
                reply = await call(client, "runs.get", {"run_id": run_id})
                if reply.is_error:
                    break
            assert reply is not None and reply.error["code"] == "rate_limited"
            served.finish(live, run_id, output="ok")
            assert (await waiting).ok["state"] == "completed"
            assert (await call(client, "runs.get", {"run_id": run_id})).ok["state"] == "completed"

    asyncio.run(go())


def test_a_key_revoked_during_a_wait_ends_it(served: Served):
    live = served.live()
    run_id = start(served, live)["run_id"]
    served.running(live, run_id)
    timer = later(0.5, served.sql, "UPDATE api_keys SET revoked_at = now() WHERE tenant_id = :t", t=live.tenant)
    reply = served.tool(live.key, "runs.wait", {"run_id": run_id, "timeout_seconds": 30})
    timer.join()
    assert reply.error["code"] == "key_inactive"


def test_an_unexpected_error_never_leaks(served: Served, monkeypatch):
    from eap_api.services.external_runs import ExternalRunService

    live = served.live()

    def boom(*args, **kwargs):
        raise RuntimeError("host db-1.internal refused")

    monkeypatch.setattr(ExternalRunService, "list", boom)
    reply = served.tool(live.key, "runs.list")
    assert reply.error["code"] == "internal" and "db-1" not in json.dumps(reply.data)
    monkeypatch.setattr(ExternalRunService, "exports", boom)
    with pytest.raises(Exception) as caught:
        served.tools(live.key)
    assert "db-1" not in str(caught.value)


def test_a_refused_client_cannot_fill_the_audit_log_past_its_rate(served: Served, external_env):
    live = served.live()
    patch = f"{BASE}/clients/{live.client['id']}"
    assert served.client.patch(patch, json={"enabled": False}, headers=live.headers).status_code == 200
    external_env.setenv("EAP_EXTERNAL_REQUESTS_PER_KEY_PER_MINUTE", "2")
    assert [served.post(live.key, INITIALIZE).status_code for _ in range(4)] == [403, 403, 429, 429]
    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "runs.list", "arguments": {}}}
    r = served.post(live.key, body, headers={"MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200 and '"rate_limited"' in r.text, r.text
    rows = served.sql("SELECT 1 FROM audit_log WHERE tenant_id = :t AND outcome = 'denied'", t=live.tenant)
    assert len(rows) == 2
