"""P0.4 logs query API, acceptance tests: tenant isolation (LogQL and live Loki), errors, validation.

The live tests talk to the real Loki (EAP_LOKI_URL, default http://127.0.0.1:3100) and the OTLP
collector in front of it. Every test generates a unique run marker, tenants and run ids, so records
left in Loki by earlier runs cannot change a result.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import requests

import eap_observability as obs
from eap_api.routes import logs as logs_module
from eap_api.routes.logs import (
    LOG_SERVICES,
    LokiClient,
    LokiEntry,
    LokiQueryFailed,
    LokiUnavailable,
    QuerySlots,
    build_logql,
    get_log_source,
    get_query_slots,
    logql_string,
)

from .conftest import create_agent, get_auth_header, publish_version

SELECTOR = '{service_name=~"eap-api|eap-worker"}'
LOKI_URL = os.environ.get("EAP_LOKI_URL", "http://127.0.0.1:3100").rstrip("/")
OTLP_LOGS_URL = os.environ.get("EAP_TEST_OTLP_LOGS_URL", "http://127.0.0.1:4318/v1/logs")
LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')

HOSTILE = [
    '"',
    "\\",
    "`",
    '\\"',
    '"} or {service_name=~".+"} |= "',
    '" | eap_tenant_id!="x" |~ `.*` }',
    "a\nb\r\nc\td\x00e\x1b[0m",
    "x y z​﻿",
    '}{|=~!~|>=<=" or vector(1) // ',
    "`} | json | eap_tenant_id=~\".+\" |= `",
    "$(rm -rf /) ${x} %22 %7D",
    "é漢😀 \U0001f600",
    "\\\\\"\\\"",
    "| line_format \"{{.eap_tenant_id}}\"",
]


def unquote(literal: str) -> str:
    """Go strconv.Unquote for the escapes logql_string produces (independent of the code under test)."""
    assert literal[0] == literal[-1] == '"'
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            assert ch != '"' and ch.isprintable(), f"raw {ch!r} inside literal"
            out.append(ch)
            i += 1
            continue
        kind = body[i + 1]
        if kind in '\\"':
            out.append(kind)
            i += 2
        elif kind in "uU":
            width = 4 if kind == "u" else 8
            out.append(chr(int(body[i + 2 : i + 2 + width], 16)))
            i += 2 + width
        else:
            raise AssertionError(f"unexpected escape \\{kind}")
    return "".join(out)


def skeleton(query: str) -> str:
    return LITERAL.sub("S", query)


# --------------------------------------------------------------------------- LogQL: hostile values


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_values_never_escape_their_string(hostile):
    run_id = str(uuid.uuid4())
    query = build_logql(tenant_id="acme", run_id=run_id, q=hostile, level="warn")
    # Removing every string literal leaves exactly one tenant-filtered stream, nothing else.
    assert skeleton(query) == "{service_name=~S} | eap_tenant_id=S | eap_run_id=S | severity_number >= 13 |= S"
    literals = LITERAL.findall(query)
    assert [unquote(x) for x in literals] == ["eap-api|eap-worker", "acme", run_id, hostile]
    assert not re.search(r"[\x00-\x1f\x7f]", query)  # no raw control characters or newlines
    assert query.count("`") == hostile.count("`")  # backticks only as literal text inside quotes


@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_tenant_and_run_stay_in_their_strings(hostile):
    query = build_logql(tenant_id=hostile, run_id=hostile)
    assert skeleton(query) == "{service_name=~S} | eap_tenant_id=S | eap_run_id=S"
    assert [unquote(x) for x in LITERAL.findall(query)][1:] == [hostile, hostile]


@pytest.mark.parametrize("hostile", HOSTILE)
def test_every_query_starts_with_the_service_selector_and_tenant_filter(hostile):
    for kwargs in ({}, {"q": hostile}, {"run_id": hostile}, {"level": "error", "q": hostile}):
        query = build_logql(tenant_id="acme", **kwargs)
        assert query.startswith(f'{SELECTOR} | eap_tenant_id="acme"')
        assert query.count("{") == 1 or "{" in hostile  # one selector unless the braces are inside a literal
        assert skeleton(query).count("{") == 1 and skeleton(query).count("}") == 1


def test_selector_covers_only_our_services_and_no_regex_in_user_values():
    assert LOG_SERVICES == ("eap-api", "eap-worker")
    query = build_logql(tenant_id="acme", q=".*")
    assert "=~" in query and skeleton(query).count("=~") == 1  # only the fixed service selector
    assert "!=" not in skeleton(query) and "|~" not in skeleton(query) and "!~" not in skeleton(query)


@pytest.mark.parametrize("level", ["trace", "ERROR", "error | x", "", "warn\n", '"', "} or {x=~\".+\""])
def test_unknown_levels_are_refused(level):
    with pytest.raises(ValueError):
        build_logql(tenant_id="acme", level=level)


@pytest.mark.parametrize("tenant", ["", None])
def test_empty_tenant_is_refused(tenant):
    with pytest.raises(ValueError):
        build_logql(tenant_id=tenant)  # type: ignore[arg-type]


def test_lone_surrogate_is_refused():
    with pytest.raises(ValueError):
        build_logql(tenant_id="acme", q="a\udc00b")


def test_logql_string_round_trips_every_bmp_scalar_class():
    sample = "".join(chr(c) for c in list(range(0, 0x250)) + [0x2028, 0x200B, 0xFEFF, 0xFFFD, 0x10000, 0x1F600])
    assert unquote(logql_string(sample)) == sample


# --------------------------------------------------------------------------- route with a fake Loki


class Recorder:
    def __init__(self, entries=None, error=None, hook=None):
        self.entries, self.error, self.hook, self.calls = entries or [], error, hook, []

    def query_range(self, query, *, start_ns, end_ns, limit):
        self.calls.append({"query": query, "start_ns": start_ns, "end_ns": end_ns, "limit": limit})
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return list(self.entries)


@pytest.fixture
def source(client):
    fake = Recorder()
    client.app.dependency_overrides[get_log_source] = lambda: fake
    yield fake
    client.app.dependency_overrides.clear()


def _get(client, tenant, **params):
    return client.get("/api/v1/logs", params=params, headers=get_auth_header(tenant, "alice"))


def _entry(ts, line, tenant, run=None, **extra):
    labels = {"service_name": "eap-api", "severity_text": "INFO"}
    if tenant is not None:
        labels["eap_tenant_id"] = tenant
    if run is not None:
        labels["eap_run_id"] = run
    return LokiEntry(ts, line, {**labels, **extra})


@pytest.mark.parametrize("hostile", HOSTILE)
def test_http_hostile_q_reaches_loki_as_one_tenant_filtered_query(client, tenant, source, hostile):
    response = _get(client, tenant, q=hostile)
    assert response.status_code == 200, response.text
    (call,) = source.calls
    assert call["query"] == build_logql(tenant_id=tenant, q=hostile)
    assert skeleton(call["query"]) == "{service_name=~S} | eap_tenant_id=S |= S"
    assert [unquote(x) for x in LITERAL.findall(call["query"])][1:] == [tenant, hostile]


@pytest.mark.parametrize("hostile", [h for h in HOSTILE if not re.fullmatch(r"[0-9a-f-]{36}", h)])
def test_http_hostile_run_id_and_level_are_422_and_loki_is_not_asked(client, tenant, source, hostile):
    assert _get(client, tenant, run_id=hostile).status_code == 422
    assert _get(client, tenant, level=hostile).status_code == 422
    assert source.calls == []


def test_http_tenant_comes_only_from_the_principal(client, tenant, source):
    other = f"x{uuid.uuid4().hex[:8]}"
    response = client.get(
        "/api/v1/logs",
        params={"tenant_id": other, "eap_tenant_id": other, "tenant": other},
        headers={**get_auth_header(tenant, "alice"), "X-Tenant-Id": other},
    )
    assert response.status_code == 200
    assert source.calls[0]["query"] == f'{SELECTOR} | eap_tenant_id="{tenant}"'


def test_http_other_tenants_untagged_and_impostor_records_are_dropped(client, tenant, source):
    run = str(uuid.uuid4())
    source.entries = [
        _entry(9, "mine", tenant, run),
        _entry(8, "other", f"x{uuid.uuid4().hex[:6]}", run),
        _entry(7, "untagged", None),
        _entry(6, "empty tenant", ""),
        _entry(5, "impostor", None, eap_tenant_id_x=tenant, **{"eap.tenant_id": tenant}),
        _entry(4, "mine other run", tenant, str(uuid.uuid4())),
    ]
    assert [i["message"] for i in _get(client, tenant).json()["items"]] == ["mine", "mine other run"]
    assert [i["message"] for i in _get(client, tenant, run_id=_run_of(client, tenant, run)).json()["items"]] == ["mine"]


def _run_of(client, tenant, run):
    agent_id = create_agent(client, tenant, "alice")
    publish_version(client, agent_id, tenant, "alice")
    client.app.state.run_stores[0].create_run(tenant, uuid.UUID(agent_id), 1, "hi", uuid.UUID(run))
    return run


def test_http_other_tenants_run_is_404_and_loki_is_not_asked(client, source):
    a, b = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    run = _run_of(client, a, str(uuid.uuid4()))
    response = _get(client, b, run_id=run)
    assert response.status_code == 404 and response.json() == {"detail": "Run not found"}
    assert _get(client, b, run_id=str(uuid.uuid4())).status_code == 404  # unknown run: same answer
    assert source.calls == []
    assert _get(client, a, run_id=run).status_code == 200 and len(source.calls) == 1


# --------------------------------------------------------------------------- errors


class _Stub:
    """A local HTTP server standing in for Loki: answers every GET with (status, body) after `delay`."""

    def __init__(self, status=200, body=b"", delay=0.0):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self
        self.status, self.body, self.delay, self.paths = status, body, delay, []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    made = []

    def make(**kw):
        made.append(_Stub(**kw))
        return made[-1]

    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize(
    ("status", "body", "code", "detail"),
    [
        (200, b"<html>not json", 502, "Log search failed"),
        (200, b'{"status":"success"', 502, "Log search failed"),
        (200, b"[]", 502, "Log search failed"),
        (200, b'{"status":"success","data":{"resultType":"matrix","result":[]}}', 502, "Log search failed"),
        (200, b'{"status":"success","data":{"resultType":"streams","result":[{"stream":1,"values":[["x"]]}]}}', 502, "Log search failed"),
        (400, b"parse error at line 1: secret-detail", 502, "Log search failed"),
        (404, b"nope", 502, "Log search failed"),
        (500, b"boom secret-detail", 503, "Log search is unavailable"),
        (503, b"overloaded", 503, "Log search is unavailable"),
    ],
)
def test_real_client_against_a_misbehaving_loki(client, tenant, stub, monkeypatch, status, body, code, detail):
    monkeypatch.setenv("EAP_LOKI_URL", stub(status=status, body=body).url)
    response = _get(client, tenant)
    assert response.status_code == code
    assert response.json() == {"detail": detail}  # Loki's own text is never passed through
    assert "secret-detail" not in response.text


def test_empty_streams_answer_is_an_empty_list(client, tenant, stub, monkeypatch):
    body = b'{"status":"success","data":{"resultType":"streams","result":[]}}'
    server = stub(body=body)
    monkeypatch.setenv("EAP_LOKI_URL", server.url)
    response = _get(client, tenant, q="hi", limit=3)
    assert response.status_code == 200 and response.json()["items"] == []
    assert "query=" in server.paths[0] and "limit=3" in server.paths[0] and "direction=backward" in server.paths[0]


@pytest.mark.parametrize("value", [None, "", "   ", "not-a-url", "ftp://127.0.0.1:3100", "http://"])
def test_unconfigured_or_unusable_url_is_503_not_configured(client, tenant, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("EAP_LOKI_URL", raising=False)
    else:
        monkeypatch.setenv("EAP_LOKI_URL", value)
    response = _get(client, tenant)
    assert response.status_code == 503 and response.json() == {"detail": "Log search is not configured"}


def test_connection_refused_is_503_unavailable(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_LOKI_URL", "http://127.0.0.1:9")
    response = _get(client, tenant)
    assert response.status_code == 503 and response.json() == {"detail": "Log search is unavailable"}


@pytest.mark.parametrize("error", [LokiUnavailable("x"), LokiQueryFailed("x")])
def test_errors_still_apply_after_validation_only(client, tenant, source, error):
    source.error = error
    assert _get(client, tenant).status_code == (503 if isinstance(error, LokiUnavailable) else 502)
    assert _get(client, tenant, limit=0).status_code == 422 and len(source.calls) == 1


def test_a_slow_loki_hits_the_client_timeout_and_is_503(client, tenant, stub, monkeypatch):
    monkeypatch.setenv("EAP_LOKI_URL", stub(delay=3.0, body=b"{}").url)
    monkeypatch.setenv("EAP_LOKI_TIMEOUT_SECONDS", "1")
    started = time.monotonic()
    response = _get(client, tenant)
    assert response.status_code == 503 and response.json() == {"detail": "Log search is unavailable"}
    assert time.monotonic() - started < 2.5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 10.0), ("", 10.0), ("abc", 10.0), ("nan", 10.0), ("0", 1.0), ("-5", 1.0), ("0.2", 1.0), ("5", 5.0), ("600", 60.0), ("inf", 60.0)],
)
def test_loki_timeout_env_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("EAP_LOKI_URL", "http://127.0.0.1:3100")
    if raw is None:
        monkeypatch.delenv("EAP_LOKI_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("EAP_LOKI_TIMEOUT_SECONDS", raw)
    source = get_log_source()
    assert isinstance(source, LokiClient) and source.timeout_seconds == expected


# --------------------------------------------------------------------------- concurrency limit


@pytest.fixture
def slots(client):
    pool = QuerySlots(2)
    client.app.dependency_overrides[get_query_slots] = lambda: pool
    return pool


def test_full_slots_give_503_busy_without_asking_loki(client, tenant, source, slots):
    assert slots.try_acquire() and slots.try_acquire()
    response = _get(client, tenant)
    assert response.status_code == 503 and response.json() == {"detail": "Log search is busy; retry shortly"}
    assert source.calls == []
    slots.release()
    assert _get(client, tenant).status_code == 200 and len(source.calls) == 1


@pytest.mark.parametrize("error", [None, LokiUnavailable("x"), LokiQueryFailed("x")])
def test_slot_is_released_after_success_and_after_errors(client, tenant, source, slots, error):
    source.error = error
    for _ in range(5):  # more requests than slots: a leaked slot would turn one into "busy"
        assert _get(client, tenant).json().get("detail") != "Log search is busy; retry shortly"
    assert slots.try_acquire() and slots.try_acquire() and not slots.try_acquire()


def test_concurrent_queries_never_exceed_the_limit(client, tenant, source, slots):
    active, peak, lock, gate = 0, 0, threading.Lock(), threading.Event()

    def hold():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        gate.wait(5)
        with lock:
            active -= 1

    source.hook = hold
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(_get(client, tenant).status_code)) for _ in range(6)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(results) < 4:  # the 4 beyond the 2 slots are rejected at once
        time.sleep(0.02)
    assert sorted(results) == [503] * 4
    gate.set()
    for t in threads:
        t.join(10)
    assert sorted(results) == [200, 200, 503, 503, 503, 503] and peak == 2


@pytest.mark.parametrize(("raw", "size"), [(None, 4), ("2", 2), ("0", 1), ("-3", 1), ("1000", 64), ("x", 4), ("nan", 4)])
def test_max_queries_env(monkeypatch, raw, size):
    monkeypatch.setattr(logs_module, "_query_slots", None)
    if raw is None:
        monkeypatch.delenv("EAP_LOKI_MAX_QUERIES", raising=False)
    else:
        monkeypatch.setenv("EAP_LOKI_MAX_QUERIES", raw)
    assert get_query_slots().size == size


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("limit", ["0", "-1", "501", "10000", "abc", "1.5", ""])
def test_limit_outside_1_to_500_is_422(client, tenant, source, limit):
    assert _get(client, tenant, limit=limit).status_code == 422
    assert source.calls == []


@pytest.mark.parametrize("limit", [1, 100, 500])
def test_limit_bounds_are_inclusive_and_passed_through(client, tenant, source, limit):
    assert _get(client, tenant, limit=limit).status_code == 200
    assert source.calls[-1]["limit"] == limit


def test_loki_overreturning_is_cut_to_the_limit_newest_first(client, tenant, source):
    source.entries = [_entry(i, f"m{i}", tenant) for i in range(1, 8)]
    body = _get(client, tenant, limit=3).json()
    assert [i["message"] for i in body["items"]] == ["m7", "m6", "m5"]


def test_range_over_seven_days_is_422(client, tenant, source):
    to = datetime(2026, 11, 10, tzinfo=UTC)
    for extra in (timedelta(seconds=1), timedelta(days=1), timedelta(days=365)):
        r = _get(client, tenant, **{"from": (to - timedelta(days=7) - extra).isoformat(), "to": to.isoformat()})
        assert r.status_code == 422 and "7 days" in r.json()["detail"]
    assert source.calls == []
    # Without `to` (defaults to the server's now) a longer window is clamped to the last 7 days.
    clamped = _get(client, tenant, **{"from": "2020-01-01T00:00:00Z"})
    assert clamped.status_code == 200
    assert source.calls[-1]["end_ns"] - source.calls[-1]["start_ns"] == 7 * 86_400 * 10**9
    ok = _get(client, tenant, **{"from": (to - timedelta(days=7)).isoformat(), "to": to.isoformat()})
    assert ok.status_code == 200


def test_from_not_before_to_is_422(client, tenant, source):
    t = datetime(2026, 11, 10, 12, tzinfo=UTC)
    for frm, to in ((t, t), (t + timedelta(seconds=1), t), (t + timedelta(days=30), t)):
        r = _get(client, tenant, **{"from": frm.isoformat(), "to": to.isoformat()})
        assert r.status_code == 422 and r.json()["detail"] == "'from' must be before 'to'"
    future = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    assert _get(client, tenant, **{"from": future}).status_code == 422  # from after the default to=now
    assert source.calls == []


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-45", "1e9", "", "null"])
def test_unparseable_times_are_422(client, tenant, source, bad):
    assert _get(client, tenant, **{"from": bad}).status_code == 422
    assert _get(client, tenant, to=bad).status_code == 422
    assert source.calls == []


def test_q_longer_than_500_is_422_and_bad_utf8_is_safe(client, tenant, source):
    assert _get(client, tenant, q="x" * 501).status_code == 422
    assert _get(client, tenant, q="x" * 500).status_code == 200
    # Invalid UTF-8 on the wire (an encoded lone surrogate) is never a 5xx and never an unfiltered query.
    r = client.get("/api/v1/logs?q=a%ED%B0%80b", headers=get_auth_header(tenant, "alice"))
    assert r.status_code < 500
    for call in source.calls:
        assert call["query"].startswith(f'{SELECTOR} | eap_tenant_id="{tenant}"')
    assert len(source.calls) in (1, 2)


def test_unauthenticated_and_bad_tokens_are_401_and_never_query(client, source):
    assert client.get("/api/v1/logs").status_code == 401
    assert client.get("/api/v1/logs", headers={"Authorization": "Bearer nope"}).status_code in (401, 403)
    assert source.calls == []


# --------------------------------------------------------------------------- live Loki


def _provider(service: str):
    """The app's own export pipeline (context, redaction, batch) pointed at the local OTLP collector."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    from eap_observability.redaction import RedactingLogProcessor

    provider = LoggerProvider(resource=Resource.create({"service.name": service}))
    exporter = OTLPLogExporter(endpoint=OTLP_LOGS_URL)
    provider.add_log_record_processor(
        obs.LogContextProcessor(RedactingLogProcessor(BatchLogRecordProcessor(exporter, schedule_delay_millis=200)))
    )
    return provider


