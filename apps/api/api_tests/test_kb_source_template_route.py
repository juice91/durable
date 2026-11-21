"""GET /api/v1/kb-sources/workflow-template: the example "KB Source Synchronization" spec for Studio to publish."""

from __future__ import annotations

import json

from eap_api.services import kb_sources
from eap_schemas.workflows import WorkflowSpec

from .conftest import get_auth_header
from .test_run_e2e import REPO_ROOT

EXAMPLE = REPO_ROOT / "examples" / "workflows" / "kb-source-sync.json"


def test_template_loader_returns_the_example_spec_as_a_valid_workflow_spec():
    spec = kb_sources.load_workflow_template()
    assert spec == json.loads(EXAMPLE.read_text())["spec"]
    assert spec["name"] == "KB Source Synchronization"
    WorkflowSpec.model_validate(spec)


def test_template_loader_returns_none_when_the_example_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(kb_sources, "TEMPLATE_PATH", tmp_path / "missing.json")
    assert kb_sources.load_workflow_template() is None


def test_template_route_answers_the_spec_and_needs_no_knowledge_base(client):
    headers = get_auth_header("t-template", "alice")
    response = client.get("/api/v1/kb-sources/workflow-template", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "KB Source Synchronization"
    assert body == json.loads(EXAMPLE.read_text())["spec"]
    assert client.get("/api/v1/kb-sources/workflow-template").status_code in (401, 403)


def test_template_route_is_503_when_the_example_is_not_shipped(client, monkeypatch, tmp_path):
    monkeypatch.setattr(kb_sources, "TEMPLATE_PATH", tmp_path / "missing.json")
    response = client.get("/api/v1/kb-sources/workflow-template", headers=get_auth_header("t-template", "alice"))
    assert response.status_code == 503
    assert response.json()["code"] == "workflow_template_unavailable"
