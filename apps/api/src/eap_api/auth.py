"""Authentication dependencies for the API. Every call goes through the IAM identity chain (`eap_iam`).

`current_principal` (people routes), `agui_principal` (`/api/v1/ag-ui/*`) and `otlp_principal` build a `RequestView`,
ask the chain configured by EAP_IAM_IDENTITY (plug-ins `apikey`, `devloopback`, ...) and adapt the core `Principal` to
the two compatibility shapes the routes and services were written against: `Principal(tenant_id, user_id)` and
`AgUiPrincipal`. The credential code lives in the plug-ins; nothing here knows what a key or a dev bearer is.
"""

import re
from typing import Annotated, Any, Literal

from eap_iam import (
    ACTION_INGEST_OTLP,
    ACTION_INGEST_WEBHOOK,
    ACTION_RUN_AGUI,
    AuthError,
    IamConfigError,
    RequestView,
    require_iam_configured,
)
from eap_iam import Principal as IamPrincipal
from eap_iam.config import AUTH_CONFIGURATION_ERROR, MODE_API_KEY, MODE_DEV, MODES, auth_mode as _auth_mode
from eap_iam.messages import DEV_AUTH_DISABLED_DETAIL, INVALID_API_KEY_DETAIL, ORIGIN_NOT_ALLOWED_DETAIL
from eap_observability import bind_log_context
from eap_run_store import RunActor, bind_actor, normalize_origin
from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from eap_api.deps import iam_of


class Principal(BaseModel):
    """Represents an authenticated principal (tenant + user).

    `iam` is the core principal the identity chain built (roles included): what the authorisation service asks the
    policy engine about. It is None for a principal made by hand (a test, a script); `core_principal` then builds the
    plain user principal for `user_id`."""

    tenant_id: str
    user_id: str
    iam: IamPrincipal | None = Field(default=None, exclude=True, repr=False)


def core_principal(principal: "Principal | AgUiPrincipal") -> IamPrincipal:
    """The core principal behind a compatibility principal (see `Principal.iam`)."""
    if principal.iam is not None:
        return principal.iam
    if isinstance(principal, AgUiPrincipal) and principal.kind == "api_key":
        return IamPrincipal(
            tenant_id=principal.tenant_id, kind="api_key", subject=str(principal.key_id), provider="compat",
            scopes=frozenset(principal.scopes), agent_ids=principal.agent_ids, workflow_ids=principal.workflow_ids,
        )
    if isinstance(principal, AgUiPrincipal) and principal.kind == "architect_run":
        return IamPrincipal(tenant_id=principal.tenant_id, kind="run", subject=str(principal.user_id), provider="compat")
    return IamPrincipal(tenant_id=principal.tenant_id, kind="user", subject=str(principal.user_id), provider="compat")


# Regex for tenant/user of the dev bearer: non-empty, [A-Za-z0-9_-]+ (enforced by the devloopback plug-in).
TENANT_USER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
AUTH_MODE_DEV = MODE_DEV
AUTH_MODE_API_KEY = MODE_API_KEY
AUTH_MODES = MODES

__all__ = [
    "AUTH_CONFIGURATION_ERROR",
    "DEV_AUTH_DISABLED_DETAIL",
    "INVALID_API_KEY_DETAIL",
    "ORIGIN_NOT_ALLOWED_DETAIL",
    "IamConfigError",
    "require_iam_configured",
]


def auth_mode() -> str | None:
    """The configured EAP_AUTH_MODE when it is one of AUTH_MODES, else None."""
    import os

    return _auth_mode(os.environ)


def _view(
    request: Request, accepts: frozenset[str], action: str | None, route_params: dict[str, str] | None = None,
    body: bytes | None = None,
) -> RequestView:
    client = request.client  # the TCP peer as the ASGI server reports it; no proxy header is ever consulted here
    return RequestView(
        headers={name.lower(): value for name, value in request.headers.items()},
        peer_host=client.host if client else None,
        accepts=accepts,
        action=action,
        route_params=route_params or {},
        body=body,
    )


def refusal(exc: AuthError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=exc.detail,
        headers={"WWW-Authenticate": "Bearer"} if exc.www_authenticate and exc.status == status.HTTP_401_UNAUTHORIZED else None,
    )


