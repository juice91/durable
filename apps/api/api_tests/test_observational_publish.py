"""RM.13 publish checks of observational memory: the interim refusal is gone, and the observer model
must pass the chat model allowlist at publish, restore and draft runs."""

from __future__ import annotations

import uuid

from .run_helpers import requires_pg
from .test_knowledge_api import API
from .test_rm_checkpoint_fixes import OBSERVATIONAL, _agent_with_draft
from .test_runs import deltas, fake_dbos, pg, stream_settings, tenant_of  # noqa: F401 - fixtures

DISALLOWED = {"observational": {"model": {"provider": "openai", "name": "not-allowlisted"}}}
DISALLOWED_MESSAGE = (
    "observer model 'openai:not-allowlisted' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)"
)


@requires_pg
def test_observational_memory_publishes(pg, tenant_of, fake_dbos):
    client, _app = pg
    _tenant, h = tenant_of()
    agent_id = _agent_with_draft(client, h, OBSERVATIONAL)
    r = client.post(f"{API}/agents/{agent_id}/versions", headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["memory"]["observational"]["model"] == {"provider": "test", "name": "observer"}


@requires_pg
def test_an_observer_model_outside_the_allowlist_is_refused(pg, tenant_of, fake_dbos):
    client, _app = pg
    _tenant, h = tenant_of()
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


@requires_pg
def test_a_draft_run_with_an_observer_model_outside_the_allowlist_is_refused(pg, tenant_of, fake_dbos):
    """A draft run (never published) goes through the same check: 422, nothing enqueued."""
    client, _app = pg
    _tenant, h = tenant_of()
    agent_id = _agent_with_draft(client, h, DISALLOWED)
    r = client.post(
        f"{API}/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code == 422 and r.json()["detail"] == DISALLOWED_MESSAGE, r.text
    assert fake_dbos.calls == []
    # The same agent with an allowlisted observer model runs as a draft.
    ok = _agent_with_draft(client, h, OBSERVATIONAL)
    r = client.post(
        f"{API}/agents/{ok}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=h,
    )
    assert r.status_code in (200, 201, 202), r.text
    assert len(fake_dbos.calls) == 1
