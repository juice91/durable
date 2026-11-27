"""The plug-in's tables (its own Alembic branch, no foreign key into a core table) and every query on them.

Every row carries the tenant and every query filters by it. No password and no token is stored: an argon2id hash, a
keyed hash of each refresh token, and a digest of a throttled username."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    inspect,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection, Engine

USER_LIMIT = 10  # failures per username
IP_LIMIT = 20  # failures per client address
WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)

metadata = MetaData()

accounts = Table(
    "iam_localadmin_account",
    metadata,
    Column("tenant_id", String, primary_key=True),
    Column("username", String, nullable=False),
    Column("password_hash", String, nullable=False),
    Column("must_change", Boolean, nullable=False),
    # Who set the password: "env" (the initial password), "cli" (a reset), "setup" (the first-run page) or "user" (a
    # change). Only "env" follows EAP_LOCAL_ADMIN_INITIAL_PASSWORD.
    Column("source", String, nullable=False),
    # Bumped by every password change: tokens and sessions of an older one are dead.
    Column("version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# The one-time setup code (its digest) while there is no account.
setup_codes = Table(
    "iam_localadmin_setup",
    metadata,
    Column("tenant_id", String, primary_key=True),
    Column("code_hash", String, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

sessions = Table(
    "iam_localadmin_session",
    metadata,
    Column("id", String, primary_key=True),
    Column("tenant_id", String, nullable=False),
    Column("username", String, nullable=False),
    Column("refresh_hash", String, nullable=False),
    Column("previous_hash", String, nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
)

throttle = Table(
    "iam_localadmin_throttle",
    metadata,
    Column("tenant_id", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("attempts", Integer, nullable=False),
    Column("window_started_at", DateTime(timezone=True), nullable=False),
    Column("locked_until", DateTime(timezone=True), nullable=True),
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Account:
    username: str
    password_hash: str
    must_change: bool
    source: str
    version: int


@dataclass(frozen=True)
class Session:
    id: str
    username: str
    refresh_hash: str
    previous_hash: str | None
    version: int
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class Reservation:
    """Whether one more attempt may be made. `locked_for` is the seconds left when not; `lockout_started` when this
    call is the one that locked."""

    locked_for: int | None = None
    lockout_started: bool = False


class AdminStore:
    def __init__(self, engine: Engine, tenant_id: str, clock: Callable[[], datetime] = utcnow) -> None:
        self.engine = engine
        self.tenant_id = tenant_id
        self._clock = clock

    def missing_tables(self) -> list[str]:
        present = inspect(self.engine)
        return [name for name in metadata.tables if not present.has_table(name)]

    # --- the account -------------------------------------------------------------------------------------------------

    def account(self) -> Account | None:
        """The tenant's one admin account, or None before it is made."""
        with self.engine.connect() as c:
            return self._account(c)

    def _account(self, c: Connection) -> Account | None:
        row = c.execute(select(accounts).where(accounts.c.tenant_id == self.tenant_id)).mappings().first()
        if row is None:
            return None
        return Account(row["username"], row["password_hash"], row["must_change"], row["source"], row["version"])

    def _insert_account(self, c: Connection, username: str, password_hash: str, source: str, must_change: bool) -> bool:
        statement = insert(accounts).values(
            tenant_id=self.tenant_id, username=username, password_hash=password_hash, must_change=must_change,
            source=source, version=1, updated_at=self._clock(),
        ).on_conflict_do_nothing().returning(accounts.c.tenant_id)
        if c.execute(statement).first() is None:  # RETURNING: the driver's row count is not reliable here
            return False
        c.execute(delete(setup_codes).where(setup_codes.c.tenant_id == self.tenant_id))  # setup is over
        return True

    def create_account(self, username: str, password_hash: str, *, source: str) -> bool:
        """Make the admin with a password that must be changed. False when it already exists."""
        with self.engine.begin() as c:
            return self._insert_account(c, username, password_hash, source, must_change=True)

    # --- first-run setup ---------------------------------------------------------------------------------------------

    def save_setup_code(self, code_hash: str) -> bool:
        """Replace the tenant's setup code; False (nothing saved) when the account exists."""
        with self.engine.begin() as c:
            if self._account(c) is not None:
                return False
            c.execute(
                insert(setup_codes)
                .values(tenant_id=self.tenant_id, code_hash=code_hash, created_at=self._clock())
                .on_conflict_do_update(index_elements=["tenant_id"],
                                       set_={"code_hash": code_hash, "created_at": self._clock()})
            )
        return True

    def setup_code_hash(self) -> str | None:
        with self.engine.connect() as c:
            return c.execute(
                select(setup_codes.c.code_hash).where(setup_codes.c.tenant_id == self.tenant_id)
            ).scalar_one_or_none()

    def claim_setup(self, code_hash: str, username: str, password_hash: str) -> bool:
        """Spend the setup code on the account, atomically: of concurrent claims exactly one returns True."""
        with self.engine.begin() as c:
            current = c.execute(
                select(setup_codes.c.code_hash).where(setup_codes.c.tenant_id == self.tenant_id).with_for_update()
            ).scalar_one_or_none()  # a second claim waits here and then finds the row gone
            if current != code_hash:
                return False
            return self._insert_account(c, username, password_hash, "setup", must_change=False)

    def set_password(self, password_hash: str, *, expected_version: int, source: str, must_change: bool) -> int | None:
        """Replace the password and end every session of the account: the new version, or None when the account changed
        since `expected_version` was read."""
        with self.engine.begin() as c:
            changed = c.execute(
                update(accounts)
                .where(accounts.c.tenant_id == self.tenant_id, accounts.c.version == expected_version)
                .values(password_hash=password_hash, source=source, must_change=must_change,
                        version=expected_version + 1, updated_at=self._clock())
            ).rowcount
            if changed != 1:
                return None
            self._revoke(c, sessions.c.tenant_id == self.tenant_id)
        return expected_version + 1

    # --- sessions ----------------------------------------------------------------------------------------------------

    def create_session(self, session_id: str, username: str, refresh_hash: str, version: int, lifetime: timedelta) -> None:
        now = self._clock()
        with self.engine.begin() as c:
            c.execute(delete(sessions).where(sessions.c.tenant_id == self.tenant_id, sessions.c.expires_at <= now))
            c.execute(sessions.insert().values(
                id=session_id, tenant_id=self.tenant_id, username=username, refresh_hash=refresh_hash,
                previous_hash=None, version=version, created_at=now, expires_at=now + lifetime, revoked_at=None,
            ))

    def _one_session(self, c: Connection, session_id: str, *, lock: bool = False) -> Session | None:
        query = select(sessions).where(sessions.c.tenant_id == self.tenant_id, sessions.c.id == session_id)
        row = c.execute(query.with_for_update() if lock else query).mappings().first()
        if row is None:
            return None
        return Session(row["id"], row["username"], row["refresh_hash"], row["previous_hash"], row["version"],
                       row["expires_at"], row["revoked_at"])

    def live_session(self, session_id: str) -> Session | None:
        """The session when it is neither ended nor expired and its account's password has not changed since."""
        now = self._clock()
        query = (
            select(sessions.c.id)
            .join(accounts, (accounts.c.tenant_id == sessions.c.tenant_id) & (accounts.c.username == sessions.c.username))
            .where(sessions.c.tenant_id == self.tenant_id, sessions.c.id == session_id,
                   sessions.c.revoked_at.is_(None), sessions.c.expires_at > now,
                   sessions.c.version == accounts.c.version, accounts.c.must_change.is_(False))
        )
        with self.engine.connect() as c:
            return self._one_session(c, session_id) if c.execute(query).first() else None

    def rotate(self, session_id: str, presented: str, replacement: str) -> tuple[Session | None, bool]:
        """Swap the session's refresh hash `presented` for `replacement`: (the session, False). (None, True) when
        `presented` is the spent one, which ends the session; (None, False) for anything else."""
        if self.live_session(session_id) is None:
            return None, False
        with self.engine.begin() as c:
            session = self._one_session(c, session_id, lock=True)  # one rotation at a time
            if session is None or session.revoked_at is not None:
                return None, False
            if session.refresh_hash != presented:
                if session.previous_hash != presented:
                    return None, False
                self._revoke(c, sessions.c.id == session_id)
                return None, True
            c.execute(update(sessions).where(sessions.c.tenant_id == self.tenant_id, sessions.c.id == session_id)
                      .values(refresh_hash=replacement, previous_hash=presented))
        return session, False

    def end_session(self, session_id: str, refresh_hash: str) -> str | None:
        """End the session this refresh hash belongs to (current or just spent): its username, or None."""
        with self.engine.begin() as c:
            session = self._one_session(c, session_id, lock=True)
            if session is None or session.revoked_at is not None:
                return None
            if refresh_hash not in (session.refresh_hash, session.previous_hash):
                return None
            self._revoke(c, sessions.c.id == session_id)
        return session.username

    def _revoke(self, c: Connection, condition) -> None:  # noqa: ANN001
        c.execute(update(sessions).where(sessions.c.tenant_id == self.tenant_id, condition, sessions.c.revoked_at.is_(None))
                  .values(revoked_at=self._clock()))

    # --- throttling --------------------------------------------------------------------------------------------------

    def _throttled(self, c: Connection, key: str):  # noqa: ANN202
        return c.execute(
            select(throttle).where(throttle.c.tenant_id == self.tenant_id, throttle.c.key == key).with_for_update()
        ).mappings().one()

    def _save(self, c: Connection, key: str, **values) -> None:  # noqa: ANN003
        c.execute(update(throttle).where(throttle.c.tenant_id == self.tenant_id, throttle.c.key == key).values(**values))

    def reserve(self, key: str, limit: int) -> Reservation:
        """Count one attempt BEFORE the password is checked, so parallel attempts cannot exceed `limit`."""
        now = self._clock()
        # Rows whose window and lockout are over are forgotten, in a transaction of their own (no lock is held while
        # this attempt's row is taken).
        with self.engine.begin() as c:
            c.execute(delete(throttle).where(
                throttle.c.tenant_id == self.tenant_id, throttle.c.window_started_at <= now - WINDOW,
                or_(throttle.c.locked_until.is_(None), throttle.c.locked_until <= now), throttle.c.key != key,
            ))
        with self.engine.begin() as c:
            # The no-op update locks an existing row, so no concurrent clean-up takes it before it is read.
            c.execute(insert(throttle).values(
                tenant_id=self.tenant_id, key=key, attempts=0, window_started_at=now, locked_until=None
            ).on_conflict_do_update(index_elements=["tenant_id", "key"], set_={"key": key}))
            row = self._throttled(c, key)
            locked_until = row["locked_until"]
            if locked_until is not None and locked_until > now:
                return Reservation(locked_for=max(1, int((locked_until - now).total_seconds())))
            attempts, started = row["attempts"], row["window_started_at"]
            if locked_until is not None or started <= now - WINDOW:
                attempts, started = 0, now
            if attempts >= limit:  # that many are in flight and none has answered yet
                self._save(c, key, locked_until=now + LOCKOUT)
                return Reservation(locked_for=int(LOCKOUT.total_seconds()), lockout_started=True)
            self._save(c, key, attempts=attempts + 1, window_started_at=started, locked_until=None)
        return Reservation()

    def failed(self, key: str, limit: int) -> bool:
        """Record that a reserved attempt failed; True when it is the one that locks."""
        now = self._clock()
        with self.engine.begin() as c:
            row = c.execute(
                select(throttle).where(throttle.c.tenant_id == self.tenant_id, throttle.c.key == key).with_for_update()
            ).mappings().first()
            if row is None or row["attempts"] < limit or row["locked_until"] is not None:
                return False
            self._save(c, key, locked_until=now + LOCKOUT)
        return True

    def clear_throttle(self, *keys: str) -> None:
        """Forget the failures of `keys`, or of every key of the tenant when none is named."""
        condition = [throttle.c.tenant_id == self.tenant_id]
        if keys:
            condition.append(throttle.c.key.in_(keys))
        with self.engine.begin() as c:
            c.execute(delete(throttle).where(*condition))
