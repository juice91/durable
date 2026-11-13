"""P0.21 security tests for the P0 API surface (the gaps the per-task suites leave open).

1. LogQL injection through every `GET /logs` parameter, parameter pollution and the dev principal's
   tenant. The query is captured by a recording LogSource and parsed with an independent Go
   `strconv.Unquote`-compatible decoder: the structure is asserted exactly and every literal decodes
   back to exactly the value that went in. (`test_logs_acceptance.py` already covers the HOSTILE
   string table, unknown levels, limits and ranges; this file covers what it does not.)
2. RE2 limits through `POST /processors/test`, publish (PATCH draft + POST versions), draft runs and
   draft turns, plus the exact program-size boundary of `eap_processors`.
3. Spoofing the reserved identity names (`tenant_id`, `user_id`, `run_id`) as context field names,
   request context keys (run start, turn start, validate) and `{{context.*}}` placeholders, with
   case, whitespace and Unicode look-alike variants.
4. Oversize, deep, non-UTF-8 bodies and NUL query strings through the request guard on the new P0
   routes, and what the unguarded P0 routers (logs, models, traces) do with the same input.

Tests never call a model: runs go to a fake DBOS client (`pg` fixture from test_runs).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from eap_api.auth import current_principal
from eap_api.routes.logs import MAX_LIMIT, MAX_QUERY_TEXT, LokiEntry, get_log_source
from eap_processors import MAX_PROGRAM_SIZE, MAX_REDACTED_MATCHES, re2_pattern_error
from eap_schemas.processors import MAX_REGEX_PATTERN_LENGTH, MAX_REGEX_PATTERNS
from eap_schemas.request_context import RESERVED_CONTEXT_NAMES

from .conftest import get_auth_header
from .test_conversations import new_conversation, turn
from .test_input_hardening_acceptance import _asgi, _headers, _nested
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    FakeDBOSClient,
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

BODY_CAP = 256 * 1024
TOO_LARGE = f"request body too large (max {BODY_CAP} bytes)"
NOT_UTF8 = "request body must be UTF-8 encoded JSON (no NUL bytes)"
DEPTH = "request body is nested more than 32 levels deep"
TEST_URL = "/api/v1/processors/test"

CONTEXT_SPEC = {
    **SPEC,
    "instructions": "Help {{context.customer}}.",
    "context_fields": [
        {"name": "customer", "type": "string", "required": True, "max_length": 40},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
    ],
}
OK_CONTEXT = {"customer": "Ada"}

# Reserved names and look-alikes: case, surrounding/inner whitespace, zero-width and bidi marks,
# fullwidth letters, Cyrillic/Greek homoglyphs, a combining mark, a hyphen instead of underscore.
LOOKALIKES = [
    "Tenant_id", "TENANT_ID", "tenant_Id", "tenant_id ", " tenant_id", "tenant_id\t", "tenant_id\n",
    "tenant​_id", "tenant_id​", "‮tenant_id", "tenant_id﻿",
    "ｔｅｎａｎｔ_ｉｄ",  # fullwidth "tenant_id"
    "tenant_іd",  # Cyrillic i
    "tеnant_id",  # Cyrillic e
    "tenant_id́",  # d + combining acute
    "tenant-id", "tenant.id", "user_ID", "User_id", "run_Id", "RUN_ID", "user_id ",
]


# --------------------------------------------------------------------------- helpers: LogQL parsing


def go_unquote(literal: str) -> str:
    """Go strconv.Unquote for a double-quoted literal: \\a \\b \\f \\n \\r \\t \\v \\\\ \\" \\xHH
    \\ooo \\uXXXX \\UXXXXXXXX. A raw newline or raw '"' inside, or any other escape, is an error
    (AssertionError), exactly as Go refuses it. Independent of the code under test."""
    assert len(literal) >= 2 and literal[0] == '"' and literal[-1] == '"', literal
    body, out, i = literal[1:-1], [], 0
    simple = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"'}
    while i < len(body):
        ch = body[i]
        if ch == '"' or ch == "\n":
            raise AssertionError(f"raw {ch!r} inside a Go string literal")
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        assert i + 1 < len(body), "dangling backslash"
        kind = body[i + 1]
        if kind in simple:
            out.append(simple[kind])
            i += 2
        elif kind in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[kind]
            digits = body[i + 2 : i + 2 + width]
            assert len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits), digits
            code = int(digits, 16)
            assert kind == "x" or not (0xD800 <= code <= 0xDFFF or code > 0x10FFFF), "invalid rune"
            out.append(chr(code))
            i += 2 + width
        elif kind in "01234567":
            digits = body[i + 1 : i + 4]
            assert len(digits) == 3 and all(c in "01234567" for c in digits)
            out.append(chr(int(digits, 8)))
            i += 4
        else:
            raise AssertionError(f"invalid escape \\{kind}")
    return "".join(out)


def split_logql(query: str) -> tuple[str, list[str]]:
    """(skeleton with each string literal replaced by S, decoded literals), scanning the query
    left to right like a lexer: a literal starts at '"' and ends at the first unescaped '"'."""
    skeleton, literals, i = [], [], 0
    while i < len(query):
        ch = query[i]
        if ch == "`":
            raise AssertionError("raw-string (backtick) literal outside a double-quoted string")
        if ch != '"':
            skeleton.append(ch)
            i += 1
            continue
        j = i + 1
        while j < len(query) and query[j] != '"':
            j += 2 if query[j] == "\\" else 1
        assert j < len(query), "unterminated literal"
        literals.append(go_unquote(query[i : j + 1]))
        skeleton.append("S")
        i = j + 1
    return "".join(skeleton), literals


def expected_skeleton(*, run: bool = False, level: int | None = None, q: bool = False) -> str:
    parts = ["{service_name=~S}", "| eap_tenant_id=S"]
    if run:
        parts.append("| eap_run_id=S")
    if level is not None:
        parts.append(f"| severity_number >= {level}")
    if q:
        parts.append("|= S")
    return " ".join(parts)


def assert_no_raw_unsafe_chars(query: str) -> None:
    """Outside and inside literals, nothing invisible or line-breaking is sent raw."""
    for ch in query:
        cat = unicodedata.category(ch)
        assert cat not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"), f"raw {ch!r} ({cat}) in the query"
        assert cat != "Zs" or ch == " ", f"raw {ch!r} in the query"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entries: list[LokiEntry] = []

    def query_range(self, query, *, start_ns, end_ns, limit):
        self.calls.append({"query": query, "start_ns": start_ns, "end_ns": end_ns, "limit": limit})
        return list(self.entries)


@pytest.fixture
def logs(pg):
    client, app = pg
    fake = Recorder()
    app.dependency_overrides[get_log_source] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_log_source, None)


def _logs(client, headers, params: Any = None, raw: str | None = None):
    if raw is not None:
        return client.get(f"/api/v1/logs?{raw}", headers=headers)
    return client.get("/api/v1/logs", params=params, headers=headers)


def _own_run(app, tenant: str, agent_id: str) -> str:
    run_id = uuid.uuid4()
    app.state.run_stores[0].create_run(tenant, uuid.UUID(agent_id), 1, "hi", run_id)
    return str(run_id)


# --------------------------------------------------------------------------- 1. LogQL injection


@pytest.mark.parametrize(
    "q",
    [
        "‮}evil{‬",  # RIGHT-TO-LEFT OVERRIDE / POP DIRECTIONAL FORMATTING
        "⁦\" or 1⁩",  # isolates around a quote
        "​‌‍⁠﻿",  # zero-width space, (non-)joiner, word joiner, BOM
        "a­b؜c᠎d",  # soft hyphen, Arabic letter mark, Mongolian vowel separator
        "line sep para\u0085nel",  # Unicode line/paragraph separators, NEL
        "\x00",
        "a\x00b\x00",
        "a\rb",
        "a\r\nX-Scope-OrgID: other\r\n",
        "\x7f\x1b[31m\x9b",
        "\U000e0041\U000e007f",  # tag characters (invisible)
        "\U000f0000",  # private use
        "\\x22\\u0022\\042",  # escapes as literal text: must not be decoded twice
        "\\",
        '\\"',
    ],
)
def test_logs_q_with_format_bidi_zero_width_nul_and_crlf_stays_one_literal(pg, tenant_of, logs, q):
    client, _ = pg
    tenant, headers = tenant_of()
    r = _logs(client, headers, {"q": q})
    assert r.status_code == 200, r.text
    (call,) = logs.calls
    skeleton, literals = split_logql(call["query"])
    assert skeleton == expected_skeleton(q=True)
    assert literals == ["eap-api|eap-worker", tenant, q]
    assert_no_raw_unsafe_chars(call["query"])


@pytest.mark.parametrize("unit", ['"', "\\", "\x00", "\n", "‮", "\U0001f600", "`", "|", "}"])
def test_logs_q_at_the_500_character_boundary(pg, tenant_of, logs, unit):
    """500 code points of the costliest-to-escape characters are accepted and decode back exactly;
    501 are 422 and Loki is never asked. The cap counts characters, not escaped or UTF-8 bytes."""
    client, _ = pg
    tenant, headers = tenant_of()
    assert MAX_QUERY_TEXT == 500
    exact = unit * MAX_QUERY_TEXT
    r = _logs(client, headers, {"q": exact})
    assert r.status_code == 200, r.text
    skeleton, literals = split_logql(logs.calls[-1]["query"])
    assert skeleton == expected_skeleton(q=True) and literals[-1] == exact and literals[1] == tenant
    calls = len(logs.calls)
    over = _logs(client, headers, {"q": unit * (MAX_QUERY_TEXT + 1)})
    assert over.status_code == 422
    assert len(logs.calls) == calls
    # The logs router is not on the request guard, so this is FastAPI's default 422, which echoes
    # the caller's own rejected `q` (bounded by the cap): reflected only to its sender, never queried.
    assert over.json()["detail"][0]["loc"] == ["query", "q"] and len(over.content) < 8192


def test_logs_repeated_parameters_resolve_to_one_value_and_never_add_stages(pg, tenant_of, logs):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = make_agent(client, headers)
    own = _own_run(app, tenant, agent_id)
    other_tenant, other_headers = tenant_of()
    foreign = _own_run(app, other_tenant, make_agent(client, other_headers))

    # q twice: one line filter, holding exactly one of the values (the last), never both.
    r = _logs(client, headers, [("q", "first"), ("q", '"} or {x=~".+"')])
    assert r.status_code == 200
    skeleton, literals = split_logql(logs.calls[-1]["query"])
    assert skeleton == expected_skeleton(q=True)
    assert literals == ["eap-api|eap-worker", tenant, '"} or {x=~".+"']

    # level twice: one severity stage.
    r = _logs(client, headers, [("level", "error"), ("level", "debug")])
    assert r.status_code == 200
    assert split_logql(logs.calls[-1]["query"])[0] == expected_skeleton(level=5)
    r = _logs(client, headers, [("level", "debug"), ("level", "x | y")])
    assert r.status_code == 422

    # run_id twice: whichever value is used is ownership-checked. A foreign run last is a 404.
    calls = len(logs.calls)
    r = _logs(client, headers, [("run_id", own), ("run_id", foreign)])
    assert r.status_code == 404 and len(logs.calls) == calls
    r = _logs(client, headers, [("run_id", foreign), ("run_id", own)])
    assert r.status_code == 200
    skeleton, literals = split_logql(logs.calls[-1]["query"])
    assert skeleton == expected_skeleton(run=True) and literals[1:] == [tenant, own]
    assert foreign not in logs.calls[-1]["query"]

    # limit twice: the value used is still range-checked.
    calls = len(logs.calls)
    assert _logs(client, headers, [("limit", "1"), ("limit", str(MAX_LIMIT + 1))]).status_code == 422
    assert len(logs.calls) == calls
    assert _logs(client, headers, [("limit", str(MAX_LIMIT + 1)), ("limit", "3")]).status_code == 200
    assert logs.calls[-1]["limit"] == 3

    # from/to twice: the window actually used is still at most 7 days.
    t = datetime(2026, 11, 10, tzinfo=UTC)
    r = _logs(client, headers, [("from", (t - timedelta(days=1)).isoformat()), ("from", (t - timedelta(days=30)).isoformat()),
                                ("to", t.isoformat())])
    assert r.status_code == 422


def test_logs_unknown_parameters_are_ignored(pg, tenant_of, logs):
    """Loki's own parameter names, alternative tenant names and LogQL-looking names never reach the
    query or the Loki call; the query is byte-for-byte the plain one."""
    client, _ = pg
    tenant, headers = tenant_of()
    assert _logs(client, headers).status_code == 200
    plain = logs.calls[-1]
    hostile = '{service_name=~".+"}'
    params = [
        ("query", hostile), ("logql", hostile), ("expr", hostile), ("tenant_id", "x"), ("tenant", "x"),
        ("eap_tenant_id", "x"), ("X-Scope-OrgID", "x"), ("org_id", "x"), ("service_name", "x"),
        ("direction", "forward"), ("start", "0"), ("end", "99999999999999999999"), ("step", "1"),
        ("since", "720h"), ("user_id", "x"), ("run", "x"), ("Q", hostile), ("q ", hostile), ("q[]", hostile),
        ("LEVEL", "x"), ("from_", "2000-01-01"), ("Limit", "100000"),
    ]
    r = _logs(client, headers, params)
    assert r.status_code == 200, r.text
    call = logs.calls[-1]
    assert call["query"] == plain["query"] == f'{{service_name=~"eap-api|eap-worker"}} | eap_tenant_id="{tenant}"'
    assert call["limit"] == plain["limit"] == 100
    assert call["end_ns"] - call["start_ns"] == 3_600 * 10**9  # the default hour, not `start`/`end`


@pytest.mark.parametrize(
    "value",
    [
        '2026-11-10T00:00:00Z"} or {x=~".+"',
        "2026-11-10T00:00:00Z | eap_tenant_id=\"x\"",
        "2026-11-10T00:00:00\x00Z",
        "2026-11-10T00:00:00Z\r\n",
        "now-7d",
        "0",
        "-62135596800",
        "9999-12-31T23:59:59Z",
        "2026-11-10T00:00:00+99:00",
        "٢٠٢٦-11-10",  # Arabic-Indic digits
    ],
)
def test_logs_from_and_to_never_reach_logql_as_text(pg, tenant_of, logs, value):
    """Times are parsed into datetimes and sent to Loki only as integer nanoseconds: a hostile value
    is 422, or (if it parses) changes only the window, never the query."""
    client, _ = pg
    tenant, headers = tenant_of()
    for name in ("from", "to"):
        r = _logs(client, headers, {name: value})
        assert r.status_code in (200, 422), (name, value, r.status_code)
        if r.status_code == 200:
            call = logs.calls[-1]
            assert call["query"] == f'{{service_name=~"eap-api|eap-worker"}} | eap_tenant_id="{tenant}"'
            assert isinstance(call["start_ns"], int) and isinstance(call["end_ns"], int)
            assert 0 < call["end_ns"] - call["start_ns"] <= 7 * 86_400 * 10**9
    for call in logs.calls:
        assert call["query"] == f'{{service_name=~"eap-api|eap-worker"}} | eap_tenant_id="{tenant}"'


@pytest.mark.parametrize("level", ["WARN", "Warn", " warn", "warn ", "warn\x00", "wаrn", "5", "13", "warning", "error|fatal"])
def test_logs_level_variants_are_422_and_never_queried(pg, tenant_of, logs, level):
    client, _ = pg
    _, headers = tenant_of()
    assert _logs(client, headers, {"level": level}).status_code == 422
    assert logs.calls == []


@pytest.mark.parametrize("limit", ["+5", " 5", "5 ", "0x10", "1e2", "５", "05", "5\x00", "500.0", "1_0"])
def test_logs_limit_variants_are_422_or_a_plain_bounded_integer(pg, tenant_of, logs, limit):
    client, _ = pg
    tenant, headers = tenant_of()
    r = _logs(client, headers, {"limit": limit})
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        call = logs.calls[-1]
        assert type(call["limit"]) is int and 1 <= call["limit"] <= MAX_LIMIT
        assert call["query"] == f'{{service_name=~"eap-api|eap-worker"}} | eap_tenant_id="{tenant}"'
    else:
        assert logs.calls == []


@pytest.mark.parametrize("run_id", ["%00", "00000000-0000-0000-0000-000000000000%00", "%22%7D", "*", "..%2F..", "%ED%A0%80"])
def test_logs_hostile_run_id_on_the_wire_is_422(pg, tenant_of, logs, run_id):
    client, _ = pg
    _, headers = tenant_of()
    assert _logs(client, headers, raw=f"run_id={run_id}").status_code == 422
    assert logs.calls == []


HOSTILE_TENANTS = [
    'acme"', "acme}", "acme|x", "acme`", "acme x", "acme\\", "acme=~", 'acme"} or {x=~".+',
    "ténant", "acme.", "acme/..", "acme%22", "*", ".+", "acme;drop", "acme\t",
]


@pytest.mark.parametrize("tenant", HOSTILE_TENANTS)
def test_logs_hostile_dev_tenant_is_401_and_never_queried(pg, logs, tenant):
    client, _ = pg
    raw = f"Bearer dev:{tenant}:alice".encode("utf-8")
    r = client.get("/api/v1/logs", headers={"Authorization": raw})
    assert r.status_code == 401, (tenant, r.status_code)
    assert logs.calls == []


def test_logs_non_latin1_and_extra_colon_tenants_are_401(pg, logs):
    client, _ = pg
    for raw in (
        "Bearer dev:ｔｅｎａｎｔ:alice".encode(),  # fullwidth, UTF-8 bytes on the wire
        b"Bearer dev:acme:alice:extra",
        b"Bearer dev:acme:",
        b"Bearer dev::alice",
        b"Bearer  dev:acme:alice",
        b"bearer dev:acme:alice",
    ):
        assert client.get("/api/v1/logs", headers={"Authorization": raw}).status_code == 401, raw
    assert logs.calls == []


@pytest.mark.parametrize(
    "tenant",
    HOSTILE_TENANTS + ["acme\n", "acme\r", "acme\x00", "\nacme", "acme ", "acme\u0085", "ac​me", "аcme"],
)
def test_dev_principal_never_yields_a_tenant_outside_the_safe_charset(tenant):
    """The dependency itself (headers a client library would refuse to send included): it either
    refuses with 401, or the tenant it binds is [A-Za-z0-9_-]+ in full, so no hostile tenant can
    ever reach build_logql."""
    try:
        principal = asyncio.run(current_principal(f"Bearer dev:{tenant}:alice"))
    except HTTPException as exc:
        assert exc.status_code == 401
        return
    assert re.fullmatch(r"[A-Za-z0-9_-]+", principal.tenant_id), repr(principal.tenant_id)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", principal.user_id)


# --------------------------------------------------------------------------- 2. RE2 limits


def _blocklist(*patterns: str, action: str = "block") -> dict:
    return {"type": "regex_blocklist", "config": {"patterns": list(patterns), "action": action}}


def _console(client, headers, processors, sample):
    return client.post(TEST_URL, json={"processors": processors, "text": sample}, headers=headers)


def _versions(app, agent_id: str) -> int:
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM agent_versions WHERE agent_id = :a"), {"a": agent_id}).scalar_one()


def _runs(app, tenant: str) -> int:
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def test_pattern_count_and_length_limits_at_and_over_the_boundary(pg, tenant_of):
    """20 patterns of 200 characters are accepted by the console, PATCH and publish; 21 patterns or
    a 201-character pattern are 422 at every one of them, and nothing is stored."""
    client, app = pg
    _, headers = tenant_of()
    assert (MAX_REGEX_PATTERNS, MAX_REGEX_PATTERN_LENGTH) == (20, 200)
    longest = [f"w{i:02d}" + "x" * (MAX_REGEX_PATTERN_LENGTH - 3) for i in range(MAX_REGEX_PATTERNS)]
    assert all(len(p) == 200 for p in longest)
    # Program size: "w00xxx…" is a literal string; 200 characters stays under the cap.
    assert all(re2_pattern_error(p) is None for p in longest)
    ok = _console(client, headers, [_blocklist(*longest)], "no match here")
    assert ok.status_code == 200 and ok.json()["blocked"] is False

    too_many = [*longest, "extra"]
    too_long = ["y" * (MAX_REGEX_PATTERN_LENGTH + 1)]
    for patterns in (too_many, too_long, [""]):
        assert _console(client, headers, [_blocklist(*patterns)], "x").status_code == 422

    agent_id = make_agent(client, headers, publish=False)
    good = {**SPEC, "input_processors": [_blocklist(*longest)], "output_processors": [_blocklist(*longest)]}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": good}, headers=headers).status_code == 200
    for patterns in (too_many, too_long):
        for phase in ("input_processors", "output_processors"):
            bad = {**SPEC, phase: [_blocklist(*patterns)]}
            assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": bad}, headers=headers).status_code == 422
            assert client.post("/api/v1/agents", json={"spec": bad}, headers=headers).status_code == 422
    # The draft still holds the good spec: it publishes.
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    assert _versions(app, agent_id) == 1


INVALID_RE2 = [
    r"(a)\1",  # backreference
    r"(?P<n>a)(?P=n)",  # named backreference
    r"a(?=b)",  # lookahead
    r"a(?!b)",  # negative lookahead
    r"(?<=a)b",  # lookbehind
    r"(?<!a)b",  # negative lookbehind
    r"(?>ab)",  # atomic group
    r"a*+",  # possessive
    r"(?(1)a|b)",  # conditional
    r"(?R)",  # recursion
    r"a\Z",  # \Z is not RE2
    r"[a-",  # unterminated class
    r"a{2,1}",  # bad repetition
    r"a{1001}",  # RE2 repetition limit (1000)
    r"x*",  # matches empty text
    r"\b",  # zero-width only
]


@pytest.mark.parametrize("pattern", INVALID_RE2)
def test_invalid_re2_is_refused_on_console_publish_draft_run_and_draft_turn(pg, tenant_of, fake_dbos, pattern):
    """The same pattern is 422 on all four paths; a draft may hold it (PATCH only bounds count and
    length), but it is never published, run or enqueued."""
    client, app = pg
    tenant, headers = tenant_of()
    assert re2_pattern_error(pattern) is not None

    console = _console(client, headers, [_blocklist(pattern)], "ab" * 10)
    assert console.status_code == 422 and "processors[0].config.patterns[0]" in console.json()["detail"]

    agent_id = make_agent(client, headers)  # v1 is clean
    conv = new_conversation(client, headers, agent_id)
    for phase in ("input_processors", "output_processors"):
        bad = {**SPEC, "instructions": f"phase {phase}", phase: [_blocklist("fine", pattern)]}
        assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": bad}, headers=headers).status_code == 200
        published = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
        assert published.status_code == 422 and published.json()["detail"].startswith(f"{phase}[0].config.patterns[1]: ")
        r = start(client, headers, agent_id, version="draft")
        assert r.status_code == 422 and f"{phase}[0].config.patterns[1]" in r.json()["detail"]
        t = turn(client, headers, conv, version="draft")
        assert t.status_code == 422 and f"{phase}[0].config.patterns[1]" in t.json()["detail"]
    assert _versions(app, agent_id) == 1
    assert _runs(app, tenant) == 0
    assert fake_dbos.calls == []


PATHOLOGICAL = [
    (r"(a+)+$", "a" * 32767 + "!"),
    (r"(x*)*y", "x" * 32768),
    (r"(a|aa)*c", "a" * 32768),
    (r"(a|a)*b", "a" * 32768),
    (r"(\w+\s?)+$", "ab " * 10922 + "!"),
    (r"(.*a){12}", "a" * 32768),
    (r"a+b|a", "a" * 32768),  # O(n * matches) when finding every match
    (r"(?s).*.*.*=.*", "x" * 32768),
]


@pytest.mark.parametrize(("pattern", "sample"), PATHOLOGICAL)
@pytest.mark.parametrize("action", ["block", "redact"])
def test_pathological_patterns_on_a_32_kib_console_sample_answer_in_bounded_time(pg, tenant_of, pattern, sample, action):
    """Every classic ReDoS shape either compiles under the program-size cap and answers well within
    the console's 2 s budget (plus one search), or is refused as too complex. Outcomes carry no text."""
    client, _ = pg
    _, headers = tenant_of()
    assert len(sample.encode()) <= 32 * 1024
    started = time.perf_counter()
    r = _console(client, headers, [_blocklist(pattern, action=action)], sample)
    elapsed = time.perf_counter() - started
    assert elapsed < 3.0, (pattern, action, elapsed)
    if r.status_code == 422:
        assert "too complex" in r.json()["detail"], r.text
        return
    assert r.status_code == 200, r.text
    (result,) = r.json()["results"]
    assert set(result) <= {"index", "processor_type", "action", "match_counts", "reason"}
    assert set(result["match_counts"]) <= {"pattern_0"}
    assert all(type(v) is int for v in result["match_counts"].values())
    assert result.get("reason") in (None, "budget_exceeded", "too_many_matches")
    if r.json()["blocked"]:
        assert r.json()["text"] is None


