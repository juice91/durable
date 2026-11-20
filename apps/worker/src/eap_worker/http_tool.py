"""`http.request`: one HTTPS request from a run or a workflow step.

The platform's general HTTP tool: a workflow can call a JSON API with a credential, an agent can too
(behind an approval when it names a secret).

- Arguments: `eap_schemas.tools.HttpRequestInput`. The tool itself answers the stable codes
  `url_not_https` and `body_conflict` (a result, so an agent run and a workflow step see the same).
- Secrets: a string equal to `secret:<name>` in headers, query, json or body, or containing one (headers
  only: `Bearer secret:<name>`), is replaced by the tenant's stored value inside this call, right before the
  request is sent (the same store the MCP and built-in tool paths read, eap_worker.secret_refs). The recorded
  arguments, events, spans and the step output keep the reference only. The response (headers, json, text) is
  redacted with `secret_variants` of the values used. An unknown reference is the result `secret_not_found`
  and nothing is sent. The run's SecretFunctionToolset leaves this tool alone (it resolves for itself).
- Outbound: the tenant `OutboundPolicy` through its `GuardedTransport` (https only, public addresses only,
  checked again at connect, redirects are followed when `max_redirects > 0`; each hop is checked like a new
  request and must stay on the same host or an allowed one).
- Result: `{status, ok, headers, json | text, truncated?, elapsed_ms, redirected_from?}`. A non-2xx status is
  a result. A transport failure is `{error: {code, message}}` with `outbound_blocked`, `http_timeout`,
  `http_connect_failed`, `secret_not_found`, `secret_unreadable`, `response_too_large`. The messages are fixed
  texts: never the URL, a header or an exception text.
- `max_redirects`: integer 0 to 5, default 0. When > 0, redirects (301/302/303/307/308) are followed up to the
  limit. Each hop is checked like a new request and must stay on the same host as the original URL or in
  `allowed_hosts` (when provided). Cross-host hops drop `Authorization` and `Cookie` headers. 303 and 301/302 on
  POST switch to GET without a body; 307/308 keep the method and body. The result gains `redirected_from` (the
  chain of URLs) only when at least one hop was followed.
- `body_from_url` (`HttpBodySource`: `url`, `max_bytes` default 1,000,000 and at most 5,000,000, `allowed_hosts`): the
  request body is fetched first, by a GET through the same guarded client (https only, public addresses only,
  redirects not followed: a 3xx is refused), from a host in `allowed_hosts` only, and must answer `image/*`; at
  most `max_bytes` are read (a longer body, or a larger content-length, is refused). Those bytes are the request
  body, with `Content-Type` set to the fetched one unless the caller set one. Any refusal is
  `{error: {code: "body_source_rejected", detail}}` with a fixed detail (never the URL), and nothing is sent.
  It excludes `json` and `body` (`body_conflict`). The main request's result is unchanged.
- Reducers (`response_query`, `extract_html`, `parse_feed`; at most one, else `reducer_conflict`): a workflow step's
  output is capped at 32 KiB, so one reducer shrinks the body in this tool and only its result is recorded (the raw
  body never is). With a reducer `max_response_bytes` may go up to 1 MiB (256 KiB without one:
  `max_response_bytes_too_large`). `response_query` is `json.query` over the JSON body (the result is `json`);
  `extract_html` is `html.extract` over the text (the extraction is `text`; `base_url` defaults to the request
  url); `parse_feed` is `feed.parse` over the text (the parsed result is `text`). The result keeps `status`, `ok`,
  `headers`, `elapsed_ms` and adds `reduced: {kind, input_bytes}`. A reducer failure is that tool's own
  `{error: {code, message}}` with `request: {status}` alongside (`response_not_json` when `response_query` met a
  body that is not JSON). A non-2xx status applies the reducer when the body allows it; otherwise the normal
  result with `text` cut to 4 KiB. `drop_response_paths` applies before `response_query`.
- Gating: an agent run's call that names a secret waits for an approval (`SpecCapability`,
  eap_worker.capabilities); a workflow tool step does not (the spec was published by a person).
- Architect notes: article pages often answer 301 to a canonical URL (trailing slash, www); set `max_redirects: 3`
  on page fetches to follow these redirects safely, with each hop checked like a new request and constrained to
  the same host or hosts listed in `allowed_hosts`.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import re
import time
from urllib.parse import urljoin, urlsplit
from collections.abc import Callable, Sequence
from typing import Any

import httpx2
from pydantic import Field
from pydantic_ai import RunContext
from pydantic_core import to_jsonable_python

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.http_request import EMBEDDED_REF, http_request_secret_names, http_request_summary
from eap_schemas.secrets import is_secret_ref
from eap_schemas.tools import (
    HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP,
    HTTP_MAX_RESPONSE_BYTES_CAP,
    TOOL_DECLARATIONS,
    FeedInput,
    HttpBodySource,
    HttpExtractHtml,
    HttpParseFeed,
    HttpResponseQuery,
)
from eap_secrets import SecretsError
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy
from eap_secrets.outbound_errors import walk_chain
from eap_secrets.redaction import mask_split_secrets, redact, secret_variants
from typing import Annotated, Literal

from eap_worker.feed_tool import feed_parse
from eap_worker.html_extract_tool import html_extract
from eap_worker.json_query_tool import json_query
from eap_worker.secret_refs import SecretRefNotFound, secret_store

log = logging.getLogger("eap_worker.http_tool")

TOOL_ID = "http.request"
RESPONSE_HEADERS = ("content-type", "content-length", "location", "retry-after", "x-request-id")
RESPONSE_HEADER_PREFIXES = ("ratelimit-",)
MAX_HEADER_VALUE_CHARS = 512
NON_2XX_TEXT_BYTES = 4096


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


# ------------------------------------------------------------------------------------- seams


def outbound_policy() -> OutboundPolicy:
    """The tenant outbound policy (private, link-local and metadata addresses blocked). A seam for tests."""
    return OutboundPolicy.from_env()


def new_client(policy: OutboundPolicy, timeout_s: float) -> httpx2.AsyncClient:
    """The guarded client: redirects are not followed, the proxy environment is ignored. A seam for tests
    (a mock transport)."""
    client = policy.http_client(timeout=httpx2.Timeout(timeout_s))
    client.headers["Accept-Encoding"] = "identity"  # raw bytes are the decoded bytes: the cap is exact
    return client


def secret_lookup(ctx: RunContext[Any]) -> Callable[[str], str | None]:
    """name -> the tenant's stored value or None (blocking). A seam for tests."""
    from eap_worker.runtime import get_runtime

    store = secret_store(get_runtime().engine)
    tenant_id = ctx.deps.tenant_id
    return lambda name: store.resolve(tenant_id, name)


