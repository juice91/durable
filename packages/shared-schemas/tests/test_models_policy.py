"""Tests for the platform model allowlist."""

from __future__ import annotations

import pickle

import pytest

from eap_schemas.models_policy import (
    ALLOWED_MODELS_ENV,
    ModelNotAllowed,
    ModelPolicy,
)


# 1. None/blank parses to the default `test:*` allowlist.
@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_blank_gives_default_entries(value):
    assert ModelPolicy.parse(value).entries == ("test:*",)


# 2. Entries are stripped and kept in order; blank items (trailing comma) are skipped.
def test_parse_strips_and_keeps_order():
    policy = ModelPolicy.parse("litellm:gemma4, anthropic:*")
    assert policy.entries == ("litellm:gemma4", "anthropic:*")


def test_parse_skips_empty_items_from_trailing_comma():
    assert ModelPolicy.parse("a:b,").entries == ("a:b",)


# 3. Malformed entries raise ValueError.
@pytest.mark.parametrize(
    "bad",
    ["nocolon", ":name", "provider:", "*:x", "pro*:x", "litellm:gem*", "litellm:*x"],
)
def test_malformed_entries_raise(bad):
    with pytest.raises(ValueError):
        ModelPolicy.parse(bad)


# 4. `allows`: exact match, whole-name wildcard, provider isolation, case sensitivity, blanks refused.
def test_allows_exact_match():
    policy = ModelPolicy.parse("litellm:gemma4")
    assert policy.allows("litellm", "gemma4")
    assert not policy.allows("litellm", "gemma5")


def test_allows_wildcard_name_for_provider():
    policy = ModelPolicy.parse("litellm:*")
    assert policy.allows("litellm", "gemma4")
    assert policy.allows("litellm", "anything")


def test_allows_different_provider_refused():
    policy = ModelPolicy.parse("litellm:*")
    assert not policy.allows("anthropic", "claude-opus-5-5")


def test_allows_is_case_sensitive():
    policy = ModelPolicy.parse("litellm:gemma4")
    assert not policy.allows("LiteLLM", "gemma4")


def test_allows_refuses_empty_provider_or_name():
    policy = ModelPolicy.parse("test:*")
    assert not policy.allows("", "ticket")
    assert not policy.allows("test", "")


# 5. `check` returns None when allowed, raises ModelNotAllowed otherwise.
def test_check_returns_none_when_allowed():
    policy = ModelPolicy.parse("test:*")
    assert policy.check("test", "ticket") is None


def test_check_raises_with_provider_and_name():
    policy = ModelPolicy.parse("test:*")
    with pytest.raises(ModelNotAllowed) as exc:
        policy.check("litellm", "gemma4")
    assert exc.value.provider == "litellm"
    assert exc.value.name == "gemma4"
    assert "litellm:gemma4" in str(exc.value)
    assert ALLOWED_MODELS_ENV in str(exc.value)


# 6. ModelNotAllowed survives pickling.
def test_not_allowed_pickles():
    policy = ModelPolicy.parse("test:*")
    with pytest.raises(ModelNotAllowed) as exc:
        policy.check("litellm", "gemma4")
    restored = pickle.loads(pickle.dumps(exc.value))
    assert restored.provider == "litellm"
    assert restored.name == "gemma4"
    assert str(restored) == str(exc.value)


# 7. from_env: unset/blank -> default; explicit entry parses.
def test_from_env_unset_matches_parse_none():
    assert ModelPolicy.from_env({}) == ModelPolicy.parse(None)


def test_from_env_explicit_entry():
    assert ModelPolicy.from_env({"EAP_ALLOWED_MODELS": "x:y"}).entries == ("x:y",)


# 8. str(policy) joins entries with commas.
def test_str_joins_entries_with_commas():
    policy = ModelPolicy.parse("litellm:gemma4, anthropic:*")
    assert str(policy) == "litellm:gemma4,anthropic:*"


# Extra: a name containing a colon is matched whole (first colon splits).
def test_name_with_colon_is_matched_whole():
    policy = ModelPolicy.parse("litellm:org:model")
    assert policy.allows("litellm", "org:model")
    assert not policy.allows("litellm", "org")
