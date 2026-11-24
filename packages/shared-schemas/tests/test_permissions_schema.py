"""The `permissions` block (IAM P0b, design 3.1): omitted while absent so stored specs keep their JSON and hash,
kept when present-but-empty, and the read-only extractor of what a spec implies (3.4)."""

from eap_schemas import AgentSpec, ModelRef, canonical_spec_hash
from eap_schemas.permissions import AgentPermission, McpPermission, Permissions, implied_permissions
from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash

MODEL = ModelRef(provider="test", name="echo")
WORKFLOW = {
    "name": "w", "input_schema": {"type": "object"}, "start": "a",
    "steps": [{"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}}],
}


def test_an_agent_spec_without_the_block_keeps_its_json_and_hash():
    spec = AgentSpec(name="a", model=MODEL)
    assert "permissions" not in spec.model_dump(mode="json")
    assert canonical_spec_hash(spec) == canonical_spec_hash(AgentSpec.model_validate(spec.model_dump(mode="json")))


def test_a_present_block_is_kept_even_when_empty_and_changes_the_hash():
    bare = AgentSpec(name="a", model=MODEL)
    empty = AgentSpec(name="a", model=MODEL, permissions=Permissions())
    assert empty.model_dump(mode="json")["permissions"] == {}
    assert canonical_spec_hash(empty) != canonical_spec_hash(bare)
    full = AgentSpec(name="a", model=MODEL, permissions=Permissions(secrets=("crm_token",), tools=("http.request",)))
    assert full.model_dump(mode="json")["permissions"] == {"secrets": ["crm_token"], "tools": ["http.request"]}


def test_a_workflow_spec_omits_an_absent_block_too():
    spec = WorkflowSpec.model_validate(WORKFLOW)
    assert "permissions" not in spec.model_dump(mode="json")
    with_block = WorkflowSpec.model_validate({**WORKFLOW, "permissions": {"http_hosts": ["api.example.com"]}})
    assert with_block.model_dump(mode="json")["permissions"] == {"http_hosts": ["api.example.com"]}
    assert canonical_workflow_hash(with_block) != canonical_workflow_hash(spec)


def test_the_block_refuses_unknown_fields():
    import pytest

    with pytest.raises(Exception):
        Permissions.model_validate({"secrets": [], "everything": True})


def test_the_agent_extractor_reads_tools_kbs_mcp_and_agents():
    kb = "3f1c2a40-0000-4000-8000-000000000001"
    report = implied_permissions(
        {"tools": ["http.request", "clock.now"], "knowledge_bases": [{"id": kb}], "mcp_tools": [
            {"server": "tickets", "tool": "create_ticket"}, {"server": "tickets", "tool": "list_tickets"}],
         "agent_tools": [{"agent_id": "9a2e"}], "instructions": "never use secret:in_prose"},
        "agent",
    )
    p = report.permissions
    assert p.tools == ("http.request", "clock.now") and p.knowledge_bases == (kb,)
    assert p.mcp == (McpPermission(server="tickets", tools=("create_ticket", "list_tickets")),)
    assert p.agents == (AgentPermission(agent_id="9a2e"),)
    assert p.secrets == () and not report.dynamic


def test_the_workflow_extractor_reads_hosts_secrets_agents_and_flags_dynamic_refs():
    spec = {
        "name": "w", "input_schema": {"type": "object"}, "start": "a", "steps": [
            {"id": "a", "type": "tool", "tool_id": "http.request", "next": "b",
             "input": {"url": "https://api.example.com/v1/x", "headers": {"Authorization": "Bearer secret:crm_token"}}},
            {"id": "b", "type": "tool", "tool_id": "http.request", "next": "c",
             "input": {"url": {"$ref": "input.url"}, "json": {"token": "secret:other.token"}}},
            {"id": "c", "type": "agent", "agent_id": "9a2e", "input": {}},
        ],
    }
    report = implied_permissions(spec, "workflow")
    assert report.permissions.http_hosts == ("api.example.com",)
    assert report.permissions.secrets == ("crm_token", "other.token")
    assert report.permissions.agents == (AgentPermission(agent_id="9a2e"),)
    assert report.permissions.tools == ("http.request",)
    assert report.dynamic and report.dynamic_paths == ("steps[1].input.url",)