@pytest.fixture
def live(monkeypatch):
    """(emit logger, flush): records of this test only, via a private logger and provider."""
    from opentelemetry.sdk._logs import LoggingHandler

    assert requests.get(f"{LOKI_URL}/ready", timeout=3).status_code == 200, "Loki must be running"
    provider = _provider("eap-worker")
    logger = logging.getLogger(f"eap_live_{uuid.uuid4().hex}")
    logger.propagate, handler = False, LoggingHandler(level=logging.DEBUG, logger_provider=provider)
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    monkeypatch.setenv("EAP_LOKI_URL", LOKI_URL)
    yield logger, provider.force_flush
    logger.removeHandler(handler)
    provider.shutdown()


def _search(client, tenant, want, timeout=25.0, **params):
    """Poll GET /logs until `want(items)` holds (Loki ingests asynchronously); returns the last items."""
    deadline, items = time.monotonic() + timeout, []
    while True:
        response = _get(client, tenant, **params)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        if want(items) or time.monotonic() > deadline:
            return items
        time.sleep(0.5)


def _messages(items):
    return sorted(i["message"] for i in items)


def _settle(client, tenant, marker, **params):
    """Wait until the tenant sees something with `marker`, returns the items (all with the marker)."""
    return _search(client, tenant, lambda it: len(it) > 0, q=marker, **params)


