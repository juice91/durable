"""GET /api/v1/spans: the materialised spans, newest first, tenant-scoped, run filters plus span_kind."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from eap_run_store.spans import SpanStore

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _span(run_id: str, n: int, kind: str, name: str, agent_id: str | None = None, parent: str | None = None):
    return {
        "span_id": f"{run_id}:{n}", "parent_id": parent, "kind": kind, "name": name,
        "start": T0 + timedelta(seconds=n), "end": T0 + timedelta(seconds=n + 1), "status": "ok",
        "agent_id": agent_id, "workflow_id": None, "model": "test:echo" if kind == "model_request" else None,
    }


def _seed(client, app, h, tenant: str) -> tuple[str, str]:
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201
    SpanStore(app.state.database_engine).replace_for_run(tenant, run_id, [
        _span(run_id, 0, "run", "Helper", agent_id),
        _span(run_id, 1, "model_request", "model 0", agent_id, f"{run_id}:0"),
        _span(run_id, 2, "tool_call", "tickets__create", agent_id, f"{run_id}:0"),
        _span(run_id, 3, "model_request", "model 1", agent_id, f"{run_id}:0"),
    ])
    return agent_id, run_id


def _cleanup(app, tenant: str) -> None:
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": tenant})


def test_spans_list_is_tenant_scoped_and_filters_by_span_kind(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    other, oh = tenant_of()
    try:
        agent_id, run_id = _seed(client, app, h, tenant)
        body = client.get("/api/v1/spans", headers=h).json()
        assert set(body) == {"items", "next_before"} and body["next_before"] is None
        assert [i["span_id"] for i in body["items"]] == [f"{run_id}:{n}" for n in (3, 2, 1, 0)]
        first = body["items"][0]
        assert first == {
            "run_id": run_id, "span_id": f"{run_id}:3", "parent_id": f"{run_id}:0", "kind": "model_request",
            "name": "model 1", "start": (T0 + timedelta(seconds=3)).isoformat(),
            "end": (T0 + timedelta(seconds=4)).isoformat(), "status": "ok", "agent_id": agent_id,
            "workflow_id": None, "model": "test:echo", "source": "platform", "trace_id": None, "service_name": None,
        }
        assert client.get("/api/v1/spans", headers=oh).json() == {"items": [], "next_before": None}
        tools = client.get("/api/v1/spans", params={"span_kind": "tool_call"}, headers=h).json()["items"]
        assert [i["name"] for i in tools] == ["tickets__create"]
        assert client.get("/api/v1/spans", params={"q": "MODEL"}, headers=h).json()["items"][0]["kind"] == "model_request"
        assert client.get("/api/v1/spans", params={"agent_id": str(uuid.uuid4())}, headers=h).json()["items"] == []
        assert len(client.get("/api/v1/spans", params={"agent_id": agent_id}, headers=h).json()["items"]) == 4
        window = {"from": (T0 + timedelta(seconds=2)).isoformat(), "to": (T0 + timedelta(seconds=3)).isoformat()}
        assert [i["kind"] for i in client.get("/api/v1/spans", params=window, headers=h).json()["items"]] == ["tool_call"]
        assert client.get("/api/v1/spans", params={"status": "failed"}, headers=h).json()["items"] == []
    finally:
        _cleanup(app, tenant)


def test_spans_pages_by_cursor_and_rejects_bad_input(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        _seed(client, app, h, tenant)
        seen, before = [], None
        for _ in range(5):
            params = {"limit": 3} | ({"before": before} if before else {})
            body = client.get("/api/v1/spans", params=params, headers=h).json()
            seen += [i["span_id"] for i in body["items"]]
            before = body["next_before"]
            if before is None:
                break
        assert len(seen) == 4 and len(set(seen)) == 4
        assert client.get("/api/v1/spans", params={"before": "garbage"}, headers=h).status_code == 422
        assert client.get("/api/v1/spans", params={"limit": 201}, headers=h).status_code == 422
        assert client.get("/api/v1/spans", params={"from": T0.isoformat(), "to": T0.isoformat()}, headers=h).status_code == 422
    finally:
        _cleanup(app, tenant)


def test_spans_and_runs_agree_on_tags_metadata_and_q(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    try:
        agent_id = make_agent(client, h)
        runs = []
        for i, (text_in, ctx) in enumerate([
            ("printer on fire", {"team": "ops"}), ("printer jammed", {"team": "dev"}), ("billing", {"team": "ops"}),
        ]):
            run_id = str(uuid.uuid4())
            assert start(client, h, agent_id, run_id).status_code == 201
            with app.state.database_engine.begin() as conn:
                conn.execute(text("UPDATE runs SET input = :i, request_context = CAST(:c AS jsonb) WHERE id = :r"),
                             {"i": text_in, "c": '{"team": "%s"}' % ctx["team"], "r": run_id})
            SpanStore(app.state.database_engine).replace_for_run(tenant, run_id, [_span(run_id, i, "run", "Helper", agent_id)])
            runs.append(run_id)
        for params in (
            {"tags": f"agent:{agent_id}", "metadata": "team=ops", "q": "PRINTER"},
            {"tags": "kind:agent,ctx:team", "metadata": "team=ops"},
            {"tags": "draft"},
            {"status": "queued", "q": "printer"},
        ):
            from_runs = {i["run_id"] for i in client.get("/api/v1/runs", params={k: v for k, v in params.items()}, headers=h).json()["items"]}
            # spans' own `q` is the span name, so the run-text filter is compared without it
            span_params = {k: v for k, v in params.items() if k != "q"}
            from_spans = {i["run_id"] for i in client.get("/api/v1/spans", params=span_params, headers=h).json()["items"]}
            expected = {i["run_id"] for i in client.get("/api/v1/runs", params=span_params, headers=h).json()["items"]}
            assert from_spans == expected, params
            if "q" not in params:
                assert from_spans == from_runs, params
        assert {i["run_id"] for i in client.get("/api/v1/runs", params={"tags": f"agent:{agent_id}", "metadata": "team=ops", "q": "PRINTER"}, headers=h).json()["items"]} == {runs[0]}
        hist = client.get("/api/v1/runs/histogram", params={"metadata": "team=ops", "q": "printer"}, headers=h).json()
        assert sum(b["runs"] for b in hist["buckets"]) == 1
    finally:
        _cleanup(app, tenant)
