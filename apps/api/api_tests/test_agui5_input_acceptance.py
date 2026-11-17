"""AGUI.5 acceptance tests (eap_api.ag_ui.input): the tester's additions. No database."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from eap_api.ag_ui import input as agui
from eap_api.ag_ui.input import AgUiInputError, parse_run_agent_input
from eap_schemas import AgentSpec

from .conftest import get_auth_header


def body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "threadId": "th",
        "runId": "r",
        "messages": [{"id": "u1", "role": "user", "content": "hello"}],
    }
    base.update(overrides)
    return base


def user(i: str, text: str) -> dict[str, Any]:
    return {"id": i, "role": "user", "content": text}


def asst(i: str = "a") -> dict[str, Any]:
    return {"id": i, "role": "assistant", "content": "ok"}


def tool(call_id: str, content: Any = "r", **extra: Any) -> dict[str, Any]:
    return {"id": f"m-{call_id}", "role": "tool", "toolCallId": call_id, "content": content, **extra}


def refused(payload: Any) -> AgUiInputError:
    with pytest.raises(AgUiInputError) as exc:
        parse_run_agent_input(payload)
    return exc.value


def fp(**overrides: Any) -> str:
    return parse_run_agent_input(body(**overrides)).fingerprint()


def spec(**kw: Any) -> AgentSpec:
    return AgentSpec.model_validate({"name": "a", "instructions": "x", "model": {"provider": "test", "name": "echo"}, **kw})


# ------------------------------------------------------------------ tail extraction (D5)


def test_tail_only_new_user_messages_after_last_assistant() -> None:
    m = parse_run_agent_input(body(messages=[user("u0", "old"), asst("a1"), user("u1", "A"), user("u2", "B")]))
    assert (m.input_text, m.input_message_ids, m.tool_results) == ("A\n\nB", ("u1", "u2"), ())
    assert m.ignored.messages_before_tail == 2


def test_tail_only_tool_results_answering_pending_calls() -> None:
    m = parse_run_agent_input(body(messages=[user("u0", "q"), asst(), tool("c1"), tool("c2")]))
    assert m.input_text == "" and m.input_message_ids == () and m.last_user_text is None
    assert [r.tool_call_id for r in m.tool_results] == ["c1", "c2"]
    agui.check_tool_results(m, {"c1", "c2"})


def test_tail_mixed_user_and_tool_results_keeps_order_of_each() -> None:
    m = parse_run_agent_input(body(messages=[asst(), tool("c2"), user("u1", "A"), tool("c1"), user("u2", "B")]))
    assert m.input_text == "A\n\nB" and [r.tool_call_id for r in m.tool_results] == ["c2", "c1"]


def test_tail_duplicate_user_messages_are_kept_and_duplicate_tool_results_refused() -> None:
    m = parse_run_agent_input(body(messages=[user("u1", "same"), user("u1", "same")]))
    assert m.input_text == "same\n\nsame" and m.input_message_ids == ("u1", "u1")
    assert refused(body(messages=[tool("c1"), user("u", "x"), tool("c1")])).detail[0]["type"] == "client_tool_result_duplicate"


def test_same_call_id_before_and_after_the_assistant_is_not_a_duplicate() -> None:
    m = parse_run_agent_input(body(messages=[tool("c1"), asst(), tool("c1")]))
    assert len(m.tool_results) == 1 and m.ignored.messages_before_tail == 2


def test_reordered_history_before_the_tail_is_ignored() -> None:
    a = parse_run_agent_input(body(messages=[user("x", "1"), asst("a1"), user("y", "2"), asst("a2"), user("u1", "go")]))
    b = parse_run_agent_input(body(messages=[asst("a2"), user("y", "2"), asst("a1"), user("x", "1"), asst("a3"), user("u1", "go")]))
    assert a.input_text == b.input_text == "go" and a.fingerprint() == b.fingerprint()


def test_history_diverging_from_the_stored_one_does_not_change_the_mapping() -> None:
    a = body(messages=[user("x", "real"), asst("a1"), user("u1", "go")])
    b = body(messages=[user("zz", "invented"), {**asst("a9"), "content": "never said"}, user("u1", "go")])
    assert fp(**a) == fp(**b)


@pytest.mark.parametrize("messages", [None, [], [asst()], [{"id": "s", "role": "system", "content": "x"}]])
def test_empty_or_tailess_input_is_no_new_input(messages: Any) -> None:
    err = refused(body(messages=messages))
    assert (err.status_code, err.detail[0]["type"]) == (422, "no_new_input")


def test_last_assistant_wins_over_earlier_ones_and_blank_users_skipped() -> None:
    m = parse_run_agent_input(body(messages=[asst("a1"), user("u1", "gone"), asst("a2"), user("u2", " "), user("u3", "kept")]))
    # Blank user messages contribute neither text nor id (review m5).
    assert m.input_text == "kept" and m.input_message_ids == ("u3",) and m.last_user_text == "kept"


# ------------------------------------------------------------------ fingerprint


def test_fingerprint_stable_across_key_order_and_irrelevant_noise() -> None:
    a = body(
        forwardedProps={"pydanticos": {"requestContext": {"a": 1, "b": {"x": 1, "y": 2}}, "input": {"p": [1, {"q": 2, "r": 3}]}}},
        resume=[{"interruptId": "i2", "status": "resolved", "payload": {"approved": True, "reason": "k"}}, {"interruptId": "i1", "status": "cancelled"}],
        messages=[asst(), tool("c2", error="e"), tool("c1")],
    )
    b = json.loads(json.dumps(a))
    b["forwardedProps"]["pydanticos"] = {"input": {"p": [1, {"r": 3, "q": 2}]}, "requestContext": {"b": {"y": 2, "x": 1}, "a": 1}}
    b["resume"] = [{"status": "cancelled", "interruptId": "i1"}, {"payload": {"reason": "k", "approved": True}, "status": "resolved", "interruptId": "i2"}]
    b["messages"] = [asst("other-id"), tool("c1"), tool("c2", error="e")]
    b.update(threadId="th2", runId="r2", state={"s": 1}, context=[{"description": "d", "value": "v"}])
    assert fp(**a) == fp(**b)


@pytest.mark.parametrize(
    "a, b",
    [
        ({"messages": [user("u", "x")]}, {"messages": [user("u", "x ")]}),
        ({"messages": [tool("c1", "1")]}, {"messages": [tool("c1", "2")]}),
        ({"messages": [tool("c1", "1")]}, {"messages": [tool("c1", "1", error="e")]}),
        ({"messages": [tool("c1")]}, {"messages": [tool("c2")]}),
        ({"forwardedProps": {"pydanticos": {"version": 1}}}, {"forwardedProps": {"pydanticos": {"version": 2}}}),
        ({"forwardedProps": {"pydanticos": {"model": {"provider": "test", "name": "a"}}}}, {"forwardedProps": {"pydanticos": {"model": {"provider": "test", "name": "b"}}}}),
        ({"forwardedProps": {"pydanticos": {"requestContext": {"k": 1}}}}, {"forwardedProps": {"pydanticos": {"requestContext": {"k": "1"}}}}),
        ({"forwardedProps": {"pydanticos": {"input": 1}}}, {"forwardedProps": {"pydanticos": {"input": "1"}}}),
        ({"forwardedProps": {"pydanticos": {"input": None}}}, {"forwardedProps": {"pydanticos": {"input": {}}}}),
        ({"tools": [{"name": "t"}]}, {"tools": [{"name": "t", "description": "d"}]}),
        ({"tools": [{"name": "t"}]}, {"tools": [{"name": "t", "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}]}),
        ({"resume": [{"interruptId": "i", "status": "resolved"}]}, {"resume": [{"interruptId": "j", "status": "resolved"}]}),
    ],
)
def test_fingerprint_changes_when_anything_that_matters_changes(a: dict[str, Any], b: dict[str, Any]) -> None:
    base: dict[str, Any] = {"messages": [user("u", "hi")]}
    assert fp(**{**base, **a}) != fp(**{**base, **b})


def test_fingerprint_has_no_collisions_across_fields() -> None:
    # The same string in different roles must not collide.
    same = "shared"
    variants = [
        fp(messages=[user("u", same)]),
        fp(messages=[user("u", "hi")], forwardedProps={"pydanticos": {"resourceId": same}}),
        fp(messages=[user("u", "hi")], forwardedProps={"pydanticos": {"input": same}}),
        fp(messages=[user("u", "hi")], forwardedProps={"pydanticos": {"requestContext": {"k": same}}}),
        fp(messages=[user("u", "hi")], tools=[{"name": same}]),
        fp(messages=[user("u", "hi")], tools=[{"name": "t", "description": same}]),
        fp(messages=[tool(same)]),
        fp(messages=[tool("c", same)]),
        fp(messages=[user("u", "hi")], resume=[{"interruptId": same, "status": "resolved"}]),
        fp(messages=[user("u", "hi")], resume=[{"interruptId": "i", "status": "resolved", "payload": same}]),
    ]
    assert len(set(variants)) == len(variants)
    assert all(len(v) == 64 and v == v.lower() for v in variants)


def test_resource_id_and_workflow_input_do_not_collide() -> None:
    a = fp(forwardedProps={"pydanticos": {"resourceId": "x", "input": "y"}})
    b = fp(forwardedProps={"pydanticos": {"resourceId": "y", "input": "x"}})
    c = fp(forwardedProps={"pydanticos": {"resourceId": "x"}})
    assert len({a, b, c}) == 3
    assert fp(forwardedProps={"pydanticos": {"resourceId": "x"}}) != fp(forwardedProps={"pydanticos": {"input": "x"}})


# ------------------------------------------------------------------ id mapping (D3, D4)


def test_derived_ids_injective_with_colons_unicode_and_boundaries() -> None:
    ids = ["a:b", "a", "b", "a:", ":b", "é", "é", "日本", "a\u0000", "", "x" * 256]
    pairs = [(t, r) for t in ids for r in ids]
    runs = {agui.derive_agent_run_id("t", "ag", t, r) for t, r in pairs}
    assert len(runs) == len(pairs)
    tenants = {agui.derive_agent_run_id(t, a, "th", "r") for t in ("a:b", "a", "b") for a in ("a:b", "a", "b")}
    assert len(tenants) == 9
    convs = {agui.derive_conversation_id("t", "ag", i) for i in ids}
    assert len(convs) == len(ids) and convs.isdisjoint(runs)
    assert all(uuid.UUID(c).version == 5 for c in convs | runs)


def test_derived_ids_are_deterministic_and_pinned_to_the_namespace() -> None:
    assert agui.derive_conversation_id("t", "a", "th") == agui.derive_conversation_id("t", "a", "th")
    expected = uuid.uuid5(agui.AGUI_NS, json.dumps(["conversation", "t", "a", "th"], separators=(",", ":")))
    assert agui.derive_conversation_id("t", "a", "th") == str(expected)


@pytest.mark.parametrize("key", ["threadId", "runId"])
def test_thread_and_run_id_limits_and_charset(key: str) -> None:
    assert parse_run_agent_input(body(**{key: "x" * 256}))
    assert parse_run_agent_input(body(**{key: "a:b #.c~%"}))
    # The ids are URL path segments of the attach routes (AG-UI checkpoint review, MINOR 1):
    # printable ASCII only, no '/', '\\' or '..', and not '.'.
    bad_path = ("日本é", "a/b", "a\\b", "..", ".", "a..b")
    for bad in ("x" * 257, "", "a\x00b", "a\tb", "a\x7fb", None, 1, ["a"], {"a": 1}, True, *bad_path):
        err = refused(body(**{key: bad}))
        assert err.status_code == 422 and err.detail[0]["loc"] == ["body", key]
    err = refused({k: v for k, v in body().items() if k != key})
    assert err.detail[0]["loc"] == ["body", key]


# ------------------------------------------------------------------ client tools


def test_client_tools_validated_through_the_contracts() -> None:
    cases = {
        "": "client_tool_invalid", "a b": "client_tool_invalid", "x" * 65: "client_tool_invalid",
        "retrieve": "client_tool_shadows_server_tool",
    }
    for name, code in cases.items():
        err = refused(body(tools=[{"name": name}]))
        assert err.detail[0]["type"] == code and err.detail[0]["loc"][:3] == ["body", "tools", 0], name
    assert refused(body(tools=[{"name": "t", "description": "d" * 4097}])).detail[0]["type"] == "client_tool_invalid"
    assert refused(body(tools=[{"parameters": {}}])).detail[0]["type"] == "client_tool_invalid"
    assert refused(body(tools="t")).detail[0]["type"] == "client_tool_invalid"
    assert refused(body(tools=["t"])).detail[0]["loc"] == ["body", "tools", 0]
    assert parse_run_agent_input(body(tools=[{"name": "n" * 64}, {"name": "A-b_9"}])).client_tools[1].name == "A-b_9"


def test_client_tool_clashes_with_every_server_tool_kind() -> None:
    s = spec(tools=["web.search"], mcp_tools=[{"server": "files", "tool": "read"}])
    # Reserved platform names are refused at parse time (the AGUI.1 contract); spec and `mcp__`
    # names parse and are refused by check_client_tool_clashes.
    for name in ("retrieve", "update_working_memory", "final_result"):
        err = refused(body(tools=[{"name": "ok"}, {"name": name}]))
        assert err.detail[0]["type"] == "client_tool_shadows_server_tool"
        assert err.detail[0]["loc"][:3] == ["body", "tools", 1]
    for name in ("web__search", "mcp__files__read", "mcp__other__x"):
        tools = parse_run_agent_input(body(tools=[{"name": "ok"}, {"name": name}])).client_tools
        with pytest.raises(AgUiInputError) as exc2:
            agui.check_client_tool_clashes(tools, s)
        assert exc2.value.status_code == 422 and exc2.value.detail[0]["loc"] == ["body", "tools", 1, "name"]
    agui.check_client_tool_clashes(parse_run_agent_input(body(tools=[{"name": "web"}, {"name": "files"}])).client_tools, s)
    agui.check_client_tool_clashes((), s)


def test_tool_result_for_unknown_call_is_refused_with_its_index() -> None:
    m = parse_run_agent_input(body(messages=[asst(), tool("c1"), tool("ghost")]))
    with pytest.raises(AgUiInputError) as exc:
        agui.check_tool_results(m, ["c1"])
    d = exc.value.detail[0]
    assert (exc.value.status_code, d["type"], d["loc"]) == (422, "client_tool_result_not_pending", ["body", "messages", 2, "toolCallId"])
    with pytest.raises(AgUiInputError):
        agui.check_tool_results(m, [])
    agui.check_tool_results(parse_run_agent_input(body()), [])


def test_resume_coverage_every_open_interrupt_answered() -> None:
    entry = lambda i, st="resolved": {"interruptId": i, "status": st}  # noqa: E731
    full = parse_run_agent_input(body(messages=[], resume=[entry("i1"), entry("i2", "cancelled")]))
    agui.check_resume_covers(full, ["i1", "i2"])
    agui.check_resume_covers(full, [], ["i1", "i2"])  # both already decided elsewhere
    agui.check_resume_covers(full, ["i2"], ["i1"])
    for open_ids, decided in (([], []), (["i2"], []), ([], ["i1"])):  # an entry naming no interrupt
        with pytest.raises(AgUiInputError) as exc:
            agui.check_resume_covers(full, open_ids, decided)
        assert (exc.value.status_code, exc.value.detail[0]["type"]) == (422, "resume_unknown_interrupt")
    for open_ids in (["i1", "i2", "i3"], ["i3"]):
        with pytest.raises(AgUiInputError) as exc:
            agui.check_resume_covers(full, open_ids)
        assert (exc.value.status_code, exc.value.detail[0]["type"]) == (422, "resume_missing_interrupt")
    # A resume of one interrupt does not cover a second open one, even with a user message beside it.
    mixed = parse_run_agent_input(body(resume=[entry("i1")]))
    with pytest.raises(AgUiInputError):
        agui.check_resume_covers(mixed, ["i1", "i2"])
    agui.check_resume_covers(parse_run_agent_input(body()), ["i1", "i2"])  # no resume: Q5, the route re-sends


# ------------------------------------------------------------------ forwardedProps.pydanticos, ignored fields


@pytest.mark.parametrize("key", ["verison", "Version", "resource_id", "extra", "state", "tools", ""])
def test_pydanticos_unknown_keys_are_rejected(key: str) -> None:
    err = refused(body(forwardedProps={"pydanticos": {"version": 1, key: 1}}))
    assert (err.status_code, err.detail[0]["type"]) == (422, "run_options_invalid")
    assert err.detail[0]["loc"][:3] == ["body", "forwardedProps", "pydanticos"]


@pytest.mark.parametrize(
    "opts",
    [{"version": 0}, {"version": 2**31}, {"version": True}, {"version": "latest"}, {"version": 1.5}, {"model": "x"},
     {"model": {"provider": "test"}}, {"requestContext": []}, {"resourceId": "a b"}, {"resourceId": "x" * 129}],
)
def test_pydanticos_values_are_strict(opts: dict[str, Any]) -> None:
    assert refused(body(forwardedProps={"pydanticos": opts})).detail[0]["type"] == "run_options_invalid"


def test_other_forwarded_props_are_ignored_and_null_means_defaults() -> None:
    for fwd in (None, {}, {"pydanticos": None}, {"other": {"version": "bad"}}, [], "x"):
        assert parse_run_agent_input(body(forwardedProps=fwd)).options == parse_run_agent_input(body()).options


def test_state_and_context_are_ignored_whatever_they_hold() -> None:
    base = fp()
    for extra in (
        {"state": {"messages": [{"role": "system", "content": "x"}]}},
        {"state": "s", "context": "not even a list"},
        {"context": [{"description": "d", "value": {"deep": [1, 2]}}] * 5},
        {"state": [1, 2, 3], "parentRunId": 7, "protocolVersion": {"x": 1}},
    ):
        assert fp(**extra) == base
    m = parse_run_agent_input(body(state={"a": 1}, context=[1, 2]))
    assert m.ignored.state_present and m.ignored.context_entries == 2
    assert not parse_run_agent_input(body()).ignored.state_present


# ------------------------------------------------------------------ size and depth (D18)

LIMIT = 2 * 1024 * 1024


def raw_app() -> FastAPI:
    router = APIRouter(route_class=agui.agui_route_class())

    @router.post("/ag-ui/raw")
    async def raw(request: Request) -> dict[str, Any]:
        return {"threadId": (await request.json())["threadId"]}

    app = FastAPI()
    app.include_router(router)
    return app


def post_raw(text: str) -> Any:
    headers = {**get_auth_header("t1", "alice"), "content-type": "application/json"}
    return TestClient(raw_app()).post("/ag-ui/raw", content=text.encode(), headers=headers)


def padded(total: int) -> str:
    head, tail = '{"threadId":"t","state":"', '"}'
    return head + "x" * (total - len(head) - len(tail)) + tail


def nested(levels: int) -> str:
    return '{"threadId":"t","state":' + "[" * (levels - 1) + "]" * (levels - 1) + "}"


def test_body_limit_exact_boundary_is_2_mib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAP_AGUI_MAX_BODY_BYTES", raising=False)
    assert len(padded(LIMIT).encode()) == LIMIT
    ok = post_raw(padded(LIMIT))
    assert ok.status_code == 200 and ok.json() == {"threadId": "t"}
    big = post_raw(padded(LIMIT + 1))
    assert big.status_code == 413 and big.json() == {"detail": f"request body too large (max {LIMIT} bytes)"}


def test_depth_limit_exact_boundary_is_64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EAP_AGUI_MAX_BODY_BYTES", raising=False)
    assert post_raw(nested(64)).status_code == 200
    deep = post_raw(nested(65))
    assert deep.status_code == 422 and deep.json() == {"detail": agui.AGUI_DEPTH_DETAIL}
    assert "64" in agui.AGUI_DEPTH_DETAIL
    # Brackets inside strings do not count.
    assert post_raw('{"threadId":"t","state":"' + "[" * 200 + '"}').status_code == 200


# ------------------------------------------------------------------ leaks

S = "SECRET-7f3a9c"


def leak_cases() -> list[dict[str, Any]]:
    sys_msg = {"id": S, "role": S, "content": S}
    return [
        body(threadId=S + "\n"), body(runId=S * 40),
        body(messages=[sys_msg]), body(messages=[user(S, [{"type": S, "text": S}])]),
        body(messages=[{"id": S, "role": "user", "content": S * 5000}]),
        body(messages=[user("u", S), {"id": "x", "role": "user", "content": {"k": S}}]),
        body(messages=[tool(S + " bad id")]), body(messages=[tool(S), tool(S)]),
        body(messages=[tool("c", S * 5000)]), body(messages=[tool("c", "x", error={"e": S})]),
        body(tools=[{"name": S + " !"}]), body(tools=[{"name": "t", "parameters": {"type": S}}]),
        body(tools=[{"name": "t", "parameters": {"type": "object", "properties": {"p": {"$ref": "http://" + S}}}}]),
        body(tools=[{"name": S}, {"name": S}]), body(tools=[{"name": "t", "description": S * 5000}]),
        body(forwardedProps={"pydanticos": {"version": S}}),
        body(forwardedProps={"pydanticos": {"model": {"provider": S}}}),
        body(forwardedProps={"pydanticos": {"requestContext": {"x": float("nan"), S: S}}}),
        body(resume=[{"interruptId": S, "status": S}]), body(resume=[{"interruptId": S + " x", "status": "resolved"}]),
        body(resume=[{"interruptId": S, "status": "resolved"}] * 2),
        body(resume=[{"interruptId": "i", "status": "resolved", "payload": {S: float("inf")}}]),
    ]


@pytest.mark.parametrize("payload", leak_cases(), ids=lambda p: hashlib.sha1(json.dumps(p, default=str).encode()).hexdigest()[:6])
def test_no_input_value_leaks_into_errors(payload: dict[str, Any]) -> None:
    err = refused(payload)
    assert err.status_code == 422
    text = json.dumps(err.detail) + str(err) + repr(err) + str(err.detail)
    assert S not in text
    d = err.detail[0]
    assert set(d) == {"loc", "msg", "type"} and len(d["msg"]) <= 200
    assert all(isinstance(p, (str, int)) and S not in str(p) for p in d["loc"])


def test_check_errors_do_not_leak_values() -> None:
    m = parse_run_agent_input(body(messages=[asst(), tool(S)], tools=[{"name": "mcp__" + S}], resume=[{"interruptId": S, "status": "resolved"}]))
    for call in (
        lambda: agui.check_tool_results(m, ["other"]),
        lambda: agui.check_client_tool_clashes(m.client_tools, spec()),
        lambda: agui.check_resume_covers(m, [S + "2", "z"]),
        lambda: agui.check_resume_covers(m, [], ["z"]),
        lambda: agui.workflow_input(parse_run_agent_input(body(messages=[user("u", "not json " + S)]))),
        lambda: agui.thread_resource_id(agui.AgUiRunOptions.model_validate({"resourceId": ".."}), user_id=S, api_key_id=S),
    ):
        with pytest.raises(AgUiInputError) as exc:
            call()
        assert S not in json.dumps(exc.value.detail) and S not in str(exc.value)


def test_unknown_option_key_name_is_not_echoed() -> None:
    # A request-supplied key name (up to the 2 MiB body) must not come back in `loc`.
    err = refused(body(forwardedProps={"pydanticos": {S: 1}}))
    assert err.detail[0]["type"] == "run_options_invalid"
    assert S not in json.dumps(err.detail)
