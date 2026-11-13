"""GET /logs (P0.4): tenant-scoped log search over Loki, with the LogQL built only on the server.

Loki is replaced by a fake LogSource (dependency override) that records the query it was given and
answers with entries labelled like real Loki answers (structured metadata merged into the labels).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from eap_api.routes.logs import (
    LokiEntry,
    LokiQueryFailed,
    LokiUnavailable,
    build_logql,
    get_log_source,
    logql_string,
    parse_query_range,
)

from .conftest import create_agent, get_auth_header, publish_version

SELECTOR = '{service_name=~"eap-api|eap-worker"}'


class FakeLoki:
    def __init__(self, entries: list[LokiEntry] | None = None, error: Exception | None = None) -> None:
        self.entries = entries or []
        self.error = error
        self.calls: list[dict] = []

    def query_range(self, query: str, *, start_ns: int, end_ns: int, limit: int) -> list[LokiEntry]:
        self.calls.append({"query": query, "start_ns": start_ns, "end_ns": end_ns, "limit": limit})
        if self.error is not None:
            raise self.error
        return list(self.entries)


def _entry(ts_ns: int, line: str, tenant: str | None, run: str | None = None, **labels: str) -> LokiEntry:
    base = {"service_name": "eap-worker", "severity_text": "INFO", "severity_number": "9"}
    if tenant is not None:
        base["eap_tenant_id"] = tenant
    if run is not None:
        base["eap_run_id"] = run
    return LokiEntry(ts_ns=ts_ns, line=line, labels={**base, **labels})


@pytest.fixture
def fake_loki(client):
    fake = FakeLoki()
    client.app.dependency_overrides[get_log_source] = lambda: fake
    yield fake
    client.app.dependency_overrides.pop(get_log_source, None)


def _make_run(client, tenant: str) -> str:
    agent_id = create_agent(client, tenant, "alice")
    publish_version(client, agent_id, tenant, "alice")
    run_store, _ = client.app.state.run_stores
    run_id = uuid.uuid4()
    run_store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", run_id)
    return str(run_id)


# --------------------------------------------------------------------------- LogQL building


def test_logql_for_tenant_only():
    assert build_logql(tenant_id="acme") == f'{SELECTOR} | eap_tenant_id="acme"'


def test_logql_with_every_parameter():
    assert build_logql(tenant_id="acme", run_id="r-1", q="boom", level="warn") == (
        f'{SELECTOR} | eap_tenant_id="acme" | eap_run_id="r-1" | severity_number >= 13 |= "boom"'
    )


@pytest.mark.parametrize(
    ("text", "literal"),
    [
        ('a"b', '"a\\"b"'),
        ("a\\b", '"a\\\\b"'),
        ("a`b", '"a`b"'),
        ('x" | eap_tenant_id="other', '"x\\" | eap_tenant_id=\\"other"'),
        ('"} or {service_name=~".+"', '"\\"} or {service_name=~\\".+\\""'),
        ("line1\nline2\r\t", '"line1\\u000aline2\\u000d\\u0009"'),
        ("\x00\x7f ", '"\\u0000\\u007f\\u2028"'),
        ("\\\"", '"\\\\\\""'),
        ("é漢😀", '"é漢😀"'),
    ],
)
def test_logql_string_escapes(text, literal):
    assert logql_string(text) == literal


def test_hostile_q_stays_inside_one_line_filter():
    q = '" | eap_tenant_id!="x" |~ `.*` }\n{service_name=~".+"} |= "'
    assert build_logql(tenant_id="acme", q=q) == (
        f'{SELECTOR} | eap_tenant_id="acme" |= '
        '"\\" | eap_tenant_id!=\\"x\\" |~ `.*` }\\u000a{service_name=~\\".+\\"} |= \\""'
    )


def test_logql_refuses_empty_tenant_unknown_level_and_surrogates():
    with pytest.raises(ValueError):
        build_logql(tenant_id="")
    with pytest.raises(ValueError):
        build_logql(tenant_id="acme", level="trace")
    with pytest.raises(ValueError):
        logql_string("\ud800")


def test_parse_query_range_merges_labels_and_rejects_garbage():
    body = (
        '{"status":"success","data":{"resultType":"streams","result":[{"stream":{"service_name":"eap-api",'
        '"eap_tenant_id":"acme"},"values":[["1700000000000000001","hello"]]}]}}'
    )
    (entry,) = parse_query_range(body)
    assert entry == LokiEntry(1700000000000000001, "hello", {"service_name": "eap-api", "eap_tenant_id": "acme"})
    for bad in ("not json", '{"status":"error"}', '{"status":"success","data":{"resultType":"matrix","result":[]}}'):
        with pytest.raises(LokiQueryFailed):
            parse_query_range(bad)


# --------------------------------------------------------------------------- the route


def test_search_uses_the_principal_tenant_and_shapes_items(client, tenant, fake_loki):
    run_id = str(uuid.uuid4())
    fake_loki.entries = [
        _entry(1_700_000_000_000_000_000, "older", tenant),
        _entry(
            1_700_000_001_500_000_000, "newer", tenant, run_id,
            severity_text="WARN", trace_id="ab" * 16, span_id="cd" * 8, service_name="eap-api",
        ),
    ]
    response = client.get("/api/v1/logs", params={"q": "x"}, headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert fake_loki.calls[0]["query"] == f'{SELECTOR} | eap_tenant_id="{tenant}" |= "x"'
    assert fake_loki.calls[0]["limit"] == 100
    assert body["limit"] == 100
    assert body["items"] == [
        {
            "ts": "2023-11-14T22:13:21.500000+00:00",
            "level": "warn",
            "message": "newer",
            "service": "eap-api",
            "trace_id": "ab" * 16,
            "span_id": "cd" * 8,
            "run_id": run_id,
        },
        {
            "ts": "2023-11-14T22:13:20+00:00",
            "level": "info",
            "message": "older",
            "service": "eap-worker",
            "trace_id": None,
            "span_id": None,
            "run_id": None,
        },
    ]


def test_other_tenant_and_untagged_records_never_leave(client, tenant, fake_loki):
    """Even if Loki returned them, records of another tenant or without a tenant are dropped."""
    fake_loki.entries = [
        _entry(3, "mine", tenant),
        _entry(2, "theirs", "other-tenant"),
        _entry(1, "untagged", None),
    ]
    response = client.get("/api/v1/logs", headers=get_auth_header(tenant, "alice"))
    assert [i["message"] for i in response.json()["items"]] == ["mine"]


def test_another_tenants_run_is_404_and_loki_is_not_asked(client, fake_loki):
    tenant_a, tenant_b = f"ta{uuid.uuid4().hex[:8]}", f"tb{uuid.uuid4().hex[:8]}"
    run_a = _make_run(client, tenant_a)
    response = client.get("/api/v1/logs", params={"run_id": run_a}, headers=get_auth_header(tenant_b, "bob"))
    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"
    assert fake_loki.calls == []


def test_own_run_filters_by_run_and_defaults_the_window_to_the_run(client, tenant, fake_loki):
    run_id = _make_run(client, tenant)
    fake_loki.entries = [_entry(2, "in run", tenant, run_id), _entry(1, "other run", tenant, str(uuid.uuid4()))]
    before = datetime.now(UTC)
    response = client.get("/api/v1/logs", params={"run_id": run_id}, headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 200, response.text
    assert [i["message"] for i in response.json()["items"]] == ["in run"]
    call = fake_loki.calls[0]
    assert call["query"] == f'{SELECTOR} | eap_tenant_id="{tenant}" | eap_run_id="{run_id}"'
    start = datetime.fromisoformat(response.json()["from"])
    assert before - timedelta(minutes=2) < start < before  # a minute before the run was created


def test_level_is_a_minimum_severity(client, tenant, fake_loki):
    client.get("/api/v1/logs", params={"level": "error"}, headers=get_auth_header(tenant, "alice"))
    assert fake_loki.calls[0]["query"].endswith("| severity_number >= 17")
    assert client.get("/api/v1/logs", params={"level": "trace"}, headers=get_auth_header(tenant, "alice")).status_code == 422


def test_range_and_limit_caps(client, tenant, fake_loki):
    h = get_auth_header(tenant, "alice")
    to = datetime(2026, 11, 10, tzinfo=UTC)
    ok = client.get("/api/v1/logs", params={"from": (to - timedelta(days=7)).isoformat(), "to": to.isoformat(), "limit": 500}, headers=h)
    assert ok.status_code == 200, ok.text
    assert fake_loki.calls[-1]["limit"] == 500
    assert fake_loki.calls[-1]["end_ns"] - fake_loki.calls[-1]["start_ns"] == 7 * 86_400 * 10**9
    too_long = client.get("/api/v1/logs", params={"from": (to - timedelta(days=7, seconds=1)).isoformat(), "to": to.isoformat()}, headers=h)
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == "the time range may span at most 7 days"
    backwards = client.get("/api/v1/logs", params={"from": to.isoformat(), "to": to.isoformat()}, headers=h)
    assert backwards.status_code == 422
    assert client.get("/api/v1/logs", params={"limit": 501}, headers=h).status_code == 422
    assert client.get("/api/v1/logs", params={"limit": 0}, headers=h).status_code == 422
    assert client.get("/api/v1/logs", params={"q": "x" * 501}, headers=h).status_code == 422
    assert len(fake_loki.calls) == 1


def test_naive_times_are_utc(client, tenant, fake_loki):
    response = client.get(
        "/api/v1/logs", params={"from": "2026-11-10T00:00:00", "to": "2026-11-10T01:00:00"}, headers=get_auth_header(tenant, "alice")
    )
    assert response.status_code == 200
    assert fake_loki.calls[0]["start_ns"] == int(datetime(2026, 11, 10, tzinfo=UTC).timestamp()) * 10**9


def test_needs_authentication(client, fake_loki):
    assert client.get("/api/v1/logs").status_code == 401


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (LokiUnavailable("ConnectionRefusedError"), 503, "Log search is unavailable"),
        (LokiQueryFailed(400), 502, "Log search failed"),
    ],
)
def test_loki_errors_have_fixed_messages(client, tenant, fake_loki, error, code, detail):
    fake_loki.error = error
    response = client.get("/api/v1/logs", headers=get_auth_header(tenant, "alice"))
    assert response.status_code == code
    assert response.json() == {"detail": detail}


def test_not_configured_is_503(client, tenant, monkeypatch):
    monkeypatch.delenv("EAP_LOKI_URL", raising=False)
    response = client.get("/api/v1/logs", headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Log search is not configured"}


def test_unreachable_loki_is_503(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_LOKI_URL", "http://127.0.0.1:9")  # discard port: connection refused
    response = client.get("/api/v1/logs", headers=get_auth_header(tenant, "alice"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Log search is unavailable"}


def test_api_log_records_carry_the_request_tenant(client, tenant, fake_loki):
    """The principal binds eap.tenant_id for the rest of the request (eap_observability)."""
    import eap_observability as obs

    seen: list[dict] = []

    class Recording(FakeLoki):
        def query_range(self, query, **kwargs):
            seen.append(obs.current_log_context())
            logging.getLogger("eap_api").info("querying loki")
            return []

    client.app.dependency_overrides[get_log_source] = lambda: Recording()
    assert client.get("/api/v1/logs", headers=get_auth_header(tenant, "alice")).status_code == 200
    assert seen == [{"eap.tenant_id": tenant}]
    assert obs.current_log_context() == {}


# --------------------------------------------------------------------------- shared tasks carry no tenant


def test_shared_sse_listener_logs_carry_no_tenant():
    """The delta LISTEN task is started inside the first SSE request but serves every tenant for the
    life of the process: its records (here a reconnect failure) must not carry that request's tenant."""
    import asyncio
    import time

    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    import eap_observability as obs
    from eap_api.auth import current_principal
    from eap_api.sse import PostgresDeltaSource

    obs.shutdown_telemetry()
    exporter = InMemoryLogRecordExporter()
    assert obs.setup_logging("eap-api", exporter=exporter)

    def listener_records():
        obs._state.logger_provider.force_flush()
        return [
            r.log_record for r in exporter.get_finished_logs()
            if "delta LISTEN connection ended" in str(r.log_record.body)
        ]

    async def sse_request_as_tenant_a():
        principal = await current_principal(authorization="Bearer dev:tenant-a:alice")
        assert obs.current_log_context() == {"eap.tenant_id": principal.tenant_id}
        source = PostgresDeltaSource("postgresql://nobody:x@127.0.0.1:9/none?connect_timeout=1")
        try:
            async with source(str(uuid.uuid4())):
                deadline = time.monotonic() + 10
                while not listener_records() and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
        finally:
            await source.close()

    try:
        asyncio.run(sse_request_as_tenant_a())
        records = listener_records()
        assert records, "the listener logged no reconnect failure"
        for record in records:
            assert "eap.tenant_id" not in dict(record.attributes or {})
            assert "eap.run_id" not in dict(record.attributes or {})
    finally:
        obs.shutdown_telemetry()


