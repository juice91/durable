"""Processor phases: the four-phase catalog and the spec fields (parity round 2, Task 9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, ModelRef, canonical_spec_hash
from eap_schemas.processors import PROCESSOR_DECLARATIONS, PROCESSOR_PHASES

MODEL = {"provider": "test", "name": "m"}
PII = {"type": "pii_redact"}
MODERATION = {"type": "moderation", "config": {"model": MODEL}}


def _spec(**extra) -> dict:
    return {"name": "a", "model": MODEL, **extra}


def test_phase_order_is_input_step_stream_output() -> None:
    assert PROCESSOR_PHASES == ("input", "step", "stream", "output")


def test_catalog_declares_phases_per_type() -> None:
    d = PROCESSOR_DECLARATIONS
    assert d["moderation"].phases == ("input", "output")
    for kind in ("pii_redact", "regex_blocklist", "max_length"):
        assert d[kind].phases == ("input", "step", "stream", "output")


@pytest.mark.parametrize("field", ["step_processors", "stream_processors"])
def test_spec_rejects_moderation_in_step_and_stream(field: str) -> None:
    with pytest.raises(ValidationError, match="processor_phase_unsupported"):
        AgentSpec.model_validate(_spec(**{field: [MODERATION]}))


def test_spec_accepts_moderation_in_input_and_output() -> None:
    spec = AgentSpec.model_validate(_spec(input_processors=[MODERATION], output_processors=[MODERATION]))
    assert len(spec.input_processors) == 1


def test_spec_accepts_step_and_stream_and_limits_them() -> None:
    spec = AgentSpec.model_validate(_spec(step_processors=[PII], stream_processors=[PII]))
    assert spec.step_processors[0].type == "pii_redact" and spec.stream_processors[0].type == "pii_redact"
    with pytest.raises(ValidationError):
        AgentSpec.model_validate(_spec(stream_processors=[PII] * 9))


def test_older_specs_validate_and_hash_the_same() -> None:
    spec = AgentSpec.model_validate(_spec())
    assert spec.step_processors == () and spec.stream_processors == ()
    dumped = spec.model_dump(mode="json")
    assert "step_processors" not in dumped and "stream_processors" not in dumped
    assert canonical_spec_hash(spec) == canonical_spec_hash(AgentSpec(name="a", model=ModelRef(**MODEL)))