def test_console_redaction_match_cap_at_and_over_the_boundary(pg, tenant_of):
    client, _ = pg
    _, headers = tenant_of()
    assert MAX_REDACTED_MATCHES == 1000
    at = _console(client, headers, [_blocklist("q7", action="redact")], "q7 " * 1000)
    assert at.status_code == 200 and at.json()["blocked"] is False
    assert at.json()["results"][0]["match_counts"] == {"pattern_0": 1000}
    assert "q7" not in at.json()["text"]
    over = _console(client, headers, [_blocklist("q7", action="redact")], "q7 " * 1001)
    assert over.status_code == 200 and over.json()["blocked"] is True and over.json()["text"] is None
    assert over.json()["results"] == [
        {"index": 0, "processor_type": "regex_blocklist", "action": "block", "match_counts": {}, "reason": "too_many_matches"}
    ]


def test_console_outcomes_never_contain_matched_text(pg, tenant_of):
    client, _ = pg
    _, headers = tenant_of()
    secret = "Zk7SecretQx"
    email = "zk7.secret@example.org"
    chain = [
        {"type": "pii_redact", "config": {"action": "redact"}},
        _blocklist(secret, r"Zk7\w+", action="redact"),
        {"type": "max_length", "config": {"max_chars": 10_000, "action": "truncate"}},
    ]
    r = _console(client, headers, chain, f"{secret} and {email} and {secret}")
    assert r.status_code == 200
    body = r.json()
    assert secret not in body["text"] and email not in body["text"]
    results = json.dumps(body["results"])
    for fragment in (secret, email, "Zk7", "example.org"):
        assert fragment not in results
    blocked = _console(client, headers, [_blocklist(secret)], f"x {secret} y")
    assert blocked.json()["text"] is None and secret not in blocked.text


