"""The A2A surface `/api/v1/external/a2a/{endpoint_id}` (inbound design 5.2, sections 4, 6, 7 and 8) over the real app on
Postgres, driven in-process by the official `a2a-sdk` client for the pinned protocol (A2A 1.0; 0.3 is refused)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from a2a.client import A2ACardResolver
from a2a.client.errors import A2AClientError, AgentCardResolutionError
from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport
from a2a.types.a2a_pb2 import (
    DeleteTaskPushNotificationConfigRequest,
    GetTaskPushNotificationConfigRequest,
    GetTaskRequest,
    ListTaskPushNotificationConfigsRequest,
    Part,
    Role,
    SendMessageRequest,
    SubscribeToTaskRequest,
    TaskPushNotificationConfig,
    TaskState,
)
from a2a.utils import errors as a2a_errors
from eap_run_store import ExternalAccessStore

from .external_a2a_helpers import (  # noqa: F401
    CARD_PATH,
    ENDPOINT,
    PREFIX,
    A2AWorld,
    a2a,
    as_dict,
    external_env,
    list_request,
    message,
    state,
)
from .external_world import BASE, INPUT_SCHEMA, PUBLIC, Live
from .run_helpers import requires_pg

pytestmark = requires_pg

ONE_STRING = INPUT_SCHEMA  # {"q": string}, required, nothing else
TWO_FIELDS = {
    "type": "object", "properties": {"q": {"type": "string"}, "n": {"type": "integer"}}, "required": ["q"],
    "additionalProperties": False,
}
EXTENSION = "urn:eap:a2a:skill-input-schema:v1"


def later(seconds: float, fn, *args, **kwargs) -> threading.Timer:
    timer = threading.Timer(seconds, fn, args, kwargs)
    timer.start()
    return timer


def send(world: A2AWorld, live: Live, msg=None, **kwargs):
    msg = msg if msg is not None else message({"q": "hello"})
    skill = kwargs.pop("skill", None)
    return world.run(live, lambda agent: agent.send(msg, **kwargs), skill=skill)


def raises(world: A2AWorld, live: Live, error: type[Exception], body, **kwargs) -> Exception:
    with pytest.raises(error) as caught:
        world.run(live, body, **kwargs)
    return caught.value


# -------------------------------------------------------------------------------------------------- acceptance


def test_the_official_client_resolves_sends_gets_lists_and_cancels(a2a: A2AWorld):
    live = a2a.a2a()

    async def go(agent) -> None:
        assert agent.card.name == "Acme automations"
        assert [i.protocol_version for i in agent.card.supported_interfaces] == ["1.0"]
        extended = await agent.extended()
        assert [s.id for s in extended.skills] == ["run_report"]

        task = await agent.send(message({"skill": "run_report", "input": {"q": "x"}}, context_id="ctx-1"))
        assert state(task) == "SUBMITTED" and task.context_id == "ctx-1"
        a2a.running(live, task.id)
        got = await agent.get(task.id)
        assert state(got) == "WORKING" and not got.artifacts and not got.history
        a2a.finish(live, task.id, output=json.dumps({"total": 3}), output_format="json")
        done = await agent.get(task.id)
        assert state(done) == "COMPLETED"
        assert [a.artifact_id for a in done.artifacts] == ["result"]
        assert as_dict(done.artifacts[0].parts[0]) == {"data": {"total": 3}, "mediaType": "application/json"}

        second = await agent.send(message({"q": "y"}))
        cancelled = await agent.cancel(second.id)
        assert state(cancelled) == "CANCELED"
        listed = await agent.list()
        assert [t.id for t in listed.tasks] == [second.id, task.id]
        assert listed.total_size == 2 and listed.next_page_token == "" and listed.page_size == 50
        assert not any(t.artifacts for t in listed.tasks)

    a2a.run(live, go)
    runs = live.runs()
    assert [r["principal"] for r in runs] == [f"api_key:{live.key.split('_')[1]}"] * 2
    assert [r["started_by_kind"] for r in runs] == ["api_key"] * 2 and [r["on_behalf_of"] for r in runs] == [None] * 2
    source = ExternalAccessStore(a2a.engine).run_source(live.tenant, str(runs[0]["id"]))
    assert source["kind"] == "external_a2a" and source["export"] == "run_report"


def test_a_version_0_3_client_is_refused_with_version_not_supported(a2a: A2AWorld):
    live = a2a.a2a()

    async def go() -> None:
        async with a2a.http(live.key) as http:
            card = await A2ACardResolver(http, a2a.base(live)).get_agent_card()
            old = CompatJsonRpcTransport(http, card, card.supported_interfaces[0].url, is_shared_httpx=True)
            request = SendMessageRequest(message=message({"q": "x"}))
            with pytest.raises(a2a_errors.VersionNotSupportedError):
                await old.send_message(request)
            with pytest.raises(a2a_errors.VersionNotSupportedError):
                await old.get_task(GetTaskRequest(id=str(uuid.uuid4())))

    asyncio.run(go())
    assert live.runs() == []


@pytest.mark.parametrize("headers, query, ok", [
    ({"A2A-Version": "1.0"}, "", True),
    ({"A2A-Version": "1.0.1"}, "", True),
    ({"A2A-Version": ""}, "?A2A-Version=1.0", True),
    ({"A2A-Version": ""}, "", False),
    ({"A2A-Version": "0.3"}, "", False),
    ({"A2A-Version": "1.1"}, "", False),
    ({"A2A-Version": "2.0"}, "", False),
    ({"A2A-Version": "latest"}, "", False),
])
def test_the_version_is_the_header_or_the_query_parameter(a2a: A2AWorld, headers: dict, query: str, ok: bool):
    live = a2a.a2a()
    path = f"{PREFIX}/{live.endpoint['endpoint_id']}/rpc{query}"
    r = a2a.rpc(live, headers=headers, path=path)
    assert r.status_code == 200, r.text
    if ok:
        assert r.json()["result"]["tasks"] == []
    else:
        error = r.json()["error"]
        assert error["code"] == -32009 and error["data"][0]["reason"] == "VERSION_NOT_SUPPORTED" and r.json()["id"] == 1


def test_the_documented_client_example_runs_a_workflow(a2a: A2AWorld, capsys):
    """docs/examples/external_a2a_client.py, as an agent would use it."""
    path = Path(__file__).resolve().parents[3] / "docs" / "examples" / "external_a2a_client.py"
    spec = importlib.util.spec_from_file_location("external_a2a_client", path)
    example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example)
    live = a2a.a2a()

    def work() -> None:
        end = time.monotonic() + 20
        while not live.runs() and time.monotonic() < end:
            time.sleep(0.05)
        a2a.finish(live, str(live.runs()[0]["id"]), output=json.dumps({"total": 3}), output_format="json")

    async def go() -> None:
        async with a2a.http(live.key) as http:
            client = await example.connect(http, a2a.base(live))
            assert [s.id for s in await example.skills(client)] == ["run_report"]
            worker = threading.Thread(target=work)
            worker.start()
            task = await example.run_workflow(client, "run_report", {"q": "x"}, message_id="m-1", poll_seconds=0.05)
            worker.join()
            assert example.result_of(task) == {"total": 3}
            again = await example.run_workflow(client, "run_report", {"q": "x"}, message_id="m-1")
            assert again.id == task.id

    asyncio.run(go())
    assert len(live.runs()) == 1


# -------------------------------------------------------------------------------------------------- cards


def test_the_public_card_lists_no_workflow_and_says_what_it_can_do(a2a: A2AWorld):
    live = a2a.a2a()
    url = f"{PREFIX}/{live.endpoint['endpoint_id']}{CARD_PATH}"
    r = a2a.client.get(url)  # no credential
    assert r.status_code == 200, r.text
    card = r.json()
    assert card["name"] == "Acme automations" and card["description"] == "Runs the exported workflows."
    assert card["supportedInterfaces"] == [{"url": f"{a2a.base(live)}/rpc", "protocolBinding": "JSONRPC",
                                            "protocolVersion": "1.0"}]
    capabilities = card["capabilities"]
    assert capabilities["streaming"] is False and capabilities["pushNotifications"] is False
    assert capabilities["extendedAgentCard"] is True
    assert [(e["uri"], e.get("required", False), "params" in e) for e in capabilities["extensions"]] == [
        (EXTENSION, False, False)]
    assert card["securitySchemes"] == {"runKey": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}
    assert card["securityRequirements"] == [{"schemes": {"runKey": {"list": []}}}]
    assert [s["id"] for s in card["skills"]] == ["run_exported_workflow"]
    assert "returnImmediately" in card["skills"][0]["description"]
    everything = json.dumps(card)
    assert "run_report" not in everything and live.workflow not in everything and live.tenant not in everything
    assert r.headers["cache-control"] == "public, max-age=300" and r.headers["etag"]
    again = a2a.client.get(url, headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    a2a.make_endpoint(live.headers, card_name="Renamed")
    changed = a2a.client.get(url, headers={"If-None-Match": r.headers["etag"]})
    assert changed.status_code == 200 and changed.json()["name"] == "Renamed"
    assert a2a.client.post(url).status_code == 405


def test_the_public_card_of_a_skill_base_says_nothing_about_the_skill(a2a: A2AWorld):
    live = a2a.a2a()
    cards = []
    for skill in ("run_report", "no_such_skill"):
        r = a2a.client.get(f"{PREFIX}/{live.endpoint['endpoint_id']}/skills/{skill}{CARD_PATH}")
        assert r.status_code == 200, r.text
        assert r.json()["supportedInterfaces"][0]["url"] == f"{a2a.base(live, skill)}/rpc"
        cards.append(r.text.replace(skill, "NAME").replace(r.json()["version"], "V"))
    assert cards[0] == cards[1] and "Run the report" not in cards[0]
    assert a2a.client.get(
        f"{PREFIX}/{live.endpoint['endpoint_id']}/skills/Not%20A%20Name{CARD_PATH}").status_code == 404


def test_the_extended_card_lists_the_skills_this_key_reaches(a2a: A2AWorld):
    live = a2a.a2a(examples=["Report for Q3"], tags=["finance"])
    hidden = a2a.workflow(live.headers)
    assert a2a.enable(live.headers, a2a.export(live.headers, hidden, name="not_allowed", protocols=["a2a"])).status_code == 200
    a2a.export(live.headers, live.workflow, name="drafted_only", protocols=["a2a"])
    a2a.another_export(live, "mcp_only", protocols=["mcp"])
    a2a.another_export(live, "another", title="Another one", description="")
    a2a.a2a(name="other_tenants")

    card = as_dict(a2a.run(live, lambda agent: agent.extended()))
    skills = {s["id"]: s for s in card["skills"]}
    assert list(skills) == ["another", "run_report"]
    report = skills["run_report"]
    assert report["name"] == "Run the report" and report["description"].startswith("Builds the report.")
    assert report["tags"] == ["finance"] and "Report for Q3" in report["examples"]
    example = json.loads(report["examples"][0])
    assert example["skill"] == "run_report" and set(example["input"]) == {"q"}
    assert report["inputModes"] == ["application/json", "text/plain"]
    assert report["outputModes"] == ["application/json"]
    assert skills["another"]["description"] and skills["another"]["tags"]  # both required by the specification
    (extension,) = card["capabilities"]["extensions"]
    assert extension["uri"] == EXTENSION and set(extension["params"]) == {"another", "run_report"}
    assert extension["params"]["run_report"]["required"] == ["q"]
    assert live.workflow not in json.dumps(card) and "not_allowed" not in json.dumps(card)

    only = as_dict(a2a.run(live, lambda agent: agent.extended(), skill="another"))
    assert [s["id"] for s in only["skills"]] == ["another"]
    assert only["supportedInterfaces"][0]["url"] == f"{a2a.base(live, 'another')}/rpc"


@pytest.mark.parametrize("skill", ["not_allowed", "missing", None])
def test_without_a_reachable_skill_there_is_no_extended_card(a2a: A2AWorld, skill: str | None):
    live = a2a.a2a()
    if skill is None:  # the key reaches nothing at all
        a2a.client.post(f"{BASE}/exports/{live.export['id']}/disable", headers=live.headers)
    raises(a2a, live, a2a_errors.ExtendedAgentCardNotConfiguredError, lambda agent: agent.extended(), skill=skill)


def test_the_unauthenticated_card_is_limited_per_address(a2a: A2AWorld, external_env):  # noqa: F811
    live = a2a.a2a()
    external_env.setenv("EAP_EXTERNAL_CARD_REQUESTS_PER_IP_PER_MINUTE", "2")
    url = f"{PREFIX}/{live.endpoint['endpoint_id']}{CARD_PATH}"
    codes = [a2a.client.get(url).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert int(a2a.client.get(url).headers["retry-after"]) >= 1
    assert a2a.rpc(live).status_code == 200  # the card's bucket is its own


@pytest.mark.parametrize("why", ["switch_off", "no_base_url", "disabled", "unknown", "rotated", "malformed"])
def test_where_there_is_no_endpoint_there_is_404(a2a: A2AWorld, external_env, why: str):  # noqa: F811
    live = a2a.a2a()
    endpoint_id = live.endpoint["endpoint_id"]
    if why == "switch_off":
        external_env.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "false")
    elif why == "no_base_url":
        external_env.setenv("EAP_EXTERNAL_BASE_URL", "")
    elif why == "disabled":
        a2a.make_endpoint(live.headers, enabled=False)
    elif why == "unknown":
        endpoint_id = "0" * 32
    elif why == "rotated":
        assert a2a.client.post(f"{ENDPOINT}/rotate", headers=live.headers).status_code == 200
    else:
        endpoint_id = "x" * 32
    for path in (f"{PREFIX}/{endpoint_id}{CARD_PATH}", f"{PREFIX}/{endpoint_id}/skills/run_report{CARD_PATH}"):
        assert a2a.client.get(path).status_code == 404, path
    for method in ("POST", "GET"):
        r = a2a.rpc(live, method=method, path=f"{PREFIX}/{endpoint_id}/rpc")
        expected = 405 if method == "GET" and why in ("disabled", "unknown", "rotated") else 404
        assert r.status_code == expected, (method, r.status_code, r.text)

    async def resolve() -> None:
        async with a2a.http(live.key) as http:
            with pytest.raises(AgentCardResolutionError) as caught:
                await A2ACardResolver(http, f"{PUBLIC}{PREFIX}/{endpoint_id}").get_agent_card()
            assert caught.value.status_code == 404

    asyncio.run(resolve())
    assert live.runs() == []


# -------------------------------------------------------------------------------------------------- who may call


def test_only_a_run_key_of_this_tenant_gets_in(a2a: A2AWorld):
    live = a2a.a2a()
    foreign = a2a.a2a()
    r = a2a.rpc(live, key=None)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert a2a.rpc(live, key="pos_nonsense").status_code == 401
    tenant_headers = a2a.rpc(live, key=None, headers={"Authorization": live.headers["Authorization"]})
    assert tenant_headers.status_code == 401  # a person's bearer
    mcp = a2a.client.post("/api/v1/api-keys", json={"name": "m", "scopes": ["mcp"]}, headers=live.headers)
    assert mcp.status_code == 201, mcp.text
    assert a2a.rpc(live, key=mcp.json()["key"]).status_code == 403
    cross = a2a.rpc(live, key=foreign.key)  # another tenant's endpoint
    assert cross.status_code == 404 and "tenant" not in cross.text
    rows = a2a.sql("SELECT detail FROM audit_log WHERE tenant_id = :t AND outcome <> 'success'"
                   " AND action = 'external.access'", t=foreign.tenant)
    assert [row["detail"]["reason"] for row in rows] == ["endpoint_not_found"]
    # a run key is refused everywhere else, the MCP control endpoint included
    assert a2a.client.post("/api/v1/mcp", json={}, headers={"Authorization": f"Bearer {live.key}"}).status_code == 403
    assert a2a.client.get("/api/v1/workflows", headers={"Authorization": f"Bearer {live.key}"}).status_code in (401, 403)
    assert live.runs() == [] and foreign.runs() == []


def test_a_revoked_key_a_disabled_client_and_a_foreign_origin_stop_at_once(a2a: A2AWorld):
    live = a2a.a2a()
    patch = f"{BASE}/clients/{live.client['id']}"
    assert a2a.client.patch(patch, json={"enabled": False}, headers=live.headers).status_code == 200
    r = a2a.rpc(live)
    assert r.status_code == 403 and r.json()["error"]["code"] == "client_disabled", r.text
    assert a2a.client.patch(patch, json={"enabled": True}, headers=live.headers).status_code == 200
    assert a2a.rpc(live).status_code == 200
    limited = a2a.key(live.headers, live.client["id"], [live.workflow],
                      allowed_origins=["https://agents.example.test"])["key"]
    assert a2a.rpc(live, key=limited, headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert a2a.rpc(live, key=limited, headers={"Origin": "https://agents.example.test"}).status_code == 200
    a2a.sql("UPDATE api_keys SET revoked_at = now() WHERE tenant_id = :t", t=live.tenant)
    assert a2a.rpc(live).status_code == 401
    error = raises(a2a, live, A2AClientError, lambda agent: agent.list())
    assert "401" in str(error)


# -------------------------------------------------------------------------------------------------- sending


def test_a_send_creates_a_task_with_a_generated_context(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live)
    assert state(task) == "SUBMITTED" and task.id == str(live.runs()[0]["id"])
    assert task.context_id and task.context_id != task.id
    assert task.status.timestamp.seconds > 0 and not task.artifacts and not task.history
    metadata = as_dict(task)["metadata"]
    assert metadata["export"] == "run_report" and metadata["version"] == 1 and metadata["deadline"].endswith("Z")
    assert json.loads(live.runs()[0]["input"]) == {"q": "hello"}
    assert a2a.run(live, lambda agent: agent.get(task.id)).context_id == task.context_id


@pytest.mark.parametrize("form", ["envelope", "metadata", "only_export", "text", "skill_base", "skill_base_text"])
def test_the_ways_a_message_names_its_skill_and_input(a2a: A2AWorld, form: str):
    live = a2a.a2a()
    kwargs: dict = {}
    if form not in ("only_export", "text"):
        a2a.another_export(live, "second")
    if form == "envelope":
        msg = message({"skill": "second", "input": {"q": "v"}})
    elif form == "metadata":
        msg = message({"q": "v"}, metadata={"skill": "second"})
    elif form == "only_export":
        msg = message({"q": "v"})
    elif form == "text":
        msg = message(text="v")
    elif form == "skill_base":
        msg, kwargs = message({"q": "v"}), {"skill": "second"}
    else:
        msg, kwargs = message(text="v"), {"skill": "second"}
    task = send(a2a, live, msg, **kwargs)
    assert as_dict(task)["metadata"]["export"] == ("run_report" if form in ("only_export", "text") else "second")
    assert json.loads(live.runs()[0]["input"]) == {"q": "v"}


def test_whole_numbers_of_a_data_part_reach_the_workflow_as_integers(a2a: A2AWorld):
    live = a2a.a2a(name="count")
    workflow = a2a.workflow(live.headers, input_schema=TWO_FIELDS)
    export = a2a.export(live.headers, workflow, name="two", protocols=["a2a"])
    assert a2a.enable(live.headers, export).status_code == 200
    key = a2a.key(live.headers, live.client["id"], [workflow])["key"]
    msg = message({"q": "x", "n": 3})
    task = a2a.run(live, lambda agent: agent.send(msg), key=key)
    stored = a2a.sql("SELECT input FROM runs WHERE tenant_id = :t AND id = :i", t=live.tenant, i=task.id)[0]["input"]
    assert '"n":3}' in stored.replace(" ", "") or '"n":3,' in stored.replace(" ", ""), stored


def test_without_a_skill_and_with_several_the_refusal_names_them(a2a: A2AWorld):
    live = a2a.a2a()
    a2a.another_export(live, "second")
    for msg in (message({"q": "x"}), message(text="x")):
        error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(msg))
        assert "run_report" in error.message and "second" in error.message
    assert live.runs() == []


@pytest.mark.parametrize("name", ["hidden", "drafted", "missing", "mcp_only", "theirs"])
def test_a_skill_the_key_may_not_start_is_refused(a2a: A2AWorld, name: str):
    live = a2a.a2a()
    hidden = a2a.workflow(live.headers)
    assert a2a.enable(live.headers, a2a.export(live.headers, hidden, name="hidden", protocols=["a2a"])).status_code == 200
    a2a.export(live.headers, live.workflow, name="drafted", protocols=["a2a"])
    a2a.another_export(live, "mcp_only", protocols=["mcp"])
    a2a.a2a(name="theirs")
    for msg, kwargs in (
        (message({"skill": name, "input": {"q": "x"}}), {}),
        (message({"q": "x"}, metadata={"skill": name}), {}),
        (message({"q": "x"}), {"skill": name}),
    ):
        error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(msg), **kwargs)
        assert "run_report" in error.message and error.data["reason"] == "export_not_found"
    assert live.runs() == []


def test_a_skill_base_runs_its_own_skill_only(a2a: A2AWorld):
    live = a2a.a2a()
    a2a.another_export(live, "second")
    msg = message({"skill": "run_report", "input": {"q": "x"}})
    error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(msg), skill="second")
    assert error.data["reason"] == "skill_mismatch" and live.runs() == []
    assert state(send(a2a, live, message({"skill": "second", "input": {"q": "x"}}), skill="second")) == "SUBMITTED"


def raw_part(**fields) -> Part:
    return Part(**fields)


@pytest.mark.parametrize("parts", [
    [raw_part(raw=b"%PDF", media_type="application/pdf")],
    [raw_part(url="https://example.com/file", media_type="application/json")],
    [raw_part(text="x"), raw_part(url="https://example.com/file")],
    [raw_part(text="<b>x</b>", media_type="text/html")],
])
def test_files_urls_and_other_media_types_are_not_accepted(a2a: A2AWorld, parts: list):
    live = a2a.a2a()
    raises(a2a, live, a2a_errors.ContentTypeNotSupportedError, lambda agent: agent.send(message(parts=parts)))
    assert live.runs() == []


def test_text_is_not_accepted_for_a_structured_input(a2a: A2AWorld):
    tenant, headers = a2a.tenant()
    workflow = a2a.workflow(headers, input_schema=TWO_FIELDS)
    export = a2a.export(headers, workflow, name="two", protocols=["a2a"])
    assert a2a.enable(headers, export).status_code == 200
    client = a2a.client_record(headers)
    live = Live(a2a, tenant, headers, workflow, export, client, a2a.key(headers, client["id"], [workflow])["key"])
    live.endpoint = a2a.make_endpoint(headers)  # type: ignore[attr-defined]
    raises(a2a, live, a2a_errors.ContentTypeNotSupportedError, lambda agent: agent.send(message(text="x")))
    skill = as_dict(a2a.run(live, lambda agent: agent.extended()))["skills"][0]
    assert skill["inputModes"] == ["application/json"]
    assert live.runs() == []


@pytest.mark.parametrize("change, field", [
    ({"parts": []}, "message.parts"),
    ({"parts": [{}]}, "message.parts"),
    ({"parts": [{"text": "a", "data": {}}]}, "message.parts"),
    ({"parts": [{"data": {"q": "a"}}, {"data": {"q": "b"}}]}, "message.parts"),
    ({"parts": [{"data": {"q": "a"}}, {"text": "b"}]}, "message.parts"),
    ({"parts": [{"data": "just a string"}]}, "message.parts"),
    ({"parts": [{"text": 5}]}, "message.parts"),
    ({"messageId": ""}, "message.messageId"),
    ({"messageId": "m" * 129}, "message.messageId"),
    ({"messageId": None}, "message.messageId"),
    ({"role": "ROLE_AGENT"}, "message.role"),
    ({"role": None}, "message.role"),
    ({"contextId": "c" * 201}, "message.contextId"),
    ({"contextId": 5}, "message.contextId"),
    ({"taskId": 5}, "message.taskId"),
    ({"metadata": {"skill": 5}}, "metadata.skill"),
    ({"metadata": {"skill": "s" * 201}}, "metadata.skill"),
    ({"contextId": "a\x00b"}, "message.contextId"),
    ({"metadata": "x"}, "message.metadata"),
])
def test_a_message_outside_the_contract_is_invalid_params(a2a: A2AWorld, change: dict, field: str):
    live = a2a.a2a()
    msg = {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"data": {"q": "x"}}]} | change
    msg = {k: v for k, v in msg.items() if v is not None}
    payload = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}}
    error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.raw(payload))
    r = a2a.rpc(live, payload)
    violations = [d for d in r.json()["error"]["data"] if d["@type"].endswith("BadRequest")]
    assert violations and violations[0]["fieldViolations"][0]["field"] == field, (error, r.text)
    assert live.runs() == []


@pytest.mark.parametrize("params", [
    None, [], {}, {"message": "x"},
    {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": {"q": "x"}}]}, "configuration": "x"},
    {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": {"q": "x"}}]},
     "configuration": {"returnImmediately": "yes"}},
    {"message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": {"q": "x"}}]}, "metadata": 4},
])
def test_send_params_outside_the_contract_are_invalid_params(a2a: A2AWorld, params):
    live = a2a.a2a()
    payload = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage"} | ({} if params is None else {"params": params})
    raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.raw(payload))
    assert live.runs() == []


@pytest.mark.parametrize("field", ["messageId", "contextId", "taskId"])
def test_an_id_the_database_cannot_hold_is_invalid_params(a2a: A2AWorld, field: str):
    live = a2a.a2a()
    msg = {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": {"q": "x"}}]} | {field: "a\ud800"}
    r = a2a.rpc(live, {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602, r.text
    assert live.runs() == []


def test_bad_input_is_refused_without_echoing_it(a2a: A2AWorld):
    live = a2a.a2a()
    msg = message({"q": 5, "secret": "do-not-echo"})
    error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(msg))
    assert error.data["reason"] == "input_invalid"
    r = a2a.rpc(live, {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": as_dict(msg)}})
    assert "do-not-echo" not in r.text
    assert [d["@type"].rsplit(".", 1)[1] for d in r.json()["error"]["data"]] == ["ErrorInfo", "BadRequest"]
    big = message({"q": "x" * (33 * 1024)})
    error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(big))
    assert error.data["reason"] == "input_too_large" and live.runs() == []


def test_push_notification_settings_in_a_send_are_not_supported(a2a: A2AWorld):
    live = a2a.a2a()
    config = TaskPushNotificationConfig(url="https://example.com/hook")
    raises(a2a, live, a2a_errors.PushNotificationNotSupportedError,
           lambda agent: agent.send(message({"q": "x"}), task_push_notification_config=config))
    assert live.runs() == []


def test_the_deadline_comes_from_the_metadata_and_its_cancel_reads_as_a_failure(a2a: A2AWorld):
    live = a2a.a2a()
    for where in ("request", "message"):
        msg = message({"q": "x"}, metadata={"deadlineSeconds": 60} if where == "message" else None)
        task = send(a2a, live, msg, metadata={"deadlineSeconds": 60} if where == "request" else None)
        deadline = datetime.fromisoformat(as_dict(task)["metadata"]["deadline"])
        assert 40 < (deadline - datetime.now(timezone.utc)).total_seconds() <= 60
    for bad in (0, -1, 1.5, "60", True):
        error = raises(a2a, live, a2a_errors.InvalidParamsError,
                       lambda agent: agent.send(message({"q": "x"}), metadata={"deadlineSeconds": bad}))
        assert error.data["reason"] == "deadline_invalid", bad
    assert len(live.runs()) == 2
    # what the worker's sweep does with an overdue run
    assert ExternalAccessStore(a2a.engine).request_deadline_cancel(live.tenant, task.id)
    a2a.app.state.run_stores[0].cancel_run_tree(live.tenant, task.id)
    got = a2a.run(live, lambda agent: agent.get(task.id))
    assert state(got) == "FAILED" and as_dict(got)["metadata"]["errorCode"] == "deadline_exceeded"
    assert "deadline" in got.status.message.parts[0].text and got.status.message.role == Role.ROLE_AGENT


# -------------------------------------------------------------------------------------------------- idempotency, quotas


def test_a_resend_of_the_same_message_is_the_same_task(a2a: A2AWorld):
    live = a2a.a2a()
    msg = message({"q": "x"}, message_id="m-1")
    first = send(a2a, live, msg)
    again = send(a2a, live, msg)
    assert (again.id, again.context_id) == (first.id, first.context_id) and len(live.runs()) == 1
    for other in (
        message({"q": "other"}, message_id="m-1"),
        message({"q": "x"}, message_id="m-1", context_id="another-context"),
    ):
        error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(other))
        assert error.data["reason"] == "idempotency_conflict"
    stranger = a2a.second_client(live)
    assert send(a2a, stranger, msg).id != first.id and len(live.runs()) == 2


def test_a_resend_finds_its_task_although_the_exports_changed(a2a: A2AWorld):
    live = a2a.a2a()
    msg = message({"q": "x"}, message_id="m-1")
    first = send(a2a, live, msg)
    a2a.another_export(live, "second")  # the skill is no longer implied
    assert send(a2a, live, msg).id == first.id
    a2a.client.post(f"{BASE}/exports/{live.export['id']}/disable", headers=live.headers)  # only `second` is left
    assert send(a2a, live, msg).id == first.id and len(live.runs()) == 1
    error = raises(a2a, live, a2a_errors.InvalidParamsError,
                   lambda agent: agent.send(message({"skill": "run_report", "input": {"q": "x"}})))
    assert error.data["reason"] == "export_not_found"


def test_a_retry_after_a_failed_hand_off_starts_the_same_task(a2a: A2AWorld):
    live = a2a.a2a()
    msg = message({"q": "x"}, message_id="m-1")
    a2a.fake.fail_times = 1
    payload = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage",
               "params": {"message": as_dict(msg), "configuration": {"returnImmediately": True}}}
    r = a2a.rpc(live, payload)
    assert r.status_code == 503 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["code"] == -32603 and r.json()["error"]["data"][0]["metadata"]["reason"] == "start_unavailable"
    assert state(send(a2a, live, msg)) == "SUBMITTED" and len(live.runs()) == 1


def test_a_quota_refusal_is_429_with_retry_after(a2a: A2AWorld, external_env):  # noqa: F811
    live = a2a.a2a()
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT", "1")
    send(a2a, live)
    error = raises(a2a, live, A2AClientError, lambda agent: agent.send(message({"q": "y"})))
    assert "429" in str(error)
    payload = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": as_dict(message({"q": "y"}))}}
    r = a2a.rpc(live, payload)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["error"]["data"][0]["metadata"]["reason"] == "quota_exceeded" and len(live.runs()) == 1


def test_grants_that_stopped_being_enforced_refuse_a_start(a2a: A2AWorld, external_env):  # noqa: F811
    live = a2a.a2a()
    external_env.setenv("EAP_AGENT_GRANTS_ENFORCE", "0")
    error = raises(a2a, live, a2a_errors.UnsupportedOperationError, lambda agent: agent.send(message({"q": "x"})))
    assert error.data["reason"] == "grants_not_enforced" and live.runs() == []


# -------------------------------------------------------------------------------------------------- blocking sends


def test_a_blocking_send_answers_the_finished_task(a2a: A2AWorld):
    live = a2a.a2a()

    def work() -> None:
        end = time.monotonic() + 20
        while not live.runs() and time.monotonic() < end:
            time.sleep(0.05)
        a2a.finish(live, str(live.runs()[0]["id"]), output=json.dumps([1]), output_format="json")

    worker = threading.Thread(target=work)
    worker.start()
    task = send(a2a, live, wait=True)
    worker.join()
    assert state(task) == "COMPLETED" and as_dict(task.artifacts[0].parts[0])["data"] == [1]


def test_a_blocking_send_returns_when_a_person_is_needed(a2a: A2AWorld):
    live = a2a.a2a()

    def work() -> None:
        end = time.monotonic() + 20
        while not live.runs() and time.monotonic() < end:
            time.sleep(0.05)
        run_id = str(live.runs()[0]["id"])
        a2a.running(live, run_id)
        a2a.await_approval(live, run_id)

    worker = threading.Thread(target=work)
    worker.start()
    task = send(a2a, live, wait=True)
    worker.join()
    assert state(task) == "INPUT_REQUIRED"


def test_a_blocking_send_stops_waiting_after_60_seconds_with_the_working_task(a2a: A2AWorld, monkeypatch):
    from eap_api.external_a2a import server

    assert server.BLOCKING_SECONDS == 60  # decision Q6
    monkeypatch.setattr(server, "BLOCKING_SECONDS", 1)
    live = a2a.a2a()
    msg = message({"q": "hello"}, message_id="blocked")
    first = send(a2a, live, msg)  # returnImmediately
    a2a.running(live, first.id)
    before = time.monotonic()
    waited = send(a2a, live, msg, wait=True)
    assert waited.id == first.id and state(waited) == "WORKING" and time.monotonic() - before >= 1
    before = time.monotonic()
    send(a2a, live, msg)
    assert time.monotonic() - before < 30  # returnImmediately never waits for the run


# -------------------------------------------------------------------------------------------------- follow-up messages


def test_a_message_to_a_task_changes_nothing_in_this_release(a2a: A2AWorld):
    live = a2a.a2a(client_inputs=True)
    task = send(a2a, live, message({"q": "x"}, context_id="c-1"))
    a2a.running(live, task.id)
    for waiting in (False, True):
        if waiting:
            a2a.await_approval(live, task.id)
            assert state(a2a.run(live, lambda agent: agent.get(task.id))) == "INPUT_REQUIRED"
        reply = message({"approved": True}, task_id=task.id, context_id="c-1")
        error = raises(a2a, live, a2a_errors.UnsupportedOperationError, lambda agent: agent.send(reply))
        assert error.data["reason"] == "answer_not_supported"
    mismatch = message({"approved": True}, task_id=task.id, context_id="other")
    error = raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.send(mismatch))
    assert error.data["reason"] == "context_mismatch"
    undecided = a2a.sql("SELECT 1 FROM pending_approvals WHERE tenant_id = :t AND decided_at IS NULL", t=live.tenant)
    assert len(undecided) == 1 and len(live.runs()) == 1

    a2a.finish(live, task.id, output="done")
    reply = message(text="more", task_id=task.id)
    error = raises(a2a, live, a2a_errors.UnsupportedOperationError, lambda agent: agent.send(reply))
    assert error.data["reason"] == "task_terminal"
    rows = a2a.sql("SELECT outcome, detail FROM audit_log WHERE tenant_id = :t AND action = 'external.run.answer'"
                   " ORDER BY at", t=live.tenant)
    assert [r["detail"]["reason"] for r in rows] == [
        "answer_not_supported", "answer_not_supported", "context_mismatch", "task_terminal"]


def test_a_waiting_task_says_a_person_decides_and_nothing_else(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live)
    a2a.running(live, task.id)
    a2a.await_approval(live, task.id)
    got = a2a.run(live, lambda agent: agent.get(task.id))
    assert state(got) == "INPUT_REQUIRED"
    assert got.status.message.parts[0].text == "waiting for a person in the platform to decide"
    assert got.status.message.task_id == task.id and got.status.message.context_id == task.context_id
    text = json.dumps(as_dict(got))
    assert "ok_gate" not in text and "internal prompt" not in text
    assert state(a2a.run(live, lambda agent: agent.cancel(task.id))) == "CANCELED"


# -------------------------------------------------------------------------------------------------- state mapping


@pytest.mark.parametrize("status, kwargs, expected, extra", [
    ("completed", {"output": "plain"}, "COMPLETED", {}),
    ("skipped", {"output": "nothing to do"}, "COMPLETED", {"outcome": "skipped"}),
    ("failed", {"error": "boom at db-1.internal", "end_data": {"error_code": "approval_rejected"}}, "FAILED",
     {"errorCode": "approval_rejected"}),
    ("failed", {"error": "boom at db-1.internal"}, "FAILED", {"errorCode": "run_failed"}),
    ("needs_attention", {"error": "step post: unknown outcome"}, "FAILED",
     {"errorCode": "outcome_unknown", "retrySafe": False}),
])
def test_the_states_of_section_6(a2a: A2AWorld, status: str, kwargs: dict, expected: str, extra: dict):
    live = a2a.a2a()
    task = send(a2a, live)
    a2a.running(live, task.id)
    a2a.finish(live, task.id, status, **kwargs)
    got = a2a.run(live, lambda agent: agent.get(task.id))
    assert state(got) == expected, as_dict(got)
    metadata = as_dict(got)["metadata"]
    assert extra.items() <= metadata.items(), metadata
    everything = json.dumps(as_dict(got))
    assert "db-1" not in everything and "step post" not in everything
    if expected == "COMPLETED":
        assert as_dict(got.artifacts[0].parts[0])["data"] == kwargs["output"]
    else:
        assert not got.artifacts and got.status.message.parts[0].text
    listed = a2a.run(live, lambda agent: agent.list(status=TaskState.Value(f"TASK_STATE_{expected}")))
    assert [t.id for t in listed.tasks] == [task.id]


def test_status_only_and_oversized_results_have_no_artifact(a2a: A2AWorld):
    live = a2a.a2a()
    big = send(a2a, live)
    a2a.finish(live, big.id, output="x" * (256 * 1024 + 1))
    got = a2a.run(live, lambda agent: agent.get(big.id))
    assert state(got) == "COMPLETED" and not got.artifacts and as_dict(got)["metadata"]["truncated"] is True
    quiet = a2a.a2a(result="status_only")
    task = send(a2a, quiet)
    a2a.finish(quiet, task.id, output="private")
    got = a2a.run(quiet, lambda agent: agent.get(task.id))
    assert state(got) == "COMPLETED" and not got.artifacts and "private" not in json.dumps(as_dict(got))


# -------------------------------------------------------------------------------------------------- get, list, cancel


def test_nothing_of_another_client_tenant_or_person_is_visible(a2a: A2AWorld):
    live = a2a.a2a()
    mine = send(a2a, live)
    stranger = a2a.second_client(live)
    foreign = a2a.a2a()
    person = a2a.client.post(f"/api/v1/workflows/{live.workflow}/runs", json={"input": {"q": "x"}},
                             headers=live.headers)
    assert person.status_code == 202, person.text
    for who, task_id in (
        (stranger, mine.id), (foreign, mine.id), (live, person.json()["run_id"]), (live, str(uuid.uuid4())),
        (live, "nonsense"),
    ):
        for body in (
            lambda agent: agent.get(task_id),
            lambda agent: agent.cancel(task_id),
            lambda agent: agent.send(message({"q": "x"}, task_id=task_id)),
        ):
            error = raises(a2a, who, a2a_errors.TaskNotFoundError, body)
            assert error.message == "Task not found" and error.data["taskId"] == task_id[:200]
    for other in (stranger, foreign):
        listed = a2a.run(other, lambda agent: agent.list())
        assert not listed.tasks and listed.total_size == 0
    assert [t.id for t in a2a.run(live, lambda agent: agent.list()).tasks] == [mine.id]
    assert [r["status"] for r in live.runs()] == ["queued", "queued"]


def test_list_pages_filters_and_includes_artifacts_on_request(a2a: A2AWorld):
    live = a2a.a2a()
    ids = [send(a2a, live, message({"q": str(n)}, context_id="batch" if n else "solo")).id for n in range(3)]
    time.sleep(0.01)
    marker = datetime.now(timezone.utc)
    a2a.finish(live, ids[0], output="done")

    async def go(agent) -> None:
        first = await agent.list(page_size=2)
        assert first.page_size == 2 and first.total_size == 3 and first.next_page_token
        assert first.tasks[0].id == ids[0]  # the latest status change first
        rest = await agent.list(page_size=2, page_token=first.next_page_token)
        assert rest.next_page_token == "" and rest.total_size == 3
        assert sorted(t.id for t in [*first.tasks, *rest.tasks]) == sorted(ids)
        stamps = [t.status.timestamp.ToNanoseconds() for t in [*first.tasks, *rest.tasks]]
        assert stamps == sorted(stamps, reverse=True)
        batch = await agent.list(context_id="batch")
        assert sorted(t.id for t in batch.tasks) == sorted(ids[1:]) and batch.total_size == 2
        assert {t.context_id for t in batch.tasks} == {"batch"}
        done = await agent.list(status=TaskState.TASK_STATE_COMPLETED, include_artifacts=True)
        assert [t.id for t in done.tasks] == [ids[0]] and as_dict(done.tasks[0].artifacts[0].parts[0])["data"] == "done"
        queued = await agent.list(status=TaskState.TASK_STATE_SUBMITTED)
        assert sorted(t.id for t in queued.tasks) == sorted(ids[1:])
        for never in (TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_AUTH_REQUIRED):
            empty = await agent.list(status=never)
            assert not empty.tasks and empty.total_size == 0
        request = {"jsonrpc": "2.0", "id": 1, "method": "ListTasks",
                   "params": {"statusTimestampAfter": marker.isoformat().replace("+00:00", "Z")}}
        after = (await agent.raw(request))["result"]
        assert [t["id"] for t in after["tasks"]] == [ids[0]] and after["totalSize"] == 1
        assert "artifacts" not in after["tasks"][0]

    a2a.run(live, go)


def test_a_run_started_over_mcp_is_a_task_with_its_run_id_as_context(a2a: A2AWorld):
    live = a2a.a2a(protocols=["mcp", "a2a"])
    view = live.start()

    async def go(agent) -> None:
        got = await agent.get(view["run_id"])
        assert got.context_id == view["run_id"]
        assert [t.id for t in (await agent.list(context_id=view["run_id"])).tasks] == [view["run_id"]]

    a2a.run(live, go)


@pytest.mark.parametrize("method, params", [
    ("GetTask", {}), ("GetTask", {"id": 5}), ("GetTask", {"id": ""}), ("GetTask", {"id": "x", "historyLength": -1}),
    ("GetTask", {"id": "x", "historyLength": "many"}), ("CancelTask", {}), ("CancelTask", {"id": None}),
    ("ListTasks", {"pageSize": 0}), ("ListTasks", {"pageSize": 101}), ("ListTasks", {"pageSize": True}),
    ("ListTasks", {"pageToken": "nope"}), ("ListTasks", {"status": "RUNNING"}), ("ListTasks", {"historyLength": -2}),
    ("ListTasks", {"statusTimestampAfter": "yesterday"}), ("ListTasks", {"statusTimestampAfter": "2026-01-01T00:00:00"}),
    ("ListTasks", {"contextId": 3}), ("ListTasks", {"includeArtifacts": "yes"}), ("GetExtendedAgentCard", []),
])
def test_params_outside_the_contract_are_invalid_params(a2a: A2AWorld, method: str, params):
    live = a2a.a2a()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    raises(a2a, live, a2a_errors.InvalidParamsError, lambda agent: agent.raw(payload))


def test_history_is_empty_whatever_length_is_asked(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live)
    for length in (0, 5):
        assert not a2a.run(live, lambda agent: agent.get(task.id, history_length=length)).history


def test_cancel_ends_a_task_once(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live)
    a2a.running(live, task.id)
    assert state(a2a.run(live, lambda agent: agent.cancel(task.id))) == "CANCELED"
    error = raises(a2a, live, a2a_errors.TaskNotCancelableError, lambda agent: agent.cancel(task.id))
    assert error.data["taskId"] == task.id
    done = send(a2a, live, message({"q": "y"}))
    a2a.finish(live, done.id, output="ok")
    raises(a2a, live, a2a_errors.TaskNotCancelableError, lambda agent: agent.cancel(done.id))


def test_a_cancel_that_cannot_finish_yet_is_retryable(a2a: A2AWorld, monkeypatch):
    from eap_api.services.errors import Unavailable
    from eap_api.services.external_runs import ExternalRunService

    live = a2a.a2a()
    task = send(a2a, live)

    def pending(*args, **kwargs):
        raise Unavailable("the cancel is recorded and not finished; it completes by itself", code="cancel_unavailable")

    monkeypatch.setattr(ExternalRunService, "cancel", pending)
    r = a2a.rpc(live, {"jsonrpc": "2.0", "id": 4, "method": "CancelTask", "params": {"id": task.id}})
    assert r.status_code == 503 and r.headers["retry-after"] and r.json()["id"] == 4
    assert r.json()["error"]["data"][0]["metadata"]["reason"] == "cancel_unavailable"


# -------------------------------------------------------------------------------------------------- what is not offered


def test_streaming_and_push_are_refused_as_the_card_says(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live)

    async def go(agent) -> None:
        transport = agent.client._transport
        with pytest.raises(a2a_errors.UnsupportedOperationError):
            async for _ in transport.send_message_streaming(SendMessageRequest(message=message({"q": "x"}))):
                pass
        with pytest.raises(a2a_errors.UnsupportedOperationError):
            async for _ in transport.subscribe(SubscribeToTaskRequest(id=task.id)):
                pass
        for call, request in (
            (agent.client.create_task_push_notification_config,
             TaskPushNotificationConfig(task_id=task.id, url="https://example.com/hook")),
            (agent.client.get_task_push_notification_config,
             GetTaskPushNotificationConfigRequest(task_id=task.id, id="c")),
            (agent.client.list_task_push_notification_configs, ListTaskPushNotificationConfigsRequest(task_id=task.id)),
            (agent.client.delete_task_push_notification_config,
             DeleteTaskPushNotificationConfigRequest(task_id=task.id, id="c")),
        ):
            with pytest.raises(a2a_errors.PushNotificationNotSupportedError):
                await call(request)

    a2a.run(live, go)
    assert len(live.runs()) == 1


@pytest.mark.parametrize("method", ["Nope", "message/send", "tasks/get", "ApproveTask", "AnswerTask", "getTask"])
def test_an_unknown_method_is_method_not_found(a2a: A2AWorld, method: str):
    live = a2a.a2a()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    raises(a2a, live, a2a_errors.MethodNotFoundError, lambda agent: agent.raw(payload))


@pytest.mark.parametrize("payload", [
    {"foo": 1}, {"jsonrpc": "1.0", "id": 1, "method": "ListTasks"}, {"jsonrpc": "2.0", "id": 1},
    {"jsonrpc": "2.0", "method": "ListTasks"}, {"jsonrpc": "2.0", "id": {"a": 1}, "method": "ListTasks"},
    {"jsonrpc": "2.0", "id": True, "method": "ListTasks"}, [], [list_request()], "x", 4,
])
def test_a_body_that_is_no_request_is_invalid_request(a2a: A2AWorld, payload):
    live = a2a.a2a()
    raises(a2a, live, a2a_errors.InvalidRequestError, lambda agent: agent.raw(payload))


@pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe", b"[" * 5000])
def test_a_body_that_is_not_json_is_a_parse_error(a2a: A2AWorld, body: bytes):
    live = a2a.a2a()
    r = a2a.rpc(live, body)
    assert r.status_code == 200 and r.json()["id"] is None
    assert r.json()["error"]["code"] == -32700, r.text
    # as the SDK client reads it
    from a2a.client.transports.jsonrpc import JsonRpcTransport

    with pytest.raises(a2a_errors.JSONParseError):
        JsonRpcTransport(None, None, "")._check_jsonrpc_response(r.json())  # type: ignore[arg-type]


def test_an_unexpected_error_never_leaks(a2a: A2AWorld, monkeypatch):
    from eap_api.services.external_runs import ExternalRunService

    live = a2a.a2a()

    def boom(*args, **kwargs):
        raise RuntimeError("host db-1.internal refused")

    monkeypatch.setattr(ExternalRunService, "list", boom)
    error = raises(a2a, live, a2a_errors.InternalError, lambda agent: agent.list())
    assert "db-1" not in error.message


def test_the_card_declares_nothing_that_would_need_the_other_errors(a2a: A2AWorld):
    """`ExtensionSupportRequiredError` needs a required extension; `InvalidAgentResponseError` is a client's finding."""
    live = a2a.a2a()
    card = a2a.run(live, lambda agent: agent.extended())
    assert not any(e.required for e in card.capabilities.extensions)


