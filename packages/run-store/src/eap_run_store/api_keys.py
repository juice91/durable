"""Tenant API keys (0017_api_keys): the credential third-party AG-UI frontends authenticate with (D16
of an internal design note).

A key reads `pos_<id>_<secret>`: `id` is 24 lowercase hex digits (not secret; it names the row and
is shown in lists), `secret` 43 URL-safe base64 characters (256 random bits). Only
`secret_hash = sha256("<id>:<secret>")` is stored: the plaintext exists only in the value
`ApiKeyStore.create` returns, once. Binding the id into the hash makes each hash unique to its row
(a per-key salt), so one row's hash can never authenticate another.

`authenticate` answers one way (None) for a malformed, unknown, wrong-secret, revoked or expired
key, compares the hash with `hmac.compare_digest` (also against a dummy hash when the id is
unknown or the token malformed, so every path does the same work), and writes `last_used_at` at
most once a minute per key. Nothing here logs a key or a hash.

Allowlists: a key with neither `agent_ids` nor `workflow_ids` (both NULL) reaches every agent and
workflow of its tenant. Once either is set the key is restricted on both: `create` stores the other
as `[]`, and `eap_api.auth.AgUiPrincipal` also treats a NULL list as "nothing" unless both are NULL.

Every read and write except `authenticate` (which learns the tenant from the row) is tenant-scoped.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from eap_run_store.store import Base

API_KEY_PREFIX = "pos_"
# `agui:run` runs agents and workflows over AG-UI; `mcp` reads the tenant's platform through the MCP control
# endpoint (/api/v1/mcp); `manage` implies `mcp` and also changes it. A key without either is refused there.
# `run` is an external agent's key (docs/external-access.md): it belongs to an external client and carries nothing else.
RUN_SCOPE = "run"
API_KEY_SCOPES = ("agui:run", "mcp", "manage", "otlp", RUN_SCOPE)
RUN_KEY_DEFAULT_EXPIRY_DAYS = 90
RUN_KEY_MAX_EXPIRY_DAYS = 365
DEFAULT_API_KEY_SCOPES = ("agui:run",)
API_KEY_NAME_MAX_CHARS = 100
API_KEY_ALLOWLIST_MAX_ITEMS = 100
API_KEY_ALLOWLIST_ID_MAX_CHARS = 200
API_KEY_ORIGINS_MAX_ITEMS = 20
API_KEY_ORIGIN_MAX_CHARS = 255
# Keys that are neither revoked nor expired, per tenant.
MAX_ACTIVE_API_KEYS_PER_TENANT = 100
# ... and per person, for the personal keys one person makes for themselves.
MAX_ACTIVE_API_KEYS_PER_OWNER = 20
API_KEY_OWNER_MAX_CHARS = 255
# last_used_at is written at most this often per key.
LAST_USED_THROTTLE_SECONDS = 60

# Same texts as migration 0017_api_keys.
API_KEY_ID_CHECK = "id ~ '^[0-9a-f]{24}$'"
API_KEY_SECRET_HASH_CHECK = "secret_hash ~ '^[0-9a-f]{64}$'"
API_KEY_NAME_CHECK = "char_length(name) BETWEEN 1 AND 100"
API_KEY_SCOPES_CHECK = "jsonb_typeof(scopes) = 'array'"
API_KEY_AGENT_IDS_CHECK = "agent_ids IS NULL OR jsonb_typeof(agent_ids) = 'array'"
API_KEY_WORKFLOW_IDS_CHECK = "workflow_ids IS NULL OR jsonb_typeof(workflow_ids) = 'array'"
API_KEY_ALLOWED_ORIGINS_CHECK = "allowed_origins IS NULL OR jsonb_typeof(allowed_origins) = 'array'"
# Same text as migration 0039_api_key_owner.
API_KEY_OWNER_CHECK = (
    "(owner_subject IS NULL AND owner_email IS NULL) OR "
    "(owner_subject IS NOT NULL AND char_length(owner_subject) BETWEEN 1 AND 255)"
)

# Same texts as migration 0043_external_access.
API_KEY_RUN_KEY_CHECK = (
    "NOT (scopes @> '[\"run\"]'::jsonb) OR (jsonb_array_length(scopes) = 1 AND external_client_id IS NOT NULL"
    " AND workflow_ids IS NOT NULL AND jsonb_array_length(workflow_ids) > 0 AND agent_ids IS NULL"
    " AND expires_at IS NOT NULL AND owner_subject IS NULL)"
)
API_KEY_EXTERNAL_CLIENT_CHECK = "external_client_id IS NULL OR scopes @> '[\"run\"]'::jsonb"

_TOKEN_RE = re.compile(r"pos_([0-9a-f]{24})_([A-Za-z0-9_-]{43})")
_KEY_ID_RE = re.compile(r"[0-9a-f]{24}")
# A dummy hash compared when there is no row, so a miss costs the same digest and comparison.
_DUMMY_HASH = hashlib.sha256(b"eap-api-key-dummy").hexdigest()


class ApiKeyModel(Base):
    """One tenant API key. See the module docstring and 0017_api_keys."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column("id", String, nullable=False)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    secret_hash: Mapped[str] = mapped_column("secret_hash", String, nullable=False)
    scopes: Mapped[list] = mapped_column(
        "scopes", JSONB, nullable=False, server_default=text("'[\"agui:run\"]'::jsonb")
    )
    agent_ids: Mapped[list | None] = mapped_column("agent_ids", JSONB(none_as_null=True), nullable=True)
    workflow_ids: Mapped[list | None] = mapped_column("workflow_ids", JSONB(none_as_null=True), nullable=True)
    allowed_origins: Mapped[list | None] = mapped_column("allowed_origins", JSONB(none_as_null=True), nullable=True)
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime | None] = mapped_column("expires_at", DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column("revoked_at", DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column("last_used_at", DateTime(timezone=True), nullable=True)
    owner_subject: Mapped[str | None] = mapped_column("owner_subject", Text, nullable=True)
    owner_email: Mapped[str | None] = mapped_column("owner_email", Text, nullable=True)
    external_client_id: Mapped[str | None] = mapped_column("external_client_id", PG_UUID(as_uuid=False), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("id", name="api_keys_pkey"),
        CheckConstraint(API_KEY_ID_CHECK, name="ck_api_keys_id"),
        CheckConstraint(API_KEY_SECRET_HASH_CHECK, name="ck_api_keys_secret_hash"),
        CheckConstraint(API_KEY_NAME_CHECK, name="ck_api_keys_name"),
        CheckConstraint(API_KEY_SCOPES_CHECK, name="ck_api_keys_scopes"),
        CheckConstraint(API_KEY_AGENT_IDS_CHECK, name="ck_api_keys_agent_ids"),
        CheckConstraint(API_KEY_WORKFLOW_IDS_CHECK, name="ck_api_keys_workflow_ids"),
        CheckConstraint(API_KEY_ALLOWED_ORIGINS_CHECK, name="ck_api_keys_allowed_origins"),
        CheckConstraint(API_KEY_OWNER_CHECK, name="ck_api_keys_owner"),
        Index("ix_api_keys_tenant_created", "tenant_id", "created_at"),
        Index("ix_api_keys_tenant_owner", "tenant_id", "owner_subject"),
        ForeignKeyConstraint(
            ["tenant_id", "external_client_id"], ["external_clients.tenant_id", "external_clients.id"],
            name="fk_api_keys_external_client",
        ),
        CheckConstraint(API_KEY_RUN_KEY_CHECK, name="ck_api_keys_run_key"),
        CheckConstraint(API_KEY_EXTERNAL_CLIENT_CHECK, name="ck_api_keys_external_client"),
        Index(
            "ix_api_keys_external_client", "tenant_id", "external_client_id",
            postgresql_where=text("external_client_id IS NOT NULL"),
        ),
    )


@dataclass(frozen=True)
class ApiKeyRecord:
    """A key as the API shows it: never the secret or its hash."""

    id: str
    tenant_id: str
    name: str
    scopes: tuple[str, ...]
    agent_ids: tuple[str, ...] | None
    workflow_ids: tuple[str, ...] | None
    allowed_origins: tuple[str, ...] | None
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    # The person a personal key belongs to (subject, and the e-mail they had at creation); None for a tenant key.
    owner_subject: str | None = None
    owner_email: str | None = None
    # A `run` key: the external client it belongs to.
    external_client_id: str | None = None

    @property
    def prefix(self) -> str:
        """The non-secret start of the key, `pos_<id>_`, to recognise it by."""
        return f"{API_KEY_PREFIX}{self.id}_"

    def status(self, now: datetime | None = None) -> str:
        """`revoked`, `expired` or `active`."""
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= (now or _now()):
            return "expired"
        return "active"


@dataclass(frozen=True)
class CreatedApiKey:
    """`ApiKeyStore.create`'s result: the record and the plaintext key, shown once. The key is
    left out of repr so a logged or printed value never carries it."""

    record: ApiKeyRecord
    key: str = field(repr=False)


class ApiKeyLimitExceeded(Exception):
    """The tenant already has MAX_ACTIVE_API_KEYS_PER_TENANT active keys, or (`per_owner`) the person already has
    MAX_ACTIVE_API_KEYS_PER_OWNER personal ones. Pickle-safe."""

    def __init__(self, tenant_id: str, limit: int, per_owner: bool = False):
        super().__init__(tenant_id, limit, per_owner)
        self.tenant_id = tenant_id
        self.limit = limit
        self.per_owner = per_owner

    def __str__(self) -> str:
        if self.per_owner:
            return f"at most {self.limit} active personal API keys per person; revoke one first"
        return f"at most {self.limit} active API keys per tenant; revoke one first"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_secret(key_id: str, secret: str) -> str:
    """The stored hash of a key: sha256 hex of `<id>:<secret>`."""
    return hashlib.sha256(f"{key_id}:{secret}".encode()).hexdigest()


def parse_api_key(token: str) -> tuple[str, str] | None:
    """(id, secret) of a well-formed `pos_<id>_<secret>`, else None."""
    if not isinstance(token, str):
        return None
    match = _TOKEN_RE.fullmatch(token)
    return (match.group(1), match.group(2)) if match else None


def looks_like_api_key(token: str) -> bool:
    """True when the credential claims to be an API key (starts with `pos_`), well-formed or not."""
    return isinstance(token, str) and token.startswith(API_KEY_PREFIX)


def normalize_origin(origin: str) -> str:
    """`scheme://host[:port]`, lowercased, for an http(s) origin with no path, query, fragment or
    credentials (ValueError otherwise). A default port is dropped, as browsers send it."""
    if not isinstance(origin, str) or not origin or len(origin) > API_KEY_ORIGIN_MAX_CHARS:
        raise ValueError("an origin is a non-empty string of at most 255 characters")
    if "*" in origin:
        # No wildcard origins, in any position: an allowlist names each origin exactly (D17).
        raise ValueError(f"wildcards are not allowed in an origin; list each origin: {origin!r}")
    try:
        parts = urlsplit(origin.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"not an origin: {origin!r}") from exc
    scheme = parts.scheme.lower()
    if (
        scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(f"not an origin (scheme://host[:port]): {origin!r}")
    host = parts.hostname.lower()
    if not host.isascii():
        # Browsers send an internationalized host in its punycode (A-label) form.
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"not an origin (bad internationalized host): {origin!r}") from None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}" if port in (None, default) else f"{scheme}://{host}:{port}"


def _validated_ids(values: Iterable[str] | None, what: str) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError(f"{what} must be a list of ids")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value) > API_KEY_ALLOWLIST_ID_MAX_CHARS:
            raise ValueError(f"{what}: each id is a non-empty string of at most {API_KEY_ALLOWLIST_ID_MAX_CHARS} characters")
        if value not in out:
            out.append(value)
    if len(out) > API_KEY_ALLOWLIST_MAX_ITEMS:
        raise ValueError(f"{what}: at most {API_KEY_ALLOWLIST_MAX_ITEMS} ids")
    return out