def _program_size(pattern: str) -> int:
    import re2

    from eap_processors.regex import _options

    compiled = re2.compile(pattern, _options())
    return max(compiled.programsize, compiled.reverseprogramsize)


@pytest.mark.parametrize("shape", ["a{%d}", "[a-c]x{%d}", "(?:ab){%d}"])
def test_program_size_cap_is_inclusive_at_the_exact_boundary(shape):
    """Search the repetition count for the largest program at or under MAX_PROGRAM_SIZE and the next
    one over it: the first compiles, the second is refused naming its size."""
    assert MAX_PROGRAM_SIZE == 1000
    under = over = None
    for n in range(1, 1001):
        size = _program_size(shape % n)
        if size <= MAX_PROGRAM_SIZE:
            under = (n, size)
        else:
            over = (n, size)
            break
    assert under is not None and over is not None and over[0] == under[0] + 1
    assert re2_pattern_error(shape % under[0]) is None
    error = re2_pattern_error(shape % over[0])
    assert error == (
        f"is too complex (RE2 program size {over[1]}, at most {MAX_PROGRAM_SIZE}); use smaller "
        "repetition counts or narrower character classes"
    )


# --------------------------------------------------------------------------- 3. reserved names


def _agent_with_context(client, headers, spec: dict | None = None) -> str:
    r = client.post("/api/v1/agents", json={"spec": spec or CONTEXT_SPEC}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


@pytest.mark.parametrize("name", sorted(RESERVED_CONTEXT_NAMES) + LOOKALIKES)
def test_reserved_and_lookalike_context_field_names_are_refused_at_create_and_patch(pg, tenant_of, name):
    client, app = pg
    _, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    before = client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["draft_spec"]
    fields = [*CONTEXT_SPEC["context_fields"], {"name": name, "type": "string"}]
    spec = {**CONTEXT_SPEC, "context_fields": fields}
    created = client.post("/api/v1/agents", json={"spec": spec}, headers=headers)
    assert created.status_code == 422, (name, created.text[:300])
    patched = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=headers)
    assert patched.status_code == 422, (name, patched.text[:300])
    assert client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["draft_spec"] == before
    assert _versions(app, agent_id) == 1


