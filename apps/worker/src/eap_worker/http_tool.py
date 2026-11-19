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
  checked again at connect, no redirects followed: a 3xx is returned with its `location`).
- Result: `{status, ok, headers, json | text, truncated?, elapsed_ms}`. A non-2xx status is a result. A
  transport failure is `{error: {code, message}}` with `outbound_blocked`, `http_timeout`,
  `http_connect_failed`, `secret_not_found`, `secret_unreadable`, `response_too_large`. The messages are fixed
  texts: never the URL, a header or an exception text.
- `body_from_url` (`HttpBodySource`: `url`, `max_bytes` default 1,000,000 and at most 5,000,000, `allowed_hosts`): the
  request body is fetched first, by a GET through the same guarded client (https only, public addresses only,
  redirects not followed: a 3xx is refused), from a host in `allowed_hosts` only, and must answer `image/*`; at
  most `max_bytes` are read (a longer body, or a larger content-length, is refused). Those bytes are the request
  body, with `Content-Type` set to the fetched one unless the caller set one. Any refusal is
  `{error: {code: "body_source_rejected", detail}}` with a fixed detail (never the URL), and nothing is sent.
  It excludes `json` and `body` (`body_conflict`). The main request's result is unchanged.
- Gating: an agent run's call that names a secret waits for an approval (`SpecCapability`,
  eap_worker.capabilities); a workflow tool step does not (the spec was published by a person).
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import re
import time
from urllib.parse import urlsplit
from collections.abc import Callable, Sequence
from typing import Any

import httpx2
from pydantic import Field
from pydantic_ai import RunContext
from pydantic_core import to_jsonable_python

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.http_request import EMBEDDED_REF, http_request_secret_names, http_request_summary
from eap_schemas.secrets import is_secret_ref
from eap_schemas.tools import HTTP_MAX_RESPONSE_BYTES_CAP, TOOL_DECLARATIONS, HttpBodySource
from eap_secrets import SecretsError
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy
from eap_secrets.outbound_errors import walk_chain
from eap_secrets.redaction import mask_split_secrets, redact, secret_variants
from typing import Annotated, Literal

from eap_worker.secret_refs import SecretRefNotFound, secret_store

log = logging.getLogger("eap_worker.http_tool")

TOOL_ID = "http.request"
RESPONSE_HEADERS = ("content-type", "content-length", "location", "retry-after", "x-request-id")
RESPONSE_HEADER_PREFIXES = ("ratelimit-",)
MAX_HEADER_VALUE_CHARS = 512


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


async def _answer(
    response: httpx2.Response, max_bytes: int, variants: list[str], started: float, drop: Sequence[str] = ()
) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    width = (max(len(v) for v in variants) + 4) * 4 if variants else 0
    declared = response.headers.get("content-length")
    is_json = _is_json(content_type)
    if is_json and declared and declared.isdigit() and int(declared) > max_bytes:
        return _error("response_too_large", "the response body is larger than max_response_bytes")
    raw, more = await _read(response, max_bytes + width + 1 if not is_json else max_bytes)
    result: dict[str, Any] = {
        "status": response.status_code,
        "ok": 200 <= response.status_code < 300,
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
    max_response_bytes: Annotated[int, Field(ge=1, le=HTTP_MAX_RESPONSE_BYTES_CAP)] = 65536,
    drop_response_paths: Annotated[list[Annotated[str, Field(pattern=r"^/")]] | None, Field(max_length=16)] = None,
    body_from_url: HttpBodySource | None = None,
) -> dict[str, Any]:
    """Send one HTTPS request (see the module docstring)."""
    if not url.lower().startswith("https://"):
        return _error("url_not_https", "the url must start with https://")
    if (json is not None and body is not None) or (body_from_url is not None and (json is not None or body is not None)):
        return _error("body_conflict", "send json, body or body_from_url, only one")
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
        async with new_client(outbound_policy(), float(timeout_s)) as client:
            async with asyncio.timeout(timeout_s):
                async with client.stream(
                    method, url, headers=request["headers"], params=request["query"], json=request["json"],
                    content=content,
                ) as response:
                    return to_jsonable_python(await _answer(response, max_response_bytes, variants, started, drop_response_paths or ()))
    except Exception as exc:  # noqa: BLE001 - classified; the exception text is never returned
        code = _failure_code(exc)
        log.warning("http.request failed code=%s error_type=%s", code, type(exc).__name__)
        return _error(code, _FAILURES[code])


def register_http_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[TOOL_ID], http_request, approval_summary=http_request_summary)


__all__ = ["http_request", "register_http_tool", "resolve_request"]