def test_live_two_tenants_never_see_each_other_nor_untagged_records(client, live):
    logger, flush = live
    marker = f"mk{uuid.uuid4().hex}"
    a, b, c = (f"{p}{uuid.uuid4().hex[:8]}" for p in "abc")
    with obs.log_context(tenant_id=a):
        logger.warning("%s only-a", marker)
    with obs.log_context(tenant_id=b):
        logger.warning("%s only-b", marker)
    logger.warning("%s untagged", marker)  # outside any tenant context
    flush()
    items_a = _search(client, a, lambda it: len(it) >= 1, q=marker)
    items_b = _search(client, b, lambda it: len(it) >= 1, q=marker)
    assert _messages(items_a) == [f"{marker} only-a"]
    assert _messages(items_b) == [f"{marker} only-b"]
    # A tenant that logged nothing sees nothing, although the records exist in Loki.
    time.sleep(1)
    assert _get(client, c, q=marker).json()["items"] == []
    # The untagged record is really stored (so its absence above is isolation, not ingest delay).
    raw = requests.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": f'{{service_name="eap-worker"}} |= "{marker} untagged"', "limit": 5},
        timeout=10,
    ).json()
    assert sum(len(s["values"]) for s in raw["data"]["result"]) == 1
    for items in (items_a, items_b):
        assert all(i["service"] == "eap-worker" and i["level"] == "warn" for i in items)