@pytest.mark.parametrize("name", sorted(RESERVED_CONTEXT_NAMES))
def test_a_reserved_field_planted_in_the_stored_draft_is_never_published_or_run(pg, tenant_of, fake_dbos, name):
    """The draft row tampered with in SQL (PATCH would refuse it): publish and a draft run refuse
    it with a 4xx, and no version, run or enqueue happens."""
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    spec = {**CONTEXT_SPEC, "instructions": f"Hi {{{{context.{name}}}}}",
            "context_fields": [*CONTEXT_SPEC["context_fields"], {"name": name, "type": "string"}]}
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE agents SET draft_spec = CAST(:s AS jsonb) WHERE id = :a"),
                     {"s": json.dumps(spec), "a": agent_id})
    r = start(client, headers, agent_id, version="draft", request_context={"customer": "Ada", name: "x"})
    assert r.status_code == 422, r.text[:300]
    assert r.json()["detail"].startswith("The draft is not a valid agent spec (context_fields.2.name: ")
    # Publish answers exactly as the draft run does (one shared helper, validate_stored_draft).
    published = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert published.status_code == 422, published.text[:300]
    assert published.json()["detail"].startswith("The draft is not a valid agent spec (context_fields.2.name: ")
    assert _versions(app, agent_id) == 1 and _runs(app, tenant) == 0 and fake_dbos.calls == []


