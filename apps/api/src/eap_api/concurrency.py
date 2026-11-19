"""Optimistic concurrency over HTTP: `ETag` / `If-Match` on the definition-class resources.

The token is the row's `revision` (eap_run_store.revisions), served as a strong, quoted integer
`ETag: "<revision>"` by every single-resource read and every write of agents, workflows, schedules,
eval suites and cases, connections, kb sources, scorers and MCP servers (list items carry the
`revision` field only). A PATCH/PUT carries `If-Match: "<revision>"` (the bare integer and `*` are
accepted too):

- stale -> 412 `{"detail", "code": "revision_mismatch", "current_revision", "current": <the resource
  as GET returns it>}` with the current `ETag`, so a client can diff without a second request;
- `If-Match` missing -> still allowed (transitional: MCP clients and scripts), with the response header
  `Warning: 299 - "write without If-Match; send the ETag you read"`; with `EAP_REQUIRE_IF_MATCH=1`
  the write is refused with 428 `precondition_required` instead;
- `*` -> any current revision matches (the write is deliberate, there is no warning).

A route takes `pre: IfMatch`, loads the row, calls `pre.check(...)` for the fail-fast comparison, hands
`pre.expected` to the store (which compares and bumps in the same UPDATE, so a race between the check and
the write is still refused) and answers through `pre.respond(...)`. See docs/concurrency.md.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends, Header, Request, Response
from fastapi.responses import JSONResponse

from eap_run_store.revisions import RevisionMismatch

REQUIRE_IF_MATCH_ENV = "EAP_REQUIRE_IF_MATCH"
MISMATCH_CODE = "revision_mismatch"
REQUIRED_CODE = "precondition_required"
INVALID_CODE = "invalid_if_match"
WARNING_MISSING = '299 - "write without If-Match; send the ETag you read"'

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ETAG = re.compile(r'^(?:W/)?"?(\d{1,9})"?$')

# (resource body, its revision): what a stale write answers with.
Current = Callable[[], tuple[dict[str, Any], int]]


def require_if_match() -> bool:
    """EAP_REQUIRE_IF_MATCH=1 turns a write without `If-Match` into 428 (read per request)."""
    return os.environ.get(REQUIRE_IF_MATCH_ENV, "").strip().lower() in _TRUTHY


def etag(revision: int) -> str:
    return f'"{revision}"'


class ConcurrencyError(Exception):
    """A refusal the app turns into its JSON response (412, 428 or 400); see register(app)."""

    def __init__(self, status_code: int, body: dict[str, Any], revision: int | None = None):
        super().__init__(body.get("detail"))
        self.status_code = status_code
        self.body = body
        self.revision = revision


def register(app: Any) -> None:
    @app.exception_handler(ConcurrencyError)
    async def _concurrency_error(_request: Request, exc: ConcurrencyError) -> JSONResponse:
        response = JSONResponse(status_code=exc.status_code, content=exc.body)
        if exc.revision is not None:
            response.headers["ETag"] = etag(exc.revision)
        return response


def stamp(response: Response, revision: int) -> Response:
    """Set `ETag` on any response (a JSONResponse, or the `Response` FastAPI injects)."""
    response.headers["ETag"] = etag(revision)
    return response


def read_response(content: Any, revision: int, status_code: int = 200) -> JSONResponse:
    """A single-resource GET: the body with its `ETag`."""
    return stamp(JSONResponse(content=content, status_code=status_code), revision)  # type: ignore[return-value]


class Precondition:
    """The parsed `If-Match` of one write."""

    def __init__(self, expected: int | None, header: str | None):
        self.expected = expected  # None: no comparison (header missing or `*`)
        self.missing = header is None

    def stamp(self, response: Response, revision: int) -> Response:
        """`ETag` for the new revision, and the transitional `Warning` when the write had no `If-Match`."""
        stamp(response, revision)
        if self.missing:
            response.headers["Warning"] = WARNING_MISSING
        return response

    def respond(self, content: Any, revision: int, status_code: int = 200) -> JSONResponse:
        return self.stamp(JSONResponse(content=content, status_code=status_code), revision)  # type: ignore[return-value]

    def stale(self, current: Current) -> ConcurrencyError:
        body, revision = current()
        return ConcurrencyError(
            412,
            {
                "detail": (
                    f"this resource changed since you read it (revision {revision}, you sent "
                    f"{self.expected}); re-read it or send the current ETag to overwrite"
                ),
                "code": MISMATCH_CODE,
                "current_revision": revision,
                "current": body,
            },
            revision,
        )

    def check(self, revision: int, current: Current) -> None:
        """Fail-fast: 412 when `If-Match` names another revision than the loaded row's."""
        if self.expected is not None and revision != self.expected:
            raise self.stale(current)

    @contextmanager
    def guard(self, current: Current, session: Any = None) -> Iterator[None]:
        """Turn the store's RevisionMismatch (the guarded UPDATE found the row moved on between the load and
        the write) into the same 412, after rolling back `session` so `current` re-reads committed data."""
        try:
            yield
        except RevisionMismatch:
            if session is not None:
                session.rollback()
            raise self.stale(current) from None


def _parse(header: str | None) -> Precondition:
    if header is None:
        if require_if_match():
            raise ConcurrencyError(
                428,
                {"detail": "this write needs an If-Match header: send the ETag you read, or * to overwrite",
                 "code": REQUIRED_CODE},
            )
        return Precondition(None, None)
    value = header.strip()
    if value == "*":
        return Precondition(None, value)
    found = _ETAG.match(value)
    if found is None:
        raise ConcurrencyError(
            400, {"detail": 'If-Match must be one ETag such as "3" (or *)', "code": INVALID_CODE})
    return Precondition(int(found.group(1)), value)


def if_match(
    if_match: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description='The ETag (`"<revision>"`) of the resource you read, or `*`. A stale value answers 412 '
                        '`revision_mismatch` with the current resource; a missing header is allowed for now '
                        '(response `Warning`) unless EAP_REQUIRE_IF_MATCH=1 (428).',
        ),
    ] = None,
) -> Precondition:
    return _parse(if_match)


IfMatch = Annotated[Precondition, Depends(if_match)]

_ETAG_HEADER = {"ETag": {"description": 'The resource\'s current revision, quoted: `"3"`.',
                         "schema": {"type": "string"}}}


def etag_responses(*, success: int = 200, write: bool = True) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries documenting the `ETag` response header (OpenAPI); with `write`, the 412 and 428
    refusals of a precondition too."""
    entries: dict[int | str, dict[str, Any]] = {
        success: {"description": "OK", "headers": {**_ETAG_HEADER}},
    }
    if write:
        entries[412] = {
            "description": "Precondition Failed: `{detail, code: revision_mismatch, current_revision, current}`",
            "headers": {**_ETAG_HEADER},
        }
        entries[428] = {"description": "Precondition Required (EAP_REQUIRE_IF_MATCH=1): `{detail, code}`"}
    return entries
