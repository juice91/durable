"""The A2A server of external agents (design 5.2): per tenant, under `/api/v1/external/a2a/{endpoint_id}`.

- GET  {base}/.well-known/agent-card.json                the public card (no credential; rate limited per address)
- POST {base}/rpc                                        JSON-RPC, A2A 1.0: SendMessage, GetTask, ListTasks,
                                                         CancelTask, GetExtendedAgentCard
- the same two under {base}/skills/{name}                for a client that cannot name a skill in a message

A run is a task. Every call is one or more calls into `ExternalRunService`, which holds every rule (ownership,
idempotency, quotas, deadlines, audit). The switch is `EAP_EXTERNAL_ACCESS_ENABLED`, off: 404; the door is the one of the
MCP surface (`eap_api.external_door`). No method approves or answers anything, and nothing is streamed or pushed.

Protocol refusals are JSON-RPC errors with HTTP 200. What the caller should simply repeat later keeps its HTTP status
(429 over a rate or quota, 503 not yet possible; both with Retry-After), with a JSON-RPC error body.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from eap_api.auth import run_actor
from eap_api.deps import database_engine_of, external_guard_of, external_limiter_of, open_session
from eap_api.external_a2a import protocol as p
from eap_api.external_a2a.cards import extended_card, public_card
from eap_api.external_a2a.tasks import task_of
from eap_api.external_door import caller_address, enter, not_found, printable_label, rate_limited, refusal
from eap_api.external_settings import ExternalSettings
from eap_api.service_deps.external_access import external_run_service_of
from eap_api.services.errors import Forbidden, InvalidInput, NotFound, ServiceError, TooManyRequests, Unavailable
from eap_api.services.external_wait import wait_for_run

log = logging.getLogger("eap_api.external_a2a")

PREFIX = "/api/v1/external/a2a"
CARD_PATH = "/.well-known/agent-card.json"
PROTOCOL = "a2a"
BLOCKING_SECONDS = 60  # how long a blocking send holds the connection before it answers the working task (Q6)
CARD_MAX_AGE = 300
EXTENDED_CARD_MAX_AGE = 30  # it differs by key and changes when an admin changes an export
RETRY_LATER_SECONDS = 2
_ENDPOINT_ID = re.compile(r"[0-9a-f]{32}")
_SKILL = re.compile(r"[a-z][a-z0-9_]{0,47}")
# a service refusal's code -> the field a caller would change
_FIELDS = {
    "input_invalid": "message.parts", "input_too_large": "message.parts", "deadline_invalid": "metadata.deadlineSeconds",
    "cursor_invalid": "pageToken", "context_mismatch": "message.contextId", "skill_required": "metadata.skill",
    "skill_mismatch": "metadata.skill", "export_not_found": "metadata.skill",
    "idempotency_conflict": "message.messageId", "idempotency_key_invalid": "message.messageId",
}
_UNSUPPORTED = frozenset({
    "task_terminal", "answer_not_supported", "grants_not_enforced", "workflow_archived", "start_refused",
})


def a2a_error(exc: ServiceError, task_id: str | None) -> p.A2AError:
    """The A2A error of a service refusal, its stable code in the details."""
    details = {"reason": exc.code}
    if task_id is not None:
        details["taskId"] = task_id
    if exc.code == "run_not_found":
        return p.A2AError(p.TASK_NOT_FOUND, details=details)
    if exc.code == "cancel_refused":
        return p.A2AError(p.TASK_NOT_CANCELABLE, details=details)
    if exc.code == "content_type_not_supported":
        return p.A2AError(p.CONTENT_TYPE_NOT_SUPPORTED, f"Incompatible content types: {exc.message}", details=details)
    if exc.code in _UNSUPPORTED:
        return p.A2AError(p.UNSUPPORTED_OPERATION, f"This operation is not supported: {exc.message}", details=details)
    field = _FIELDS.get(exc.code)
    if field is None:
        return p.A2AError(p.INTERNAL, details=details)
    found = exc.details if isinstance(exc, InvalidInput) and isinstance(exc.details, list) else []
    # Locations and messages the service made safe to echo: never the rejected value.
    violations = [
        (".".join([field, *(str(x) for x in e.get("loc") or ())])[:200], str(e.get("msg") or e.get("type")))
        for e in found
    ] or [(field, exc.message)]
    return p.A2AError(p.INVALID_PARAMS, f"Invalid parameters: {exc.message}", details=details, violations=violations)


def _answer(request_id: Any, *, result: Any = None, error: p.A2AError | None = None, status: int = 200,
            headers: dict[str, str] | None = None) -> Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is None:
        body["result"] = result
    else:
        body["error"] = error.body()
    return Response(
        json.dumps(body, default=str), status_code=status, media_type="application/json",
        headers={"Cache-Control": "no-store"} | (headers or {}),
    )


def _later(request_id: Any, status: int, exc: ServiceError, seconds: int) -> Response:
    error = p.A2AError(p.INTERNAL, exc.message, details={"reason": exc.code})
    return _answer(request_id, error=error, status=status, headers={"Retry-After": str(seconds)})


class _Call:
    """One request's caller. Each service call gets its own database session, in the threadpool."""

    def __init__(self, app: Any, principal: Any, base: str, endpoint_id: str, skill: str | None) -> None:
        self.app, self.principal, self.base = app, principal, base
        self.endpoint_id, self.skill = endpoint_id, skill

    async def service(self, method: str, *args: Any, **kwargs: Any) -> Any:
        def go() -> Any:
            with open_session(database_engine_of(self.app)) as session:
                return getattr(external_run_service_of(self.app, session), method)(*args, **kwargs)

        return await run_in_threadpool(go)

    async def send(self, params: Any) -> dict[str, Any]:
        sent = p.read_send(params)
        if sent.task_id is not None:
            await self.service("follow_up", self.principal, sent.task_id, sent.context_id)
        view = await self.service(
            "send", self.principal, PROTOCOL, message_id=sent.message_id, data=sent.data, text=sent.text,
            skill=sent.skill, base_skill=self.skill, context_id=sent.context_id,
            deadline_seconds=sent.deadline_seconds,
        )
        if not sent.return_immediately:
            started = view
            try:
                view = await wait_for_run(
                    view, lambda: self.service("get", self.principal, started["run_id"], task_fields=True),
                    BLOCKING_SECONDS,
                )
            except Forbidden:
                raise
            except Exception:  # noqa: BLE001 - the run exists: its task must reach the caller even if watching it failed
                log.exception("external a2a: waiting for a started run failed")
        return {"task": task_of(view)}

    async def get(self, params: Any) -> dict[str, Any]:
        return task_of(await self.service("get", self.principal, p.read_task_id(params), task_fields=True))

    async def cancel(self, params: Any) -> dict[str, Any]:
        done = await self.service(
            "cancel", self.principal, p.read_task_id(params), require_cancelable=True, task_fields=True
        )
        return task_of(done["run"])

    async def list(self, params: Any) -> dict[str, Any]:
        query = p.read_list(params)
        empty = {"tasks": [], "nextPageToken": "", "pageSize": query.page_size, "totalSize": 0}
        state = None
        if query.state is not None:
            state = p.TASK_STATES[query.state]
            if state is None:  # a valid state that no task here ever has
                return empty
        page = await self.service(
            "list", self.principal, state=state, limit=query.page_size, cursor=query.page_token,
            context_id=query.context_id, changed_after=query.changed_after, with_result=query.include_artifacts,
            with_total=True, task_fields=True,
        )
        return empty | {
            "tasks": [task_of(view) for view in page["items"]], "nextPageToken": page["next_cursor"] or "",
            "totalSize": page["total"],
        }

    async def card(self, params: Any) -> dict[str, Any]:
        p.read_empty(params)
        exports = await self.service("exports", self.principal, PROTOCOL)
        exports = sorted(
            (e for e in exports if self.skill in (None, e["name"])), key=lambda export: export["name"]
        )
        if not exports:
            raise p.A2AError(p.EXTENDED_CARD_NOT_CONFIGURED)
        endpoint = await self.service("public_endpoint", self.endpoint_id)
        return extended_card(self.base, endpoint, exports)