# -------------------------------------------------------------------------------------------------- limits and audit


def test_the_body_cap_the_key_rate_and_the_concurrency_limit(a2a: A2AWorld, external_env):  # noqa: F811
    live = a2a.a2a()
    r = a2a.rpc(live, list_request(padding="x" * (64 * 1024)))
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    other = a2a.second_client(live)
    external_env.setenv("EAP_EXTERNAL_REQUESTS_PER_KEY_PER_MINUTE", "2")
    a2a.app.state.external_guard = None
    assert [a2a.rpc(live).status_code for _ in range(3)] == [200, 200, 429]
    r = a2a.rpc(live)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and r.json()["error"]["code"] == "rate_limited"
    assert a2a.rpc(other).status_code == 200  # per key


def test_concurrent_calls_per_key_are_capped(a2a: A2AWorld, external_env):  # noqa: F811
    external_env.setenv("EAP_EXTERNAL_MAX_CONCURRENT_PER_KEY", "1")
    live = a2a.a2a()

    async def go() -> None:
        async with a2a.agent(live) as agent:
            blocked = asyncio.create_task(agent.send(message({"q": "x"}), wait=True))
            # The run row says the send holds the key's one slot: asking earlier would race it for the slot.
            runs: list = []
            end = time.monotonic() + 30
            while not runs and time.monotonic() < end:
                await asyncio.sleep(0.05)
                runs = await asyncio.to_thread(live.runs)
            with pytest.raises(A2AClientError, match="429"):
                await agent.list()
            await asyncio.to_thread(a2a.finish, live, str(runs[0]["id"]), "completed", output="ok")
            assert state(await blocked) == "COMPLETED"
            assert (await agent.list()).total_size == 1

    asyncio.run(go())


