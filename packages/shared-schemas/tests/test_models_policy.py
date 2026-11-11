"""Tests for the platform model allowlist."""

import pytest

from eap_schemas.models_policy import ModelNotAllowed, ModelPolicy


def test_default_is_test_models_only():
    for value in (None, "", "   "):
        policy = ModelPolicy.parse(value)
        assert policy.allows("test", "ticket")
        assert policy.allows("test", "anything")
        assert not policy.allows("litellm", "qwen3.8-thinking")
        assert not policy.allows("openai", "gpt-4")


def test_from_env_unset_uses_default():
    assert ModelPolicy.from_env({}).entries == ("test:*",)


def test_exact_and_wildcard_entries():
    policy = ModelPolicy.from_env({"EAP_ALLOWED_MODELS": " test:* , litellm:qwen3.8-thinking,openai:*"})
    assert policy.allows("test", "ticket")
    assert policy.allows("litellm", "qwen3.8-thinking")
    assert not policy.allows("litellm", "qwen3.8")
    assert not policy.allows("litellm", "gpt-4o")
    assert policy.allows("openai", "gpt-4")
    assert not policy.allows("anthropic", "claude")


def test_matching_is_exact_not_prefix_or_case_insensitive():
    policy = ModelPolicy.parse("litellm:qwen")
    assert not policy.allows("litellm", "qwen-large")
    assert not policy.allows("LiteLLM", "qwen")
    assert not policy.allows("litellm", "")
    assert not policy.allows("", "qwen")


def test_name_with_colon_is_matched_whole():
    policy = ModelPolicy.parse("litellm:org:model")
    assert policy.allows("litellm", "org:model")
    assert not policy.allows("litellm", "org")


def test_check_raises_naming_the_model():
    policy = ModelPolicy.parse("test:*")
    policy.check("test", "ticket")
    with pytest.raises(ModelNotAllowed) as exc:
        policy.check("litellm", "expensive-model")
    assert "litellm:expensive-model" in str(exc.value)
    assert isinstance(exc.value, ValueError)


@pytest.mark.parametrize("bad", ["openai", "openai:", ":gpt-4", "*:*", "*:gpt-4", "open*:x", "openai:gpt-*"])
def test_malformed_entries_raise(bad):
    with pytest.raises(ValueError):
        ModelPolicy.parse(bad)
