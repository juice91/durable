"""POST /tools/{tool_id}/runs: focused implementer tests of the route contract and the hand-off.

The enqueue goes to a fake DBOSClient inside the real DBOSRunLauncher (as in test_runs.py), so these
check the exact `eap.tool_run` enqueue call without a worker.
"""

from __future__ import annotations

import uuid

from .test_runs import fake_dbos, pg, tenant_of, stream_settings, deltas  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg


def _post(client, headers, tool_id: str, body: dict):
    return client.post(f"/api/v1/tools/{tool_id}/runs", json=body, headers=headers)


def test_tool_run_is_recorded_and_enqueued(pg, tenant_of, fake_dbos):
    client, _app = pg
    tenant, headers = tenant_of()
    run_id = str(uuid.uuid4())
    r = _post(client, headers, "tickets.create", {"input": {"title": "Printer", "body": "on fire"}, "run_id": run_id})
    assert r.status_code == 202, r.text
    assert r.json()["run_id"] == run_id
    assert r.json()["kind"] == "tool" and r.json()["tool_id"] == "tickets.create"

    (options, args), = fake_dbos.calls
    assert options["queue_name"] == "eap-agent-runs"
    assert options["workflow_name"] == "eap.tool_run"
    assert options["workflow_id"] == run_id
    assert args == (run_id, tenant, "tickets.create", '{"body":"on fire","title":"Printer"}')

    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["kind"] == "tool" and run["tool_id"] == "tickets.create"
    assert run["agent_id"] is None and run["version"] is None
    assert run["input"] == '{"body":"on fire","title":"Printer"}'
    assert run["status"] == "queued"

    # Idempotent retry: same run_id + input -> 202 again (re-enqueued); different input -> 409.
    again = _post(client, headers, "tickets.create", {"input": {"body": "on fire", "title": "Printer"}, "run_id": run_id})
    assert again.status_code == 202 and len(fake_dbos.calls) == 2
    other = _post(client, headers, "tickets.create", {"input": {"title": "x", "body": "y"}, "run_id": run_id})
    assert other.status_code == 409

    listed = client.get("/api/v1/runs?kind=tool", headers=headers).json()["items"]
    assert [i["run_id"] for i in listed] == [run_id]
    assert client.get("/api/v1/runs?kind=agent", headers=headers).json()["items"] == []
    # Another tenant cannot see it.
    _, other_headers = tenant_of()
    assert client.get(f"/api/v1/runs/{run_id}", headers=other_headers).status_code == 404


def test_tool_run_refusals(pg, tenant_of, fake_dbos, monkeypatch):
    client, _app = pg
    _, headers = tenant_of()
    assert _post(client, headers, "no.such.tool", {"input": {}}).status_code == 404

    bad = _post(client, headers, "tickets.create", {"input": {"title": "only a title", "extra": 1}})
    assert bad.status_code == 422
    errors = {(tuple(e["loc"]), e["type"]) for e in bad.json()["detail"]}
    assert errors == {(("body", "input", "body"), "missing"), (("body", "input", "extra"), "extra_forbidden")}

    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    refused = _post(client, headers, "tickets.create", {"input": {"title": "a", "body": "b"}})
    assert refused.status_code == 422
    assert refused.json()["detail"] == (
        "tool 'tickets.create' is not allowed on this platform (not in the EAP_ALLOWED_TOOLS allowlist)"
    )
    assert _post(client, headers, "clock.now", {"input": {}}).status_code == 202
    assert len(fake_dbos.calls) == 1


# --------------------------------------------------------------------------- failed hand-off


def test_failed_enqueue_marks_the_created_run_failed_and_a_retry_requeues_it(pg, tenant_of, fake_dbos):
    from eap_run_store import ENQUEUE_FAILED_ERROR

    client, _app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())
    body = {"input": {"title": "a", "body": "b"}, "run_id": run_id}
    fake_dbos.fail_times = 1
    r = _post(client, headers, "tickets.create", body)
    assert r.status_code == 503
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (run["status"], run["error"]) == ("failed", ENQUEUE_FAILED_ERROR)

    retry = _post(client, headers, "tickets.create", body)
    assert retry.status_code == 202 and retry.json()["status"] == "queued"
    assert len(fake_dbos.calls) == 1
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (run["status"], run["error"]) == ("queued", None)