async def authenticate_request(
    request: Request, accepts: frozenset[str], action: str | None = None
) -> IamPrincipal:
    """Run the identity chain for `request`: the principal, or the HTTPException the refusal maps to. The log
    context for the rest of the request carries eap.tenant_id (the logs API filters on it)."""
    try:
        principal = await iam_of(request.app).chain.authenticate(_view(request, accepts, action))
    except AuthError as exc:
        raise refusal(exc) from None
    bind_log_context(tenant_id=principal.tenant_id)
    request.state.iam_principal = principal  # read by the audit middleware (eap_api.audit)
    bind_actor(run_actor(principal))
    return principal


WEBHOOK_KINDS = frozenset({"webhook"})


async def authenticate_webhook(request: Request, trigger_id: str, body: bytes) -> IamPrincipal:
    """The principal of `POST /api/v1/hooks/{trigger_id}`: the trigger, proven by its HMAC signature over `body`, and
    nothing else. The route accepts ONLY the `webhook` kind, so the chain never consults the dev bearer, an Access JWT or
    an API key (an `Authorization` header is not read). An AuthError (the route renders it uniformly) carries a `reason`
    for counters only."""
    view = _view(request, WEBHOOK_KINDS, ACTION_INGEST_WEBHOOK, {"trigger_id": trigger_id}, body)
    principal = await iam_of(request.app).chain.authenticate(view)
    bind_log_context(tenant_id=principal.tenant_id)
    request.state.iam_principal = principal
    bind_actor(run_actor(principal))
    return principal


def run_actor(principal: IamPrincipal) -> RunActor:
    """Who a run started by this request acts as (design 4.1): the user, the key (for its creator), or a run."""
    owner = principal.extra.get("owner") if principal.kind == "api_key" else None
    if owner:  # a personal API key: the run is the owner's
        return RunActor(kind="user", subject=str(owner), on_behalf_of=str(owner))
    on_behalf_of = principal.subject if principal.kind == "user" else principal.on_behalf_of
    roles = tuple(sorted(principal.roles)) if principal.kind == "user" else None
    return RunActor(kind=principal.kind, subject=principal.subject, on_behalf_of=on_behalf_of, roles=roles)


def bind_agui_actor(principal: "AgUiPrincipal") -> None:
    """The same binding for the compatibility shape (the MCP control endpoint authenticates to it): a run a tool call
    starts names who started it."""
    if principal.kind == "api_key" and principal.owner_subject:
        actor = RunActor(kind="user", subject=principal.owner_subject, on_behalf_of=principal.owner_subject)
    elif principal.kind == "api_key":
        actor = RunActor(kind="api_key", subject=str(principal.key_id))
    elif principal.kind == "architect_run":
        actor = RunActor(kind="run", subject=str(principal.user_id))
    else:
        roles = () if principal.iam is None else tuple(sorted(principal.iam.roles))
        actor = RunActor(kind="user", subject=str(principal.user_id), on_behalf_of=str(principal.user_id), roles=roles)
    bind_actor(actor)


# --- people routes: the dev bearer (and, later, user identity providers) ---------------------------

USER_KINDS = frozenset({"user"})


PEOPLE_KINDS = frozenset({"user", "service"})


async def iam_principal(request: Request) -> IamPrincipal:
    """The core principal of a people route (a user or a service identity, never an API key or a run token): the
    dependency of `GET /api/v1/iam/capabilities`. Keys stay refused outside their own routes."""
    return await authenticate_request(request, PEOPLE_KINDS, None)


CorePrincipalDep = Annotated[IamPrincipal, Depends(iam_principal)]

_IN_PROCESS_CHAIN: Any = None


async def _in_process_principal(authorization: str | None) -> IamPrincipal:
    """A direct Python call with no HTTP request (a test, a script): there is no network peer, so the call is as
    local as a loopback one. HTTP never reaches this: FastAPI always injects the request."""
    global _IN_PROCESS_CHAIN
    if _IN_PROCESS_CHAIN is None:
        from eap_iam import load_iam

        _IN_PROCESS_CHAIN = load_iam().chain
    headers = {"authorization": authorization} if authorization is not None else {}
    try:
        principal = await _IN_PROCESS_CHAIN.authenticate(RequestView(headers=headers, peer_host="127.0.0.1", accepts=USER_KINDS))
    except AuthError as exc:
        raise refusal(exc) from None
    bind_log_context(tenant_id=principal.tenant_id)
    bind_actor(run_actor(principal))
    return principal


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,  # kept for the OpenAPI contract; the chain reads the request
    request: Request = None,  # type: ignore[assignment]
) -> Principal:
    """The user behind a people route: tenant_id and user_id from a `kind="user"` identity provider (the dev bearer
    over loopback today). Raises 401 when no provider recognises the request; API keys are never accepted here."""
    if request is None:
        principal = await _in_process_principal(authorization)
    else:
        principal = await authenticate_request(request, USER_KINDS, None)
    return Principal(tenant_id=principal.tenant_id, user_id=principal.subject, iam=principal)


