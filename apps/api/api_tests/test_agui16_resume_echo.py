"""AGUI.16: CopilotKit's resume echo (eap_api.ag_ui.input, the "Resume echoes" rule).

CopilotKit 1.83 (`useInterrupt`) resolves a `reason: "tool_call"` interrupt by sending
`resume[{interruptId, status, payload}]` AND a `tool` message answering the gated server call with
the payload. That message is dropped (counted only): the resume entry alone is the decision.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from eap_api.ag_ui.input import AgUiInputError, check_tool_results, parse_run_agent_input

from .conftest import get_auth_header
from .test_agui8_workflow_endpoints import (  # noqa: F401  (fixtures used by name)
    _no_hang,
    ag,
    body,
    check,
    decisions,
    finished,
    post,
    publish,
    script,
    tenant,
    types,
)

RUN = "0dabafb5-38a9-5341-b377-4db5003d6cc7"
CALL = "pyd_ai_10b4b42371904f039c240ae22624900c"
INTERRUPT = f"approval:{RUN}:tool:{CALL}"


def copilotkit_body(*, echo: bool = True, echo_call: str = CALL, payload: Any = None) -> dict[str, Any]:
    """The body CopilotKit 1.83's runtime sent in the AGUI.16 browser check (ids shortened)."""
    payload = {"approved": True} if payload is None else payload
    messages: list[dict[str, Any]] = [
        {"id": "u1", "role": "user", "content": "create a ticket"},
        {
            "id": "r1-m0",
            "role": "assistant",
            "content": "",
            "toolCalls": [{"id": CALL, "type": "function", "function": {"name": "tickets__create", "arguments": "{}"}}],
        },
    ]
    if echo:
        messages.append({"id": "t1", "role": "tool", "content": json.dumps(payload), "toolCallId": echo_call})
    return {
        "threadId": "thread-1",
        "runId": "run-2",
        "state": {},
        "messages": messages,
        "tools": [],
        "context": [],
        "forwardedProps": {"__copilotkitRequestId": "x"},
        "resume": [{"interruptId": INTERRUPT, "status": "resolved", "payload": payload}],
    }


def test_the_copilotkit_shape_is_accepted_and_the_echo_dropped(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="eap_api.ag_ui.input"):
        mapped = parse_run_agent_input(copilotkit_body())
    assert mapped.tool_results == () and mapped.tool_result_message_indexes == ()
    assert [e.interrupt_id for e in mapped.resume] == [INTERRUPT]
    assert mapped.ignored.resume_echoes == 1
    check_tool_results(mapped, [])  # nothing left to answer a pending client call
    (record,) = [r for r in caplog.records if r.getMessage() == "ag-ui input mapped"]
    assert record.agui_resume_echoes == 1
    assert "approved" not in json.dumps(record.__dict__, default=str)  # counts only, no content


def test_the_fingerprint_is_the_same_with_and_without_the_echo() -> None:
    with_echo = parse_run_agent_input(copilotkit_body(echo=True))
    without = parse_run_agent_input(copilotkit_body(echo=False))
    assert with_echo.fingerprint() == without.fingerprint()
    # A denial's echo is dropped too, and the payload still changes the fingerprint (via resume).
    denied = parse_run_agent_input(copilotkit_body(payload={"approved": False}))
    assert denied.tool_results == () and denied.fingerprint() != with_echo.fingerprint()


def test_a_tool_message_for_another_call_is_still_refused() -> None:
    mapped = parse_run_agent_input(copilotkit_body(echo_call="other-call"))
    assert [r.tool_call_id for r in mapped.tool_results] == ["other-call"]
    assert mapped.ignored.resume_echoes == 0
    with pytest.raises(AgUiInputError) as exc:
        check_tool_results(mapped, [])
    assert exc.value.detail[0]["type"] == "client_tool_result_not_pending"
    assert exc.value.detail[0]["loc"] == ["body", "messages", 2, "toolCallId"]


def test_only_tool_interrupts_of_this_request_drop_messages() -> None:
    # No resume entry: the same tool message is a tool result as before.
    plain = copilotkit_body()
    del plain["resume"]
    assert [r.tool_call_id for r in parse_run_agent_input(plain).tool_results] == [CALL]
    # A workflow step interrupt (not the tool form) names no call: nothing is dropped.
    step = copilotkit_body()
    step["resume"] = [{"interruptId": f"approval:{RUN}:approve", "status": "resolved", "payload": {"approved": True}}]
    assert [r.tool_call_id for r in parse_run_agent_input(step).tool_results] == [CALL]
    # A tool-form id whose run part is not a UUID is not ours: nothing is dropped.
    odd = copilotkit_body()
    odd["resume"] = [{"interruptId": f"approval:not-a-uuid:tool:{CALL}", "status": "resolved", "payload": {"approved": True}}]
    assert [r.tool_call_id for r in parse_run_agent_input(odd).tool_results] == [CALL]


def test_the_copilotkit_resume_records_the_decision_through_the_endpoint(ag, tenant, script):
    """End to end on the agent route (AGUI.8's scripted gated agent): the CopilotKit shape is no
    longer 422; the approval is recorded once and the turn continues; an echo for another call
    is still 422 and records nothing."""
    client, app = ag
    h = get_auth_header(tenant, "alice")
    publish(client, h, script)
    url = f"/api/v1/ag-ui/agents/{script.agent_id}"
    status, _, first = post(client, url, {**body("chat", "a1"), "messages": [{"id": "u1", "role": "user", "content": "send it"}]}, h)
    assert status == 200
    (interrupt,) = finished(first)["outcome"]["interrupts"]
    call = interrupt["toolCallId"]
    history = [
        {"id": "u1", "role": "user", "content": "send it"},
        {"id": "a1-m0", "role": "assistant", "content": "",
         "toolCalls": [{"id": call, "type": "function", "function": {"name": "tickets__create", "arguments": "{}"}}]},
    ]
    entry = [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": True}}]

    wrong = {**body("chat", "a2", resume=entry),
             "messages": [*history, {"id": "t0", "role": "tool", "content": "{}", "toolCallId": "not-the-call"}]}
    status, detail, _ = post(client, url, wrong, h)
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_result_not_pending"
    assert decisions(app, tenant) == []

    echoed = {**body("chat", "a3", resume=entry),
              "messages": [*history, {"id": "t1", "role": "tool", "content": '{"approved":true}', "toolCallId": call}]}
    status, _, frames = post(client, url, echoed, h)
    assert status == 200
    check(frames, known_tool_calls=[call])
    assert "CUSTOM:pydanticos.approval_decided" in types(frames) and types(frames)[-1] == "RUN_FINISHED"
    (decision,) = decisions(app, tenant)
    assert decision["type"] == "tool_approved"
    # The same request without the echo is the same run (same fingerprint): an attach, not a 409.
    status, _, again = post(client, url, body("chat", "a3", resume=entry), h)
    assert status == 200 and types(again)[0] == "RUN_STARTED"
    assert len(decisions(app, tenant)) == 1