def _ctx_paths(client, headers, agent_id: str, conv: str, ctx: Any, version: Any = None):
    """(name, response) of every way a request context reaches the API."""
    yield "run", start(client, headers, agent_id, version=version, request_context=ctx)
    yield "turn", turn(client, headers, conv, version=version, request_context=ctx)
    yield "validate", client.post(
        f"/api/v1/agents/{agent_id}/request-context/validate",
        json={"version": version, "request_context": ctx}, headers=headers,
    )


@pytest.mark.parametrize("version", [None, "draft"])
@pytest.mark.parametrize("name", sorted(RESERVED_CONTEXT_NAMES))
def test_reserved_request_context_keys_are_422_on_run_turn_and_validate(pg, tenant_of, fake_dbos, version, name):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    conv = new_conversation(client, headers, agent_id)
    spoof = f"t_spoof_{uuid.uuid4().hex[:6]}"
    for path, r in _ctx_paths(client, headers, agent_id, conv, {**OK_CONTEXT, name: spoof}, version):
        assert r.status_code == 422, (path, r.status_code, r.text[:300])
        assert r.json()["detail"] == [
            {"loc": ["body", "request_context", name], "msg": "is reserved and cannot be supplied", "type": "reserved_name"}
        ], path
        assert spoof not in r.text
    assert _runs(app, tenant) == 0 and fake_dbos.calls == []