def feedback_viewer(principal: Any) -> str | None:
    """The user whose own feedback may be shown, or None: API keys have no user, so they see none (a plain dev
    `Principal` is never a key)."""
    if not principal.user_id or getattr(principal, "is_api_key", False):
        return None
    return principal.user_id


# --- AG-UI principals: a tenant API key or the dev bearer (AGUI.3; D16 of the AG-UI plan) -------

AGUI_RUN_SCOPE = "agui:run"
MISSING_SCOPE_DETAIL = f"API key lacks the {AGUI_RUN_SCOPE} scope"
AGENT_NOT_FOUND_DETAIL = "Agent not found"
WORKFLOW_NOT_FOUND_DETAIL = "Workflow not found"
OTLP_SCOPE = "otlp"
MISSING_OTLP_SCOPE_DETAIL = f"API key lacks the {OTLP_SCOPE} scope"
KEY_ROUTE_KINDS = frozenset({"user", "api_key"})
MCP_ROUTE_KINDS = frozenset({"user", "api_key", "run"})


class AgUiPrincipal(BaseModel):
    """Who calls an AG-UI route: a tenant API key (`kind="api_key"`) or the dev bearer (`"dev"`). The MCP control
    endpoint also takes `"architect_run"`: a live run of the built-in Architect calling its `control__*` tools
    (`user_id` is `architect-run:<run id>`; see `eap_api.mcp_control.auth`).

    A key carries its scopes and optional allowlists. Both `agent_ids` and `workflow_ids` None is
    a tenant-wide key; once either is set, a None list on the other dimension allows nothing
    (fail closed). `allowed_origins` None means any origin. The dev bearer has no limits. Keys may not run drafts or override the model (D16)."""

    tenant_id: str
    kind: Literal["dev", "api_key", "architect_run"]
    user_id: str | None = None
    key_id: str | None = None
    scopes: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] | None = None
    workflow_ids: tuple[str, ...] | None = None
    allowed_origins: tuple[str, ...] | None = None
    # A personal API key: the person it acts for and the key's prefix (`pos_<id>_`). None for a tenant key.
    owner_subject: str | None = None
    via_api_key: str | None = None
    # The core principal the chain built (see `Principal.iam`); never serialised.
    iam: IamPrincipal | None = Field(default=None, exclude=True, repr=False)

    @property
    def is_api_key(self) -> bool:
        return self.kind == "api_key"

    @property
    def may_run_drafts(self) -> bool:
        return self.kind == "dev"

    @property
    def may_override_model(self) -> bool:
        return self.kind == "dev"

    @property
    def default_resource_id(self) -> str:
        """The resource of a conversation derived from a thread when the client names none (D3):
        the dev bearer's user id, else `apikey:<key_id>`."""
        if self.kind == "architect_run":
            return str(self.user_id)
        return self.user_id if self.kind == "dev" and self.user_id else f"apikey:{self.key_id}"

    @property
    def is_unrestricted(self) -> bool:
        """Neither allowlist is set: every agent and workflow of the tenant (the dev bearer, or a
        key created without allowlists)."""
        return self.agent_ids is None and self.workflow_ids is None

    def allows_agent(self, agent_id: str) -> bool:
        """Fail closed: a NULL agent list allows everything only when the workflow list is NULL too."""
        if self.is_unrestricted:
            return True
        return self.agent_ids is not None and str(agent_id) in self.agent_ids

    def allows_workflow(self, workflow_id: str) -> bool:
        """Fail closed: a NULL workflow list allows everything only when the agent list is NULL too."""
        if self.is_unrestricted:
            return True
        return self.workflow_ids is not None and str(workflow_id) in self.workflow_ids


