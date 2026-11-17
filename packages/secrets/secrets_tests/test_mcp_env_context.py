from __future__ import annotations

import base64
import os

import pytest

from eap_secrets import KEY_ENV, DecryptionFailed, Keyring, mcp_env_context, mcp_header_context


def test_env_context_binds_tenant_server_and_name() -> None:
    base = mcp_env_context("t1", "s1", "API_TOKEN")
    assert base == mcp_env_context("t1", "s1", "API_TOKEN")
    assert len({base, mcp_env_context("t2", "s1", "API_TOKEN"), mcp_env_context("t1", "s2", "API_TOKEN"),
                mcp_env_context("t1", "s1", "OTHER")}) == 4


def test_env_and_header_contexts_never_collide() -> None:
    assert mcp_env_context("t1", "s1", "Authorization") != mcp_header_context("t1", "s1", "Authorization")


def test_a_sealed_header_cannot_be_opened_as_an_env_value() -> None:
    ring = Keyring.from_env({KEY_ENV: base64.b64encode(os.urandom(32)).decode()})
    sealed = ring.encrypt("v", context=mcp_header_context("t1", "s1", "X-Key"))
    with pytest.raises(DecryptionFailed):
        ring.decrypt(sealed.ciphertext, sealed.key_id, context=mcp_env_context("t1", "s1", "X-Key"))
    env = ring.encrypt("v", context=mcp_env_context("t1", "s1", "X_KEY"))
    assert ring.decrypt(env.ciphertext, env.key_id, context=mcp_env_context("t1", "s1", "X_KEY")) == "v"
