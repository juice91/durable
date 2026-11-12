"""EAP secrets: authenticated encryption of small tenant secrets with the platform key.

The platform key comes from the environment, never from the database:

- `EAP_SECRETS_KEY`: the current key, base64 of exactly 32 random bytes. New secrets are encrypted
  with it. Generate one with
  `python -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())'`.
- `EAP_SECRETS_OLD_KEYS` (optional): comma-separated earlier keys, kept only to decrypt secrets
  written before a rotation.

Each key has a key id (a fingerprint of the key, never the key itself) stored next to every
ciphertext, so decryption picks the right key and a rotation needs no schema change: set the new key,
move the old one to EAP_SECRETS_OLD_KEYS, re-encrypt at leisure.

Ciphertext is AES-256-GCM: `nonce (12 bytes) || ciphertext || tag`. The caller passes a `context`
(e.g. tenant, owner id and field name) as associated data, so a ciphertext copied to another row or
tenant fails to decrypt. Failures never say why and never contain plaintext or key material.

`eap_secrets.outbound` holds the outbound connection policy (SSRF guard) used for every connection
to a tenant-supplied URL.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ENV = "EAP_SECRETS_KEY"
OLD_KEYS_ENV = "EAP_SECRETS_OLD_KEYS"
KEY_BYTES = 32
NONCE_BYTES = 12
GENERATE_COMMAND = "python -c 'import base64,os; print(base64.b64encode(os.urandom(32)).decode())'"


class SecretsError(RuntimeError):
    """Base class; messages never contain key material or plaintext."""


class SecretsKeyMissing(SecretsError):
    def __init__(self) -> None:
        super().__init__(
            f"{KEY_ENV} is not set; it is required to store or use encrypted secrets (MCP server "
            f"headers). Set it to base64 of 32 random bytes, generated with: {GENERATE_COMMAND}"
        )


class SecretsKeyInvalid(SecretsError):
    def __init__(self, variable: str) -> None:
        super().__init__(
            f"{variable} is not a valid key: expected base64 of exactly {KEY_BYTES} bytes; "
            f"generate one with: {GENERATE_COMMAND}"
        )


class DecryptionFailed(SecretsError):
    def __init__(self) -> None:
        super().__init__("secret could not be decrypted (unknown key id, wrong context or corrupted ciphertext)")


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: bytes
    key_id: str

    def __repr__(self) -> str:
        return f"SealedSecret(key_id={self.key_id!r}, ciphertext=<{len(self.ciphertext)} bytes>)"


def generate_key() -> str:
    """A fresh platform key in the EAP_SECRETS_KEY format."""
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def key_id_for(key: bytes) -> str:
    """A stable, non-reversible id for `key` (16 hex chars of a domain-separated SHA-256)."""
    return hashlib.sha256(b"eap-secrets/key-id/v1\x00" + key).hexdigest()[:16]


def _decode_key(value: str, variable: str) -> bytes:
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SecretsKeyInvalid(variable) from None
    if len(raw) != KEY_BYTES:
        raise SecretsKeyInvalid(variable)
    return raw


class Keyring:
    """The current key plus any older keys still needed for decryption."""

    def __init__(self, current: bytes, old: tuple[bytes, ...] = ()) -> None:
        self._current_id = key_id_for(current)
        self._keys: dict[str, AESGCM] = {key_id_for(k): AESGCM(k) for k in old}
        self._keys[self._current_id] = AESGCM(current)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Keyring:
        """Build from EAP_SECRETS_KEY / EAP_SECRETS_OLD_KEYS; SecretsKeyMissing/Invalid otherwise."""
        env = os.environ if env is None else env
        value = env.get(KEY_ENV, "")
        if not value.strip():
            raise SecretsKeyMissing()
        current = _decode_key(value, KEY_ENV)
        old = tuple(
            _decode_key(part, OLD_KEYS_ENV) for part in env.get(OLD_KEYS_ENV, "").split(",") if part.strip()
        )
        return cls(current, old)

    @property
    def current_key_id(self) -> str:
        return self._current_id

    def encrypt(self, plaintext: str, *, context: bytes) -> SealedSecret:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._keys[self._current_id].encrypt(nonce, plaintext.encode("utf-8"), context)
        return SealedSecret(ciphertext=nonce + sealed, key_id=self._current_id)

    def decrypt(self, ciphertext: bytes, key_id: str, *, context: bytes) -> str:
        aead = self._keys.get(key_id)
        if aead is None or len(ciphertext) <= NONCE_BYTES:
            raise DecryptionFailed()
        try:
            plain = aead.decrypt(ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:], context)
        except InvalidTag:
            raise DecryptionFailed() from None
        return plain.decode("utf-8")

    def __repr__(self) -> str:
        return f"Keyring(current_key_id={self._current_id!r}, keys={len(self._keys)})"

    __str__ = __repr__


def load_keyring(env: Mapping[str, str] | None = None) -> Keyring:
    """The platform keyring from the environment; raises a clear SecretsKeyMissing when unset."""
    return Keyring.from_env(env)


__all__ = [
    "GENERATE_COMMAND",
    "KEY_ENV",
    "OLD_KEYS_ENV",
    "DecryptionFailed",
    "Keyring",
    "SealedSecret",
    "SecretsError",
    "SecretsKeyInvalid",
    "SecretsKeyMissing",
    "generate_key",
    "key_id_for",
    "load_keyring",
]
