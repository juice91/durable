"""ModelSettings: the sampling, loop and retry fields (W1.1), their ranges, omission and hash stability."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, ModelSettings, canonical_spec_hash
from eap_schemas.model_settings_support import merge_model_settings, unsupported_settings

BASE = {"name": "Minimal", "model": {"provider": "test", "name": "echo"}}

# Computed before ModelSettings gained the new fields (same literals as test_spec_hash_stability).
MINIMAL_HASH = "a353a875819982f6ad02d6ae7bc416cd573402ed78c8a84e32211db1dacc3a1c"
TEMPERATURE_ONLY_JSON = {"temperature": 0.2, "max_tokens": None}


@pytest.mark.parametrize(
    "field,good,bad",
    [
        ("top_p", [0, 0.5, 1], [-0.01, 1.01]),
        ("top_k", [1, 40], [0, -1]),
        ("frequency_penalty", [-2, 0, 2], [-2.01, 2.01]),
        ("presence_penalty", [-2, 0, 2], [-2.01, 2.01]),
        ("seed", [0, -5, 2**63 - 1], [2**63, 1.5, "7", True]),
        ("max_steps", [1, 200], [0, 201]),
        ("max_retries", [0, 10], [-1, 11]),
    ],
)
def test_ranges(field, good, bad):
    for value in good:
        assert getattr(ModelSettings(**{field: value}), field) == value
    for value in bad:
        with pytest.raises(ValidationError):
            ModelSettings(**{field: value})


def test_unknown_field_still_forbidden():
    with pytest.raises(ValidationError):
        ModelSettings(top_z=1)


def test_unset_new_fields_are_omitted_but_the_old_two_stay():
    assert ModelSettings().model_dump(mode="json") == {"temperature": None, "max_tokens": None}
    assert ModelSettings(temperature=0.2).model_dump(mode="json") == TEMPERATURE_ONLY_JSON


def test_set_fields_are_dumped_and_round_trip():
    ms = ModelSettings(top_p=0.9, top_k=5, seed=7, max_steps=3, max_retries=0, frequency_penalty=0.5)
    dumped = ms.model_dump(mode="json")
    assert dumped["max_retries"] == 0 and dumped["seed"] == 7
    assert "presence_penalty" not in dumped
    assert ModelSettings.model_validate(dumped) == ms


def test_hash_of_a_spec_without_the_new_fields_is_unchanged():
    assert canonical_spec_hash(AgentSpec.model_validate(BASE)) == MINIMAL_HASH
    explicit_none = {**BASE, "model_settings": {"top_p": None, "seed": None, "max_steps": None}}
    assert canonical_spec_hash(AgentSpec.model_validate(explicit_none)) == MINIMAL_HASH


def test_a_set_field_changes_the_hash():
    spec = AgentSpec.model_validate({**BASE, "model_settings": {"top_p": 0.5}})
    assert canonical_spec_hash(spec) != MINIMAL_HASH


def test_provider_support():
    all_set = ModelSettings(temperature=0.1, top_p=0.5, top_k=3, frequency_penalty=1, presence_penalty=1, seed=1)
    assert unsupported_settings("anthropic", all_set) == [
        "temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "seed",
    ]
    assert unsupported_settings("openai", all_set) == ["top_k"]
    assert unsupported_settings("litellm", all_set) == []
    assert unsupported_settings("anthropic", ModelSettings(max_tokens=5, max_steps=2, max_retries=1)) == []


def test_merge_override_wins_only_where_set():
    merged = merge_model_settings(ModelSettings(temperature=0.2, top_p=0.9), ModelSettings(top_p=0.1, seed=4))
    assert (merged.temperature, merged.top_p, merged.seed) == (0.2, 0.1, 4)
    base = ModelSettings(temperature=0.2)
    assert merge_model_settings(base, None) is base


def test_anthropic_rejects_top_p_and_top_k():
    assert unsupported_settings("anthropic", ModelSettings(top_p=0.9, top_k=5)) == ["top_p", "top_k"]
