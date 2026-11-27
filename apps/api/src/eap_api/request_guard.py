"""Input hardening shared by every JSON write route of the API.

`guarded_json_route(...)` returns an APIRoute class; a router that sets it as `route_class` gets,
for each POST/PUT/PATCH request, before FastAPI parses the body:

- more than ``max_body_bytes`` -> 413 {"detail": "request body too large (max N bytes)"}; the
  declared Content-Length is checked first, then the stream, so an oversized body is never buffered;
  with ``soft_body_bytes`` and ``allow_beyond_soft``, a body that streams past the soft cap is
  refused there (413 naming the soft cap) unless ``allow_beyond_soft(prefix)`` accepts the bytes
  read so far (the knowledge base upload: only a base64 body may grow to the binary guard);
- not strict UTF-8 (an optional BOM), or any NUL byte -> 415
  {"detail": "request body must be UTF-8 encoded JSON (no NUL bytes)"};
- objects/arrays nested deeper than ``max_depth`` -> 422 {"detail": "<depth_detail>"} (a string);

and then, on every method, after authentication (so a 401 still wins over a 422):

- any path parameter, query parameter (name or value) or string in the body (object keys
  included) that PostgreSQL cannot store in or compare with a text or JSONB column (U+0000, or an
  unpaired UTF-16 surrogate written as a \\uD800-\\uDFFF escape) -> 422 with the compact list below,
  one item per offending string (at most MAX_REPORTED_ERRORS), naming its location: ["path", name],
  ["query", name] or ["body", ...field path]. An endpoint marked @schema_checks_storable skips only
  the body scan (its model reports those itself); its path and query are still checked.

Every RequestValidationError the route raises (FastAPI's own body/query/path validation included)
answers 422 {"detail": [{"loc", "msg", "type"}, ...]}: never the rejected input or ctx, each part
bounded in size. HTTPExceptions a route raises itself (422 strings included) are unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine, Sequence
from typing import Annotated, Any

from fastapi import Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.params import Depends as DependsParam
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from eap_api.auth import Principal, current_principal
from eap_api.text_checks import (  # noqa: F401 - re-exported: the checks live in a framework-free module
    UTF8_BOM,
    decode_utf8_json_body,
    json_depth_exceeds,
    MAX_REPORTED_ERRORS,
    UNSTORABLE_ERROR_TYPE,
    unstorable_reason,
    unstorable_strings,
)

DEFAULT_MAX_BODY_BYTES = 256 * 1024  # the run payload limit (EAP_MAX_RUN_PAYLOAD_BYTES default)
DEFAULT_MAX_BODY_DEPTH = 32
# Bodies larger than this are decoded and scanned in the threadpool instead of on the event loop.
INLINE_SCAN_MAX_BYTES = 256 * 1024
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})

NOT_UTF8_DETAIL = "request body must be UTF-8 encoded JSON (no NUL bytes)"

_STATE_KEY = "eap_unstorable_errors"
_LOC_PART_MAX = 100


def too_large_detail(limit: int) -> str:
    return f"request body too large (max {limit} bytes)"


async def read_capped_body(
    request: Request,
    limit: int,
    *,
    soft_limit: int | None = None,
    allow_beyond_soft: Callable[[bytes], bool] | None = None,
) -> bytes | int | None:
    """The request body, or None as soon as it is known to exceed ``limit`` bytes (declared
    Content-Length first, then while streaming): an oversized body is never buffered whole.

    With ``soft_limit`` (below ``limit``) and ``allow_beyond_soft``: once the stream passes the
    soft limit, ``allow_beyond_soft`` sees the bytes read so far (once); when it refuses, the soft
    limit (an int) is returned instead of the body, so at most ``soft_limit`` plus one chunk is
    buffered."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    checked = soft_limit is None or allow_beyond_soft is None or soft_limit >= limit
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not checked and size > soft_limit:  # type: ignore[operator]
            checked = True
            prefix = b"".join(chunks)
            chunks = [prefix]
            if not allow_beyond_soft(prefix):  # type: ignore[misc]
                return soft_limit
    return b"".join(chunks)


def _loc_part(part: Any) -> int | str:
    return part if isinstance(part, int) else str(part)[:_LOC_PART_MAX]


