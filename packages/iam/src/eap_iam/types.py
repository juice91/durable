"""The types of the IAM interfaces. Pure data: no web, database or plug-in imports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PrincipalKind = Literal["user", "api_key", "service", "run", "webhook"]
# `webhook` is a machine principal a verified inbound webhook delivery produces (the `webhook` identity plug-in); it is
# never part of the default of `RequestView.accepts`, so only the hook route (which names it) can ever receive one.
ALL_KINDS: frozenset[str] = frozenset({"user", "api_key", "service", "run"})

# An action is a stable string `<verb>` or `<domain>.<verb>`; the constants below are the ones P0a uses.
Action = str
ACTION_RUN_AGUI = "run.agui"
ACTION_INGEST_OTLP = "ingest.otlp"
ACTION_MCP_READ = "mcp.read"
ACTION_MCP_MANAGE = "mcp.manage"
ACTION_INGEST_WEBHOOK = "ingest.webhook"
# An external agent starting, following or cancelling an exported workflow (the `run` API-key scope; docs/external-access.md).
ACTION_RUN_EXTERNAL = "run.external"


class Principal(BaseModel):
    """Who a request acts as. Immutable; built once per request by the identity chain.

    `kind="run"` is a run acting for another principal (the Architect's run token). `scopes` and the three
    allowlists are constraints carried by API-key principals; user principals leave them empty / None."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    kind: PrincipalKind
    subject: str  # user: user id or e-mail; api_key: key id; service: service id; run: run id
    provider: str  # which IdentityProvider produced it: "loopback-dev", "api-key", ...
    on_behalf_of: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()
    agent_ids: tuple[str, ...] | None = None
    workflow_ids: tuple[str, ...] | None = None
    allowed_origins: tuple[str, ...] | None = None
    # Provider-specific extras a compatibility adapter may need; never logged, never in API answers.
    extra: Mapping[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResourceRef:
    kind: str  # "agent", "workflow", ...
    id: str | None  # None: a collection or "any of this kind"
    tenant_id: str
    owner: str | None = None


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str  # stable code, e.g. "role:admin", "deny:no_grant"
    explain: dict[str, Any] = Field(default_factory=dict)  # never contains secrets


@dataclass(frozen=True)
class All:
    """Every id of the kind (no SQL predicate)."""


@dataclass(frozen=True)
class Only:
    ids: tuple[str, ...]


@dataclass(frozen=True)
class Except:
    ids: tuple[str, ...]


IdFilter = All | Only | Except

_LOOPBACK_NAMES = frozenset({"localhost"})
# Headers that prove a request passed a proxy: such a request is never loopback, whatever its peer address says.
PROXY_HEADERS = ("cf-access-jwt-assertion", "x-forwarded-for", "cf-connecting-ip", "forwarded", "x-real-ip")


def is_loopback_address(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ip_address(host)
    except ValueError:
        return False  # "localhost" and test peers are not addresses: the ASGI server reports an IP
    mapped = getattr(ip, "ipv4_mapped", None)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


@dataclass(frozen=True)
class RequestView:
    """A read-only view of one request, built by the entry point, so a provider never imports a web framework.

    `headers` has lower-cased names. `peer_host` is the TCP peer address the ASGI server reports, with no proxy
    header considered. `accepts` are the principal kinds the route takes; `action` names what the route does
    (`run.agui`, `ingest.otlp`, `mcp.read`), or None for a route with no scope. `route_params` (path parameters)
    and `body` (the raw bytes, read once by the entry point) are set only by routes whose credential is a signature
    over the body (`ingest.webhook`)."""

    headers: Mapping[str, str] = field(default_factory=dict)
    peer_host: str | None = None
    accepts: frozenset[str] = ALL_KINDS
    action: Action | None = None
    route_params: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    @property
    def has_proxy_headers(self) -> bool:
        return any(name in self.headers for name in PROXY_HEADERS)

    @property
    def is_loopback(self) -> bool:
        """The TCP peer is 127.0.0.1 / ::1 and the request carries no proxy header."""
        return is_loopback_address(self.peer_host) and not self.has_proxy_headers


class AuthError(Exception):
    """A request presented this provider's credential and it is invalid. The chain answers `status` (401 by
    default; 403 for a valid key that may not do this) and stops: a bad credential never falls through."""

    def __init__(
        self, detail: str, status: int = 401, *, www_authenticate: bool = True, reason: str | None = None
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.www_authenticate = www_authenticate
        # A stable code for counters and logs only (`signature_mismatch`, `not_enabled`): never sent to the caller.
        self.reason = reason


@dataclass(frozen=True)
class SessionTokens:
    """A signed-in session of a password provider: a short-lived access token (the bearer) and the refresh token that
    renews it. Neither is ever shown by `repr`."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int  # seconds the access token lives
    tenant_id: str
    subject: str


@dataclass(frozen=True)
class SignInResult:
    """What a correct password gets: a session, or none (`tokens` None) because the password must be changed first."""

    tenant_id: str
    subject: str
    tokens: SessionTokens | None


class SignInError(Exception):
    """A password sign-in, change or refresh was refused. `code` is stable (`invalid_credentials`, `locked`,
    `weak_password`, `invalid_token`; of the first-run setup also `invalid_setup_code`, `invalid_username`,
    `setup_closed`); `detail` is the text for the caller and never carries what was typed.

    `tenant_id` and `subject` are for the audit log: the account's subject only when the attempt named it, else a fixed
    placeholder (what a person typed into the username field may be a password). `lockout_started` marks the one attempt
    that locked, `reused` a spent refresh token that ended its session."""

    def __init__(
        self, code: str, detail: str, *, tenant_id: str, subject: str, retry_after: int | None = None,
        lockout_started: bool = False, reused: bool = False,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.tenant_id = tenant_id
        self.subject = subject
        self.retry_after = retry_after
        self.lockout_started = lockout_started
        self.reused = reused


class IamConfigError(RuntimeError):
    """The IAM configuration refuses to start (unknown or duplicate plug-in name, no policy engine, ...)."""


ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
ROLE_VIEWER = "viewer"
MEMBER_ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_EDITOR, ROLE_VIEWER)


class Member(BaseModel):
    """One member of a tenant as the membership source reports it. `id` is the source's stable user id (the ZITADEL
    user id), the handle of every change; `roles` are the project roles it holds."""

    model_config = ConfigDict(frozen=True)

    id: str
    email: str | None = None
    display_name: str | None = None
    roles: tuple[str, ...] = ()
    status: str = "active"  # "active" | "invited" | "inactive"


class MembershipError(Exception):
    """A membership change was refused. `code` is stable (`not_found`, `last_admin`, `invalid`, `conflict`,
    `no_user_creation`, `unavailable`); the entry point maps it to a status. `detail` never carries a credential."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- Policy engines that store grants and resource links (design 2.3, 5.6) ---------------------------------------------

# The only relations Studio may write as a per-resource grant; the others (`tenant`, `granted_to`, ...) are internal.
GRANT_RELATIONS: tuple[str, ...] = ("owner", "editor", "runner", "approver", "viewer")
GRANT_SUBJECT_USER = "user"
GRANT_SUBJECT_ROLE = "role"


@dataclass(frozen=True)
class Grant:
    """`subject` holds `relation` on `resource`. `subject_type` says what the subject is: `user` (a user or service
    subject) or `role` (a tenant role, `admin` / `editor` / `viewer`: everyone who holds it in the resource's tenant)."""

    subject: str
    relation: str
    resource: ResourceRef
    subject_type: str = GRANT_SUBJECT_USER


@dataclass(frozen=True)
class GrantableRelation:
    """A relation a kind accepts as a grant, and the tenant roles that may hold it (empty: people only)."""

    relation: str
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccessRequirement:
    """What lets a caller do an action: any of these tenant `roles`, or any of these grants (`relations`) on the
    resource. Facts of the policy model: it names nobody."""

    roles: tuple[str, ...] = ()
    relations: tuple[str, ...] = ()


class PolicyUnavailable(Exception):
    """The policy engine could not answer (unreachable, timed out, or it answered with an error). Fail closed: the entry
    point answers 503 and nothing is allowed. `detail` names the call and the status, never a credential or a body."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class GrantError(Exception):
    """A grant or registration was refused as malformed (`code`: `invalid_relation`, `invalid_subject`,
    `invalid_resource`). Nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CatalogEntry:
    """One platform resource as the catalog lists it: what the engine needs to link it to its tenant and owner.
    `creator` is the subject recorded when it was created (None: not recorded; a legacy value such as `operator`)."""

    kind: str
    id: str
    tenant_id: str
    creator: str | None = None