def _validated_origins(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        raise ValueError("allowed_origins must be a list of origins")
    out: list[str] = []
    for value in values:
        origin = normalize_origin(value)
        if origin not in out:
            out.append(origin)
    if len(out) > API_KEY_ORIGINS_MAX_ITEMS:
        raise ValueError(f"allowed_origins: at most {API_KEY_ORIGINS_MAX_ITEMS} origins")
    return out


def _record(row: ApiKeyModel) -> ApiKeyRecord:
    def _tuple(value: list | None) -> tuple[str, ...] | None:
        return None if value is None else tuple(value)

    return ApiKeyRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        scopes=tuple(row.scopes or ()),
        agent_ids=_tuple(row.agent_ids),
        workflow_ids=_tuple(row.workflow_ids),
        allowed_origins=_tuple(row.allowed_origins),
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
        owner_subject=row.owner_subject,
        owner_email=row.owner_email,
        external_client_id=None if row.external_client_id is None else str(row.external_client_id),
    )


def _run_key_expiry(
    scopes: list[str], external_client_id: str | None, agent_ids: list[str] | None, workflow_ids: list[str] | None,
    expires_at: datetime | None, owner_subject: str | None,
) -> datetime | None:
    """The rules of an external client's `run` key (ValueError names the broken one); its expiry, defaulted. A key
    without that scope keeps `expires_at` and may name no client."""
    if RUN_SCOPE not in scopes:
        if external_client_id is not None:
            raise ValueError("only a key with the run scope belongs to an external client")
        return expires_at
    if scopes != [RUN_SCOPE]:
        raise ValueError("a run key carries no other scope")
    if external_client_id is None:
        raise ValueError("a run key belongs to an external client")
    if owner_subject is not None:
        raise ValueError("a personal key cannot carry the run scope")
    if agent_ids is not None:
        raise ValueError("a run key reaches no agent")
    if not workflow_ids:
        raise ValueError("a run key needs a workflow allowlist")
    if expires_at is None:
        return _now() + timedelta(days=RUN_KEY_DEFAULT_EXPIRY_DAYS)
    if expires_at.tzinfo is not None and expires_at > _now() + timedelta(days=RUN_KEY_MAX_EXPIRY_DAYS):
        raise ValueError(f"a run key expires at most {RUN_KEY_MAX_EXPIRY_DAYS} days from now")
    return expires_at