HANDLERS = {
    "SendMessage": _Call.send, "GetTask": _Call.get, "ListTasks": _Call.list, "CancelTask": _Call.cancel,
    "GetExtendedAgentCard": _Call.card,
}


class _Endpoint:
    """One route of the surface, as an ASGI app: the switch answers before the method is looked at."""

    methods: tuple[str, ...] = ()

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        request = Request(scope, receive)
        settings = ExternalSettings.from_env()
        endpoint_id = scope["path_params"]["endpoint_id"]
        skill = scope["path_params"].get("skill")
        if (
            not settings.enabled or not settings.base_url or not _ENDPOINT_ID.fullmatch(endpoint_id)
            or (skill is not None and not _SKILL.fullmatch(skill))
        ):
            response: Response = not_found()
        elif request.method not in self.methods:
            response = refusal(405, "method_not_allowed", "method not allowed", {"Allow": ", ".join(self.methods)})
        else:
            base = f"{settings.base_url}{PREFIX}/{endpoint_id}" + ("" if skill is None else f"/skills/{skill}")
            response = await self.answer(request, settings, base, endpoint_id, skill)
        await response(scope, receive, send)

    async def answer(
        self, request: Request, settings: ExternalSettings, base: str, endpoint_id: str, skill: str | None
    ) -> Response:
        raise NotImplementedError


