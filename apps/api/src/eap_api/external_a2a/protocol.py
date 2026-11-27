"""A2A 1.0 over JSON-RPC (specification 3, 5.4 and 9): the errors, the request envelope, the version and the parameters
of the methods this server serves. Pure parsing: nothing here decides what a caller may do.

Only protocol 1.0 is spoken. A request without `A2A-Version` is 0.3 by the specification and is refused. The `a2a-sdk`
is a test-only dependency (the conformance oracle); this module is our own reading of the wire format: ProtoJSON, with
unknown fields ignored."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

PROTOCOL_VERSION = "1.0"
_VERSION = re.compile(r"1\.0(\.\d+)?")
ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"
BAD_REQUEST = "type.googleapis.com/google.rpc.BadRequest"
ERROR_DOMAIN = "a2a-protocol.org"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
EXTENDED_CARD_NOT_CONFIGURED = -32007
VERSION_NOT_SUPPORTED = -32009
# code -> (ErrorInfo reason, default message)
ERRORS = {
    PARSE_ERROR: ("PARSE_ERROR", "Invalid JSON payload"),
    INVALID_REQUEST: ("INVALID_REQUEST", "Request payload validation error"),
    METHOD_NOT_FOUND: ("METHOD_NOT_FOUND", "Method not found"),
    INVALID_PARAMS: ("INVALID_PARAMS", "Invalid parameters"),
    INTERNAL: ("INTERNAL_ERROR", "Internal error"),
    TASK_NOT_FOUND: ("TASK_NOT_FOUND", "Task not found"),
    TASK_NOT_CANCELABLE: ("TASK_NOT_CANCELABLE", "Task cannot be canceled"),
    PUSH_NOT_SUPPORTED: ("PUSH_NOTIFICATION_NOT_SUPPORTED", "Push notifications are not supported"),
    UNSUPPORTED_OPERATION: ("UNSUPPORTED_OPERATION", "This operation is not supported"),
    CONTENT_TYPE_NOT_SUPPORTED: ("CONTENT_TYPE_NOT_SUPPORTED", "Incompatible content types"),
    EXTENDED_CARD_NOT_CONFIGURED: ("EXTENDED_AGENT_CARD_NOT_CONFIGURED", "This key reaches no skill here"),
    VERSION_NOT_SUPPORTED: ("VERSION_NOT_SUPPORTED", "Only A2A protocol version 1.0 is supported"),
}
STREAMING_METHODS = frozenset({"SendStreamingMessage", "SubscribeToTask"})
PUSH_METHODS = frozenset({
    "CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
    "DeleteTaskPushNotificationConfig",
})
METHODS = frozenset({"SendMessage", "GetTask", "ListTasks", "CancelTask", "GetExtendedAgentCard"})
TASK_STATES = {  # TaskState -> the run states it filters on (None: a state no task of this server ever has)
    "TASK_STATE_SUBMITTED": "queued", "TASK_STATE_WORKING": "running", "TASK_STATE_INPUT_REQUIRED": "input_required",
    "TASK_STATE_COMPLETED": "completed", "TASK_STATE_CANCELED": "cancelled", "TASK_STATE_FAILED": "failed",
    "TASK_STATE_REJECTED": None, "TASK_STATE_AUTH_REQUIRED": None,
}
UNSPECIFIED_STATE = "TASK_STATE_UNSPECIFIED"
MESSAGE_ID_MAX_CHARS = 128  # the idempotency key of a run
ID_MAX_CHARS = 200  # a task or context id
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 50, 100
_INT = re.compile(r"-?\d{1,10}")
_EXACT_INTEGERS = 2**53


class A2AError(Exception):
    """A JSON-RPC error answer. `details` go into the ErrorInfo metadata (strings); `violations` into a BadRequest."""

    def __init__(
        self, code: int, message: str | None = None, *, details: dict[str, str] | None = None,
        violations: list[tuple[str, str]] | None = None,
    ) -> None:
        self.code = code
        self.message = message or ERRORS[code][1]
        self.details = details or {}
        self.violations = violations or []
        super().__init__(self.message)

    def body(self) -> dict[str, Any]:
        data: list[dict[str, Any]] = [{
            "@type": ERROR_INFO, "reason": ERRORS[self.code][0], "domain": ERROR_DOMAIN,
            "metadata": {**self.details, "timestamp": timestamp(datetime.now(timezone.utc))},
        }]
        if self.violations:
            data.append({"@type": BAD_REQUEST, "fieldViolations": [
                {"field": field, "description": description} for field, description in self.violations
            ]})
        return {"code": self.code, "message": self.message, "data": data}


def invalid(field: str, description: str, **details: str) -> A2AError:
    return A2AError(INVALID_PARAMS, f"Invalid parameters: {field}: {description}", details=details,
                    violations=[(field, description)])


def timestamp(value: datetime) -> str:
    """ISO 8601 in UTC with milliseconds (specification 5.6.1)."""
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------------ the envelope


@dataclass(frozen=True)
class Request:
    id: Any
    method: str
    params: Any


def read_envelope(body: bytes) -> Request:
    """The JSON-RPC request of a body: PARSE_ERROR or INVALID_REQUEST. Batches and notifications are not requests."""
    try:
        message = json.loads(body)
    except (ValueError, RecursionError):
        raise A2AError(PARSE_ERROR) from None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        raise A2AError(INVALID_REQUEST)
    request_id, method = message.get("id"), message.get("method")
    if not isinstance(request_id, (str, int, float)) or isinstance(request_id, bool) or not isinstance(method, str):
        raise A2AError(INVALID_REQUEST)
    return Request(request_id, method, message.get("params"))


def check_version(version: str | None) -> None:
    if not _VERSION.fullmatch((version or "").strip()):
        raise A2AError(VERSION_NOT_SUPPORTED, details={"supportedVersions": PROTOCOL_VERSION})


# ------------------------------------------------------------------------------------------------ fields


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid(field, "must be a JSON object")
    return value


def _optional_object(value: Any, field: str) -> dict[str, Any]:
    return {} if value is None else _object(value, field)


def _string(value: Any, field: str, maximum: int = ID_MAX_CHARS) -> str | None:
    """None when absent or empty (a proto3 string's default)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > maximum or not _storable(value):
        raise invalid(field, f"must be a string of at most {maximum} characters")
    return value


def _storable(value: str) -> bool:
    """No NUL and no lone surrogate: the database and the key's hash take neither."""
    return "\x00" not in value and not any("\ud800" <= c <= "\udfff" for c in value)


def _skill(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > ID_MAX_CHARS or not _storable(value):
        raise invalid("metadata.skill", "must be the id of a skill")
    return value


def _task_id(params: dict[str, Any]) -> str:
    value = params.get("id")
    if not isinstance(value, str) or not value:
        raise invalid("id", "is required")
    if len(value) > ID_MAX_CHARS or not _storable(value):
        raise A2AError(TASK_NOT_FOUND, details={"taskId": value[:ID_MAX_CHARS]})
    return value


def _integer(value: Any, field: str, minimum: int, maximum: int = 2**31 - 1) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and _INT.fullmatch(value):
        value = int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise invalid(field, f"must be an integer from {minimum} to {maximum}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise invalid(field, "must be true or false")
    return value


def _whole_numbers(value: Any) -> Any:
    """A protobuf `Value` carries every number as a double: a whole one is the integer it was."""
    if isinstance(value, float) and value.is_integer() and abs(value) <= _EXACT_INTEGERS:
        return int(value)
    if isinstance(value, dict):
        return {k: _whole_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_whole_numbers(v) for v in value]
    return value


# ------------------------------------------------------------------------------------------------ SendMessage

_CONTENT = ("text", "raw", "url", "data")
_MEDIA_TYPES = {"text": ("", "text/plain"), "data": ("", "application/json")}


@dataclass(frozen=True)
class Send:
    message_id: str
    task_id: str | None
    context_id: str | None
    data: dict[str, Any] | None
    text: str | None
    skill: str | None
    deadline_seconds: Any  # checked by the service, like every other surface's
    return_immediately: bool


def _content(parts: Any) -> tuple[dict[str, Any] | None, str | None]:
    """`(data, text)` of the one part a message to a workflow has. Files and URLs are refused: nothing is fetched."""
    if not isinstance(parts, list) or not parts:
        raise invalid("message.parts", "at least one part is required")
    found: list[tuple[str, dict[str, Any]]] = []
    for part in parts:
        kinds = [k for k in _CONTENT if k in part] if isinstance(part, dict) else []
        if len(kinds) != 1:
            raise invalid("message.parts", "each part has exactly one of text, data, raw and url")
        found.append((kinds[0], part))
    for kind, part in found:
        media_type = part.get("mediaType") or ""
        if kind in ("raw", "url") or not isinstance(media_type, str) or (
            media_type.split(";")[0].strip().lower() not in _MEDIA_TYPES[kind]
        ):
            raise A2AError(
                CONTENT_TYPE_NOT_SUPPORTED,
                "Incompatible content types: send one application/json data part or one text/plain part",
            )
    if len(found) != 1:
        raise invalid("message.parts", "send one data part (the input) or one text part")
    kind, part = found[0]
    value = part[kind]
    if kind == "text":
        if not isinstance(value, str):
            raise invalid("message.parts", "text must be a string")
        return None, value
    if not isinstance(value, dict):
        raise invalid("message.parts", "the data part must hold a JSON object")
    return _whole_numbers(value), None


def read_send(params: Any) -> Send:
    """The parameters of `SendMessage`. The skill is chosen by a data part `{"skill", "input"}` or by
    `message.metadata.skill` (then the data part is the input); a service decides the rest."""
    params = _object(params, "params")
    configuration = _optional_object(params.get("configuration"), "configuration")
    if configuration.get("taskPushNotificationConfig") is not None:
        raise A2AError(PUSH_NOT_SUPPORTED)
    _integer(configuration.get("historyLength"), "configuration.historyLength", 0)
    return_immediately = _boolean(configuration.get("returnImmediately"), "configuration.returnImmediately")
    message = _object(params.get("message"), "message")
    message_id = _string(message.get("messageId"), "message.messageId", MESSAGE_ID_MAX_CHARS)
    if message_id is None:
        raise invalid("message.messageId", "is required")
    if message.get("role") != "ROLE_USER":
        raise invalid("message.role", "must be ROLE_USER")
    task_id = _string(message.get("taskId"), "message.taskId")
    context_id = _string(message.get("contextId"), "message.contextId")
    metadata = _optional_object(message.get("metadata"), "message.metadata")
    request_metadata = _optional_object(params.get("metadata"), "metadata")
    data, text = _content(message.get("parts"))
    skill = None if metadata.get("skill") is None else _skill(metadata["skill"])
    if skill is None and data is not None and set(data) == {"skill", "input"} and isinstance(data["skill"], str):
        skill, data = _skill(data["skill"]), data["input"]
        if not isinstance(data, dict):
            raise invalid("message.parts", "the skill's input must be a JSON object")
    deadline = request_metadata.get("deadlineSeconds", metadata.get("deadlineSeconds"))
    return Send(message_id, task_id, context_id, data, text, skill, _whole_numbers(deadline), return_immediately)


# ------------------------------------------------------------------------------------------------ the other methods


def read_task_id(params: Any) -> str:
    """The parameters of `GetTask` and `CancelTask`: the task id."""
    params = _object(params, "params")
    _integer(params.get("historyLength"), "historyLength", 0)
    return _task_id(params)


@dataclass(frozen=True)
class ListQuery:
    context_id: str | None
    state: str | None  # the TaskState asked for, None for any
    page_size: int
    page_token: str | None
    changed_after: datetime | None
    include_artifacts: bool


def read_list(params: Any) -> ListQuery:
    params = _optional_object(params, "params")
    _integer(params.get("historyLength"), "historyLength", 0)
    status = params.get("status")
    if status in (None, "", UNSPECIFIED_STATE):
        status = None
    elif not isinstance(status, str) or status not in TASK_STATES:
        raise invalid("status", "must be a TaskState")
    size = _integer(params.get("pageSize"), "pageSize", 1, MAX_PAGE_SIZE)
    after = params.get("statusTimestampAfter")
    if after is not None:
        try:
            after = datetime.fromisoformat(after)
        except (TypeError, ValueError):
            after = False
        if not after or after.tzinfo is None:
            raise invalid("statusTimestampAfter", "must be an ISO 8601 time with a time zone")
    return ListQuery(
        _string(params.get("contextId"), "contextId"), status, size or DEFAULT_PAGE_SIZE,
        _string(params.get("pageToken"), "pageToken", 1000), after,
        _boolean(params.get("includeArtifacts"), "includeArtifacts"),
    )


def read_empty(params: Any) -> None:
    """The parameters of `GetExtendedAgentCard`: nothing this server reads."""
    _optional_object(params, "params")