def test_unauthenticated_calls_are_limited_per_address_and_write_nothing(a2a: A2AWorld, external_env):  # noqa: F811
    live = a2a.a2a()
    external_env.setenv("EAP_EXTERNAL_IP_RATE_PER_MINUTE", "2")
    a2a.app.state.external_guard = None
    assert [a2a.rpc(live, key="nonsense").status_code for _ in range(3)] == [401, 401, 429]
    assert a2a.sql("SELECT count(*) AS n FROM audit_log WHERE tenant_id = :t AND outcome = 'denied'",
                   t=live.tenant)[0]["n"] == 0


def test_audit_rows_name_the_client_and_never_the_input(a2a: A2AWorld):
    live = a2a.a2a()
    task = send(a2a, live, message({"q": "very-private-input"}, message_id="secret-message-id"))
    a2a.run(live, lambda agent: agent.cancel(task.id), headers={"User-Agent": "langgraph-agent/1.0"})
    raises(a2a, live, a2a_errors.InvalidParamsError,
           lambda agent: agent.send(message({"skill": "missing", "input": {"q": "very-private-input"}})))
    rows = a2a.sql(
        "SELECT action, outcome, actor_kind, actor_subject, detail FROM audit_log"
        " WHERE tenant_id = :t AND action LIKE 'external.run.%' ORDER BY at", t=live.tenant)
    assert [(r["action"], r["outcome"]) for r in rows] == [
        ("external.run.start", "success"), ("external.run.cancel", "success"), ("external.run.start", "failed")]
    for row in rows:
        assert row["actor_kind"] == "api_key" and row["actor_subject"] == live.key.split("_")[1]
        assert row["detail"]["client"] == live.client["name"]
        assert row["detail"]["agent"]  # the caller's self-declared name, a label only
    assert rows[0]["detail"]["protocol"] == "a2a" and rows[1]["detail"]["agent"] == "langgraph-agent/1.0"
    dumped = json.dumps([dict(r) for r in rows], default=str)
    assert "very-private-input" not in dumped and "secret-message-id" not in dumped
    assert a2a.sql("SELECT count(*) AS n FROM audit_log WHERE tenant_id = :t AND action NOT LIKE 'external.%'"
                   " AND actor_kind = 'api_key'", t=live.tenant)[0]["n"] == 0