def test_failed_enqueue_never_touches_a_run_it_did_not_create(pg, tenant_of, fake_dbos):
    """Ambiguous success: the first request enqueued (the workflow exists); a retry whose enqueue
    raises must not mark that run failed."""
    client, _app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())
    body = {"input": {}, "run_id": run_id}
    assert _post(client, headers, "clock.now", body).status_code == 202
    fake_dbos.fail_times = 1
    assert _post(client, headers, "clock.now", body).status_code == 503
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["status"] == "queued"


def test_failed_enqueue_does_not_overwrite_a_run_the_worker_already_started(pg, tenant_of, fake_dbos):
    """The enqueue raised after DBOS accepted it and the worker already set `running`: kept."""
    from sqlalchemy import text

    client, app = pg
    _, headers = tenant_of()
    run_id = str(uuid.uuid4())

    def enqueue_then_raise(options, *args):
        with app.state.database_engine.begin() as conn:
            conn.execute(text("UPDATE runs SET status = 'running' WHERE id = :r"), {"r": run_id})
        raise ConnectionError("lost the reply")

    fake_dbos.enqueue = enqueue_then_raise
    assert _post(client, headers, "clock.now", {"input": {}, "run_id": run_id}).status_code == 503
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (run["status"], run["error"]) == ("running", None)


# --------------------------------------------------------------------------- no input echo


def _assert_safe_422(r) -> None:
    assert r.status_code == 422, r.text
    assert "TOP_SECRET" not in r.text
    assert all(set(e) == {"loc", "msg", "type"} for e in r.json()["detail"])


def test_rejected_bodies_never_echo_the_input(pg, tenant_of, fake_dbos):
    client, _app = pg
    _, headers = tenant_of()
    url = "/api/v1/tools/tickets.create/runs"

    wrong_type = client.post(url, json={"input": "TOP_SECRET"}, headers=headers)
    _assert_safe_422(wrong_type)
    assert [(e["loc"], e["type"]) for e in wrong_type.json()["detail"]] == [(["body", "input"], "dict_type")]

    extra = client.post(url, json={"input": {"title": "a", "body": "b"}, "note": "TOP_SECRET"}, headers=headers)
    _assert_safe_422(extra)
    assert [(e["loc"], e["type"]) for e in extra.json()["detail"]] == [(["body", "note"], "extra_forbidden")]

    malformed = client.post(
        url, content=b'{"input": {"title": "TOP_SECRET"', headers={**headers, "Content-Type": "application/json"}
    )
    _assert_safe_422(malformed)
    assert [e["type"] for e in malformed.json()["detail"]] == ["json_invalid"]

    bad_field = client.post(url, json={"input": {"title": ["TOP_SECRET"], "body": "b"}}, headers=headers)
    _assert_safe_422(bad_field)
    assert [(e["loc"], e["type"]) for e in bad_field.json()["detail"]] == [(["body", "input", "title"], "string_type")]

    bad_run_id = client.post(url, json={"input": {}, "run_id": "TOP_SECRET"}, headers=headers)
    _assert_safe_422(bad_run_id)
    assert fake_dbos.calls == []

    # Authentication is checked before the body.
    assert client.post(url, json={"input": "TOP_SECRET"}).status_code == 401


def test_successful_enqueue_answers_queued_when_a_concurrent_owner_marked_the_run_failed(pg, tenant_of, fake_dbos):
    """R1 created the run and its enqueue failed (run marked failed) while R2's enqueue succeeded:
    R2's 202 says queued, not failed."""
    from eap_run_store import ENQUEUE_FAILED_ERROR

    client, app = pg
    tenant, headers = tenant_of()
    run_id = str(uuid.uuid4())
    run_store = app.state.run_stores[0]
    original = fake_dbos.enqueue

    def enqueue_while_owner_fails(options, *args):
        original(options, *args)
        assert run_store.mark_enqueue_failed(tenant, uuid.UUID(run_id))  # R1's failure, interleaved

    fake_dbos.enqueue = enqueue_while_owner_fails
    r = _post(client, headers, "clock.now", {"input": {}, "run_id": run_id})
    assert r.status_code == 202
    assert (r.json()["status"], r.json()["display_status"]) == ("queued", "queued")
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["error"] == ENQUEUE_FAILED_ERROR
