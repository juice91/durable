"""The types of the IAM interfaces. Pure data: no web, database or plug-in imports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PrincipalKind = Literal["user", "api_key", "service", "run"]
ALL_KINDS: frozenset[str] = frozenset({"user", "api_key", "service", "run"})

# An action is a stable string `<verb>` or `<domain>.<verb>`; the constants below are the ones P0a uses.
Action = str
ACTION_RUN_AGUI = "run.agui"
ACTION_INGEST_OTLP = "ingest.otlp"
ACTION_MCP_READ = "mcp.read"
ACTION_MCP_MANAGE = "mcp.manage"


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
    (`run.agui`, `ingest.otlp`, `mcp.read`), or None for a route with no scope."""

    headers: Mapping[str, str] = field(default_factory=dict)
    peer_host: str | None = None
    accepts: frozenset[str] = ALL_KINDS
    action: Action | None = None

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

    def __init__(self, detail: str, status: int = 401, *, www_authenticate: bool = True) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status
        self.www_authenticate = www_authenticate


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
    `unavailable`); the entry point maps it to a status. `detail` never carries a credential."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- Policy engines that store grants and resource links (design 2.3, 5.6) ---------------------------------------------

# The only relations Studio may write as a per-resource grant; the others (`tenant`, `usable_by`, ...) are internal.
GRANT_RELATIONS: tuple[str, ...] = ("owner", "editor", "runner", "approver")


@dataclass(frozen=True)
class Grant:
    """`subject` (a user or service subject, never a role) holds `relation` on `resource`."""

    subject: str
    relation: str
    resource: ResourceRef


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