@pytest.mark.parametrize(
    "hostile",
    [
        '" | eap_tenant_id=~".+',
        '"} or {service_name=~".+"} |= "',
        '`} | json | eap_tenant_id!="zzz" |= `',
        "\\\" |~ \".*\"",
        "a\nb | eap_tenant_id=~\".*\"\n",
        "}",
    ],
)
def test_live_hostile_q_is_literal_text_and_finds_no_other_tenant(client, live, hostile):
    logger, flush = live
    marker = f"mk{uuid.uuid4().hex}"
    a, b = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    with obs.log_context(tenant_id=a):
        logger.warning("%s literal %s", marker, hostile)
    with obs.log_context(tenant_id=b):
        logger.warning("%s secret-of-b", marker)
    flush()
    _settle(client, b, marker)  # b's record is in Loki by now
    # The hostile text, searched literally, matches exactly a's own record containing it ...
    own = _search(client, a, lambda it: len(it) >= 1, q=f"{marker} literal {hostile}")
    assert _messages(own) == [f"{marker} literal {hostile}"]
    # ... a's hostile-prefixed queries never surface b's record, and b never sees a's.
    for q in (hostile, f"{marker}{hostile}", f"{hostile}{marker}"):
        assert all("secret-of-b" not in i["message"] for i in _get(client, a, q=q).json()["items"])
    assert all("literal" not in i["message"] for i in _get(client, b, q=marker).json()["items"])
    # An injection attempt that would widen to every tenant returns nothing at all.
    assert _get(client, a, q=f'{marker}" or eap_tenant_id=~".+').json()["items"] == []


