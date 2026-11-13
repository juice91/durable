"""RM.14 publish checks of semantic recall: the interim refusal is gone, and the embedding model must
pass the embedding allowlist (EAP_ALLOWED_EMBEDDING_MODELS) at publish, restore and draft runs."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_knowledge_api import API
from .test_rm_checkpoint_fixes import SEMANTIC_RECALL, _agent_with_draft
from .test_runs import SPEC, deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

DISALLOWED = {"semantic_recall": {"embedding_model": "openai:text-embedding-3-small"}}
DISALLOWED_MESSAGE = (
    "memory.semantic_recall: embedding model 'openai:text-embedding-3-small' is not allowed on this platform "
    "(not in the EAP_ALLOWED_EMBEDDING_MODELS allowlist)"
)


@requires_pg
def test_semantic_recall_publishes_and_runs_as_a_draft(pg, tenant_of, fake_dbos):
    client, _app = pg
    _tenant, h = tenant_of()
    agent_id = _agent_with_draft(client, h, SEMANTIC_RECALL)
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["memory"]["semantic_recall"]["embedding_model"] == "test:hash"
    r = client.post(
        f"{API}/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code in (200, 201, 202), r.text
    assert len(fake_dbos.calls) == 1


@requires_pg
def test_an_embedding_model_outside_the_allowlist_is_refused(pg, tenant_of, fake_dbos):
    client, app = pg
    _tenant, h = tenant_of()

    # Publish and draft run.
    agent_id = _agent_with_draft(client, h, DISALLOWED)
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 422 and r.json()["detail"] == DISALLOWED_MESSAGE, r.text
    r = client.post(
        f"{API}/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["detail"] == DISALLOWED_MESSAGE, r.text
    assert fake_dbos.calls == []

    # Restore of a version stored under an older allowlist.
    plain = _agent_with_draft(client, h)
    assert client.post(f"{API}/agents/{plain}/versions", headers=h).status_code == 201
    stored = {**SPEC, "instructions": "v1 with recall", "memory": DISALLOWED}
    with app.state.database_engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_versions SET spec = CAST(:s AS jsonb) WHERE agent_id = :a AND version = 1"),
            {"s": json.dumps(stored), "a": plain},
        )
    r = client.patch(f"{API}/agents/{plain}", json={"spec": {**SPEC, "instructions": "v2"}}, headers=h)
    assert r.status_code == 200, r.text
    assert client.post(f"{API}/agents/{plain}/versions", headers=h).status_code == 201
    r = client.post(f"{API}/agents/{plain}/versions/1/restore", json={"discard_draft": True}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == DISALLOWED_MESSAGE, r.text


@requires_pg
def test_a_wildcard_model_without_dimensions_is_refused(pg, tenant_of, fake_dbos):
    """`test:*` allows any test model by wildcard, but only test:hash and test:hash768 have known
    dimensions: another name is refused with the policy's dimensions message."""
    client, _app = pg
    _tenant, h = tenant_of()
    agent_id = _agent_with_draft(client, h, {"semantic_recall": {"embedding_model": "test:unknown"}})
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("memory.semantic_recall: embedding model 'test:unknown' is allowed by a wildcard")


@requires_pg
def test_a_misconfigured_embedding_allowlist_is_a_503(pg, tenant_of, fake_dbos, monkeypatch):
    """The allowlist is checked at startup; if the environment changes under the process, publish
    answers 503 (never a 422 that blames the agent) and nothing is enqueued."""
    client, _app = pg
    _tenant, h = tenant_of()
    agent_id = _agent_with_draft(client, h, SEMANTIC_RECALL)
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:hash=999")  # not 768, 1024 or 1536
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured"
    r = client.post(
        f"{API}/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code == 503 and fake_dbos.calls == [], r.text