def to_agui_principal(principal: IamPrincipal, dev_scopes: tuple[str, ...] = ()) -> AgUiPrincipal:
    """The compatibility shape of a core principal: a key, a live Architect run or a user (the dev bearer)."""
    if principal.kind == "api_key":
        order = principal.extra.get("scope_order")
        return AgUiPrincipal(
            tenant_id=principal.tenant_id,
            kind="api_key",
            key_id=principal.subject,
            scopes=tuple(order) if order is not None else tuple(sorted(principal.scopes)),
            agent_ids=principal.agent_ids,
            workflow_ids=principal.workflow_ids,
            allowed_origins=principal.allowed_origins,
            owner_subject=principal.extra.get("owner"),
            via_api_key=principal.extra.get("via_api_key"),
            iam=principal,
        )
    if principal.kind == "run":
        return AgUiPrincipal(
            tenant_id=principal.tenant_id,
            kind="architect_run",
            user_id=principal.subject,
            scopes=tuple(sorted(principal.scopes, reverse=True)),
            iam=principal,
        )
    return AgUiPrincipal(
        tenant_id=principal.tenant_id, kind="dev", user_id=principal.subject, scopes=dev_scopes, iam=principal
    )


async def agui_principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> AgUiPrincipal:  # noqa: ARG001
    """The principal of an AG-UI route (`/api/v1/ag-ui/*` only; every other route keeps `current_principal`, so a
    key is refused there).

    `Authorization: Bearer pos_<id>_<secret>` is an API key: it works whatever EAP_AUTH_MODE is. A malformed,
    unknown, wrong-secret, revoked or expired key is 401 `Invalid API key`; a key without the `agui:run` scope is 403.
    Anything else is the dev bearer, which needs EAP_AUTH_MODE=dev (else 401 `dev bearer authentication is not
    enabled`). The key itself is never logged."""
    principal = await authenticate_request(request, KEY_ROUTE_KINDS, ACTION_RUN_AGUI)
    return to_agui_principal(principal, (AGUI_RUN_SCOPE,))


async def otlp_principal(request: Request, authorization: Annotated[str | None, Header()] = None) -> AgUiPrincipal:  # noqa: ARG001
    """The principal of `POST /api/v1/otlp/v1/traces`: a tenant API key with the `otlp` scope, or the dev bearer
    (EAP_AUTH_MODE=dev). Unknown, revoked or expired key: 401 `Invalid API key`; a key without the scope: 403. The
    tenant is the key's, never anything the payload says. An `Origin` header (a browser exporter) must be one the key
    allows; exporters outside a browser send none."""
    principal = await authenticate_request(request, KEY_ROUTE_KINDS, ACTION_INGEST_OTLP)
    return to_agui_principal(principal, (OTLP_SCOPE,))


def _normalized_origins(origins: tuple[str, ...]) -> set[str]:
    """A key's stored origins, normalized again (they are normalized on write; a hand-edited row
    may not be). An entry that is not an origin matches nothing."""
    out: set[str] = set()
    for entry in origins:
        try:
            out.add(normalize_origin(entry))
        except ValueError:
            continue
    return out


def require_allowed_origin(allowed_origins: tuple[str, ...] | None, origin: str | None) -> None:
    """The per-key origin check (D17; the AGUI.3 review obligation): a present `Origin` not in a non-NULL
    `allowed_origins` (both sides normalized by `eap_run_store.normalize_origin`) is 403. No `Origin` header (a
    server-side caller) passes, and a NULL list allows any origin. An `Origin` that is not an http(s) origin (`null`,
    from a sandboxed frame or a file) never matches. The `apikey` plug-in applies the same rule inside the chain."""
    if allowed_origins is None or origin is None:
        return
    try:
        normalized = normalize_origin(origin)
    except ValueError:
        normalized = None
    if normalized is None or normalized not in _normalized_origins(allowed_origins):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=ORIGIN_NOT_ALLOWED_DETAIL)


def require_agui_agent(principal: AgUiPrincipal, agent_id: str) -> None:
    """404 `Agent not found` for an agent outside the key's allowlist (the same answer as an
    agent that does not exist, so a key cannot probe for ids). The caller still loads the agent
    tenant-scoped, which gives the same 404 for another tenant's agent."""
    if not principal.allows_agent(agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AGENT_NOT_FOUND_DETAIL)


def require_agui_workflow(principal: AgUiPrincipal, workflow_id: str) -> None:
    """404 `Workflow not found` for a workflow outside the key's allowlist (see require_agui_agent)."""
    if not principal.allows_workflow(workflow_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKFLOW_NOT_FOUND_DETAIL)


AgUiPrincipalDep = Annotated[AgUiPrincipal, Depends(agui_principal)]