def _active_clause(now: Any):
    return (ApiKeyModel.revoked_at.is_(None)) & (
        ApiKeyModel.expires_at.is_(None) | (ApiKeyModel.expires_at > now)
    )


def _touch(session: Session, key_id: str) -> datetime | None:
    """`last_used_at = now()` unless it was set less than LAST_USED_THROTTLE_SECONDS ago."""
    return session.execute(
        update(ApiKeyModel)
        .where(
            ApiKeyModel.id == key_id,
            ApiKeyModel.last_used_at.is_(None)
            | (ApiKeyModel.last_used_at < func.now() - text(f"interval '{LAST_USED_THROTTLE_SECONDS} seconds'")),
        )
        .values(last_used_at=func.now())
        .returning(ApiKeyModel.last_used_at)
    ).scalar_one_or_none()


class ApiKeyStore:
    """Tenant API keys: create (the plaintext once), list, get, revoke, authenticate."""

    def __init__(self, engine):
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def create(
        self,
        tenant_id: str,
        *,
        name: str,
        created_by: str,
        scopes: Iterable[str] = DEFAULT_API_KEY_SCOPES,
        agent_ids: Iterable[str] | None = None,
        workflow_ids: Iterable[str] | None = None,
        allowed_origins: Iterable[str] | None = None,
        expires_at: datetime | None = None,
        owner_subject: str | None = None,
        owner_email: str | None = None,
        external_client_id: str | None = None,
    ) -> CreatedApiKey:
        """A new key (ValueError on invalid input; ApiKeyLimitExceeded past the tenant's cap).

        Allowlists are deduplicated in order. Both `agent_ids` and `workflow_ids` None is a
        tenant-wide key; when only one is given, the other is stored as [] (nothing). `allowed_origins`
        None means any origin. Origins
        are normalized (normalize_origin). `expires_at` must be timezone-aware and in the future.

        `owner_subject` makes a personal key (with the owner's `owner_email` as a display snapshot): it counts
        against MAX_ACTIVE_API_KEYS_PER_OWNER as well as the tenant's cap. None is a tenant key.

        A `run` key (an external agent's) names `external_client_id` (a client of this tenant), carries no other scope,
        has a non-empty workflow allowlist and no agent one, and expires: `expires_at` None means
        RUN_KEY_DEFAULT_EXPIRY_DAYS, at most RUN_KEY_MAX_EXPIRY_DAYS."""
        if owner_subject is not None and (
            not isinstance(owner_subject, str) or not owner_subject or len(owner_subject) > API_KEY_OWNER_MAX_CHARS
        ):
            raise ValueError("owner_subject is a non-empty string of at most 255 characters")
        if owner_subject is None:
            owner_email = None
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required")
        if not isinstance(created_by, str) or not created_by:
            raise ValueError("created_by is required")
        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name or len(clean_name) > API_KEY_NAME_MAX_CHARS:
            raise ValueError(f"name is 1 to {API_KEY_NAME_MAX_CHARS} characters")
        if isinstance(scopes, str):
            raise ValueError("scopes must be a list")
        clean_scopes: list[str] = []
        for scope in scopes:
            if scope not in API_KEY_SCOPES:
                raise ValueError(f"unknown scope {scope!r}; known scopes: {', '.join(API_KEY_SCOPES)}")
            if scope not in clean_scopes:
                clean_scopes.append(scope)
        if not clean_scopes:
            raise ValueError("a key needs at least one scope")
        clean_agents = _validated_ids(agent_ids, "agent_ids")
        clean_workflows = _validated_ids(workflow_ids, "workflow_ids")
        expires_at = _run_key_expiry(
            clean_scopes, external_client_id, clean_agents, clean_workflows, expires_at, owner_subject
        )
        # A key restricted on one dimension is restricted on both: the omitted allowlist is stored
        # as [] (nothing), so the row says what the key can do. Only both omitted is tenant-wide.
        if clean_agents is not None and clean_workflows is None:
            clean_workflows = []
        elif clean_workflows is not None and clean_agents is None and RUN_SCOPE not in clean_scopes:
            clean_agents = []
        clean_origins = _validated_origins(allowed_origins)
        if expires_at is not None:
            if expires_at.tzinfo is None:
                raise ValueError("expires_at must include a timezone")
            if expires_at <= _now():
                raise ValueError("expires_at must be in the future")

        key_id = secrets.token_hex(12)
        secret = secrets.token_urlsafe(32)
        with self.Session() as session, session.begin():
            # Serialize creates per tenant so the active-key cap holds under concurrency.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"api_keys:{tenant_id}"}
            )
            active = session.execute(
                select(func.count())
                .select_from(ApiKeyModel)
                .where(ApiKeyModel.tenant_id == tenant_id, _active_clause(func.now()))
            ).scalar_one()
            if active >= MAX_ACTIVE_API_KEYS_PER_TENANT:
                raise ApiKeyLimitExceeded(tenant_id, MAX_ACTIVE_API_KEYS_PER_TENANT)
            if owner_subject is not None:
                owned = session.execute(
                    select(func.count())
                    .select_from(ApiKeyModel)
                    .where(
                        ApiKeyModel.tenant_id == tenant_id,
                        ApiKeyModel.owner_subject == owner_subject,
                        _active_clause(func.now()),
                    )
                ).scalar_one()
                if owned >= MAX_ACTIVE_API_KEYS_PER_OWNER:
                    raise ApiKeyLimitExceeded(tenant_id, MAX_ACTIVE_API_KEYS_PER_OWNER, per_owner=True)
            if external_client_id is not None and session.execute(
                text("SELECT 1 FROM external_clients WHERE tenant_id = :t AND id::text = :c"),
                {"t": tenant_id, "c": str(external_client_id)},
            ).first() is None:
                raise ValueError("external client not found")
            row = ApiKeyModel(
                id=key_id,
                tenant_id=tenant_id,
                name=clean_name,
                secret_hash=hash_secret(key_id, secret),
                scopes=clean_scopes,
                agent_ids=clean_agents,
                workflow_ids=clean_workflows,
                allowed_origins=clean_origins,
                created_by=created_by,
                expires_at=expires_at,
                owner_subject=owner_subject,
                owner_email=owner_email[:320] if owner_email else None,
                external_client_id=external_client_id,
            )
            session.add(row)
            session.flush()
            session.refresh(row)
            record = _record(row)
        return CreatedApiKey(record=record, key=f"{API_KEY_PREFIX}{key_id}_{secret}")

    def list(self, tenant_id: str) -> list[ApiKeyRecord]:
        """The tenant's keys, revoked and expired ones included, newest first."""
        with self.Session() as session:
            rows = session.execute(
                select(ApiKeyModel)
                .where(ApiKeyModel.tenant_id == tenant_id)
                .order_by(ApiKeyModel.created_at.desc(), ApiKeyModel.id.desc())
            ).scalars()
            return [_record(row) for row in rows]

    def list_owned(self, tenant_id: str, owner_subject: str) -> list[ApiKeyRecord]:
        """The personal keys `owner_subject` made in the tenant (revoked and expired included), newest first. Never
        a tenant key, never another person's."""
        if not owner_subject:
            return []
        with self.Session() as session:
            rows = session.execute(
                select(ApiKeyModel)
                .where(ApiKeyModel.tenant_id == tenant_id, ApiKeyModel.owner_subject == owner_subject)
                .order_by(ApiKeyModel.created_at.desc(), ApiKeyModel.id.desc())
            ).scalars()
            return [_record(row) for row in rows]

    def revoke_owned(self, tenant_id: str, owner_subject: str, key_id: str) -> ApiKeyRecord | None:
        """Revoke one of `owner_subject`'s personal keys (idempotent); None for a key that is not theirs: unknown,
        another person's, or a tenant key."""
        if not owner_subject or not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            return None
        with self.Session() as session, session.begin():
            row = session.execute(
                update(ApiKeyModel)
                .where(
                    ApiKeyModel.tenant_id == tenant_id,
                    ApiKeyModel.id == key_id,
                    ApiKeyModel.owner_subject == owner_subject,
                )
                .values(revoked_at=func.coalesce(ApiKeyModel.revoked_at, func.now()))
                .returning(ApiKeyModel)
            ).scalar_one_or_none()
            return None if row is None else _record(row)

    def list_run_keys(self, tenant_id: str, external_client_id: str | None = None) -> list[ApiKeyRecord]:
        """The tenant's `run` keys (of one external client when given), revoked and expired included, newest first."""
        query = select(ApiKeyModel).where(ApiKeyModel.tenant_id == tenant_id, ApiKeyModel.external_client_id.is_not(None))
        if external_client_id is not None:
            query = query.where(ApiKeyModel.external_client_id == str(external_client_id))
        with self.Session() as session:
            rows = session.execute(query.order_by(ApiKeyModel.created_at.desc(), ApiKeyModel.id.desc())).scalars()
            return [_record(row) for row in rows]

    def get(self, tenant_id: str, key_id: str) -> ApiKeyRecord | None:
        """One of the tenant's keys, else None (another tenant's key is None too)."""
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            return None
        with self.Session() as session:
            row = session.execute(
                select(ApiKeyModel).where(ApiKeyModel.tenant_id == tenant_id, ApiKeyModel.id == key_id)
            ).scalar_one_or_none()
            return None if row is None else _record(row)

    def revoke(self, tenant_id: str, key_id: str) -> ApiKeyRecord | None:
        """Revoke one of the tenant's keys (idempotent: the first revocation time stays); None when
        the tenant has no such key."""
        if not isinstance(key_id, str) or not _KEY_ID_RE.fullmatch(key_id):
            return None
        with self.Session() as session, session.begin():
            row = session.execute(
                update(ApiKeyModel)
                .where(ApiKeyModel.tenant_id == tenant_id, ApiKeyModel.id == key_id)
                .values(revoked_at=func.coalesce(ApiKeyModel.revoked_at, func.now()))
                .returning(ApiKeyModel)
            ).scalar_one_or_none()
            return None if row is None else _record(row)

    def authenticate(self, token: str, *, touch: bool = True) -> ApiKeyRecord | None:
        """The active key `token` names, else None: one answer for malformed, unknown, wrong-secret,
        revoked and expired keys. Constant-time hash comparison on every path. On success,
        `last_used_at` is updated at most once per LAST_USED_THROTTLE_SECONDS; `touch=False`
        leaves it to a later `touch_last_used` (a caller that may still refuse the request, e.g.
        for its scope or origin, so a refused request does not look like a use)."""
        parsed = parse_api_key(token)
        if parsed is None:
            hmac.compare_digest(hash_secret("0" * 24, "x"), _DUMMY_HASH)
            return None
        key_id, secret = parsed
        with self.Session() as session, session.begin():
            row = session.execute(select(ApiKeyModel).where(ApiKeyModel.id == key_id)).scalar_one_or_none()
            expected = row.secret_hash if row is not None else _DUMMY_HASH
            matches = hmac.compare_digest(hash_secret(key_id, secret), expected)
            if row is None or not matches:
                return None
            record = _record(row)
            if record.status() != "active":
                return None
            touched = _touch(session, key_id) if touch else None
            if touched is not None:
                record = replace(record, last_used_at=touched)
            return record

    def touch_last_used(self, key_id: str) -> datetime | None:
        """Record a use of the key (throttled like `authenticate`); the new `last_used_at`, or None
        when the throttle skipped it or the key is gone."""
        with self.Session() as session, session.begin():
            return _touch(session, key_id)
