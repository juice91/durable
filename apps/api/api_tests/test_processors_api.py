"""Processors API (P0.9): catalog, test console, publish validation, recorded outcomes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from eap_run_store import RunStore

from .conftest import create_agent, get_auth_header

BLOCKLIST = {"type": "regex_blocklist", "config": {"patterns": [r"(?i)\bsecret\b"]}}


def _agent_with_processors(client, tenant: str, user: str, input_processors: list, output_processors=()) -> str:
    agent_id = create_agent(client, tenant, user)
    spec = client.get(f"/api/v1/agents/{agent_id}", headers=get_auth_header(tenant, user)).json()["draft_spec"]
    spec["input_processors"] = list(input_processors)
    if output_processors:
        spec["output_processors"] = list(output_processors)
    response = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=get_auth_header(tenant, user))
    assert response.status_code == 200, response.text
    return agent_id


def test_catalog_lists_every_type_with_config_schema(client, tenant, user) -> None:
    response = client.get("/api/v1/processors", headers=get_auth_header(tenant, user))
    assert response.status_code == 200
    body = response.json()
    by_id = {item["id"]: item for item in body["items"]}
    assert set(by_id) == {"pii_redact", "regex_blocklist", "max_length", "moderation"}
    assert by_id["moderation"]["deterministic"] is False
    assert by_id["regex_blocklist"]["config_schema"]["properties"]["patterns"]["maxItems"] == 20
    assert "$defs" not in json.dumps(body)
    assert body["limits"]["max_test_sample_bytes"] == 32 * 1024


def test_catalog_returns_four_phases_and_phase_filter(client, tenant, user) -> None:
    body = client.get("/api/v1/processors", headers=get_auth_header(tenant, user)).json()
    by_id = {item["id"]: item for item in body["items"]}
    assert by_id["pii_redact"]["phases"] == ["input", "step", "stream", "output"]
    assert by_id["moderation"]["phases"] == ["input", "output"]
    for phase in ("step", "stream"):
        response = client.get(
            "/api/v1/processor-results", params={"phase": phase}, headers=get_auth_header(tenant, user)
        )
        assert response.status_code == 200, response.text
    bad = client.get("/api/v1/processor-results", params={"phase": "nope"}, headers=get_auth_header(tenant, user))
    assert bad.status_code == 422


def test_catalog_requires_auth(client) -> None:
    assert client.get("/api/v1/processors").status_code == 401


def test_test_console_redacts_and_blocks(client, tenant, user) -> None:
    headers = get_auth_header(tenant, user)
    response = client.post(
        "/api/v1/processors/test",
        json={"processors": [{"type": "pii_redact"}], "text": "mail bob@example.com"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "text": "mail [REDACTED:email]",
        "blocked": False,
        "results": [{"index": 0, "processor_type": "pii_redact", "action": "redact", "match_counts": {"email": 1}}],
    }
    response = client.post(
        "/api/v1/processors/test",
        json={"processors": [{"type": "pii_redact"}, BLOCKLIST, {"type": "max_length", "config": {"max_chars": 2}}],
              "text": "my SECRET plan"},
        headers=headers,
    )
    body = response.json()
    assert body["blocked"] is True and body["text"] is None
    assert [r["action"] for r in body["results"]] == ["allow", "block"]
    assert "SECRET" not in json.dumps(body["results"])


def test_test_console_refuses_moderation_bad_re2_and_big_samples(client, tenant, user) -> None:
    headers = get_auth_header(tenant, user)
    moderation = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}
    response = client.post("/api/v1/processors/test", json={"processors": [moderation], "text": "x"}, headers=headers)
    assert response.status_code == 422 and "playground" in response.json()["detail"]

    lookahead = {"type": "regex_blocklist", "config": {"patterns": ["(?=a)b"]}}
    response = client.post("/api/v1/processors/test", json={"processors": [lookahead], "text": "x"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("processors[0].config.patterns[0]: not valid RE2 syntax")

    response = client.post(
        "/api/v1/processors/test",
        json={"processors": [{"type": "pii_redact"}], "text": "é" * (16 * 1024 + 1)},
        headers=headers,
    )
    assert response.status_code == 422 and "32 KiB" in response.json()["detail"]

    response = client.post("/api/v1/processors/test", json={"processors": [], "text": "x"}, headers=headers)
    assert response.status_code == 422


def test_publish_rejects_invalid_re2_and_accepts_valid(client, tenant, user) -> None:
    headers = get_auth_header(tenant, user)
    bad = _agent_with_processors(
        client, tenant, user, [BLOCKLIST], [{"type": "regex_blocklist", "config": {"patterns": [r"(a)\1"]}}]
    )
    response = client.post(f"/api/v1/agents/{bad}/versions", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("output_processors[0].config.patterns[0]: not valid RE2 syntax")

    good = _agent_with_processors(client, tenant, user, [BLOCKLIST, {"type": "pii_redact"}])
    assert client.post(f"/api/v1/agents/{good}/versions", headers=headers).status_code == 201


def test_publish_rejects_moderation_model_outside_allowlist(client, tenant, user) -> None:
    moderation = {"type": "moderation", "config": {"model": {"provider": "openai", "name": "not-allowed"}}}
    agent_id = _agent_with_processors(client, tenant, user, [], [moderation])
    response = client.post(f"/api/v1/agents/{agent_id}/versions", headers=get_auth_header(tenant, user))
    assert response.status_code == 422 and "not-allowed" in response.json()["detail"]


def _seed_result(engine, tenant: str, agent_id: str, ts: datetime, data: dict, run_id: uuid.UUID | None = None) -> str:
    run_id = run_id or uuid.uuid4()
    RunStore(engine).create_run(tenant, uuid.UUID(agent_id), 1, "input", run_id)
    with engine.begin() as conn:
        seq = conn.execute(
            text("SELECT COALESCE(MAX(seq), -1) + 1 FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) "
                "VALUES (:t, :r, :s, :k, 'processor_result', CAST(:d AS JSONB), :ts)"
            ),
            {"t": tenant, "r": str(run_id), "s": seq, "k": f"processor:{data['phase']}:{data['index']}",
             "d": json.dumps(data), "ts": ts},
        )
    return str(run_id)


def test_processor_results_filters_pages_and_isolates_tenants(client, tenant, user) -> None:
    engine = client.app.state.database_engine
    headers = get_auth_header(tenant, user)
    agent_id = create_agent(client, tenant, user)
    other_agent = create_agent(client, tenant, user)
    base = datetime(2026, 11, 1, tzinfo=UTC)
    redact = {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "redact",
              "match_counts": {"email": 2}, "matched": "bob@example.com"}
    block = {"phase": "output", "index": 1, "processor_type": "regex_blocklist", "action": "block",
             "match_counts": {"pattern_0": 1}}
    runs = [_seed_result(engine, tenant, agent_id, base + timedelta(minutes=i), redact if i % 2 else block)
            for i in range(5)]
    _seed_result(engine, tenant, other_agent, base, redact)

    other_tenant = f"t{uuid.uuid4().hex[:10]}"
    foreign_agent = create_agent(client, other_tenant, user)
    _seed_result(engine, other_tenant, foreign_agent, base, redact)

    body = client.get("/api/v1/processor-results", headers=headers).json()
    assert len(body["items"]) == 6 and body["next_before"] is None
    assert body["items"][0]["run_id"] == runs[4]
    # Only outcome fields are served, never anything else stored in the event.
    assert "bob@example.com" not in json.dumps(body)
    assert set(body["items"][1]) == {"run_id", "seq", "ts", "agent_id", "agent_version", "parent_run_id", "phase",
                                     "index", "processor_type", "action", "match_counts"}

    by_agent = client.get(f"/api/v1/processor-results?agent_id={agent_id}&type=pii_redact&action=redact",
                          headers=headers).json()
    assert [i["run_id"] for i in by_agent["items"]] == [runs[3], runs[1]]
    assert client.get("/api/v1/processor-results?phase=output", headers=headers).json()["items"][0]["action"] == "block"

    window = client.get(
        "/api/v1/processor-results",
        params={"agent_id": agent_id, "from": (base + timedelta(minutes=1)).isoformat(),
                "to": (base + timedelta(minutes=3)).isoformat()},
        headers=headers,
    ).json()
    assert [i["run_id"] for i in window["items"]] == [runs[2], runs[1]]

    seen: list[str] = []
    before = None
    while True:
        params = {"agent_id": agent_id, "limit": 2, **({"before": before} if before else {})}
        page = client.get("/api/v1/processor-results", params=params, headers=headers).json()
        seen += [i["run_id"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == list(reversed(runs))

    # Another tenant sees only its own result, even with this tenant's agent id.
    foreign = client.get(f"/api/v1/processor-results?agent_id={agent_id}",
                         headers=get_auth_header(other_tenant, user)).json()
    assert foreign["items"] == []
    assert len(client.get("/api/v1/processor-results", headers=get_auth_header(other_tenant, user)).json()["items"]) == 1


def test_processor_results_validation(client, tenant, user) -> None:
    headers = get_auth_header(tenant, user)
    for query in ("type=nope", "action=nope", "phase=middle", "before=not-a-cursor", "limit=0",
                  "from=2026-11-02T00:00:00Z&to=2026-11-01T00:00:00Z"):
        assert client.get(f"/api/v1/processor-results?{query}", headers=headers).status_code == 422, query


def test_processor_results_statement_timeout_is_503(client, tenant, user, monkeypatch) -> None:
    """A real Postgres statement timeout (QueryCanceled) answers 503, not 500."""
    from sqlalchemy.orm import Session

    from eap_api.database import get_session
    from eap_api.routes import processors as processors_routes

    monkeypatch.setattr(processors_routes, "RESULTS_STATEMENT_TIMEOUT_MS", 50)

    class SlowSession(Session):
        def execute(self, statement, *args, **kwargs):
            if not str(statement).startswith("SET LOCAL"):
                super().execute(text("SELECT pg_sleep(1)"))
            return super().execute(statement, *args, **kwargs)

    def slow_session():
        session = SlowSession(client.app.state.database_engine)
        try:
            yield session
        finally:
            session.close()

    client.app.dependency_overrides[get_session] = slow_session
    try:
        response = client.get("/api/v1/processor-results", headers=get_auth_header(tenant, user))
    finally:
        client.app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 503
    assert response.json()["detail"] == processors_routes.RESULTS_TIMEOUT_DETAIL


def test_processor_results_serve_only_documented_count_keys(client, tenant, user) -> None:
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    data = {"phase": "input", "index": 0, "processor_type": "regex_blocklist", "action": "redact",
            "match_counts": {"pattern_0": 2, "pattern_19": 1, "pattern_20": 1, "email": 1, "chars_over_limit": 3,
                             "ZQSECRET9931": 1, "pattern_x": 1, "phone": "1", "iban": -1}}
    _seed_result(engine, tenant, agent_id, datetime(2026, 11, 1, tzinfo=UTC), data)
    (item,) = client.get("/api/v1/processor-results", headers=get_auth_header(tenant, user)).json()["items"]
    assert item["match_counts"] == {"pattern_0": 2, "pattern_19": 1, "email": 1, "chars_over_limit": 3}


def test_processor_results_serve_only_known_reasons(client, tenant, user) -> None:
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    base = datetime(2026, 11, 1, tzinfo=UTC)
    for minute, reason in enumerate(("budget_exceeded", "ZQSECRET9931", "too_many_matches", "input_too_large")):
        _seed_result(engine, tenant, agent_id, base + timedelta(minutes=minute),
                     {"phase": "input", "index": 0, "processor_type": "regex_blocklist", "action": "block",
                      "match_counts": {}, "reason": reason})
    items = client.get("/api/v1/processor-results", headers=get_auth_header(tenant, user)).json()["items"]
    assert [i.get("reason") for i in items] == ["input_too_large", "too_many_matches", None, "budget_exceeded"]


def test_processor_results_other_operational_errors_are_a_generic_503(client, tenant, user) -> None:
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from eap_api.database import get_session
    from eap_api.routes import processors as processors_routes

    class BrokenSession(Session):
        def execute(self, statement, *args, **kwargs):
            if not str(statement).startswith("SET LOCAL"):
                raise OperationalError("SELECT ...", {}, Exception("server closed the connection unexpectedly"))
            return super().execute(statement, *args, **kwargs)

    def broken_session():
        session = BrokenSession(client.app.state.database_engine)
        try:
            yield session
        finally:
            session.close()

    client.app.dependency_overrides[get_session] = broken_session
    try:
        response = client.get("/api/v1/processor-results", headers=get_auth_header(tenant, user))
    finally:
        client.app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 503
    assert response.json()["detail"] == processors_routes.RESULTS_UNAVAILABLE_DETAIL


def test_test_console_and_publish_refuse_patterns_over_the_program_size_cap(client, tenant, user) -> None:
    headers = get_auth_header(tenant, user)
    too_complex = {"type": "regex_blocklist", "config": {"patterns": ["[a-q][^u-z]{900}x"]}}
    response = client.post("/api/v1/processors/test", json={"processors": [too_complex], "text": "x"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("processors[0].config.patterns[0]: is too complex (RE2 program size")
    agent_id = _agent_with_processors(client, tenant, user, [too_complex])
    response = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("input_processors[0].config.patterns[0]: is too complex")


def test_test_console_fails_closed_on_its_budget(client, tenant, user) -> None:
    import random
    import time

    rng = random.Random(7)
    sample = "".join(rng.choice("ab") for _ in range(32 * 1024))
    worst = {"type": "regex_blocklist", "config": {"patterns": [f"[ab]*a[ab]{{988}}c|q{i}" for i in range(20)]}}
    started = time.perf_counter()
    response = client.post("/api/v1/processors/test", json={"processors": [worst] * 8, "text": sample},
                           headers=get_auth_header(tenant, user))
    elapsed = time.perf_counter() - started
    assert response.status_code == 200
    body = response.json()
    assert body["blocked"] is True and body["text"] is None
    assert body["results"][-1]["reason"] == "budget_exceeded"
    assert elapsed < 2.0 + 2.0  # the console budget plus one search of slack


def test_processor_results_survive_unhashable_stored_values(client, tenant, user) -> None:
    """Stored event data is not validated: lists and dicts in any field are dropped, never a 500."""
    engine = client.app.state.database_engine
    agent_id = create_agent(client, tenant, user)
    junk = {"phase": ["input"], "index": {"i": 0}, "processor_type": {"t": 1}, "action": ["block"],
            "match_counts": {"email": [1], "phone": {"n": 1}, "pattern_0": 2}, "reason": ["budget_exceeded"],
            "categories": [["hate"], {"c": "violence"}, "violence", "ZQSECRET9931"]}
    _seed_result(engine, tenant, agent_id, datetime(2026, 11, 1, tzinfo=UTC), junk)
    _seed_result(engine, tenant, agent_id, datetime(2026, 11, 2, tzinfo=UTC), {**junk, "reason": {"r": 1}})
    response = client.get("/api/v1/processor-results", headers=get_auth_header(tenant, user))
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["phase"] is None and item["index"] is None and item["processor_type"] is None
        assert item["action"] is None and "reason" not in item
        assert item["match_counts"] == {"pattern_0": 2} and item["categories"] == ["violence"]
