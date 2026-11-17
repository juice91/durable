"""Typed refusals of the definition operations (publish, restore, archive of agents and workflows).

`DefinitionError` carries the HTTP status and the JSON body the API answers with; the API renders
it through one exception handler (eap_api.app), and an Architect tool renders it as a model-visible
`{error: {code, message}}`. The classes only describe the refusal: nothing here imports a web or
agent library.
"""

from __future__ import annotations

from typing import Any


class DefinitionError(Exception):
    """A refused definition operation: `status_code` and the body `{detail, code?, **extra}`."""

    status_code = 500

    def __init__(self, detail: str, *, code: str | None = None, **extra: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.extra = extra

    @property
    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"detail": self.detail}
        if self.code is not None:
            body["code"] = self.code
        body.update(self.extra)
        return body


class DefinitionBadRequest(DefinitionError):
    status_code = 400


class DefinitionNotFound(DefinitionError):
    status_code = 404


class DefinitionConflict(DefinitionError):
    status_code = 409


class DefinitionInvalid(DefinitionError):
    status_code = 422


class DefinitionUnavailable(DefinitionError):
    status_code = 503


_BY_STATUS: dict[int, type[DefinitionError]] = {
    400: DefinitionBadRequest,
    404: DefinitionNotFound,
    409: DefinitionConflict,
    422: DefinitionInvalid,
    503: DefinitionUnavailable,
}


def refuse(status_code: int, detail: str, **kwargs: Any) -> DefinitionError:
    """The DefinitionError subclass for `status_code` (`raise refuse(422, "...")`)."""
    return _BY_STATUS[status_code](detail, **kwargs)


# Codes the API has always used in a body, and a tool reports.
AGENT_ARCHIVED = "Agent is archived; unarchive it to start runs, conversations or publish versions"
BUILTIN_AGENT = "builtin_agent"