def compact_errors(errors: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """loc, msg and type only (never the rejected input or ctx), each bounded in size."""
    return [
        {
            "loc": [_loc_part(part) for part in err.get("loc", ())],
            "msg": str(err.get("msg", ""))[:500],
            "type": str(err.get("type", ""))[:100],
        }
        for err in list(errors)[:MAX_REPORTED_ERRORS]
    ]


def compact_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    return compact_errors(exc.errors())


def unstorable_params(request: Request) -> list[dict[str, Any]]:
    """Compact 422 items for unstorable path parameters and query parameters (names included).
    Starlette decodes percent-escapes with errors='replace', so in practice this is %00; a lone
    surrogate cannot arrive through a URL, but is checked the same way."""
    errors = unstorable_strings(dict(request.path_params), ("path",))
    for key, value in request.query_params.multi_items():
        if len(errors) >= MAX_REPORTED_ERRORS:
            break
        errors += unstorable_strings({key: value}, ("query",))
    return errors


def _reject_unstorable_strings(
    request: Request, _principal: Annotated[Principal, Depends(current_principal)]
) -> None:
    """Route dependency: raise the unstorable-string errors found before parsing. It depends on
    the principal so authentication (cached for the route) answers first."""
    errors = getattr(request.state, _STATE_KEY, None)
    if errors:
        raise RequestValidationError(errors)


_SCHEMA_CHECKS_STORABLE = "_eap_schema_checks_storable"


def schema_checks_storable(endpoint: Callable[..., Any]) -> Callable[..., Any]:
    """Mark an endpoint whose body model already rejects unstorable strings itself (with its own
    messages), so the route-level check is skipped for it. Apply below the router decorator."""
    setattr(endpoint, _SCHEMA_CHECKS_STORABLE, True)
    return endpoint


def guarded_json_route(
    *,
    max_body_bytes: int | Callable[[], int] = DEFAULT_MAX_BODY_BYTES,
    max_depth: int = DEFAULT_MAX_BODY_DEPTH,
    depth_detail: str | None = None,
    check_storable: bool = True,
    soft_body_bytes: Callable[[], int] | None = None,
    allow_beyond_soft: Callable[[bytes], bool] | None = None,
) -> type[APIRoute]:
    """An APIRoute class with the guards described in this module's docstring.

    `max_body_bytes` may be a callable, read on every write request (a cap set by an environment
    variable, e.g. EAP_KB_MAX_DOCUMENT_BYTES for the knowledge base document upload).
    `soft_body_bytes` (a callable, read per request) and `allow_beyond_soft` set a lower cap that
    only bodies the predicate accepts may pass (see read_capped_body)."""
    nested_detail = depth_detail or f"request body is nested more than {max_depth} levels deep"
    body_limit: Callable[[], int] = max_body_bytes if callable(max_body_bytes) else (lambda: max_body_bytes)

    class GuardedJsonRoute(APIRoute):
        def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
            if check_storable:
                dependencies: list[DependsParam] = list(kwargs.get("dependencies") or [])
                # include_router re-creates the route with its dependencies: add this one once.
                if not any(d.dependency is _reject_unstorable_strings for d in dependencies):
                    dependencies.append(Depends(_reject_unstorable_strings))
                kwargs["dependencies"] = dependencies
            super().__init__(path, endpoint, **kwargs)
            self.checks_storable = check_storable
            self.checks_body_strings = check_storable and not getattr(endpoint, _SCHEMA_CHECKS_STORABLE, False)

        def _inspect_body(self, body: bytes) -> tuple[Response | None, list[dict[str, Any]]]:
            """UTF-8 and NUL (415), depth (422), then the unstorable strings of the parsed body."""
            text = decode_utf8_json_body(body)
            if text is None:
                return JSONResponse(status_code=415, content={"detail": NOT_UTF8_DETAIL}), []
            if json_depth_exceeds(text, max_depth):
                return JSONResponse(status_code=422, content={"detail": nested_detail}), []
            if not (self.checks_body_strings and text.strip()):
                return None, []
            try:
                parsed = json.loads(text)
            except ValueError:
                return None, []  # FastAPI reports the malformed JSON itself
            return None, unstorable_strings(parsed)

        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handler = super().get_route_handler()

            async def guarded(request: Request) -> Response:
                errors: list[dict[str, Any]] = []
                if self.checks_storable:
                    errors = unstorable_params(request)
                if request.method in WRITE_METHODS:
                    limit = body_limit()
                    soft = soft_body_bytes() if soft_body_bytes is not None else None
                    body = await read_capped_body(request, limit, soft_limit=soft, allow_beyond_soft=allow_beyond_soft)
                    if body is None:
                        return JSONResponse(status_code=413, content={"detail": too_large_detail(limit)})
                    if isinstance(body, int):  # past the soft cap, and the predicate refused
                        return JSONResponse(status_code=413, content={"detail": too_large_detail(body)})
                    request._body = body  # FastAPI's request.body() returns these bytes; no second read
                    if len(body) > INLINE_SCAN_MAX_BYTES:
                        # Large bodies (the knowledge base upload): scan in the threadpool, not on the loop.
                        rejection, body_errors = await run_in_threadpool(self._inspect_body, body)
                    else:
                        rejection, body_errors = self._inspect_body(body)
                    if rejection is not None:
                        return rejection
                    errors += body_errors
                if errors:
                    setattr(request.state, _STATE_KEY, errors[:MAX_REPORTED_ERRORS])
                try:
                    return await handler(request)
                except RequestValidationError as exc:
                    return JSONResponse(status_code=422, content={"detail": compact_validation_errors(exc)})

            return guarded

    return GuardedJsonRoute


# The default for the API's JSON write routes (agents, runs, conversations, MCP servers, tools).
GuardedJsonRoute = guarded_json_route()