@pytest.mark.parametrize("key", ["eap_tenant_id", "eap-tenant-id", "eap tenant id", "eap_tenant.id", "eap_run_id", "eap-run-id"])
def test_live_impostor_attribute_never_shows_a_record_to_another_tenant(client, live, key):
    logger, flush = live
    marker = f"mk{uuid.uuid4().hex}"
    victim, other = f"v{uuid.uuid4().hex[:8]}", f"o{uuid.uuid4().hex[:8]}"
    run = str(uuid.uuid4())
    with obs.log_context(tenant_id=other):
        logger.warning("%s forged-in-other", marker, extra={key: victim})  # forged id in another tenant's context
    logger.warning("%s forged-untagged", marker, extra={key: victim})  # forged id with no tenant context at all
    with obs.log_context(tenant_id=victim, run_id=run):
        logger.warning("%s genuine", marker)
    flush()
    seen = _search(client, victim, lambda it: len(it) >= 1, q=marker)
    assert _messages(seen) == [f"{marker} genuine"]  # the forged records do not show for the victim ...
    time.sleep(1.5)
    assert _messages(_get(client, victim, q=marker).json()["items"]) == [f"{marker} genuine"]
    assert _messages(_get(client, other, q=marker).json()["items"]) == [f"{marker} forged-in-other"]  # ... only for their real tenant
    assert all(i["run_id"] in (None, run) for i in _get(client, victim, q=marker).json()["items"])
    # The forged key itself never reached Loki as a competing value for the label.
    raw = requests.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={"query": f'{{service_name="eap-worker"}} |= "{marker} forged"', "limit": 10},
        timeout=10,
    ).json()
    for stream in raw["data"]["result"]:
        for value in stream["values"]:
            meta = {k: v for g in (value[2] or {}).values() for k, v in g.items()} if len(value) > 2 else {}
            assert victim not in {*meta.values(), *stream["stream"].values()}