# ------------------------------------------------------------------------------------- secrets


def _resolve_header(value: str, lookup: Callable[[str], str | None], used: list[str]) -> str:
    def sub(match: re.Match[str]) -> str:
        resolved = lookup(match.group(1))
        if resolved is None:
            raise SecretRefNotFound(match.group(0))
        used.append(resolved)
        return resolved

    return EMBEDDED_REF.sub(sub, value)


def _resolve_exact(value: Any, lookup: Callable[[str], str | None], used: list[str]) -> Any:
    if isinstance(value, str):
        name = is_secret_ref(value)
        if name is None:
            return value
        resolved = lookup(name)
        if resolved is None:
            raise SecretRefNotFound(value)
        used.append(resolved)
        return resolved
    if isinstance(value, dict):
        return {k: _resolve_exact(v, lookup, used) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_resolve_exact(v, lookup, used) for v in value]
    return value


def resolve_request(
    request: dict[str, Any], lookup: Callable[[str], str | None]
) -> tuple[dict[str, Any], list[str]]:
    """`request` ({headers, query, json, body}) with every reference replaced, and the values used (blocking)."""
    used: list[str] = []
    out = dict(request)
    if request.get("headers"):
        out["headers"] = {k: _resolve_header(v, lookup, used) for k, v in request["headers"].items()}
    for key in ("query", "json", "body"):
        if request.get(key) is not None:
            out[key] = _resolve_exact(request[key], lookup, used)
    return out, used


