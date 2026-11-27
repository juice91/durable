"""`ExternalRunService`: every rule of sections 4, 6, 7.1 and 7.3 of the inbound design, over real Postgres. There is no
public route yet; the service is called as the MCP and A2A controllers will call it, with the principal the identity
chain builds from the key."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from eap_iam import AuthError, Principal

from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, TooManyRequests, Unavailable

from .external_world import BASE, NOISY_STEPS, Live, World, external_env, use_engine, world  # noqa: F401
from .run_helpers import requires_pg

pytestmark = requires_pg


def refused(exc_type, code: str, fn, *args, **kwargs):
    with pytest.raises(exc_type) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, exc.value.code
    return exc.value


def ids(live: Live, run_id: str) -> tuple[str, uuid.UUID]:
    return live.tenant, uuid.UUID(run_id)


def stores(world: World):
    return world.app.state.run_stores


# -------------------------------------------------------------------------------------------------- start


def test_a_start_runs_the_workflow_as_the_key_and_for_nobody(world: World):
    live = world.live()
    view = live.start(deadline_seconds=600)
    assert view["state"] == "queued" and view["done"] is False and view["export"] == "run_report" and view["version"] == 1
    assert view["poll_after_seconds"] > 0 and view.get("result") is None and view.get("error") is None
    deadline = datetime.fromisoformat(view["deadline"])
    assert timedelta(seconds=590) < deadline - datetime.now(timezone.utc) <= timedelta(seconds=600)
    (run,) = live.runs()
    key_id = live.key.split("_")[1]
    assert str(run["id"]) == view["run_id"] and run["principal"] == f"api_key:{key_id}"
    assert run["started_by_kind"] == "api_key" and run["on_behalf_of"] is None
    assert json.loads(run["input"]) == {"q": "hello"} and len(world.fake.calls) == 1


def test_the_deadline_is_the_shorter_of_the_callers_and_the_exports(world: World):
    live = world.live(max_runtime_seconds=120)
    for asked, expected in ((None, 120), (60, 60), (100_000, 120)):
        view = live.start(key=f"k-{asked}", deadline_seconds=asked)
        left = datetime.fromisoformat(view["deadline"]) - datetime.now(timezone.utc)
        assert timedelta(seconds=expected - 10) < left <= timedelta(seconds=expected)
    refused(InvalidInput, "deadline_invalid", live.start, key="bad", deadline_seconds=0)


def test_exports_lists_only_what_this_key_may_start_over_this_protocol(world: World):
    live = world.live()
    hidden = world.workflow(live.headers)
    assert world.enable(live.headers, world.export(live.headers, hidden, name="not_in_allowlist")).status_code == 200
    world.export(live.headers, live.workflow, name="drafted_only")
    found = world.call("exports", live.principal(), "mcp")
    assert [e["name"] for e in found] == ["run_report"]
    assert found[0]["input_schema"]["required"] == ["q"] and found[0]["result"] == "output" and found[0]["title"]
    assert set(found[0]) == {"name", "title", "description", "tags", "examples", "input_schema", "result",
                             "require_idempotency_key", "version"}
    assert world.call("exports", live.principal(), "a2a") == []


def test_may_start_needs_every_one_of_its_conditions(world: World):
    live = world.live()
    hidden = world.workflow(live.headers)
    assert world.enable(live.headers, world.export(live.headers, hidden, name="hidden")).status_code == 200
    world.export(live.headers, live.workflow, name="drafted")
    start = lambda name, protocol="mcp": world.call(  # noqa: E731
        "start", live.principal(), protocol, name, {"q": "x"}, idempotency_key="k")
    for name, protocol in (("hidden", "mcp"), ("drafted", "mcp"), ("missing", "mcp"), ("run_report", "a2a")):
        refused(NotFound, "export_not_found", start, name, protocol)
    refused(InvalidInput, "protocol_invalid", start, "run_report", "grpc")
    # the client is disabled: effective on the next request
    r = world.client.patch(f"{BASE}/clients/{live.client['id']}", json={"enabled": False}, headers=live.headers)
    assert r.status_code == 200
    refused(Forbidden, "client_disabled", live.start)
    assert live.runs() == []


def test_a_revoked_or_expired_key_stops_at_once(world: World):
    live = world.live()
    principal = live.principal()
    world.sql("UPDATE api_keys SET revoked_at = now() WHERE tenant_id = :t", t=live.tenant)
    with pytest.raises(AuthError):
        live.principal()
    # a principal built before the revocation is refused too
    refused(Forbidden, "key_inactive", world.call, "start", principal, "mcp", "run_report", {"q": "x"}, idempotency_key="k")


def test_only_a_run_key_may_use_the_external_surface(world: World):
    live = world.live()
    people = Principal(tenant_id=live.tenant, kind="user", subject="alice", provider="loopback-dev", roles=frozenset({"admin"}))
    refused(Forbidden, "api_key_required", world.call, "list", people)
    for scope in ("agui:run", "mcp", "manage", "otlp"):
        r = world.client.post("/api/v1/api-keys", json={"name": scope, "scopes": [scope]}, headers=live.headers)
        assert r.status_code == 201, r.text
        with pytest.raises(AuthError) as exc:
            world.principal(r.json()["key"])
        assert exc.value.status == 403
        # even a principal that reached the service some other way
        forged = Principal(tenant_id=live.tenant, kind="api_key", subject=r.json()["id"], provider="api-key",
                           scopes=frozenset({scope}))
        refused(Forbidden, "missing_scope", world.call, "list", forged)
    assert live.runs() == []


def test_the_switch_and_enforced_grants_gate_every_start(world: World, external_env):
    live = world.live()
    external_env.setenv("EAP_AGENT_GRANTS_ENFORCE", "0")
    refused(Conflict, "grants_not_enforced", live.start)
    external_env.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")
    external_env.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "false")
    refused(NotFound, "external_access_disabled", live.start)
    refused(NotFound, "external_access_disabled", world.call, "list", live.principal())
    assert live.runs() == []


def test_a_latest_export_whose_contract_moved_without_a_publish_hook_is_disabled_at_the_next_start(world: World):
    live = world.live()
    world.sql("UPDATE workflow_exports SET approved_contract_hash = 'old' WHERE tenant_id = :t", t=live.tenant)
    refused(NotFound, "export_not_found", live.start)
    got = world.client.get(f"{BASE}/exports/{live.export['id']}", headers=live.headers).json()
    assert got["enabled"] is False and got["disabled_reason"] == "export_contract_changed"


# -------------------------------------------------------------------------------------------------- input and keys


def test_bad_input_is_refused_before_anything_is_claimed(world: World):
    live = world.live()
    error = refused(InvalidInput, "input_invalid", world.call, "start", live.principal(), "mcp", "run_report",
                    {"q": 5, "secret": "do-not-echo"}, idempotency_key="k")
    assert error.details and "do-not-echo" not in json.dumps(error.details)
    refused(InvalidInput, "input_too_large", live.start, q="x" * (33 * 1024))
    assert world.sql("SELECT 1 FROM external_runs WHERE tenant_id = :t", t=live.tenant) == []
    assert live.start()["state"] == "queued"  # the same key works with a corrected input


@pytest.mark.parametrize("key", ["", "x" * 129])
def test_the_idempotency_key_is_one_to_128_characters(world: World, key):
    live = world.live()
    refused(InvalidInput, "idempotency_key_invalid", live.start, key=key)


def test_a_repeated_key_is_the_same_run_and_another_input_conflicts(world: World):
    live = world.live()
    first = live.start()
    again = live.start()
    assert again["run_id"] == first["run_id"] and len(live.runs()) == 1 and len(world.fake.calls) >= 1
    refused(Conflict, "idempotency_conflict", live.start, q="other")
    assert len(live.runs()) == 1


def test_a_publish_between_a_send_and_its_retry_keeps_the_version(world: World):
    live = world.live()
    first = live.start()
    world.publish(live.headers, live.workflow)
    again = live.start()
    assert again["run_id"] == first["run_id"] and again["version"] == 1
    assert [r["workflow_version"] for r in live.runs()] == [1]
    assert live.start(key="k-2")["version"] == 2


def test_a_retry_of_a_started_run_answers_after_the_export_was_disabled_but_starts_nothing_new(world: World):
    live = world.live()
    first = live.start()
    world.client.post(f"{BASE}/exports/{live.export['id']}/disable", headers=live.headers)
    assert live.start()["run_id"] == first["run_id"]
    refused(NotFound, "export_not_found", live.start, key="k-2")
    refused(Conflict, "idempotency_conflict", live.start, q="other")
    assert [r["status"] for r in live.runs()] == ["queued"]  # disabling cancels nothing


def test_a_key_is_optional_only_where_the_export_allows_it(world: World):
    live = world.live()
    a, b = live.start(key=None), live.start(key=None)
    assert a["run_id"] != b["run_id"]
    noisy = world.live(name="post_it", steps=NOISY_STEPS)
    refused(InvalidInput, "idempotency_key_required", noisy.start, key=None)
    assert noisy.start()["state"] == "queued"


def test_the_run_id_derives_from_client_export_and_key(world: World):
    live = world.live()
    view = live.start(key="abc")
    assert view["run_id"] == str(uuid.uuid5(uuid.UUID(live.client["id"]), f"{live.export['id']}:abc"))
    assert not world.sql("SELECT 1 FROM external_runs WHERE tenant_id = :t AND idempotency_hash = 'abc'", t=live.tenant)


# -------------------------------------------------------------------------------------------------- crashes


class Crash(Exception):
    """Stands for the process dying at this point: not a refusal the service handles."""


def test_a_crash_between_the_claim_and_the_run_row_yields_one_run(world: World, monkeypatch):
    live = world.live()
    run_store = stores(world)[0]
    real = run_store.create_workflow_run

    def die(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(run_store, "create_workflow_run", die)
    with pytest.raises(Crash):
        live.start()
    assert live.runs() == [] and len(world.sql("SELECT 1 FROM external_runs WHERE tenant_id = :t", t=live.tenant)) == 1
    claimed = world.sql("SELECT run_id FROM external_runs WHERE tenant_id = :t", t=live.tenant)[0]["run_id"]
    waiting = world.call("get", live.principal(), claimed)
    assert waiting["state"] == "queued"  # claimed, not yet started
    monkeypatch.setattr(run_store, "create_workflow_run", real)
    view = live.start()
    assert view["run_id"] == claimed and len(live.runs()) == 1 and len(world.fake.calls) == 1


def test_a_crash_between_the_run_row_and_the_enqueue_yields_one_run(world: World, monkeypatch):
    live = world.live()
    launcher = world.app.state.run_launcher
    real = launcher.launch_workflow

    def die(*args, **kwargs):
        raise BaseExceptionStandIn()

    class BaseExceptionStandIn(BaseException):
        pass

    monkeypatch.setattr(launcher, "launch_workflow", die)
    with pytest.raises(BaseExceptionStandIn):
        live.start()
    assert [r["status"] for r in live.runs()] == ["queued"] and world.fake.calls == []
    monkeypatch.setattr(launcher, "launch_workflow", real)
    view = live.start()
    assert [str(r["id"]) for r in live.runs()] == [view["run_id"]] and len(world.fake.calls) == 1


def test_a_failed_hand_off_is_retryable_and_the_retry_starts_the_same_run(world: World):
    live = world.live()
    world.fake.fail_times = 1
    refused(Unavailable, "start_unavailable", live.start)
    view = live.start()
    assert view["state"] == "queued" and len(live.runs()) == 1 and len(world.fake.calls) == 1


def test_a_refusal_by_the_workflow_leaves_no_claim(world: World):
    live = world.live()
    r = world.client.post(f"/api/v1/workflows/{live.workflow}/archive", headers=live.headers)
    assert r.status_code == 200, r.text
    refused(Conflict, "workflow_archived", live.start)
    assert world.sql("SELECT 1 FROM external_runs WHERE tenant_id = :t", t=live.tenant) == []


# -------------------------------------------------------------------------------------------------- quotas


def test_quotas_per_client_tenant_and_day(world: World, external_env):
    live = world.live()
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT", "2")
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_TENANT", "3")
    live.start(key="a"), live.start(key="b")
    over = refused(TooManyRequests, "quota_exceeded", live.start, key="c")
    assert over.retry_after > 0 and len(live.runs()) == 2
    live.start(key="a")  # a replay is never counted
    other = world.client_record(live.headers)
    second = Live(world, live.tenant, live.headers, live.workflow, live.export, other,
                  world.key(live.headers, other["id"], [live.workflow])["key"])
    second.start(key="a")
    refused(TooManyRequests, "quota_exceeded", second.start, key="b")  # the tenant's cap
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_TENANT", "20")
    external_env.setenv("EAP_EXTERNAL_MAX_RUNS_PER_DAY", "2")
    refused(TooManyRequests, "quota_exceeded", live.start, key="d")


def test_a_client_record_lowers_or_raises_its_own_limits(world: World, external_env):
    live = world.live()
    patch = lambda **body: world.client.patch(  # noqa: E731
        f"{BASE}/clients/{live.client['id']}", json=body, headers=live.headers)
    assert patch(max_active_runs=1).status_code == 200
    live.start(key="a")
    refused(TooManyRequests, "quota_exceeded", live.start, key="b")
    external_env.setenv("EAP_EXTERNAL_MAX_ACTIVE_RUNS_PER_CLIENT", "1")
    assert patch(max_active_runs=2).status_code == 200  # an admin may raise it, up to the platform maximum
    live.start(key="b")
    assert patch(max_active_runs=51).status_code == 422
    assert patch(max_runs_per_day=1, max_active_runs=None).status_code == 200
    refused(TooManyRequests, "quota_exceeded", live.start, key="c")


# -------------------------------------------------------------------------------------------------- ownership


def test_a_client_sees_only_its_own_runs(world: World):
    live = world.live()
    mine = live.start()
    other = world.client_record(live.headers)
    stranger = Live(world, live.tenant, live.headers, live.workflow, live.export, other,
                    world.key(live.headers, other["id"], [live.workflow])["key"])
    foreign = world.live()  # another tenant
    person = world.client.post(f"/api/v1/workflows/{live.workflow}/runs", json={"input": {"q": "x"}}, headers=live.headers)
    assert person.status_code == 202, person.text
    for principal, run_id in (
        (stranger.principal(), mine["run_id"]), (foreign.principal(), mine["run_id"]),
        (live.principal(), person.json()["run_id"]), (live.principal(), str(uuid.uuid4())), (live.principal(), "nonsense"),
    ):
        for method in ("get", "cancel"):
            refused(NotFound, "run_not_found", world.call, method, principal, run_id)
    assert world.call("list", stranger.principal())["items"] == []
    assert world.call("list", foreign.principal())["items"] == []
    assert [i["run_id"] for i in world.call("list", live.principal())["items"]] == [mine["run_id"]]
    assert not world.sql("SELECT 1 FROM execution_events WHERE tenant_id = :t AND event_key = 'cancel'", t=live.tenant)


def test_another_tenants_export_and_client_do_not_exist(world: World):
    live, other = world.live(), world.live(name="theirs")
    refused(NotFound, "export_not_found", world.call, "start", live.principal(), "mcp", "theirs", {"q": "x"},
            idempotency_key="k")
    forged = live.principal().model_copy(update={"tenant_id": other.tenant})
    refused(Forbidden, "key_inactive", world.call, "list", forged)


def test_rotating_a_key_keeps_the_clients_runs(world: World):
    live = world.live()
    run = live.start()
    new = world.key(live.headers, live.client["id"], [live.workflow])["key"]
    old_id = live.key.split("_")[1]
    assert world.client.delete(f"{BASE}/clients/{live.client['id']}/keys/{old_id}", headers=live.headers).status_code == 204
    assert world.call("get", world.principal(new), run["run_id"])["run_id"] == run["run_id"]
    again = Live(world, live.tenant, live.headers, live.workflow, live.export, live.client, new).start()
    assert again["run_id"] == run["run_id"] and len(live.runs()) == 1


# -------------------------------------------------------------------------------------------------- state mapping


def finish(world: World, live: Live, run_id: str, status: str, **kwargs):
    run_store, _events = stores(world)
    run_store.finish_with_end(live.tenant, uuid.UUID(run_id), status, **kwargs)


def get(world: World, live: Live, run_id: str):
    return world.call("get", live.principal(), run_id)


def test_running_waiting_and_ended_states_map_as_the_design_says(world: World):
    live = world.live()
    run_store, events = stores(world)
    run_id = live.start()["run_id"]
    tenant, rid = ids(live, run_id)
    run_store.mark_running(tenant, rid)
    run_store.heartbeat(tenant, rid)
    assert get(world, live, run_id)["state"] == "running"
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    events.append_approval_waiting(
        tenant, rid, "step", "ok_gate",
        {"step_id": "ok_gate", "prompt": "internal prompt", "deadline": deadline, "workflow_id": live.workflow})
    view = get(world, live, run_id)
    assert view["state"] == "input_required" and view["waiting"] == {"kind": "approval", "answerable": False}
    assert "ok_gate" not in json.dumps(view) and "internal prompt" not in json.dumps(view)
    finish(world, live, run_id, "completed", output=json.dumps({"total": 3}), output_format="json")
    view = get(world, live, run_id)
    assert view["state"] == "completed" and view["done"] is True and view["result"] == {"total": 3}
    assert view["poll_after_seconds"] == 0 and view.get("waiting") is None


def test_an_input_step_is_input_required_and_not_yet_answerable(world: World):
    live = world.live(client_inputs=True)
    run_store, events = stores(world)
    run_id = live.start()["run_id"]
    tenant, rid = ids(live, run_id)
    run_store.mark_running(tenant, rid)
    run_store.heartbeat(tenant, rid)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    events.append_approval_waiting(tenant, rid, "step", "ask", {
        "step_id": "ask", "prompt": "p", "deadline": deadline, "workflow_id": live.workflow,
        "resume_schema": {"type": "object"},
    })
    assert get(world, live, run_id)["waiting"] == {"kind": "input", "answerable": False}


@pytest.mark.parametrize("status, error_code, state, code, retry_safe", [
    ("failed", "tool_error", "failed", "tool_error", None),
    ("failed", "approval_rejected", "failed", "approval_rejected", None),
    ("failed", "run_cancelled", "cancelled", None, None),
    ("needs_attention", "run_cancelled", "failed", "outcome_unknown", False),
])
def test_failures(world: World, status, error_code, state, code, retry_safe):
    live = world.live()
    run_id = live.start()["run_id"]
    finish(world, live, run_id, status, error="X: internal detail at host db-1.internal",
           end_data={"error_code": error_code})
    view = get(world, live, run_id)
    assert view["state"] == state and view["done"] is True and view.get("result") is None
    assert (view.get("error") or {}).get("code") == code
    assert view.get("retry_safe") == retry_safe
    assert "db-1.internal" not in json.dumps(view)


def test_a_cancel_by_the_deadline_is_a_failure_with_its_own_code(world: World):
    from eap_run_store import ExternalAccessStore

    live = world.live()
    run_id = live.start()["run_id"]
    store = ExternalAccessStore(world.engine)
    assert store.request_deadline_cancel(live.tenant, run_id)
    stores(world)[0].cancel_run_tree(live.tenant, run_id)
    view = get(world, live, run_id)
    assert view["state"] == "failed" and view["error"]["code"] == "deadline_exceeded"


def test_skipped_is_completed_and_status_only_returns_no_result(world: World):
    live = world.live(result="status_only")
    run_id = live.start()["run_id"]
    finish(world, live, run_id, "skipped", output=json.dumps({"secret": "no"}), output_format="json")
    view = get(world, live, run_id)
    assert view["state"] == "completed" and view["outcome"] == "skipped" and view.get("result") is None
    assert "secret" not in json.dumps(view)


def test_a_result_over_256_kib_is_withheld_and_flagged(world: World):
    live = world.live()
    run_id = live.start()["run_id"]
    finish(world, live, run_id, "completed", output=json.dumps({"blob": "x" * (300 * 1024)}), output_format="json")
    view = get(world, live, run_id)
    assert view["state"] == "completed" and view.get("result") is None and view["truncated"] is True


def test_the_list_filters_and_pages(world: World):
    live = world.live()
    first, second = live.start(key="a")["run_id"], live.start(key="b")["run_id"]
    finish(world, live, first, "completed", output="done")
    page = world.call("list", live.principal(), limit=1)
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = world.call("list", live.principal(), limit=1, cursor=page["next_cursor"])
    assert {page["items"][0]["run_id"], rest["items"][0]["run_id"]} == {first, second} and rest["next_cursor"] is None
    done = world.call("list", live.principal(), state="completed")["items"]
    assert [i["run_id"] for i in done] == [first] and "result" not in done[0]
    refused(InvalidInput, "state_invalid", world.call, "list", live.principal(), state="skipped")
    refused(InvalidInput, "cursor_invalid", world.call, "list", live.principal(), cursor="garbage")


# -------------------------------------------------------------------------------------------------- cancel


def test_cancel_stops_the_run_and_repeating_it_is_harmless(world: World):
    live = world.live()
    run_id = live.start()["run_id"]
    first = world.call("cancel", live.principal(), run_id)
    assert first["accepted"] is True and first["run"]["state"] == "cancelled" and first["run"]["done"] is True
    again = world.call("cancel", live.principal(), run_id)
    assert again["accepted"] is False and again["run"]["state"] == "cancelled"
    event = stores(world)[1].get_event(live.tenant, uuid.UUID(run_id), "cancel")
    assert event.data["requested_by"] == f"apikey:{live.key.split('_')[1]}"


def test_cancelling_a_claim_whose_run_never_started_is_retryable(world: World, monkeypatch):
    live = world.live()
    run_store = stores(world)[0]

    def die(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(run_store, "create_workflow_run", die)
    with pytest.raises(Crash):
        live.start()
    claimed = world.sql("SELECT run_id FROM external_runs WHERE tenant_id = :t", t=live.tenant)[0]["run_id"]
    refused(Unavailable, "cancel_unavailable", world.call, "cancel", live.principal(), claimed)


def test_an_unfinished_cancel_answers_retryable(world: World, monkeypatch):
    live = world.live()
    run_id = live.start()["run_id"]

    def die(**kwargs):
        raise RuntimeError("system database down")

    monkeypatch.setattr(world.app.state.run_launcher, "cancel_workflow_tree", die, raising=False)
    refused(Unavailable, "cancel_unavailable", world.call, "cancel", live.principal(), run_id)


def test_disabling_a_client_cancels_nothing_and_the_explicit_action_does(world: World):
    live = world.live()
    a, b = live.start(key="a")["run_id"], live.start(key="b")["run_id"]
    finish(world, live, b, "completed", output="done")
    other = world.live()
    theirs = other.start()["run_id"]
    world.client.patch(f"{BASE}/clients/{live.client['id']}", json={"enabled": False}, headers=live.headers)
    assert sorted(r["status"] for r in live.runs()) == ["completed", "queued"]
    r = world.client.post(f"{BASE}/clients/{live.client['id']}/cancel-runs", headers=live.headers)
    assert r.status_code == 200 and r.json() == {"cancelled": [a], "pending": []}
    assert sorted(r["status"] for r in live.runs()) == ["completed", "failed"]
    assert other.runs()[0]["status"] == "queued" and theirs


# -------------------------------------------------------------------------------------------------- audit


def test_starts_cancels_and_refusals_are_audited_with_the_key_and_the_client(world: World):
    live = world.live()
    run_id = live.start()["run_id"]
    world.call("cancel", live.principal(), run_id)
    refused(NotFound, "export_not_found", world.call, "start", live.principal(), "mcp", "nope", {"q": "do-not-log"},
            idempotency_key="do-not-log-either")
    refused(NotFound, "run_not_found", world.call, "get", live.principal(), str(uuid.uuid4()))
    rows = world.sql(
        "SELECT action, actor_kind, actor_subject, outcome, run_id, detail FROM audit_log"
        " WHERE tenant_id = :t AND action LIKE 'external.%' ORDER BY at, action", t=live.tenant)
    key_id = live.key.split("_")[1]
    assert {r["actor_kind"] for r in rows} == {"api_key"} and {r["actor_subject"] for r in rows} == {key_id}
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], []).append(r)
    start_ok, start_no = sorted(by_action["external.run.start"], key=lambda r: r["outcome"] != "success")
    assert start_ok["run_id"] == run_id and start_ok["detail"] == {
        "client": live.client["name"], "protocol": "mcp", "export": "run_report"}
    assert start_no["outcome"] == "failed" and start_no["detail"]["reason"] == "export_not_found"
    assert by_action["external.run.cancel"][0]["outcome"] == "success"
    assert by_action["external.run.get"][0]["detail"]["reason"] == "run_not_found"
    assert "do-not-log" not in json.dumps([dict(r["detail"]) for r in rows])


def test_a_forbidden_call_is_audited_as_denied(world: World):
    live = world.live()
    world.client.patch(f"{BASE}/clients/{live.client['id']}", json={"enabled": False}, headers=live.headers)
    refused(Forbidden, "client_disabled", live.start)
    (row,) = world.sql("SELECT outcome, detail FROM audit_log WHERE tenant_id = :t AND action = 'external.run.start'",
                       t=live.tenant)
    assert row["outcome"] == "denied" and row["detail"]["reason"] == "client_disabled"