def test_live_run_filter_and_foreign_run(client, live):
    logger, flush = live
    marker = f"mk{uuid.uuid4().hex}"
    a, b = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    run1, run2 = _run_of(client, a, str(uuid.uuid4())), _run_of(client, a, str(uuid.uuid4()))
    run_b = _run_of(client, b, str(uuid.uuid4()))
    for tenant, run, text in ((a, run1, "r1"), (a, run2, "r2"), (b, run_b, "rb")):
        with obs.log_context(tenant_id=tenant, run_id=run):
            logger.warning("%s %s", marker, text)
    with obs.log_context(tenant_id=a):
        logger.warning("%s norun", marker)
    flush()
    assert _messages(_search(client, a, lambda it: len(it) >= 3, q=marker)) == [f"{marker} norun", f"{marker} r1", f"{marker} r2"]
    only1 = _search(client, a, lambda it: len(it) >= 1, q=marker, run_id=run1)
    assert _messages(only1) == [f"{marker} r1"] and only1[0]["run_id"] == run1
    assert _get(client, a, q=marker, run_id=run_b).status_code == 404  # b's run: 404, regardless of Loki
    assert _get(client, b, q=marker, run_id=run1).status_code == 404
    assert _messages(_get(client, b, q=marker, run_id=run_b).json()["items"]) == [f"{marker} rb"]


