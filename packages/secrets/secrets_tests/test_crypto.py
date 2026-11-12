"""Authenticated encryption of small secrets with the platform key (EAP_SECRETS_KEY)."""

from __future__ import annotations

import base64
import os

import pytest

from eap_secrets import (
    KEY_ENV,
    OLD_KEYS_ENV,
    DecryptionFailed,
    Keyring,
    SecretsKeyInvalid,
    SecretsKeyMissing,
    generate_key,
    load_keyring,
)

SENTINEL = "sentinel-secret-7f3a9c"


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def test_round_trip_with_context_and_key_id():
    ring = Keyring.from_env({KEY_ENV: _key()})
    sealed = ring.encrypt(SENTINEL, context=b"t1/s1/authorization")
    assert sealed.key_id == ring.current_key_id
    assert SENTINEL.encode() not in sealed.ciphertext
    assert ring.decrypt(sealed.ciphertext, sealed.key_id, context=b"t1/s1/authorization") == SENTINEL


def test_each_encryption_uses_a_fresh_nonce():
    ring = Keyring.from_env({KEY_ENV: _key()})
    a = ring.encrypt(SENTINEL, context=b"c")
    b = ring.encrypt(SENTINEL, context=b"c")
    assert a.ciphertext != b.ciphertext


def test_wrong_context_tampering_or_wrong_key_fails_without_detail():
    ring = Keyring.from_env({KEY_ENV: _key()})
    sealed = ring.encrypt(SENTINEL, context=b"tenant-a")
    with pytest.raises(DecryptionFailed):
        ring.decrypt(sealed.ciphertext, sealed.key_id, context=b"tenant-b")
    tampered = bytearray(sealed.ciphertext)
    tampered[-1] ^= 1
    with pytest.raises(DecryptionFailed):
        ring.decrypt(bytes(tampered), sealed.key_id, context=b"tenant-a")
    other = Keyring.from_env({KEY_ENV: _key()})
    with pytest.raises(DecryptionFailed) as err:
        other.decrypt(sealed.ciphertext, sealed.key_id, context=b"tenant-a")
    assert SENTINEL not in str(err.value)


def test_rotation_old_keys_still_decrypt_new_writes_use_current():
    old, new = _key(), _key()
    before = Keyring.from_env({KEY_ENV: old}).encrypt(SENTINEL, context=b"c")
    rotated = Keyring.from_env({KEY_ENV: new, OLD_KEYS_ENV: old})
    assert rotated.decrypt(before.ciphertext, before.key_id, context=b"c") == SENTINEL
    after = rotated.encrypt(SENTINEL, context=b"c")
    assert after.key_id != before.key_id == Keyring.from_env({KEY_ENV: old}).current_key_id


def test_missing_key_is_a_clear_error_with_generation_instructions():
    with pytest.raises(SecretsKeyMissing) as err:
        load_keyring({})
    message = str(err.value)
    assert KEY_ENV in message and "base64" in message and "os.urandom(32)" in message


@pytest.mark.parametrize(
    "value",
    ["not base64 !!", base64.b64encode(b"short").decode(), base64.b64encode(os.urandom(31)).decode(), "  "],
)
def test_malformed_key_is_refused_without_echoing_it(value):
    with pytest.raises((SecretsKeyInvalid, SecretsKeyMissing)) as err:
        Keyring.from_env({KEY_ENV: value})
    if value.strip():
        assert value not in str(err.value)


def test_malformed_old_key_is_refused():
    with pytest.raises(SecretsKeyInvalid):
        Keyring.from_env({KEY_ENV: _key(), OLD_KEYS_ENV: "nope"})


def test_repr_never_shows_key_material():
    key = _key()
    ring = Keyring.from_env({KEY_ENV: key})
    assert key not in repr(ring) and key not in str(ring)
    sealed = ring.encrypt(SENTINEL, context=b"c")
    assert SENTINEL not in repr(sealed)


def test_generate_key_is_a_valid_platform_key():
    key = generate_key()
    assert len(base64.b64decode(key, validate=True)) == 32
    Keyring.from_env({KEY_ENV: key})
