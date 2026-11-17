"""P0.9 acceptance for the processors API: the test console (bounds, RE2 refusals, nothing stored,
matched text never echoed), publish-time 422s, and /processor-results isolation and censoring.
Independent of test_processors_api.py (which stays the implementer's)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from eap_run_store import RunStore

from .conftest import create_agent, get_auth_header
from .timing import best_of_3, cap

SECRET = "ZQSECRET9931"
TEST_URL = "/api/v1/processors/test"


def _test(client, headers, processors, sample):
    return client.post(TEST_URL, json={"processors": processors, "text": sample}, headers=headers)


def _blocklist(*patterns, action="block"):
    return {"type": "regex_blocklist", "config": {"patterns": list(patterns), "action": action}}


# --- test console --------------------------------------------------------------------------------

@pytest.mark.parametrize("action", ["block", "redact"])
def test_console_quadratic_pattern_on_32_kib_answers_under_one_second(client, tenant, user, action):
    headers = get_auth_header(tenant, user)
    response, elapsed = best_of_3(lambda: _test(client, headers, [_blocklist("a+b|a", action=action)], "a" * (32 * 1024)))
    assert elapsed < cap(1.0), elapsed
    body = response.json()
    assert response.status_code == 200 and body["blocked"] is True and body["text"] is None
    result = body["results"][0]
    if action == "block":
        assert result["match_counts"] == {"pattern_0": 1} and "reason" not in result
    else:
        assert result["reason"] == "too_many_matches" and result["match_counts"] == {}


def test_console_sample_limit_is_32_kib_of_utf8_bytes(client, tenant, user):
    headers = get_auth_header(tenant, user)
    ok = _test(client, headers, [{"type": "pii_redact"}], "a" * (32 * 1024))
    assert ok.status_code == 200 and ok.json()["blocked"] is False
    too_big = _test(client, headers, [{"type": "pii_redact"}], "a" * (32 * 1024 + 1))
    assert too_big.status_code == 422 and "32 KiB" in too_big.json()["detail"]
    multibyte = _test(client, headers, [{"type": "pii_redact"}], "é" * (16 * 1024 + 1))
    assert multibyte.status_code == 422


@pytest.mark.parametrize(
    "pattern", [r"(a)\1", r"(?=a)b", r"(?!a)b", r"(?<=a)b", r"(?<!a)b", r"\b", r"$", r"(?m)^", r"a*", "(unclosed"]
)
def test_console_refuses_backtracking_and_zero_width_patterns_with_a_location(client, tenant, user, pattern):
    response = _test(client, get_auth_header(tenant, user), [{"type": "pii_redact"}, _blocklist("ok", pattern)], "x")
    assert response.status_code == 422
    assert response.json()["detail"].startswith("processors[1].config.patterns[1]: ")


def test_console_shape_errors_are_422(client, tenant, user):
    headers = get_auth_header(tenant, user)
    nine = [{"type": "max_length", "config": {"max_chars": 5}}] * 9
    for body in (
        {"processors": nine, "text": "x"},
        {"processors": [{"type": "nope"}], "text": "x"},
        {"processors": [{"type": "pii_redact"}], "text": 5},
        {"processors": [{"type": "pii_redact"}]},
        {"processors": [{"type": "pii_redact"}], "text": "x", "extra": 1},
    ):
        assert client.post(TEST_URL, json=body, headers=headers).status_code == 422, body
    eight = [{"type": "max_length", "config": {"max_chars": 5}}] * 8
    assert client.post(TEST_URL, json={"processors": eight, "text": "x"}, headers=headers).status_code == 200


def test_console_requires_auth(client):
    assert client.post(TEST_URL, json={"processors": [{"type": "pii_redact"}], "text": "x"}).status_code == 401
    assert client.get("/api/v1/processor-results").status_code == 401


def test_console_block_counts_redaction_and_matched_text_never_echoed(client, tenant, user):
    headers = get_auth_header(tenant, user)
    sample = f"keep {SECRET}-one and {SECRET}-two; mail {SECRET}@example.com"
    blocked = _test(client, headers, [_blocklist("nomatch", f"{SECRET}-\\w+", f"{SECRET}-two")], sample).json()
    assert blocked["blocked"] is True and blocked["text"] is None
    assert blocked["results"][0]["match_counts"] == {"pattern_1": 1, "pattern_2": 1}
    assert SECRET not in json.dumps(blocked)
    redacted = _test(client, headers, [_blocklist(f"{SECRET}\\S*", action="redact"), {"type": "pii_redact"}], sample).json()
    assert redacted["blocked"] is False and SECRET not in json.dumps(redacted)
    assert [r["action"] for r in redacted["results"]] == ["redact", "allow"]
    pii_block = _test(client, headers, [{"type": "pii_redact", "config": {"action": "block"}}], sample).json()
    assert SECRET not in json.dumps(pii_block["results"]) and pii_block["text"] is None


def test_console_stores_nothing(client, tenant, user):
    engine = client.app.state.database_engine

    def counts():
        with engine.connect() as conn:
            return [conn.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
                    for t in ("runs", "execution_events")]

    before = counts()
    for sample in ("bob@example.com", SECRET, "a" * 1000):
        assert _test(client, get_auth_header(tenant, user), [{"type": "pii_redact"}], sample).status_code == 200
    assert counts() == before == [0, 0]


# --- publish --------------------------------------------------------------------------------------

def _agent(client, tenant, user, input_processors=(), output_processors=()):
    agent_id = create_agent(client, tenant, user)
    headers = get_auth_header(tenant, user)
    spec = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["draft_spec"]
    spec["input_processors"], spec["output_processors"] = list(input_processors), list(output_processors)
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=headers).status_code == 200
    return agent_id


@pytest.mark.parametrize("phase", ["input_processors", "output_processors"])
@pytest.mark.parametrize("pattern", [r"(a)\1", r"(?=a)b", r"(?<!a)b", r"\b", r"$", r"(?m)^"])
def test_publish_422_for_a_bad_pattern_in_either_phase_and_no_version_is_created(client, tenant, user, phase, pattern):
    headers = get_auth_header(tenant, user)
    bad = [_blocklist("fine", pattern)]
    agent_id = _agent(client, tenant, user, **{phase: bad})  # a draft may hold it
    response = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith(f"{phase}[0].config.patterns[1]: ")
    assert client.get(f"/api/v1/agents/{agent_id}/versions", headers=headers).json() == []


def test_publish_succeeds_once_the_pattern_is_fixed_and_the_version_keeps_the_processors(client, tenant, user):
    headers = get_auth_header(tenant, user)
    agent_id = _agent(client, tenant, user, [_blocklist(r"\bsecret\b")], [{"type": "pii_redact"}])
    response = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert response.status_code == 201, response.text
    version = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=headers).json()
    assert version["spec"]["input_processors"][0]["config"]["patterns"] == [r"\bsecret\b"]
    assert version["spec"]["output_processors"][0]["type"] == "pii_redact"


# --- /processor-results ---------------------------------------------------------------------------

RESULTS = "/api/v1/processor-results"
BASE_TS = datetime(2026, 11, 1, tzinfo=UTC)


def _seed(engine, tenant, agent_id, data, ts=BASE_TS, run_id=None):
    run_id = run_id or uuid.uuid4()
    RunStore(engine).create_run(tenant, uuid.UUID(agent_id), 1, "input", run_id)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) "
                 "VALUES (:t, :r, 100, 'processor:input:0', 'processor_result', CAST(:d AS JSONB), :ts)"),
            {"t": tenant, "r": str(run_id), "d": json.dumps(data), "ts": ts},
        )
    return str(run_id)


def _outcome(**extra):
    return {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "redact",
            "match_counts": {"email": 1}, **extra}


def test_results_are_tenant_isolated_by_listing_run_id_agent_id_and_cursor(client, tenant, user):
    engine = client.app.state.database_engine
    mine = create_agent(client, tenant, user)
    other_tenant = f"t{uuid.uuid4().hex[:10]}"
    theirs = create_agent(client, other_tenant, user)
    my_runs = [_seed(engine, tenant, mine, _outcome(), BASE_TS + timedelta(minutes=i)) for i in range(3)]
    their_run = _seed(engine, other_tenant, theirs, _outcome(), BASE_TS + timedelta(hours=1))

    mine_h, theirs_h = get_auth_header(tenant, user), get_auth_header(other_tenant, user)
    listed = client.get(RESULTS, headers=mine_h).json()["items"]
    assert [i["run_id"] for i in listed] == list(reversed(my_runs))
    assert their_run not in json.dumps(listed) and theirs not in json.dumps(listed)
    # Filtering by the other tenant's run id or agent id returns nothing, not 403 or the row.
    for query in (f"run_id={their_run}", f"agent_id={theirs}"):
        assert client.get(f"{RESULTS}?{query}", headers=mine_h).json() == {"items": [], "next_before": None}
    for query in (f"run_id={my_runs[0]}", f"agent_id={mine}"):
        assert client.get(f"{RESULTS}?{query}", headers=theirs_h).json()["items"] == []
    # A cursor minted for one tenant cannot be used to read the other's rows.
    page = client.get(f"{RESULTS}?limit=1", headers=mine_h).json()
    assert page["next_before"] is not None
    cross = client.get(RESULTS, params={"before": page["next_before"], "limit": 50}, headers=theirs_h).json()
    assert [i["run_id"] for i in cross["items"]] == [their_run] or cross["items"] == []
    assert not {i["run_id"] for i in cross["items"]} & set(my_runs)


def test_results_serve_only_outcome_fields_never_stored_text(client, tenant, user):
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    nasty = _outcome(matched=SECRET, text=SECRET, input=SECRET, pattern=SECRET, value=SECRET,
                     match_counts={"email": 2, "x": SECRET, "y": 1.5, "z": True})
    run_id = _seed(engine, tenant, agent_id, nasty)
    (item,) = client.get(RESULTS, headers=get_auth_header(tenant, user)).json()["items"]
    assert SECRET not in json.dumps(item) and item["run_id"] == run_id
    assert set(item) == {"run_id", "seq", "ts", "agent_id", "agent_version", "parent_run_id", "phase", "index",
                         "processor_type", "action", "match_counts"}
    assert item["match_counts"] == {"email": 2}


def test_results_reason_and_categories_are_validated_and_unknown_values_become_null(client, tenant, user):
    engine = client.app.state.database_engine
    headers = get_auth_header(tenant, user)
    agent_id = create_agent(client, tenant, user)
    good = _seed(engine, tenant, agent_id, _outcome(reason="too_many_matches", categories=["hate", SECRET]),
                 BASE_TS + timedelta(minutes=2))
    long_reason = _seed(engine, tenant, agent_id, _outcome(reason=SECRET * 20), BASE_TS + timedelta(minutes=1))
    odd = _seed(engine, tenant, agent_id, {"phase": SECRET, "index": SECRET, "processor_type": SECRET,
                                          "action": SECRET, "match_counts": SECRET}, BASE_TS)
    by_run = {i["run_id"]: i for i in client.get(RESULTS, headers=headers).json()["items"]}
    assert by_run[good]["reason"] == "too_many_matches" and by_run[good]["categories"] == ["hate"]
    assert "reason" not in by_run[long_reason]
    assert (by_run[odd]["phase"], by_run[odd]["index"], by_run[odd]["processor_type"], by_run[odd]["action"]) == (
        None, None, None, None)
    assert by_run[odd]["match_counts"] == {} and SECRET not in json.dumps(by_run)


def test_results_ignore_other_event_types_and_filters_and_limits_are_enforced(client, tenant, user):
    engine = client.app.state.database_engine
    headers = get_auth_header(tenant, user)
    agent_id = create_agent(client, tenant, user)
    run_id = _seed(engine, tenant, agent_id, _outcome())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) VALUES "
                 "(:t, :r, 101, 'k', 'model_response', CAST(:d AS JSONB), :ts)"),
            {"t": tenant, "r": run_id, "d": json.dumps(_outcome(matched=SECRET)), "ts": BASE_TS},
        )
    assert len(client.get(RESULTS, headers=headers).json()["items"]) == 1
    assert client.get(f"{RESULTS}?limit=200", headers=headers).status_code == 200
    for query in ("limit=201", "limit=0", "limit=abc", "agent_id=not-a-uuid", "run_id=nope", "type=moderationx",
                  "before=" + "a" * 300):
        assert client.get(f"{RESULTS}?{query}", headers=headers).status_code == 422, query
    assert client.get(f"{RESULTS}?type=max_length", headers=headers).json()["items"] == []
    assert client.get(f"{RESULTS}?action=redact&phase=input&type=pii_redact", headers=headers).json()["items"]


def test_results_match_count_keys_are_limited_to_the_documented_names(client, tenant, user):
    """Keys are PII kinds, `pattern_<i>` or `chars_over_limit`. The route must not serve any other key,
    since the route is the last line before the browser (defence in depth against a bad writer)."""
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    _seed(engine, tenant, agent_id, _outcome(match_counts={"email": 1, "pattern_3": 2, "chars_over_limit": 4, SECRET: 1}))
    (item,) = client.get(RESULTS, headers=get_auth_header(tenant, user)).json()["items"]
    assert SECRET not in json.dumps(item)
    assert item["match_counts"] == {"email": 1, "pattern_3": 2, "chars_over_limit": 4}


# --- program-size cap, console budget, reason allowlist ------------------------------------------

COMPLEX = r"\w{500}"  # RE2 program size 1504 (cap 1000)


def test_console_refuses_a_pattern_over_the_program_size_cap(client, tenant, user):
    response = _test(client, get_auth_header(tenant, user), [_blocklist("ok", COMPLEX)], "x")
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("processors[0].config.patterns[1]: is too complex (RE2 program size ")
    assert ", at most 1000)" in detail
    assert _test(client, get_auth_header(tenant, user), [_blocklist(r"\w{120}")], "x").status_code == 200


@pytest.mark.parametrize("phase", ["input_processors", "output_processors"])
def test_publish_422_for_a_pattern_over_the_program_size_cap(client, tenant, user, phase):
    headers = get_auth_header(tenant, user)
    agent_id = _agent(client, tenant, user, **{phase: [_blocklist("fine", COMPLEX)]})
    response = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith(f"{phase}[0].config.patterns[1]: is too complex (RE2 program size ")
    assert client.get(f"/api/v1/agents/{agent_id}/versions", headers=headers).json() == []


def test_console_budget_is_two_seconds_and_a_blown_budget_fails_closed(client, tenant, user, monkeypatch):
    from eap_api.routes import processors as routes

    assert routes.TEST_CONSOLE_BUDGET_SECONDS == 2.0
    headers = get_auth_header(tenant, user)
    chain = [_blocklist(r"[a-z]{100}[0-9]{100}", r"(\w+\s){30}", action="redact"), {"type": "pii_redact"}]
    sample = "bob@example.com plain words " * 500
    assert _test(client, headers, chain, sample).json()["blocked"] is False  # accepted within 2 s
    monkeypatch.setattr(routes, "TEST_CONSOLE_BUDGET_SECONDS", -1.0)  # an already-spent budget
    for processors in (chain, [_blocklist("zzz")], [_blocklist("zzz", action="redact")], [{"type": "pii_redact"}]):
        body = _test(client, headers, processors, sample).json()
        assert body["blocked"] is True and body["text"] is None  # never allow
        assert body["results"][0]["reason"] == "budget_exceeded" and body["results"][0]["match_counts"] == {}
        assert body["results"][0]["action"] == "block"


def test_results_serve_reason_only_when_it_is_in_outcome_reasons(client, tenant, user):
    from eap_processors import OUTCOME_REASONS

    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    served = {r: _seed(engine, tenant, agent_id, _outcome(reason=r), BASE_TS + timedelta(minutes=i))
              for i, r in enumerate(sorted(OUTCOME_REASONS))}
    short_unlisted = {r: _seed(engine, tenant, agent_id, _outcome(reason=r), BASE_TS + timedelta(hours=1, minutes=i))
                      for i, r in enumerate(["custom_reason", "bob@example.com", "", "Too_Many_Matches", "input_too_large "])}
    items = {i["run_id"]: i for i in client.get(f"{RESULTS}?limit=200", headers=get_auth_header(tenant, user)).json()["items"]}
    for reason, run_id in served.items():
        assert items[run_id]["reason"] == reason
    for run_id in short_unlisted.values():
        assert "reason" not in items[run_id]
    assert "bob@example.com" not in json.dumps(items)


@pytest.mark.parametrize("reason", [["input_too_large"], {"k": "v"}, 7, True])
def test_results_survive_a_stored_non_string_reason_without_serving_it(client, tenant, user, reason):
    """A malformed stored row must not turn the page into a 500 (an unhashable value in a set lookup)."""
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    bad = _seed(engine, tenant, agent_id, _outcome(reason=reason), BASE_TS + timedelta(minutes=1))
    good = _seed(engine, tenant, agent_id, _outcome(reason="input_too_large"), BASE_TS)
    response = client.get(RESULTS, headers=get_auth_header(tenant, user))
    assert response.status_code == 200
    items = {i["run_id"]: i for i in response.json()["items"]}
    assert "reason" not in items[bad] and items[good]["reason"] == "input_too_large"
