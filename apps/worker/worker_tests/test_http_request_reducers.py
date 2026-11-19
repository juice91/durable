"""`http.request` reducers (response_query, extract_html, parse_feed): the body is reduced inside the tool and only
the reduction is recorded. Same no-network setup as test_http_request_tool."""

from __future__ import annotations

import json

import httpx2
import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, HttpRequestInput

from worker_tests.test_http_request_tool import PASSWORD, call, sync, wire  # noqa: F401  (the fixture)

FEED = {"feed": [
    {"post": {"uri": f"at://x/{i}", "record": {"text": f"post {i} " + "filler " * 300}}, **({"reason": {"x": 1}} if i % 3 == 0 else {})}
    for i in range(30)
]}
BIG_JSON = json.dumps(FEED)
ARTICLE = "<html><head><title>T</title></head><body><article><p>" + ("Hello readable world. " * 40) + "</p></article></body></html>"
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>c</title>
<item><title>One</title><link>https://example.com/1</link><description>a</description><pubDate>Wed, 18 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>Two</title><link>https://example.com/2</link><description>b</description><pubDate>Wed, 18 Nov 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""
QUERY = {"expression": "feed[?!reason].post.{uri: uri}"}


def answer(wire, status=200, **kw):
    wire.answer = lambda r: httpx2.Response(status, **kw)


@sync
async def test_response_query_replaces_json_and_the_raw_body_is_not_recorded(wire):
    assert len(BIG_JSON) > 32 * 1024
    answer(wire, content=BIG_JSON, headers={"content-type": "application/json"})
    out = await call(response_query=QUERY, max_response_bytes=1_048_576)
    assert out["status"] == 200 and out["ok"] and "elapsed_ms" in out and "text" not in out
    assert len(out["json"]) == 20 and out["json"][0] == {"uri": "at://x/1"}
    assert out["reduced"] == {"kind": "response_query", "input_bytes": len(BIG_JSON)}
    assert "filler" not in json.dumps(out) and len(json.dumps(out)) < 32 * 1024


@sync
async def test_drop_response_paths_applies_before_the_query(wire):
    answer(wire, json={"a": {"secret": 1, "keep": 2}})
    out = await call(response_query={"expression": "a"}, drop_response_paths=["/a/secret"])
    assert out["json"] == {"keep": 2}


@sync
async def test_response_query_errors_carry_the_request_status(wire):
    answer(wire, json={"a": 1})
    out = await call(response_query={"expression": "a[["})
    assert out["error"]["code"] == "expression_invalid" and out["request"] == {"status": 200}
    answer(wire, text="not json")
    out = await call(response_query={"expression": "a"})
    assert out["error"]["code"] == "response_not_json" and out["request"] == {"status": 200}


@sync
async def test_extract_html_replaces_text(wire):
    answer(wire, content=ARTICLE, headers={"content-type": "text/html"})
    out = await call(extract_html={"mode": "article", "max_graphemes": 100})
    assert "json" not in out and out["reduced"]["kind"] == "extract_html" and out["reduced"]["input_bytes"] == len(ARTICLE)
    assert out["text"]["graphemes"] <= 100 and out["text"]["text"].startswith("Hello")
    assert "<p>" not in json.dumps(out)


@sync
async def test_extract_html_base_url_defaults_to_the_request_url_and_errors_are_the_tools_own(wire):
    page = '<html><body><main><a href="/story/1">One</a></main></body></html>'
    answer(wire, content=page, headers={"content-type": "text/html"})
    out = await call(extract_html={"mode": "links", "include_patterns": [r"^https://api\.example\.com/story/\d+"]})
    assert out["text"]["items"][0]["url"] == "https://api.example.com/story/1"
    out = await call(extract_html={"mode": "links"})
    assert out["error"]["code"] == "no_patterns" and out["request"] == {"status": 200}
    answer(wire, text="plain text")
    out = await call(extract_html={"mode": "article"})
    assert out["error"]["code"] == "html_unparseable" and out["request"]["status"] == 200


@sync
async def test_parse_feed_replaces_text_with_exclude_urls(wire):
    answer(wire, content=RSS, headers={"content-type": "application/rss+xml"})
    out = await call(parse_feed={"exclude_urls": ["https://example.com/1"], "label": "Src"})
    assert out["reduced"] == {"kind": "parse_feed", "input_bytes": len(RSS)}
    assert [i["link"] for i in out["text"]["items"]] == ["https://example.com/2"]
    assert out["text"]["items"][0]["source"] == "Src" and "<rss" not in json.dumps(out)
    answer(wire, text="<nope")
    out = await call(parse_feed={})
    assert out["error"]["code"] == "feed_invalid" and out["request"] == {"status": 200}


@sync
async def test_the_cap_goes_up_only_with_a_reducer(wire):
    answer(wire, content=BIG_JSON, headers={"content-type": "application/json"})
    assert (await call(max_response_bytes=500_000))["error"]["code"] == "max_response_bytes_too_large"
    assert (await call(response_query=QUERY, max_response_bytes=100))["error"]["code"] == "response_too_large"
    assert "json" in await call(response_query=QUERY, max_response_bytes=1_048_576)
    with pytest.raises(ValidationError):
        HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", "max_response_bytes": 500_000})
    ok = HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", "max_response_bytes": 1_048_576,
                                          "response_query": {"expression": "a"}})
    assert ok.response_query.expression == "a"
    with pytest.raises(ValidationError):
        HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", "max_response_bytes": 1_048_577,
                                         "response_query": {"expression": "a"}})


@sync
async def test_non_2xx_applies_the_reducer_when_parseable_else_a_4k_text(wire):
    answer(wire, status=429, json={"error": "slow", "extra": 1})
    out = await call(response_query={"expression": "error"})
    assert out["status"] == 429 and out["ok"] is False and out["json"] == "slow" and out["reduced"]["kind"] == "response_query"
    body = "<html>" + "x" * 50_000
    answer(wire, status=503, content=body, headers={"content-type": "text/html"})
    out = await call(parse_feed={}, max_response_bytes=100_000)
    assert out["status"] == 503 and "error" not in out and "reduced" not in out
    assert len(out["text"].encode()) <= 4096 and out["truncated"] is True
    out = await call(response_query={"expression": "a"})
    assert len(out["text"].encode()) <= 4096 and "json" not in out


@sync
async def test_two_reducers_are_a_conflict_and_nothing_is_sent(wire):
    out = await call(response_query=QUERY, parse_feed={})
    assert out["error"]["code"] == "reducer_conflict"
    out = await call(extract_html={"mode": "article"}, parse_feed={})
    assert out["error"]["code"] == "reducer_conflict"
    assert wire.requests == []


@sync
async def test_a_secret_in_the_body_never_reaches_the_reduction(wire):
    answer(wire, json={"items": [{"t": f"token {PASSWORD}"}]})
    out = await call(headers={"Authorization": "Bearer secret:pwd-main"}, response_query={"expression": "items[].t"})
    assert PASSWORD not in json.dumps(out)


def test_the_declaration_describes_the_reducers():
    text = TOOL_DECLARATIONS["http.request"].description
    for needle in ("32 KiB", "response_query", "extract_html", "parse_feed", "exclude_urls", "reducer_conflict", "1048576"):
        assert needle in text
