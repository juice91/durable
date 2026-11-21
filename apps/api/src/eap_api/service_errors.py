"""The one translator from a service's refusal to the API's JSON error body.

The body is what the API has always answered: `{"detail": <message>, "code": <code>}`, plus `errors` (a list)
when the refusal has details. A stale revision (`Conflict` with `current`) answers 412 with `current_revision`
and `current`, and the `ETag` of the current revision, as eap_api.concurrency does.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from eap_api.concurrency import etag
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, ServiceError, Unavailable

_STATUS: tuple[tuple[type[ServiceError], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (InvalidInput, 422),
    (Forbidden, 403),
    (Unavailable, 503),
)


def service_error_response(exc: ServiceError) -> JSONResponse:
    status = next((code for kind, code in _STATUS if isinstance(exc, kind)), 500)
    body: dict[str, Any] = {"detail": exc.message, "code": exc.code}
    if exc.details is not None:
        body["errors"] = exc.details
    headers: dict[str, str] = {}
    if isinstance(exc, Conflict) and exc.current is not None:
        status = 412
        body["current_revision"] = exc.current_revision
        body["current"] = exc.current
        if exc.current_revision is not None:
            headers["ETag"] = etag(exc.current_revision)
    return JSONResponse(status_code=status, content=body, headers=headers or None)


def register(app: Any) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
        return service_error_response(exc)
