"""One helper for refusals whose body must stay exactly what the API has always answered."""

from __future__ import annotations

from typing import Any

from eap_run_store.definition_errors import DefinitionError


def refusal(status_code: int, detail: Any, **kwargs: Any) -> DefinitionError:
    """A refusal with exactly this status and `{detail, code?, **extra}` body (the app renders it; an MCP
    tool reports it as `{error: {code, message}}`). Use a `ServiceError` subclass instead where the body
    carries a `code` of the service's own."""
    exc = DefinitionError(detail, **kwargs)
    exc.status_code = status_code
    return exc