@pytest.mark.parametrize("key", LOOKALIKES)
def test_lookalike_request_context_keys_are_unknown_fields_on_every_path(pg, tenant_of, fake_dbos, key):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    conv = new_conversation(client, headers, agent_id)
    for path, r in _ctx_paths(client, headers, agent_id, conv, {**OK_CONTEXT, key: "spoofed-tenant"}):
        assert r.status_code == 422, (path, key, r.status_code, r.text[:300])
        (item,) = r.json()["detail"]
        assert item["type"] == "extra_forbidden" and item["loc"][:2] == ["body", "request_context"], (path, item)
        assert "spoofed-tenant" not in r.text
    assert _runs(app, tenant) == 0 and fake_dbos.calls == []


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "principal", "tenant", "agent_id", "conversation_id"])
def test_identity_cannot_be_smuggled_as_top_level_body_fields_or_nested_values(pg, tenant_of, fake_dbos, field):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    conv = new_conversation(client, headers, agent_id)
    body = {"run_id": str(uuid.uuid4()), "input": "hi", "request_context": OK_CONTEXT, field: "t_other"}
    assert client.post(f"/api/v1/agents/{agent_id}/runs", json=body, headers=headers).status_code == 422
    assert client.post(f"/api/v1/conversations/{conv}/runs", json=body, headers=headers).status_code == 422
    validate = client.post(f"/api/v1/agents/{agent_id}/request-context/validate",
                           json={"request_context": OK_CONTEXT, field: "t_other"}, headers=headers)
    assert validate.status_code == 422
    # Nested inside a declared field's value: a type error, never a key the worker could read.
    nested = {"customer": {field: "t_other"}}
    for path, r in _ctx_paths(client, headers, agent_id, conv, nested):
        assert r.status_code == 422, path
        assert r.json()["detail"][0]["loc"] == ["body", "request_context", "customer"]
    assert _runs(app, tenant) == 0 and fake_dbos.calls == []


def test_accepted_context_is_stored_as_declared_fields_and_the_handoff_tenant_is_the_principal(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    run_id = str(uuid.uuid4())
    r = start(client, headers, agent_id, run_id=run_id, request_context={"customer": "Ada", "plan": None})
    assert r.status_code == 201, r.text
    with app.state.database_engine.connect() as conn:
        row = conn.execute(text("SELECT tenant_id, request_context FROM runs WHERE id = :r"), {"r": run_id}).one()
    assert row[0] == tenant and row[1] == {"customer": "Ada"}
    ((_, args),) = fake_dbos.calls
    assert args[0] == run_id and args[1] == tenant


PLACEHOLDERS = [
    "{{context.tenant_id}}", "{{context.user_id}}", "{{context.run_id}}", "{{ context.tenant_id }}",
    "{{context.Tenant_id}}", "{{context.tenant_id }}x", "{{context.tenant-id}}", "{{context.tenant_id.x}}",
    "{{context.__class__}}", "{{context['tenant_id']}}", "{{context.tenant_id|upper}}", "{{context.тenant_id}}",
]


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
def test_identity_placeholders_are_refused_at_publish_and_draft_run(pg, tenant_of, fake_dbos, placeholder):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    spec = {**CONTEXT_SPEC, "instructions": f"Act for {placeholder} now."}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=headers).status_code == 200
    published = client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers)
    assert published.status_code == 422, (placeholder, published.text[:300])
    assert published.json()["detail"].startswith("instructions: ")
    r = start(client, headers, agent_id, version="draft", request_context=OK_CONTEXT)
    assert r.status_code == 422 and r.json()["detail"].startswith("instructions: ")
    assert _versions(app, agent_id) == 1 and _runs(app, tenant) == 0 and fake_dbos.calls == []


