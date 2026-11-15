"""Tenant API keys (0017_api_keys): the credential third-party AG-UI frontends authenticate with (D16
of docs/plans/2026-11-13-milestone-ag-ui.md).

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
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from sqlalchemy import CheckConstraint, DateTime, Index, PrimaryKeyConstraint, String, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from eap_run_store.store import Base

API_KEY_PREFIX = "pos_"
API_KEY_SCOPES = ("agui:run",)
DEFAULT_API_KEY_SCOPES = ("agui:run",)
API_KEY_NAME_MAX_CHARS = 100
API_KEY_ALLOWLIST_MAX_ITEMS = 100
API_KEY_ALLOWLIST_ID_MAX_CHARS = 200
API_KEY_ORIGINS_MAX_ITEMS = 20
API_KEY_ORIGIN_MAX_CHARS = 255
# Keys that are neither revoked nor expired, per tenant.
MAX_ACTIVE_API_KEYS_PER_TENANT = 100
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

    __table_args__ = (
        PrimaryKeyConstraint("id", name="api_keys_pkey"),
        CheckConstraint(API_KEY_ID_CHECK, name="ck_api_keys_id"),
        CheckConstraint(API_KEY_SECRET_HASH_CHECK, name="ck_api_keys_secret_hash"),
        CheckConstraint(API_KEY_NAME_CHECK, name="ck_api_keys_name"),
        CheckConstraint(API_KEY_SCOPES_CHECK, name="ck_api_keys_scopes"),
        CheckConstraint(API_KEY_AGENT_IDS_CHECK, name="ck_api_keys_agent_ids"),
        CheckConstraint(API_KEY_WORKFLOW_IDS_CHECK, name="ck_api_keys_workflow_ids"),
        CheckConstraint(API_KEY_ALLOWED_ORIGINS_CHECK, name="ck_api_keys_allowed_origins"),
        Index("ix_api_keys_tenant_created", "tenant_id", "created_at"),
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
    """The tenant already has MAX_ACTIVE_API_KEYS_PER_TENANT active keys. Pickle-safe."""

    def __init__(self, tenant_id: str, limit: int):
        super().__init__(tenant_id, limit)
        self.tenant_id = tenant_id
        self.limit = limit

    def __str__(self) -> str:
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
    )


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
    ) -> CreatedApiKey:
        """A new key (ValueError on invalid input; ApiKeyLimitExceeded past the tenant's cap).

        Allowlists are deduplicated in order. Both `agent_ids` and `workflow_ids` None is a
        tenant-wide key; when only one is given, the other is stored as [] (nothing). `allowed_origins`
        None means any origin. Origins
        are normalized (normalize_origin). `expires_at` must be timezone-aware and in the future."""
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
        # A key restricted on one dimension is restricted on both: the omitted allowlist is stored
        # as [] (nothing), so the row says what the key can do. Only both omitted is tenant-wide.
        if clean_agents is not None and clean_workflows is None:
            clean_workflows = []
        elif clean_workflows is not None and clean_agents is None:
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
