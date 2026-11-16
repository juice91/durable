"""W2.2: trace list filters (tags, metadata, text), trace/span feedback and add-to-dataset."""

from __future__ import annotations

import uuid

from .test_evals_api import API, _agent, _suite, api, fake, pg  # noqa: F401 - fixtures


def _run(app, tenant, agent_id, text, *, context=None, model_id="test:echo", draft=None):
    from eap_run_store import RunStore

    store = RunStore(app.state.database_engine)
    rid = uuid.uuid4()
    store.create_run(
        tenant, uuid.UUID(agent_id), None if draft else 1, text, rid, request_context=context,
        draft_spec=draft, model_id=model_id,
    )
    return str(rid)


def _ids(client, h, **params):
    r = client.get(f"{API}/runs", params=params, headers=h)
    assert r.status_code == 200, r.text
    return [x["run_id"] for x in r.json()["items"]]


@pg
def test_list_filters_tags_metadata_and_text(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent = _agent(client, h)
    a = _run(app, tenant, agent, "Refund order 100%_x", context={"customer": "Ada", "region": "eu"})
    b = _run(app, tenant, agent, "Weather in Paris", context={"customer": "Bob"}, model_id="test:other")
    c = _run(app, tenant, agent, "plain", draft={"name": "d"})

    assert set(_ids(client, h)) == {a, b, c}
    # Tags are derived from what the run records.
    tags = {x["run_id"]: x["tags"] for x in client.get(f"{API}/runs", headers=h).json()["items"]}
    assert "kind:agent" in tags[a] and f"agent:{agent}" in tags[a] and "ctx:customer" in tags[a]
    assert "draft" in tags[c] and "model:test:other" in tags[b]
    assert _ids(client, h, tags="draft") == [c]
    assert _ids(client, h, tags=["kind:agent", "ctx:region"]) == [a]
    assert _ids(client, h, tags="model:test:other") == [b]
    assert _ids(client, h, tags="nope") == []
    # Metadata: request context key=value, all pairs must match.
    assert _ids(client, h, metadata="customer=Ada") == [a]
    assert _ids(client, h, metadata=["customer=Ada", "region=eu"]) == [a]
    assert _ids(client, h, metadata=["customer=Ada", "region=us"]) == []
    assert client.get(f"{API}/runs", params={"metadata": "novalue"}, headers=h).status_code == 422
    # Free text: case-insensitive over input; % and _ are literal.
    assert _ids(client, h, q="weather") == [b]
    assert _ids(client, h, q="100%_x") == [a]
    assert _ids(client, h, q="%") == [a]
    assert _ids(client, h, q="zzz") == []
    # Tenant scoping: another tenant sees none.
    _t2, h2 = tenant_of()
    assert _ids(client, h2, q="weather") == [] and _ids(client, h2, tags="kind:agent") == []


@pg
def test_feedback_crud_with_span_and_tenant_scoping(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent = _agent(client, h)
    run = _run(app, tenant, agent, "hi")
    url = f"{API}/traces/{run}/feedback"

    r = client.post(url, json={"rating": "up", "comment": "  nice  ", "score": 4.5}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["trace_id"], body["span_id"], body["rating"], body["score"], body["comment"]) == (run, None, "up", 4.5, "nice")
    assert body["created_by"] == "alice" and body["created_at"]

    span = f"run:{run}"
    r = client.post(url, json={"rating": "down", "span_id": span}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["span_id"] == span and r.json()["comment"] is None

    items = client.get(url, headers=h).json()["items"]
    assert [i["rating"] for i in items] == ["down", "up"]
    only = client.get(url, params={"span_id": span}, headers=h).json()["items"]
    assert [i["rating"] for i in only] == ["down"]

    # Validation: bad rating, unknown field, unknown span, oversized comment.
    assert client.post(url, json={"rating": "meh"}, headers=h).status_code == 422
    assert client.post(url, json={"rating": "up", "x": 1}, headers=h).status_code == 422
    assert client.post(url, json={"rating": "up", "span_id": "nope"}, headers=h).status_code == 422
    assert client.post(url, json={"rating": "up", "comment": "x" * 2001}, headers=h).status_code == 422

    # Another tenant and an unknown trace: 404; no auth: 401.
    _t2, h2 = tenant_of()
    assert client.post(url, json={"rating": "up"}, headers=h2).status_code == 404
    assert client.get(url, headers=h2).status_code == 404
    unknown = f"{API}/traces/{uuid.uuid4()}/feedback"
    assert client.get(unknown, headers=h).status_code == 404
    assert client.post(url, json={"rating": "up"}).status_code == 401


@pg
def test_add_trace_to_dataset_creates_case_and_checks_suite_tenant(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent = _agent(client, h)
    suite = _suite(client, h, agent, name="traces", cases=())
    run = _run(app, tenant, agent, "What is 2+2?")
    trace = client.get(f"{API}/runs/{run}", headers=h).json()
    r = client.post(
        f"{API}/eval-suites/{suite}/cases",
        json={"name": f"trace {run[:8]}", "input": trace["input"], "expected_output": "4",
              "metadata": {"source_trace_id": run}},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["input"] == "What is 2+2?" and r.json()["expected_output"] == "4"
    assert r.json()["metadata"] == {"source_trace_id": run}
    # Another tenant's suite is a 404 for the caller.
    _t2, h2 = tenant_of()
    other_agent = _agent(client, h2)
    other_suite = _suite(client, h2, other_agent, name="theirs", cases=())
    r = client.post(f"{API}/eval-suites/{other_suite}/cases", json={"name": "x", "input": "i"}, headers=h)
    assert r.status_code == 404
