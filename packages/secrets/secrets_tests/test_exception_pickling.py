"""eap_secrets exceptions survive pickling (OutboundBlocked is raised on the worker's MCP path,
where DBOS pickles step errors) with the same fields and message."""

from __future__ import annotations

import pickle

import pytest

from eap_secrets import KEY_ENV, DecryptionFailed, SecretsKeyInvalid, SecretsKeyMissing
from eap_secrets.outbound import OutboundBlocked


@pytest.mark.parametrize("exc,fields", [
    (OutboundBlocked("address_blocked", "loopback"), ("code", "address_class")),
    (OutboundBlocked("url_scheme"), ("code", "address_class")),
    (SecretsKeyMissing(), ()),
    (SecretsKeyInvalid(KEY_ENV), ("variable",)),
    (DecryptionFailed(), ()),
])
def test_round_trip(exc, fields):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert {f: getattr(restored, f) for f in fields} == {f: getattr(exc, f) for f in fields}


def test_messages_are_unchanged():
    assert str(OutboundBlocked("address_blocked", "loopback")) == "outbound connection refused: address_blocked (loopback)"
    assert str(OutboundBlocked("url_scheme")) == "outbound connection refused: url_scheme"
    assert str(DecryptionFailed()).startswith("secret could not be decrypted")
    assert str(SecretsKeyMissing()).startswith(f"{KEY_ENV} is not set")
    assert str(SecretsKeyInvalid("X")).startswith("X is not a valid key")