@pytest.mark.parametrize("text_", ["{{CONTEXT.tenant_id}}", "{context.tenant_id}", "{{ context_tenant_id }}", "{{ctx.tenant_id}}"])
def test_non_placeholder_lookalikes_publish_but_are_never_substituted(pg, tenant_of, text_):
    from eap_schemas import AgentSpec
    from eap_schemas.request_context import placeholders, render_instructions

    client, _ = pg
    _, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    spec = {**CONTEXT_SPEC, "instructions": f"Act for {text_}."}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=headers).status_code == 200
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    parsed = AgentSpec.model_validate(spec)
    assert placeholders(parsed.instructions) == ()
    rendered = render_instructions(parsed.instructions, {"customer": "Ada", "tenant_id": "t_evil"}, parsed.context_fields)
    assert rendered == parsed.instructions and "t_evil" not in rendered


# --------------------------------------------------------------------------- 4. request guard on P0 routes


def _routes(client, app, headers) -> tuple[list[tuple[str, str]], str, str]:
    """(method, path) of the new P0 guarded write routes, with real ids of this tenant."""
    agent_id = _agent_with_context(client, headers)
    v2 = {**CONTEXT_SPEC, "instructions": "Help {{context.customer}} again."}
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"spec": v2}, headers=headers).status_code == 200
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    conv = new_conversation(client, headers, agent_id, title="original title")
    return [
        ("POST", TEST_URL),
        ("POST", f"/api/v1/agents/{agent_id}/request-context/validate"),
        ("PATCH", f"/api/v1/conversations/{conv}"),
        ("POST", f"/api/v1/agents/{agent_id}/versions/1/restore"),
        ("POST", f"/api/v1/agents/{agent_id}/runs"),
        ("POST", f"/api/v1/conversations/{conv}/runs"),
    ], agent_id, conv


