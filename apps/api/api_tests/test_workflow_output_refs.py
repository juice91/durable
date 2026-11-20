"""AC.9: workflow publish checks `$ref`s into agent output schemas; GET /workflows/agent-output-schema."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from eap_api.routes.workflows import output_fields, output_ref_problem

from .conftest import get_auth_header
from .run_helpers import requires_pg

BASE = "/api/v1/workflows"

# Closed objects (additionalProperties: false), so an unknown field cannot exist.
TRIAGE = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["high", "low"], "description": "How urgent"},
        "owner": {"type": ["object", "null"], "properties": {"name": {"type": "string"}},
                  "additionalProperties": False},
        "tags": {"type": "array", "items": {"type": "object", "properties": {"label": {"type": "string"}},
                                            "additionalProperties": False}},
        "extra": {"type": "object"},
    },
    "required": ["priority"],
    "additionalProperties": False,
}
# The same shape with open objects (additionalProperties absent = true, as the runtime validates).
OPEN = {"type": "object", "properties": {"priority": {"type": "string"}, "tags": {"type": "array",
        "items": {"type": "object", "properties": {"label": {"type": "string"}}}}}}


@pytest.mark.parametrize(
    "path,ok",
    [
        ("priority", True),
        ("owner.name", True),
        ("tags.0.label", True),
        ("tags.3", True),
        ("extra.anything.deep", True),  # an object without properties constrains nothing
        ("priorty", False),
        ("priority.x", False),  # into a string
        ("owner.nam", False),
        ("tags.label", False),  # an array needs an index
        ("tags.0.lbl", False),
    ],
)
def test_output_ref_problem_walks_properties_and_items(path, ok):
    problem = output_ref_problem(TRIAGE, path.split("."))
    assert (problem is None) is ok, problem
    assert problem is None or problem.blocking


@pytest.mark.parametrize("path,blocking", [
    ("priorty", False), ("tags.0.lbl", False),  # not listed, but other keys are allowed
    ("priority.x", True), ("tags.label", True),  # cannot exist whatever the agent returns
])
def test_open_objects_follow_additional_properties(path, blocking):
    problem = output_ref_problem(OPEN, path.split("."))
    assert problem is not None and problem.blocking is blocking, problem
    if not blocking:
        assert "the schema allows other fields" in problem.message


def test_additional_properties_true_or_false():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True}
    assert output_ref_problem(schema, ["zzz", "y"]).blocking is False
    closed = output_ref_problem({**schema, "additionalProperties": False}, ["zzz"])
    assert closed.blocking and "'zzz' is not a field of the output (fields: a)" in closed.message


def test_output_fields_lists_readable_paths():
    paths = [f["path"] for f in output_fields(TRIAGE)]
    assert paths == ["priority", "owner", "owner.name", "tags", "tags.0", "tags.0.label", "extra"]
    assert output_fields(TRIAGE)[0] == {"path": "priority", "type": "string", "description": "How urgent"}


# --- through the API -------------------------------------------------------------------------

TENANT_USER = ("tenant-ac9", "alice")


def _auth() -> dict[str, str]:
    return get_auth_header(*TENANT_USER)


def _agent(client, schema: dict[str, Any] | None, *, versions: int = 1) -> str:
    spec: dict[str, Any] = {"name": "Triage", "model": {"provider": "test", "name": "structured"}}
    if schema is not None:
        spec["output"] = {"json_schema": schema}
    resp = client.post("/api/v1/agents", json={"spec": spec}, headers=_auth())
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=_auth()).status_code == 201
    return agent_id


def _workflow(agent_id: str, ref: str, version: int | None) -> dict[str, Any]:
    return {
        "name": "Triage branch",
        "input_schema": {"type": "object"},
        "start": "triage",
        "steps": [
            {"id": "triage", "type": "agent", "agent_id": agent_id, "version": version,
             "input": {"q": {"$ref": "input.q"}}, "next": "route"},
            {"id": "route", "type": "branch",
             "cases": [{"when": {"op": "eq", "left": {"$ref": ref}, "right": "high"}, "next": "done"}],
             "default": "done"},
            {"id": "done", "type": "agent", "agent_id": agent_id, "input": {"q": {"$ref": "steps.triage.output"}}},
        ],
    }


def _publish(client, spec):
    created = client.post(BASE, json={"spec": spec}, headers=_auth())
    assert created.status_code == 201, created.text
    return client.post(f"{BASE}/{created.json()['id']}/versions", headers=_auth())


@requires_pg
def test_pinned_known_field_publishes_without_warnings(client):
    agent_id = _agent(client, TRIAGE)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priority", 1))
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []


@requires_pg
def test_pinned_unknown_field_is_422(client):
    agent_id = _agent(client, TRIAGE)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priorty", 1))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "step 'route': $ref 'steps.triage.output.priorty'" in detail
    assert "'priorty' is not a field of the output" in detail


@requires_pg
def test_pinned_unlisted_field_of_an_open_object_only_warns(client):
    agent_id = _agent(client, OPEN)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priorty", 1))
    assert resp.status_code == 201, resp.text
    [warning] = resp.json()["warnings"]
    assert warning["code"] == "unlisted_output_field" and warning["path"] == "steps[1]"
    assert "step 'route': $ref 'steps.triage.output.priorty'" in warning["message"]


@requires_pg
def test_unpinned_unknown_field_only_warns(client):
    agent_id = _agent(client, TRIAGE)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priorty", None))
    assert resp.status_code == 201, resp.text
    [warning] = resp.json()["warnings"]
    assert warning["code"] == "unpinned_agent_output_ref" and warning["path"] == "steps[1]"
    assert "does not match the latest version's output schema" in warning["message"]


@requires_pg
def test_unpinned_matching_field_still_warns(client):
    agent_id = _agent(client, TRIAGE)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priority", None))
    assert resp.status_code == 201, resp.text
    [warning] = resp.json()["warnings"]
    assert warning["code"] == "unpinned_agent_output_ref" and warning["path"] == "steps[1]"
    message = warning["message"]
    assert "step 'route'" in message and "'steps.triage.output.priority'" in message
    assert "matches the latest version's output schema today" in message and "Pin the version" in message


@requires_pg
def test_unpinned_whole_output_ref_does_not_warn(client):
    agent_id = _agent(client, TRIAGE)
    spec = _workflow(agent_id, "steps.triage.output", None)  # `done` also reads the whole output
    resp = _publish(client, spec)
    assert resp.status_code == 201 and resp.json()["warnings"] == []


@requires_pg
def test_field_ref_into_a_text_agent_warns(client):
    agent_id = _agent(client, None)
    resp = _publish(client, _workflow(agent_id, "steps.triage.output.priority", 1))
    assert resp.status_code == 201, resp.text
    assert [w["code"] for w in resp.json()["warnings"]] == ["text_agent_output_ref"]


@requires_pg
def test_agent_output_schema_helper(client):
    agent_id = _agent(client, TRIAGE)
    resp = client.get(f"{BASE}/agent-output-schema", params={"agent_id": agent_id}, headers=_auth())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == 1 and body["pinned"] is False and body["structured"] is True
    assert body["json_schema"] == TRIAGE and {"priority", "tags.0.label"} <= {f["path"] for f in body["fields"]}

    pinned = client.get(f"{BASE}/agent-output-schema", params={"agent_id": agent_id, "version": 1}, headers=_auth())
    assert pinned.status_code == 200 and pinned.json()["pinned"] is True

    missing = client.get(f"{BASE}/agent-output-schema", params={"agent_id": agent_id, "version": 9}, headers=_auth())
    assert missing.status_code == 404 and missing.json()["detail"] == "Version not found"

    other = client.get(
        f"{BASE}/agent-output-schema", params={"agent_id": agent_id}, headers=get_auth_header("tenant-other", "bob")
    )
    assert other.status_code == 404 and other.json()["detail"] == "Agent not found"

    unknown = client.get(f"{BASE}/agent-output-schema", params={"agent_id": str(uuid.uuid4())}, headers=_auth())
    assert unknown.status_code == 404

    text_id = _agent(client, None)
    text = client.get(f"{BASE}/agent-output-schema", params={"agent_id": text_id}, headers=_auth()).json()
    assert text["structured"] is False and text["json_schema"] is None and text["fields"] == []


@requires_pg
def test_optional_input_ref_warnings(client):
    """A spec with an optional input read by a tool step warns; the same input with a default does not;
    a required input does not."""
    # Create a workflow
    spec = {
        "name": "Optional input test",
        "input_schema": {"type": "object", "properties": {"optional_field": {"type": "string"}}},
        "start": "read",
        "steps": [
            {"id": "read", "type": "tool", "tool_id": "workflow.return",
             "input": {"value": {"$ref": "input.optional_field"}}},
        ],
    }
    resp = client.post(BASE, json={"spec": spec}, headers=_auth())
    assert resp.status_code == 201, resp.text
    wf_id = resp.json()["id"]
    
    # Publish creates version 1
    pub = client.post(f"{BASE}/{wf_id}/versions", headers=_auth())
    assert pub.status_code == 201, pub.text
    # First publish should warn about optional input without default
    warnings = pub.json().get("warnings", [])
    assert any(w["code"] == "optional_input_ref_without_default" for w in warnings), warnings
    assert "optional_field" in warnings[0]["message"]


@requires_pg
def test_required_input_has_no_warning(client):
    """A required input does not produce a warning."""
    spec = {
        "name": "Required input test",
        "input_schema": {"type": "object", "properties": {"required_field": {"type": "string"}},
                        "required": ["required_field"]},
        "start": "read",
        "steps": [
            {"id": "read", "type": "tool", "tool_id": "workflow.return",
             "input": {"value": {"$ref": "input.required_field"}}},
        ],
    }
    resp = client.post(BASE, json={"spec": spec}, headers=_auth())
    assert resp.status_code == 201, resp.text
    pub = client.post(f"{BASE}/{resp.json()['id']}/versions", headers=_auth())
    assert pub.status_code == 201, pub.text
    # No warnings about required inputs
    assert not any("optional_input_ref" in w["code"] for w in pub.json().get("warnings", []))


@requires_pg
def test_input_with_default_has_no_warning(client):
    """An optional input with a default does not produce a warning."""
    spec = {
        "name": "Input with default test",
        "input_schema": {"type": "object", "properties": {"default_field": {"type": "string", "default": "yes"}}},
        "start": "read",
        "steps": [
            {"id": "read", "type": "tool", "tool_id": "workflow.return",
             "input": {"value": {"$ref": "input.default_field"}}},
        ],
    }
    resp = client.post(BASE, json={"spec": spec}, headers=_auth())
    assert resp.status_code == 201, resp.text
    pub = client.post(f"{BASE}/{resp.json()['id']}/versions", headers=_auth())
    assert pub.status_code == 201, pub.text
    # No warnings about inputs with defaults
    assert not any("optional_input_ref" in w["code"] for w in pub.json().get("warnings", []))
