"""argon2id hashing and the password policy."""

from __future__ import annotations

import threading

from argon2 import PasswordHasher, Type
from argon2.exceptions import Argon2Error, InvalidHashError

MIN_LENGTH = 12
MAX_LENGTH = 128  # bounds the work an unauthenticated caller can ask for
# One hash takes 64 MiB: at most this many at once in one process, whatever the number of callers.
MAX_CONCURRENT = 4


def encodable(value: str) -> bool:
    try:
        value.encode()
    except UnicodeError:  # a lone surrogate, which JSON can carry
        return False
    return True


def policy_problem(password: str, username: str) -> str | None:
    """Why `password` may not be the account's password, or None. The answer never repeats the password."""
    if len(password) < MIN_LENGTH:
        return f"The password must have at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"The password must have at most {MAX_LENGTH} characters."
    if not encodable(password):
        return "The password holds characters that cannot be stored."
    if password.casefold() == username.casefold():
        return "The password must not be the username."
    return None


class Passwords:
    """argon2id with the library's recommended parameters (a test passes cheaper ones)."""

    def __init__(self, **parameters: int) -> None:
        self._hasher = PasswordHasher(type=Type.ID, **parameters)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT)
        self._dummy = self._hasher.hash("no account has this password")

    def hash(self, password: str) -> str:
        with self._slots:
            return self._hasher.hash(password)

    def verify(self, hashed: str | None, password: str) -> bool:
        """True when `password` matches `hashed`. With no hash (no such account) or an unusable password, the same work
        is done against a dummy hash, so the time taken does not say which case it was."""
        usable = len(password) <= MAX_LENGTH and encodable(password)
        target = hashed if hashed is not None and usable else self._dummy
        with self._slots:
            try:
                self._hasher.verify(target, password if usable else "unusable")
            except (Argon2Error, InvalidHashError):
                return False
        return target is not self._dummy