class PublicCard(_Endpoint):
    methods = ("GET", "HEAD")

    async def answer(
        self, request: Request, settings: ExternalSettings, base: str, endpoint_id: str, skill: str | None
    ) -> Response:
        wait = external_guard_of(self.app, settings.ip_rate).acquire_card_ip(
            caller_address(request), settings.card_rate
        )
        if wait is not None:
            return rate_limited(wait)

        def read() -> dict[str, str]:
            with open_session(database_engine_of(self.app)) as session:
                return external_run_service_of(self.app, session).public_endpoint(endpoint_id)

        try:
            endpoint = await run_in_threadpool(read)
        except NotFound:
            return not_found()
        body = json.dumps(public_card(base, endpoint))
        etag = f'"{hashlib.sha256(body.encode()).hexdigest()[:32]}"'
        headers = {"ETag": etag, "Cache-Control": f"public, max-age={CARD_MAX_AGE}"}
        known = request.headers.get("if-none-match", "")
        if etag in (tag.strip().removeprefix("W/") for tag in known.split(",")):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)


class Rpc(_Endpoint):
    methods = ("POST",)

    async def answer(
        self, request: Request, settings: ExternalSettings, base: str, endpoint_id: str, skill: str | None
    ) -> Response:
        found = await enter(
            self.app, request, settings, endpoint_id=endpoint_id,
            label=lambda message, user_agent: printable_label(user_agent),
        )
        if isinstance(found, JSONResponse):
            return found
        request_id: Any = None
        method = "-"
        task_id: str | None = None
        who = run_actor(found.principal).subject
        try:
            rpc = p.read_envelope(found.body)
            request_id = rpc.id
            p.check_version(request.headers.get("a2a-version") or request.query_params.get("A2A-Version"))
            if rpc.method in p.STREAMING_METHODS:
                raise p.A2AError(p.UNSUPPORTED_OPERATION, "This operation is not supported: streaming is off")
            if rpc.method in p.PUSH_METHODS:
                raise p.A2AError(p.PUSH_NOT_SUPPORTED)
            if rpc.method not in p.METHODS:
                raise p.A2AError(p.METHOD_NOT_FOUND)
            method = rpc.method  # only now: the log never carries a name off the wire
            task_id = _task_of_params(rpc.params)
            limiter = external_limiter_of(self.app, settings.concurrent, settings.key_concurrent)
            lease = limiter.acquire(f"{found.principal.tenant_id}:{who}")
            if lease is None:
                refused = TooManyRequests(
                    "too many concurrent calls for this key; retry shortly", code="rate_limited", retry_after=1
                )
                return _later(request_id, 429, refused, 1)
            try:
                call = _Call(self.app, found.principal, base, endpoint_id, skill)
                result = await HANDLERS[method](call, rpc.params)
            finally:
                lease.release()
        except p.A2AError as exc:
            log.info("external a2a call method=%s outcome=error code=%s key=%s", method, exc.code, who)
            return _answer(request_id, error=exc)
        except ServiceError as exc:
            log.info("external a2a call method=%s outcome=error code=%s key=%s", method, exc.code, who)
            if isinstance(exc, TooManyRequests):
                return _later(request_id, 429, exc, exc.retry_after)
            if isinstance(exc, Unavailable):
                return _later(request_id, 503, exc, RETRY_LATER_SECONDS)
            if isinstance(exc, Forbidden):  # the key or the client stopped being good during the call
                return refusal(403, exc.code, exc.message)
            if exc.code in ("external_access_disabled", "endpoint_not_found"):  # switched off during the call
                return not_found()
            return _answer(request_id, error=a2a_error(exc, task_id))
        except Exception:  # noqa: BLE001 - never a traceback or an internal message to the caller
            log.exception("external a2a method %s failed", method)
            return _answer(request_id, error=p.A2AError(p.INTERNAL))
        log.info("external a2a call method=%s outcome=ok key=%s", method, who)
        headers = None
        if method == "GetExtendedAgentCard":
            headers = {"Cache-Control": f"private, max-age={EXTENDED_CARD_MAX_AGE}"}
        return _answer(request_id, result=result, headers=headers)


def _task_of_params(params: Any) -> str | None:
    """The task a request is about, for an error's details (bounded; None when it names none)."""
    if not isinstance(params, dict):
        return None
    message = params.get("message")
    value = message.get("taskId") if isinstance(message, dict) else params.get("id")
    return value[: p.ID_MAX_CHARS] if isinstance(value, str) and value else None


def mount_external_a2a(app: Any) -> None:
    """Serve the A2A surface under `/api/v1/external/a2a/{endpoint_id}` on `app`."""
    for base in (f"{PREFIX}/{{endpoint_id}}", f"{PREFIX}/{{endpoint_id}}/skills/{{skill}}"):
        app.router.routes.append(Route(base + CARD_PATH, PublicCard(app)))
        app.router.routes.append(Route(base + "/rpc", Rpc(app)))