# --------------------------------------------------------------------------- concurrency and timeout


def test_busy_when_every_query_slot_is_taken(client, tenant, fake_loki):
    from eap_api.routes.logs import QuerySlots, get_query_slots

    slots = QuerySlots(1)
    client.app.dependency_overrides[get_query_slots] = lambda: slots
    try:
        h = get_auth_header(tenant, "alice")
        assert slots.try_acquire()  # another query in flight
        busy = client.get("/api/v1/logs", headers=h)
        assert busy.status_code == 503
        assert busy.json() == {"detail": "Log search is busy; retry shortly"}
        assert fake_loki.calls == []
        slots.release()
        assert client.get("/api/v1/logs", headers=h).status_code == 200
        # A failing query gives its slot back too.
        fake_loki.error = LokiUnavailable("x")
        assert client.get("/api/v1/logs", headers=h).status_code == 503
        fake_loki.error = LokiQueryFailed(400)
        assert client.get("/api/v1/logs", headers=h).status_code == 502
        assert slots.try_acquire()
        slots.release()
    finally:
        client.app.dependency_overrides.pop(get_query_slots, None)


def test_concurrent_queries_beyond_the_bound_get_busy(client, tenant):
    import threading

    from eap_api.routes.logs import QuerySlots, get_query_slots

    started, release = threading.Barrier(3), threading.Event()

    class SlowLoki(FakeLoki):
        def query_range(self, query, **kwargs):
            started.wait(5)
            release.wait(5)
            return []

    client.app.dependency_overrides[get_log_source] = lambda: SlowLoki()
    client.app.dependency_overrides[get_query_slots] = lambda: slots
    slots = QuerySlots(2)
    h = get_auth_header(tenant, "alice")
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(client.get("/api/v1/logs", headers=h).status_code)) for _ in range(2)]
    try:
        for t in threads:
            t.start()
        started.wait(5)  # both slow queries hold a slot
        third = client.get("/api/v1/logs", headers=h)
        assert third.status_code == 503
        assert third.json()["detail"] == "Log search is busy; retry shortly"
    finally:
        release.set()
        for t in threads:
            t.join(10)
        client.app.dependency_overrides.pop(get_log_source, None)
        client.app.dependency_overrides.pop(get_query_slots, None)
    assert results == [200, 200]


@pytest.mark.parametrize(("raw", "expected"), [(None, 10.0), ("2.5", 2.5), ("0", 1.0), ("999", 60.0), ("nan", 10.0), ("x", 10.0)])
def test_loki_timeout_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("EAP_LOKI_URL", "http://127.0.0.1:3100")
    if raw is None:
        monkeypatch.delenv("EAP_LOKI_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("EAP_LOKI_TIMEOUT_SECONDS", raw)
    assert get_log_source().timeout_seconds == expected


def test_timeout_is_passed_to_the_http_call(monkeypatch):
    import urllib.request

    from eap_api.routes.logs import LokiClient

    seen = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        raise TimeoutError()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(LokiUnavailable):
        LokiClient("http://127.0.0.1:3100", timeout_seconds=3.0).query_range("{}", start_ns=0, end_ns=1, limit=1)
    assert seen == {"timeout": 3.0}