def test_live_level_is_a_minimum(client, live):
    logger, flush = live
    marker, a = f"mk{uuid.uuid4().hex}", f"a{uuid.uuid4().hex[:8]}"
    with obs.log_context(tenant_id=a):
        for name in ("debug", "info", "warning", "error", "critical"):
            getattr(logger, name)("%s %s", marker, name)
    flush()
    _search(client, a, lambda it: len(it) >= 5, q=marker)
    levels = {lvl: _messages(_get(client, a, q=marker, level=lvl).json()["items"]) for lvl in ("info", "warn", "error", "fatal")}
    assert levels["info"] == sorted(f"{marker} {n}" for n in ("info", "warning", "error", "critical"))
    assert levels["warn"] == sorted(f"{marker} {n}" for n in ("warning", "error", "critical"))
    assert levels["error"] == sorted(f"{marker} {n}" for n in ("error", "critical"))
    assert levels["fatal"] == [f"{marker} critical"]


def _push(marker: str, entries: list[tuple[str, dict[str, str], dict[str, str]]]) -> None:
    """Push (line, stream labels, structured metadata) straight to Loki as service eap-worker."""
    base = time.time_ns()
    streams = []
    for n, (line, labels, meta) in enumerate(entries):
        stream = {"service_name": "eap-worker", "severity_text": "WARN", "test_marker": marker, **labels}
        value = [str(base + n * 1000), f"{marker} {line}", meta]
        streams.append({"stream": stream, "values": [value]})
    response = requests.post(f"{LOKI_URL}/loki/api/v1/push", json={"streams": streams}, timeout=10)
    assert response.status_code == 204, response.text


def test_live_direct_push_with_structured_metadata_is_isolated_per_tenant(client, monkeypatch):
    monkeypatch.setenv("EAP_LOKI_URL", LOKI_URL)
    marker = f"mk{uuid.uuid4().hex}"
    a, b = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    _push(
        marker,
        [
            ("a1", {}, {"eap_tenant_id": a}),
            ("a2", {}, {"eap_tenant_id": a, "eap_run_id": str(uuid.uuid4())}),
            ("b1", {}, {"eap_tenant_id": b}),
            ("untagged", {}, {}),
            ("run-only", {}, {"eap_run_id": str(uuid.uuid4())}),
            ("empty-tenant", {}, {"eap_tenant_id": ""}),
            ("prefix-a", {}, {"eap_tenant_id": a + "x"}),  # a longer id that merely starts with a's
            ("upper-a", {}, {"eap_tenant_id": a.upper()}),
            ("other-service", {"service_name": "not-ours"}, {"eap_tenant_id": a}),
        ],
    )
    got_a = _search(client, a, lambda it: len(it) >= 2, q=marker)
    assert _messages(got_a) == [f"{marker} a1", f"{marker} a2"]
    assert _messages(_search(client, b, lambda it: len(it) >= 1, q=marker)) == [f"{marker} b1"]
    stranger = f"s{uuid.uuid4().hex[:8]}"
    assert _get(client, stranger, q=marker).json()["items"] == []
    assert _get(client, a, q=f"{marker} untagged").json()["items"] == []
    for tenant in (a, b):  # hostile run-less q cannot widen the filter
        assert _get(client, tenant, q=f'{marker}" or eap_tenant_id=~".+').json()["items"] == []


def test_live_stream_label_cannot_stand_in_for_the_tenant(client, monkeypatch):
    """A record pushed with eap_tenant_id as an index label (not metadata), or with both, is never shown
    to two tenants and never to a tenant other than the one it names."""
    monkeypatch.setenv("EAP_LOKI_URL", LOKI_URL)
    marker = f"mk{uuid.uuid4().hex}"
    a, b, c = (f"{p}{uuid.uuid4().hex[:8]}" for p in "abc")
    _push(
        marker,
        [
            ("label-b", {"eap_tenant_id": b}, {}),
            ("label-b-meta-a", {"eap_tenant_id": b}, {"eap_tenant_id": a}),
            ("meta-a", {}, {"eap_tenant_id": a}),
        ],
    )
    _search(client, a, lambda it: any(i["message"].endswith("meta-a") for i in it), q=marker)
    shown = {t: _messages(_get(client, t, q=marker).json()["items"]) for t in (a, b, c)}
    assert shown[c] == []
    assert f"{marker} meta-a" in shown[a] and f"{marker} meta-a" not in shown[b]
    for name in ("label-b", "label-b-meta-a"):
        owners = [t for t in (a, b) if f"{marker} {name}" in shown[t]]
        assert len(owners) <= 1, f"{name} shown to {owners}"
        if name == "label-b":
            assert owners in ([], [b])