def _state(app, tenant: str, agent_id: str, conv: str) -> tuple[Any, ...]:
    with app.state.database_engine.connect() as conn:
        title = conn.execute(text("SELECT title FROM conversations WHERE id = :c"), {"c": conv}).scalar_one()
        events = conn.execute(text("SELECT count(*) FROM execution_events WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    return (_runs(app, tenant), _versions(app, agent_id), title, events)


def test_oversize_bodies_on_p0_routes_are_413_and_store_nothing(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, headers = tenant_of()
    routes, agent_id, conv = _routes(client, app, headers)
    before = _state(app, tenant, agent_id, conv)
    auth = headers["Authorization"]
    huge_ctx = json.dumps({"run_id": str(uuid.uuid4()), "input": "hi",
                           "request_context": {"customer": "x" * BODY_CAP}}).encode()
    for method, path in routes:
        # Declared Content-Length over the cap: 413 before a byte of the body is read.
        status, body, seen = _asgi(app, method, path, [b"{}"], _headers(auth=auth, content_length=BODY_CAP + 1))
        assert (status, json.loads(body), seen) == (413, {"detail": TOO_LARGE}, []), (method, path)
        # Chunked, no Content-Length: stops at the chunk that crosses the cap, never reads the next.
        half = BODY_CAP // 2
        status, body, seen = _asgi(
            app, method, path, [b" " * half, b" " * (BODY_CAP - half + 1), b"NEVER-READ"], _headers(auth=auth)
        )
        assert (status, json.loads(body), seen) == (413, {"detail": TOO_LARGE}, ["body", "body"]), (method, path)
        # Through the HTTP client: a huge request_context (and a declared length that lies low is
        # impossible here: the client sets it from the bytes).
        r = client.request(method, path, content=huge_ctx, headers={**headers, "content-type": "application/json"})
        assert (r.status_code, r.json()) == (413, {"detail": TOO_LARGE}), (method, path)
        anon = client.request(method, path, content=huge_ctx, headers={"content-type": "application/json"})
        assert anon.status_code == 413, (method, path)  # size is judged before authentication
    assert _state(app, tenant, agent_id, conv) == before
    assert fake_dbos.calls == []


def test_request_context_over_8_kib_but_under_the_body_cap_is_422_and_stores_nothing(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, headers = tenant_of()
    agent_id = _agent_with_context(client, headers)
    conv = new_conversation(client, headers, agent_id)
    # A declared field is capped by its max_length; the 8 KiB total applies before any field check.
    for ctx in ({"customer": "x" * (200 * 1024)}, {**OK_CONTEXT, **{f"k{i}": "v" * 100 for i in range(100)}}):
        for path, r in _ctx_paths(client, headers, agent_id, conv, ctx):
            assert r.status_code == 422, (path, r.status_code)
            assert r.json()["detail"] == [
                {"loc": ["body", "request_context"], "msg": "request context must be at most 8192 bytes", "type": "too_large"}
            ], path
            assert len(r.content) < 1000
    assert _runs(app, tenant) == 0 and fake_dbos.calls == []


def test_deep_and_non_utf8_bodies_on_p0_routes(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, headers = tenant_of()
    routes, agent_id, conv = _routes(client, app, headers)
    before = _state(app, tenant, agent_id, conv)
    authed = {**headers, "content-type": "application/json"}
    payloads = {
        "utf-16": json.dumps({"text": "x"}).encode("utf-16"),
        "latin-1": '{"text": "café"}'.encode("latin-1"),
        "raw NUL": b'{"text": "a\x00b"}',
        "overlong": b'{"text": "\xc0\xaf"}',
        "encoded surrogate": b'{"text": "\xed\xa0\x80"}',
    }
    for method, path in routes:
        for label, payload in payloads.items():
            r = client.request(method, path, content=payload, headers=authed)
            assert (r.status_code, r.json()) == (415, {"detail": NOT_UTF8}), (method, path, label)
        deep = client.request(method, path, content=_nested(33), headers=authed)
        assert (deep.status_code, deep.json()) == (422, {"detail": DEPTH}), (method, path)
        # Exactly the limit is not the depth error: ordinary validation (a compact list) instead.
        exact = client.request(method, path, content=_nested(32), headers=authed)
        assert exact.status_code == 422 and isinstance(exact.json()["detail"], list), (method, path)
        # Escaped NUL / lone surrogate in a string the route would store: compact 422.
        for poison in ("a\x00b", "c\ud800d"):
            body = {"text": poison, "title": poison, "request_context": {"customer": poison},
                    "run_id": str(uuid.uuid4()), "input": poison, "processors": [{"type": "pii_redact"}]}
            r = client.request(method, path, content=json.dumps(body).encode(), headers=authed)
            assert r.status_code == 422, (method, path, poison)
            assert all(set(i) == {"loc", "msg", "type"} for i in r.json()["detail"])
            assert poison not in r.text
    assert _state(app, tenant, agent_id, conv) == before
    assert fake_dbos.calls == []


GUARDED_GETS = [
    ("/api/v1/metrics/summary?model=%00", ["query", "model"]),
    ("/api/v1/metrics/timeseries?provider=a%00", ["query", "provider"]),
    ("/api/v1/metrics/breakdown?by=agent&x%00y=1", ["query", "x?y"]),
    ("/api/v1/processor-results?before=%00", ["query", "before"]),
    ("/api/v1/processor-results?type=pii_redact&q=%00", ["query", "q"]),
]


@pytest.mark.parametrize(("url", "loc"), GUARDED_GETS)
def test_nul_in_query_strings_of_guarded_p0_reads_is_a_compact_422_after_auth(pg, tenant_of, url, loc):
    client, _ = pg
    _, headers = tenant_of()
    r = client.get(url, headers=headers)
    assert r.status_code == 422, r.text[:300]
    detail = r.json()["detail"]
    assert any(item["loc"] == loc and item["type"] == "string_unstorable" for item in detail), detail
    assert "\x00" not in r.text
    assert client.get(url).status_code == 401


@pytest.mark.parametrize(
    "url",
    [
        "/api/v1/metrics/summary?model=a%ED%A0%80b",
        "/api/v1/metrics/summary?model=%F0%9F%98",
        "/api/v1/metrics/breakdown?by=model&provider=%C0%AF",
        "/api/v1/processor-results?before=%ED%B0%80",
        "/api/v1/processor-results?type=%ED%A0%80",
    ],
)
def test_invalid_utf8_and_encoded_surrogates_in_guarded_reads_are_never_5xx(pg, tenant_of, url):
    """Starlette decodes percent-escapes with errors='replace', so an encoded surrogate arrives as
    U+FFFD: storable text. The route answers normally (200, or 422 for a value it does not accept)."""
    client, _ = pg
    _, headers = tenant_of()
    r = client.get(url, headers=headers)
    assert r.status_code in (200, 422), (url, r.status_code, r.text[:300])


def test_unguarded_p0_routers_handle_nul_and_bad_utf8_query_strings_safely(pg, tenant_of, logs):
    """logs, models and traces are not on the guard. Recorded behaviour: NUL in `q` is escaped into
    the LogQL literal (never raw); NUL in a UUID parameter is a 422; unknown parameters (NUL
    included) are ignored; nothing is a 5xx and nothing echoes the NUL."""
    client, app = pg
    tenant, headers = tenant_of()
    r = client.get("/api/v1/logs?q=a%00b", headers=headers)
    assert r.status_code == 200
    skeleton, literals = split_logql(logs.calls[-1]["query"])
    assert skeleton == expected_skeleton(q=True) and literals[-1] == "a\x00b"
    assert "\x00" not in logs.calls[-1]["query"]
    r = client.get("/api/v1/logs?q=a%ED%A0%80b", headers=headers)
    assert r.status_code == 200 and split_logql(logs.calls[-1]["query"])[1][-1] == "a���b"
    for url in ("/api/v1/logs?run_id=%00", "/api/v1/logs?from=%00", "/api/v1/logs?level=%00", "/api/v1/logs?limit=%00"):
        r = client.get(url, headers=headers)
        assert r.status_code == 422 and "\x00" not in r.text, url
    calls = len(logs.calls)
    r = client.get("/api/v1/logs?%00=%00&q%00=x", headers=headers)
    assert r.status_code == 200 and len(logs.calls) == calls + 1
    assert logs.calls[-1]["query"] == f'{{service_name=~"eap-api|eap-worker"}} | eap_tenant_id="{tenant}"'

    for url in ("/api/v1/models?x=%00", "/api/v1/models?%00=%ED%A0%80"):
        r = client.get(url, headers=headers)
        assert r.status_code == 200 and "\x00" not in r.text, url
    unknown = uuid.uuid4()
    for url, expected in (
        (f"/api/v1/runs/{unknown}/trace?x=%00", 404),
        (f"/api/v1/runs/{unknown}/trace?%ED%A0%80=1", 404),
        ("/api/v1/runs/%00/trace", 422),
        (f"/api/v1/runs/{str(unknown)[:-1]}%00/trace", 422),
        ("/api/v1/runs/%ED%A0%80/trace", 422),
    ):
        r = client.get(url, headers=headers)
        assert r.status_code == expected and "\x00" not in r.text, (url, r.status_code)