# ------------------------------------------------------------------------------------- response


def _failure_code(exc: BaseException) -> str:
    chain = list(walk_chain(exc))
    if any(isinstance(e, OutboundBlocked) for e in chain):
        return "outbound_blocked"
    if any(isinstance(e, (TimeoutError, httpx2.TimeoutException)) for e in chain):
        return "http_timeout"
    return "http_connect_failed"


_FAILURES = {
    "outbound_blocked": "the request was refused by the outbound policy",
    "http_timeout": "the request timed out",
    "http_connect_failed": "the connection could not be made",
}


def _kept_headers(headers: httpx2.Headers, variants: list[str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in RESPONSE_HEADERS or lowered.startswith(RESPONSE_HEADER_PREFIXES):
            kept[lowered] = redact(value[:MAX_HEADER_VALUE_CHARS], variants)
    return kept


async def _read(response: httpx2.Response, limit: int) -> tuple[bytes, bool]:
    """At most `limit` body bytes, and whether the body had more."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        chunks.append(chunk)
        if total > limit:
            return b"".join(chunks)[:limit], True
    return b"".join(chunks), False


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or media.endswith("+json")


def _text_window(raw: bytes, cap: int, more: bool, variants: list[str]) -> tuple[str, bool]:
    """The redacted text of `raw` cut to `cap` bytes: nothing that could be part of a secret straddling the
    cut is left (the last `width` characters of a longer body are dropped before the cut)."""
    text = raw.decode("utf-8", errors="replace")
    red = redact(text, variants)
    truncated = more or len(raw) > cap
    if variants and more:
        width = max(len(v) for v in variants) + 4
        red = red[: max(0, len(red) - width)]
    cut = red.encode("utf-8")
    if len(cut) > cap:
        cut = cut[:cap]
        truncated = True
    return cut.decode("utf-8", errors="ignore"), truncated


def drop_paths(value: Any, pointers: Sequence[str]) -> Any:
    """`value` without the members the JSON pointers (RFC 6901) name; a path that does not exist, or a value
    that is not an object or array, is left alone."""
    if not isinstance(value, (dict, list)):
        return value
    for pointer in pointers:
        tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
        parent: Any = value
        for token in tokens[:-1]:
            if isinstance(parent, dict):
                parent = parent.get(token)
            elif isinstance(parent, list) and token.isdigit() and int(token) < len(parent):
                parent = parent[int(token)]
            else:
                parent = None
                break
        last = tokens[-1] if tokens else None
        if last is None:
            continue
        if isinstance(parent, dict):
            parent.pop(last, None)
        elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
            del parent[int(last)]
    return value


Reducer = tuple[str, Callable[[Any], dict[str, Any]]]


def _reduce(kind: str, reduce: Callable[[Any], dict[str, Any]], raw: bytes, variants: list[str],
            drop: Sequence[str]) -> dict[str, Any]:
    """`{value}` or the reducer's own `{error}`."""
    if kind == "response_query":
        try:
            parsed = jsonlib.loads(raw.decode("utf-8"))
        except ValueError:
            return _error("response_not_json", "the response body is not JSON")
        outcome = reduce(redact(mask_split_secrets(drop_paths(parsed, drop), variants), variants))
        if "error" in outcome:
            return outcome
        return {"value": redact(mask_split_secrets(outcome["result"], variants), variants)}
    outcome = reduce(redact(raw.decode("utf-8", errors="replace"), variants))
    if "error" in outcome:
        return outcome
    return {"value": redact(mask_split_secrets(outcome, variants), variants)}


async def _answer(
    response: httpx2.Response, max_bytes: int, variants: list[str], started: float, drop: Sequence[str] = (),
    reducer: Reducer | None = None,
) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    width = (max(len(v) for v in variants) + 4) * 4 if variants else 0
    declared = response.headers.get("content-length")
    is_json = _is_json(content_type)
    ok = 200 <= response.status_code < 300
    if reducer is not None:
        if ok and declared and declared.isdigit() and int(declared) > max_bytes:
            return _error("response_too_large", "the response body is larger than max_response_bytes")
        raw, more = await _read(response, max_bytes)
        if ok and more:
            return _error("response_too_large", "the response body is larger than max_response_bytes")
        if not more:
            kind, reduce = reducer
            outcome = _reduce(kind, reduce, raw, variants, drop)
            if "error" not in outcome:
                reduced: dict[str, Any] = {
                    "status": response.status_code,
                    "ok": ok,
                    "headers": _kept_headers(response.headers, variants),
                    "reduced": {"kind": kind, "input_bytes": len(raw)},
                    "json" if kind == "response_query" else "text": outcome["value"],
                }
                reduced["elapsed_ms"] = int((time.monotonic() - started) * 1000)
                return reduced
            if ok:
                return {**outcome, "request": {"status": response.status_code}}
        # a non-2xx answer the reducer cannot read: the normal result, the body cut to 4 KiB
        text, truncated = _text_window(raw, NON_2XX_TEXT_BYTES, more, variants)
        result = {"status": response.status_code, "ok": ok, "headers": _kept_headers(response.headers, variants), "text": text}
        if truncated:
            result["truncated"] = True
        result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return result
    if is_json and declared and declared.isdigit() and int(declared) > max_bytes:
        return _error("response_too_large", "the response body is larger than max_response_bytes")
    raw, more = await _read(response, max_bytes + width + 1 if not is_json else max_bytes)
    result = {
        "status": response.status_code,
        "ok": ok,
        "headers": _kept_headers(response.headers, variants),
    }
    if is_json:
        if more:
            return _error("response_too_large", "the response body is larger than max_response_bytes")
        try:
            parsed = jsonlib.loads(raw.decode("utf-8"))
        except ValueError:
            pass  # not JSON after all: the text path below
        else:
            result["json"] = redact(mask_split_secrets(drop_paths(parsed, drop), variants), variants)
            result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            return result
    text, truncated = _text_window(raw, max_bytes, more, variants)
    result["text"] = text
    if truncated:
        result["truncated"] = True
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


# ------------------------------------------------------------------------------------- body source


def _rejected(detail: str) -> dict[str, Any]:
    return {"error": {"code": "body_source_rejected", "detail": detail}}


async def _fetch_source(source: HttpBodySource, timeout_s: int) -> tuple[bytes, str] | dict[str, Any]:
    """The bytes and content type of `source.url`, or a `body_source_rejected` result."""
    try:
        parts = urlsplit(source.url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return _rejected("the body source url is not valid")
    if parts.scheme != "https" or not host:
        return _rejected("the body source url must start with https://")
    if host not in {h.strip().lower() for h in source.allowed_hosts}:
        return _rejected("the body source host is not in allowed_hosts")
    try:
        async with new_client(outbound_policy(), float(timeout_s)) as client:
            async with asyncio.timeout(timeout_s):
                async with client.stream("GET", source.url) as response:
                    if not 200 <= response.status_code < 300:
                        return _rejected(
                            "the body source redirected (redirects are not followed)"
                            if 300 <= response.status_code < 400 else f"the body source answered status {response.status_code}"
                        )
                    media = response.headers.get("content-type", "").strip()
                    if not media.lower().startswith("image/"):
                        return _rejected("the body source is not an image (content-type must start with image/)")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > source.max_bytes:
                        return _rejected("the body source is larger than max_bytes")
                    raw, more = await _read(response, source.max_bytes)
    except Exception as exc:  # noqa: BLE001 - classified; the exception text is never returned
        code = _failure_code(exc)
        log.warning("http.request body source failed code=%s error_type=%s", code, type(exc).__name__)
        return _rejected(f"the body source could not be fetched ({_FAILURES[code]})")
    if more:
        return _rejected("the body source is larger than max_bytes")
    return raw, media


# ------------------------------------------------------------------------------------- reducers


def _reducer(ctx: RunContext[RunDeps], url: str, response_query: Any, extract_html: Any, parse_feed: Any) -> Reducer | None:
    """The one reducer the call names, as (kind, function), or None."""
    if response_query is not None:
        q = response_query if isinstance(response_query, HttpResponseQuery) else HttpResponseQuery.model_validate(response_query)
        return "response_query", lambda data: json_query(ctx, data, q.expression)
    if extract_html is not None:
        e = extract_html if isinstance(extract_html, HttpExtractHtml) else HttpExtractHtml.model_validate(extract_html)
        return "extract_html", lambda text: html_extract(
            ctx, text, e.base_url or url, e.mode, e.max_graphemes, e.root_selector, e.include_patterns,
            e.exclude_patterns, e.allowed_hosts, e.limit, e.exclude_urls, e.exclude_terms,
        )
    if parse_feed is not None:
        f = parse_feed if isinstance(parse_feed, HttpParseFeed) else HttpParseFeed.model_validate(parse_feed)

        def parse(text: str) -> dict[str, Any]:
            if f.label is None:
                return feed_parse(ctx, text=text, max_items=f.max_items, max_age_hours=f.max_age_hours,
                                  include_terms=f.include_terms, exclude_urls=f.exclude_urls, exclude_terms=f.exclude_terms)
            return feed_parse(ctx, feeds=[FeedInput(text=text, label=f.label)], max_items=f.max_items,
                              max_age_hours=f.max_age_hours, include_terms=f.include_terms,
                              exclude_urls=f.exclude_urls, exclude_terms=f.exclude_terms)

        return "parse_feed", parse
    return None


# ------------------------------------------------------------------------------------- redirect following helper


def _host_matches(base_url: str, allowed_hosts: list[str] | None) -> set[str]:
    """Return the set of allowed hosts: base URL's host plus any in allowed_hosts."""
    base_host = (urlsplit(base_url).hostname or "").lower()
    return {base_host, *(h.strip().lower() for h in allowed_hosts or [])}


def _strip_sensitive_headers(headers: dict[str, str] | None, original_host: str, target_host: str) -> dict[str, str] | None:
    """Strip Authorization and Cookie headers on cross-host hops."""
    if headers is None:
        return None
    if original_host == target_host:
        return headers
    return {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie")}


async def _follow_redirects(
    client: httpx2.AsyncClient,
    policy: OutboundPolicy,
    method: str,
    url: str,
    headers: dict[str, str] | None,
    query: dict[str, str] | None,
    json: Any,
    body: str | None,
    timeout_s: int,
    max_redirects: int,
    original_host: str,
    allowed_hosts: list[str] | None,
    started: float,
    max_response_bytes: int,
    drop_response_paths: Sequence[str],
    reducer: Reducer | None,
) -> dict[str, Any]:
    """Follow redirects up to max_redirects hops."""
    redirect_chain: list[str] = [url]
    current_url = url
    current_method = method
    current_headers = headers
    current_query = query
    current_json = json
    current_body = body
    current_host = original_host
    
    for hop in range(max_redirects + 1):
        try:
            async with asyncio.timeout(timeout_s):
                # Prepare request
                kwargs: dict[str, Any] = {"headers": current_headers, "params": current_query}
                if current_json is not None:
                    kwargs["json"] = current_json
                elif current_body is not None:
                    kwargs["content"] = current_body.encode("utf-8")
                
                async with client.stream(current_method, current_url, **kwargs) as response:
                    # Check if redirect
                    if response.status_code not in (301, 302, 303, 307, 308):
                        # Non-redirect response - final result
                        variants = list(secret_variants([]))  # empty variants since secrets already used
                        result = await _answer(response, max_response_bytes, variants, started, drop_response_paths, reducer)
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Get location header
                    location = response.headers.get("location")
                    if not location:
                        # Redirect without location - return the redirect response
                        variants = list(secret_variants([]))
                        result = to_jsonable_python(await _answer(response, max_response_bytes, variants, started, drop_response_paths, reducer))
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Resolve location URL
                    try:
                        target_url = urljoin(current_url, location)
                        target_parts = urlsplit(target_url)
                    except ValueError:
                        variants = list(secret_variants([]))
                        result = _error("redirect_refused", "the redirect location is not a valid URL")
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Check scheme
                    if target_parts.scheme != "https":
                        variants = list(secret_variants([]))
                        result = _error("redirect_refused", "redirects must be to https only")
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Get target host
                    target_host = (target_parts.hostname or "").lower()
                    if not target_host:
                        variants = list(secret_variants([]))
                        result = _error("redirect_refused", "the redirect location has no valid host")
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Check if we've exceeded max redirects
                    if hop >= max_redirects:
                        variants = list(secret_variants([]))
                        result = {
                            "status": response.status_code,
                            "ok": False,
                            "headers": {"location": redact(location, variants)},
                            "error": {"code": "redirect_refused", "message": "maximum redirects exceeded"},
                        }
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Resolve and check addresses at connect time for every hop
                    try:
                        await policy.resolve_checked(target_host, target_parts.port or 443)
                    except OutboundBlocked as exc:
                        variants = list(secret_variants([]))
                        result = {
                            "status": response.status_code,
                            "ok": False,
                            "headers": {"location": redact(location, variants)},
                            "error": {"code": "redirect_refused", "message": f"{exc.code}: {exc}"},
                        }
                        if len(redirect_chain) > 1:
                            result["redirected_from"] = redirect_chain
                        return result
                    
                    # Host must be same as original or in allowed_hosts
                    if target_host != current_host:
                        # Build allowed hosts set from original URL
                        allowed_set = _host_matches(redirect_chain[0], allowed_hosts)
                        if target_host not in allowed_set:
                            variants = list(secret_variants([]))
                            result = {
                                "status": response.status_code,
                                "ok": False,
                                "headers": {"location": redact(location, variants)},
                                "error": {"code": "redirect_refused", "message": "redirect host not allowed"},
                            }
                            if len(redirect_chain) > 1:
                                result["redirected_from"] = redirect_chain
                            return result
                    
                    # Strip sensitive headers on cross-host hop
                    if current_host != target_host:
                        current_headers = _strip_sensitive_headers(current_headers, current_host, target_host)
                    
                    # Prepare for next hop
                    redirect_chain.append(target_url)
                    
                    # Method handling for redirects
                    if response.status_code == 303:
                        # 303 always uses GET without body
                        current_method = "GET"
                        current_json = None
                        current_body = None
                    elif response.status_code in (301, 302):
                        # 301/302: POST -> GET without body
                        if current_method == "POST":
                            current_method = "GET"
                            current_json = None
                            current_body = None
                    # 307/308: keep method and body (already set)
                    
                    current_url = target_url
                    current_host = target_host
        
        except Exception as exc:  # noqa: BLE001
            code = _failure_code(exc)
            log.warning("http.request redirect hop failed code=%s error_type=%s", code, type(exc).__name__)
            variants = list(secret_variants([]))
            result = _error(code, _FAILURES[code])
            if len(redirect_chain) > 1:
                result["redirected_from"] = redirect_chain
            return result
    
    # Should not reach here, but handle the case where max_redirects is 0
    return _error("redirect_refused", "maximum redirects exceeded")


# ------------------------------------------------------------------------------------- the tool


async def http_request(
    ctx: RunContext[RunDeps],
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
    json: Any = None,
    body: str | None = None,
    timeout_s: Annotated[int, Field(ge=1, le=30)] = 20,
    max_response_bytes: Annotated[int, Field(ge=1, le=HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP)] = 65536,
    drop_response_paths: Annotated[list[Annotated[str, Field(pattern=r"^/")]] | None, Field(max_length=16)] = None,
    body_from_url: HttpBodySource | None = None,
    response_query: HttpResponseQuery | None = None,
    extract_html: HttpExtractHtml | None = None,
    parse_feed: HttpParseFeed | None = None,
    max_redirects: Annotated[int, Field(ge=0, le=5)] = 0,
) -> dict[str, Any]:
    """Send one HTTPS request (see the module docstring)."""
    if not url.lower().startswith("https://"):
        return _error("url_not_https", "the url must start with https://")
    if (json is not None and body is not None) or (body_from_url is not None and (json is not None or body is not None)):
        return _error("body_conflict", "send json, body or body_from_url, only one")
    if sum(r is not None for r in (response_query, extract_html, parse_feed)) > 1:
        return _error("reducer_conflict", "give at most one of response_query, extract_html and parse_feed")
    reducer = _reducer(ctx, url, response_query, extract_html, parse_feed)
    if reducer is None and max_response_bytes > HTTP_MAX_RESPONSE_BYTES_CAP:
        return _error("max_response_bytes_too_large", "max_response_bytes above 262144 needs a reducer")
    request = {"headers": headers, "query": query, "json": json, "body": body}
    used: list[str] = []
    if http_request_secret_names({"headers": headers, "query": query, "json": json, "body": body}):
        try:
            lookup = secret_lookup(ctx)
            request, used = await asyncio.to_thread(resolve_request, request, lookup)
        except SecretRefNotFound as exc:
            return _error("secret_not_found", f"{exc.ref} is not a stored secret")
        except SecretsError:
            log.warning("http.request secret could not be decrypted tenant=%s", ctx.deps.tenant_id)
            return _error("secret_unreadable", "a referenced secret could not be read")
    variants = list(secret_variants(used))
    content: bytes | None = request["body"].encode("utf-8") if request["body"] is not None else None
    if body_from_url is not None:
        source = body_from_url if isinstance(body_from_url, HttpBodySource) else HttpBodySource.model_validate(body_from_url)
        fetched = await _fetch_source(source, timeout_s)
        if isinstance(fetched, dict):
            return fetched
        content, media = fetched
        if not any(k.lower() == "content-type" for k in (request["headers"] or {})):
            request["headers"] = {**(request["headers"] or {}), "Content-Type": media}
    started = time.monotonic()
    try:
        policy = outbound_policy()
        async with new_client(policy, float(timeout_s)) as client:
            async with asyncio.timeout(timeout_s):
                # Get original host
                original_parts = urlsplit(url)
                original_host = (original_parts.hostname or "").lower()
                
                # Check if redirects are enabled
                if max_redirects > 0:
                    return await _follow_redirects(
                        client, policy, method, url, request["headers"], request["query"],
                        request["json"], request["body"], timeout_s, max_redirects,
                        original_host, extract_html.allowed_hosts if extract_html else None,
                        started, max_response_bytes, drop_response_paths or (), reducer
                    )
                
                # No redirects: single request as before
                async with client.stream(
                    method, url, headers=request["headers"], params=request["query"], json=request["json"],
                    content=content,
                ) as response:
                    return to_jsonable_python(await _answer(response, max_response_bytes, variants, started, drop_response_paths or (), reducer))
    except Exception as exc:  # noqa: BLE001 - classified; the exception text is never returned
        code = _failure_code(exc)
        log.warning("http.request failed code=%s error_type=%s", code, type(exc).__name__)
        return _error(code, _FAILURES[code])


def register_http_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[TOOL_ID], http_request, approval_summary=http_request_summary)


__all__ = ["http_request", "register_http_tool", "resolve_request"]