def test_live_api_failure_records_are_found_by_their_own_tenant_only(client, live, monkeypatch):
    """End to end: the API's own log record (route failure, eap.tenant_id from the request) lands in Loki
    and is searchable by that tenant and nobody else."""
    from opentelemetry.sdk._logs import LoggingHandler

    _, _ = live
    a, b = f"a{uuid.uuid4().hex[:8]}", f"b{uuid.uuid4().hex[:8]}"
    provider = _provider("eap-api")
    handler = LoggingHandler(level=logging.INFO, logger_provider=provider)
    api_log = logging.getLogger("eap_api")
    api_log.addHandler(handler)
    try:
        monkeypatch.setenv("EAP_LOKI_URL", "http://127.0.0.1:9")  # refused: the route logs "loki unavailable"
        assert _get(client, a).status_code == 503
        assert _get(client, b, q="not-a-match").status_code == 503
        provider.force_flush()
    finally:
        api_log.removeHandler(handler)
    monkeypatch.setenv("EAP_LOKI_URL", LOKI_URL)
    mine = _search(client, a, lambda it: any("loki unavailable" in i["message"] for i in it), q="loki unavailable")
    assert sum(i["message"].startswith("log search: loki unavailable") for i in mine) == 1
    assert all(i["service"] == "eap-api" for i in mine)
    theirs = _get(client, b, q="loki unavailable").json()["items"]
    assert len(theirs) == 1  # b has exactly their own record
    assert _get(client, f"c{uuid.uuid4().hex[:8]}", q="loki unavailable").json()["items"] == []
    provider.shutdown()


def test_live_long_tenant_and_run_ids_are_not_redacted(client, live):
    logger, flush = live
    marker = f"mk{uuid.uuid4().hex}"
    long_tenant = "t" + uuid.uuid4().hex * 4  # 129 characters, long hex like a secret
    run = _run_of(client, long_tenant, str(uuid.uuid4()))
    with obs.log_context(tenant_id=long_tenant, run_id=run):
        logger.warning("%s long", marker)
    flush()
    items = _search(client, long_tenant, lambda it: len(it) >= 1, q=marker, run_id=run)
    assert _messages(items) == [f"{marker} long"] and items[0]["run_id"] == run


# --------------------------------------------------------------------------- shared SSE listener has no tenant


def test_sse_listener_task_runs_in_an_empty_context(monkeypatch):
    from eap_api import sse

    seen: dict = {}
    marker = contextvars.ContextVar("eap_test_marker", default="unset")

    async def fake_listen(self):
        seen["log_context"] = obs.current_log_context()
        seen["marker"] = marker.get()
        await asyncio.sleep(60)

    monkeypatch.setattr(sse.PostgresDeltaSource, "_listen_forever", fake_listen)

    async def scenario():
        source = sse.PostgresDeltaSource("postgresql://unused")
        obs.bind_log_context(tenant_id="tenant-of-the-request", run_id=str(uuid.uuid4()))
        marker.set("request")
        async with source(str(uuid.uuid4())):
            await asyncio.sleep(0.05)
            task = source._listener_task
            assert task is not None
            await source.close()
            return seen

    result = asyncio.run(scenario())
    assert result == {"log_context": {}, "marker": "unset"}


def test_sse_listener_failure_record_carries_no_tenant_but_the_request_log_does():
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

    from eap_api import sse

    obs.shutdown_telemetry()
    exporter = InMemoryLogRecordExporter()
    assert obs.setup_logging("eap-api", exporter=exporter) is True
    tenant = f"t{uuid.uuid4().hex[:8]}"

    async def scenario():
        source = sse.PostgresDeltaSource("postgresql://nobody@127.0.0.1:9/none?connect_timeout=1")
        with obs.log_context(tenant_id=tenant, run_id=str(uuid.uuid4())):
            async with source(str(uuid.uuid4())):
                logging.getLogger("eap_api").warning("request-side record")  # still tagged
                for _ in range(100):
                    obs._state.logger_provider.force_flush()
                    if any("LISTEN" in (r.log_record.body or "") for r in exporter.get_finished_logs()):
                        break
                    await asyncio.sleep(0.1)
        await source.close()

    try:
        asyncio.run(scenario())
        obs._state.logger_provider.force_flush()
        records = [r.log_record for r in exporter.get_finished_logs()]
    finally:
        obs.shutdown_telemetry()
    listener = [r for r in records if "LISTEN" in (r.body or "")]
    request = [r for r in records if r.body == "request-side record"]
    assert listener and request
    assert all(obs.TENANT_LOG_ATTRIBUTE not in (r.attributes or {}) for r in listener)
    assert all(obs.RUN_LOG_ATTRIBUTE not in (r.attributes or {}) for r in listener)
    assert all((r.attributes or {}).get(obs.TENANT_LOG_ATTRIBUTE) == tenant for r in request)
