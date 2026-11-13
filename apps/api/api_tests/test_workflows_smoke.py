"""Implementer smoke check for /api/v1/workflows (task 3.7); the acceptance suite is separate."""

import json
import uuid

import pytest

from .conftest import create_agent, get_auth_header, publish_version
from .run_helpers import requires_pg

pytestmark = requires_pg
BASE = "/api/v1/workflows"


def _spec(agent_id: str, tool_id: str = "clock.now") -> dict:
    return {
        "name": "Smoke",
        "input_schema": {"type": "object"},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"q": {"$ref": "input.q"}}, "next": "now"},
            {"id": "now", "type": "tool", "tool_id": tool_id, "input": {}},
        ],
    }


def test_workflow_draft_publish_and_isolation(client, tenant, user, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    h = get_auth_header(tenant, user)
    agent_id = create_agent(client, tenant, user)

    created = client.post(BASE, json={"spec": _spec(agent_id)}, headers=h)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    assert created.json()["latest_version"] is None

    unpublished = client.post(f"{BASE}/{wid}/versions", headers=h)
    assert unpublished.status_code == 422
    assert f"step 'ask': agent '{agent_id}' has no published version" in unpublished.json()["detail"]

    publish_version(client, agent_id, tenant, user)
    first = client.post(f"{BASE}/{wid}/versions", headers=h)
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert client.post(f"{BASE}/{wid}/versions", headers=h).status_code == 409

    patched = client.patch(f"{BASE}/{wid}", json={"spec": _spec(agent_id, "tickets.create")}, headers=h)
    assert patched.status_code == 200 and patched.json()["latest_version"] == 1
    disallowed = client.post(f"{BASE}/{wid}/versions", headers=h)
    assert disallowed.status_code == 422 and "EAP_ALLOWED_TOOLS" in disallowed.json()["detail"]

    assert [v["version"] for v in client.get(f"{BASE}/{wid}/versions", headers=h).json()] == [1]
    assert client.get(f"{BASE}/{wid}/versions/1", headers=h).json()["spec"]["start"] == "ask"
    assert client.get(f"{BASE}/{wid}/versions/2", headers=h).status_code == 404

    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    assert client.get(f"{BASE}/{wid}", headers=other).status_code == 404
    assert client.post(f"{BASE}/{wid}/versions", headers=other).status_code == 404
    assert all(w["id"] != wid for w in client.get(BASE, headers=other).json())
    foreign = client.post(BASE, json={"spec": _spec(agent_id)}, headers=other).json()["id"]
    resp = client.post(f"{BASE}/{foreign}/versions", headers=other)
    assert resp.status_code == 422 and "was not found" in resp.json()["detail"]


def test_invalid_spec_is_422_with_the_validator_message(client, auth_header):
    spec = _spec(str(uuid.uuid4()))
    spec["steps"][1]["next"] = "ask"  # cycle
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422
    assert "workflow graph has a cycle: ask -> now -> ask" in resp.text


def _raw_body(levels: int) -> str:
    """A request body whose template value is `levels` lists deep (built as text, never parsed here)."""
    step = '{"id":"a","type":"agent","agent_id":"%s","input":{"q":%s1%s}}' % (uuid.uuid4(), "[" * levels, "]" * levels)
    return '{"spec":{"name":"W","input_schema":{"type":"object"},"start":"a","steps":[%s]}}' % step


def test_deeply_nested_bodies_are_422_not_500(client, auth_header):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    for levels in (1_000, 20_000):
        for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
            resp = client.request(method, url, content=_raw_body(levels), headers=headers)
            assert resp.status_code == 422, (method, levels, resp.status_code)
            assert resp.json() == {"detail": "workflow spec is nested more than 64 levels deep"}
    # Under the body cap but over the literal cap: the schema's message, naming the step.
    resp = client.post(BASE, content=_raw_body(40), headers=headers)
    assert resp.status_code == 422
    assert "step 'a': input: value of 'q' is nested more than 32 levels deep" in resp.text
    # Brackets inside strings do not count toward depth.
    spec = _spec(str(uuid.uuid4()))
    spec["steps"][1]["input"] = {"text": "[" * 500 + "{" * 500}
    assert client.post(BASE, json={"spec": spec}, headers=auth_header).status_code == 201


def _escaped_quote_body(levels: int) -> str:
    """Deep body after a string holding ONE escaped quote: a scanner that misreads the escape (e.g. on
    UTF-16 NUL bytes) ends up 'inside a string' for the whole nesting and misses it."""
    step = '{"id":"a","type":"agent","agent_id":"%s","input":{"s":"x\\"y","q":%s1%s}}' % (
        uuid.uuid4(), "[" * levels, "]" * levels,
    )
    return '{"spec":{"name":"W","input_schema":{"type":"object"},"start":"a","steps":[%s]}}' % step


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-16", "utf-32-le", "utf-32-be", "utf-32"])
def test_non_utf8_deep_bodies_are_4xx_not_500(client, auth_header, encoding):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    body = _escaped_quote_body(1_000).encode(encoding)
    for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
        resp = client.request(method, url, content=body, headers=headers)
        assert resp.status_code == 415, (method, encoding, resp.status_code)
        assert resp.json() == {"detail": "request body must be UTF-8 encoded JSON (no NUL bytes)"}


def test_nul_escape_trick_and_utf8_bom(client, auth_header):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    tricky = _escaped_quote_body(1_000).encode().replace(b'\\"', b'\\\x00"')
    for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
        assert client.request(method, url, content=tricky, headers=headers).status_code == 415
        deep_bom = b"\xef\xbb\xbf" + _escaped_quote_body(1_000).encode()
        assert client.request(method, url, content=deep_bom, headers=headers).status_code == 422
    ok = b"\xef\xbb\xbf" + json.dumps({"spec": _spec(str(uuid.uuid4()))}).encode()
    assert client.post(BASE, content=ok, headers=headers).status_code == 201


MAX_BODY = 128 * 1024


def _assert_compact_422(resp) -> None:
    assert resp.status_code == 422, resp.status_code
    assert len(resp.content) < 2_000
    for err in resp.json()["detail"]:
        assert set(err) == {"loc", "msg", "type"}


def test_oversized_bodies_are_413_with_a_small_response(client, auth_header):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    flat = ('{"spec":{"name":"W","steps":[' + ",".join(["1"] * 1_000_000) + "]}}").encode()
    just_over = b'{"spec":' + b" " * (MAX_BODY - 9) + b"1}"
    assert len(just_over) == MAX_BODY + 1
    for body in (flat, just_over):
        for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
            resp = client.request(method, url, content=body, headers=headers)
            assert resp.status_code == 413, (method, len(body), resp.status_code)
            assert resp.json() == {"detail": f"request body too large (max {MAX_BODY} bytes)"}

    def chunks():  # no Content-Length: the streamed read stops at the cap
        for _ in range(64):
            yield b" " * 32_768

    resp = client.post(BASE, content=chunks(), headers=headers)
    assert resp.status_code == 413


def test_spec_just_over_64_kib_is_a_compact_422(client, auth_header):
    from eap_schemas.workflows import MAX_SPEC_BYTES, _spec_bytes

    spec = _spec(str(uuid.uuid4()))
    spec["steps"][1]["input"] = {"pad": ""}
    spec["steps"][1]["input"]["pad"] = "x" * (MAX_SPEC_BYTES + 1 - _spec_bytes(spec))
    assert _spec_bytes(spec) == MAX_SPEC_BYTES + 1
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    _assert_compact_422(resp)
    assert "workflow spec must be at most 64 KiB as JSON" in resp.text


def _with_bad_text(field: str, bad: str) -> dict:
    spec = _spec(str(uuid.uuid4()))
    steps = spec["steps"]
    if field == "name":
        spec["name"] = f"W{bad}"
    elif field == "description":
        spec["description"] = bad
    elif field == "input_schema":
        spec["input_schema"] = {"type": "object", "description": bad}
    elif field == "input_schema_key":
        spec["input_schema"] = {"type": "object", "properties": {f"a{bad}": {}}}
    elif field == "output_schema":
        spec["output_schema"] = {"type": "string", "title": bad}
    elif field == "template_literal":
        steps[0]["input"]["lit"] = ["ok", {"deep": bad}]
    elif field == "template_key":
        steps[0]["input"][f"k{bad}"] = 1
    elif field == "condition_literal":
        steps[0]["next"] = "br"
        steps.insert(1, {"id": "br", "type": "branch", "default": "now",
                         "cases": [{"when": {"op": "eq", "left": {"$ref": "input.q"}, "right": bad}, "next": "now"}]})
    elif field == "approval_prompt":
        steps[0]["next"] = "ok"
        steps.insert(1, {"id": "ok", "type": "approval", "prompt": f"go?{bad}", "timeout_hours": 1, "next": "now"})
    return spec


@pytest.mark.parametrize("bad", ["\x00", "\ud800", "\udfff"])
@pytest.mark.parametrize("field", [
    "name", "description", "input_schema", "input_schema_key", "output_schema",
    "template_literal", "template_key", "condition_literal", "approval_prompt",
])
def test_unstorable_text_is_422_never_500(client, auth_header, field, bad):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    body = json.dumps({"spec": _with_bad_text(field, bad)}).encode()  # \u escapes, as a client would send
    assert b"\\u" in body
    for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
        resp = client.request(method, url, content=body, headers=headers)
        _assert_compact_422(resp)
        assert "workflow spec contains" in resp.text, resp.text
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["draft_spec"]["name"] == "Smoke"


@pytest.mark.parametrize("number", ["1e309", "-1e309"])
@pytest.mark.parametrize("where", ["template", "condition", "input_schema"])
def test_non_finite_numbers_are_422_not_stored_as_null(client, auth_header, where, number):
    headers = {**auth_header, "Content-Type": "application/json"}
    wid = client.post(BASE, json={"spec": _spec(str(uuid.uuid4()))}, headers=auth_header).json()["id"]
    marker = 424242
    spec = _with_bad_text("condition_literal", "")
    spec["steps"][1]["cases"][0]["when"]["right"] = marker if where == "condition" else 1
    if where == "template":
        spec["steps"][0]["input"]["lit"] = [marker]
    if where == "input_schema":
        spec["input_schema"] = {"type": "object", "maximum": marker}
    body = json.dumps({"spec": spec}).replace(str(marker), number).encode()  # raw JSON, as a client sends it
    assert number.encode() in body
    for method, url in (("POST", BASE), ("PATCH", f"{BASE}/{wid}")):
        resp = client.request(method, url, content=body, headers=headers)
        _assert_compact_422(resp)
        assert "workflow spec contains a non-finite number" in resp.text, resp.text
    assert client.get(f"{BASE}/{wid}", headers=auth_header).json()["draft_spec"]["name"] == "Smoke"