# -------------------------------------------------------------------------------------------------- the endpoint (people)


def test_an_admin_makes_changes_and_rotates_the_endpoint(a2a: A2AWorld):
    tenant, headers = a2a.tenant()
    assert a2a.client.get(ENDPOINT, headers=headers).json() == {"endpoint": None}
    assert a2a.client.post(f"{ENDPOINT}/rotate", headers=headers).status_code == 404
    for body in (
        {"card_name": "", "card_description": "d", "enabled": True},
        {"card_name": "n", "card_description": "", "enabled": True},
        {"card_name": "n" * 101, "card_description": "d", "enabled": True},
        {"card_name": "n", "card_description": "d" * 2001, "enabled": True},
        {"card_name": "n", "card_description": "d"},
        {"card_name": "n", "card_description": "d", "enabled": "yes"},
        {"card_name": "n", "card_description": "d", "enabled": True, "endpoint_id": "0" * 32},
    ):
        assert a2a.client.put(ENDPOINT, json=body, headers=headers).status_code == 422, body
    made = a2a.make_endpoint(headers)
    endpoint_id = made["endpoint_id"]
    assert len(endpoint_id) == 32 and int(endpoint_id, 16) >= 0 and tenant not in endpoint_id
    assert made["url"] == f"{PUBLIC}{PREFIX}/{endpoint_id}" and made["card_url"] == made["url"] + CARD_PATH
    assert a2a.make_endpoint(headers, card_description="New.")["endpoint_id"] == endpoint_id
    rotated = a2a.client.post(f"{ENDPOINT}/rotate", headers=headers).json()["endpoint"]
    assert rotated["endpoint_id"] != endpoint_id and rotated["card_description"] == "New."
    assert a2a.client.get(ENDPOINT, headers=headers).json()["endpoint"] == rotated
    _other, other_headers = a2a.tenant()
    assert a2a.client.get(ENDPOINT, headers=other_headers).json() == {"endpoint": None}
    assert a2a.make_endpoint(other_headers)["endpoint_id"] not in (endpoint_id, rotated["endpoint_id"])
    actions = {r["action"] for r in a2a.sql("SELECT action FROM audit_log WHERE tenant_id = :t", t=tenant)}
    assert {"save_a2a_endpoint", "rotate_a2a_endpoint"} <= actions


def test_the_endpoint_is_not_enabled_without_a_public_address(a2a: A2AWorld, external_env):  # noqa: F811
    _tenant, headers = a2a.tenant()
    external_env.setenv("EAP_EXTERNAL_BASE_URL", "")
    drafted = a2a.make_endpoint(headers, enabled=False)
    assert drafted["url"] is None and drafted["card_url"] is None
    r = a2a.client.put(ENDPOINT, headers=headers,
                       json={"card_name": "n", "card_description": "d", "enabled": True})
    assert r.status_code == 409 and r.json()["code"] == "external_base_url_unset", r.text


def test_the_endpoint_routes_are_for_people_only(a2a: A2AWorld):
    live = a2a.a2a()
    for headers in ({}, {"Authorization": f"Bearer {live.key}"}):
        assert a2a.client.get(ENDPOINT, headers=headers).status_code in (401, 403)
        assert a2a.client.put(ENDPOINT, json={}, headers=headers).status_code in (401, 403)
        assert a2a.client.post(f"{ENDPOINT}/rotate", headers=headers).status_code in (401, 403)
    assert a2a.client.get(ENDPOINT, headers=live.headers).json()["endpoint"]["endpoint_id"] == live.endpoint["endpoint_id"]
