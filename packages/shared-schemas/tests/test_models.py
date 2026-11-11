"""Tests for eap_schemas models - written FIRST to drive implementation."""

import json
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from eap_schemas import (
    AgentSpec,
    AgentVersion,
    ModelRef,
    ModelSettings,
    RunEvent,
    RunEventType,
    canonical_spec_hash,
)


class TestModelRef:
    """Test ModelRef model."""

    def test_valid_model_ref(self):
        """Valid ModelRef should be accepted."""
        ref = ModelRef(provider="openai", name="gpt-4")
        assert ref.provider == "openai"
        assert ref.name == "gpt-4"

    def test_empty_provider_rejected(self):
        """Empty provider string should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelRef(provider="", name="gpt-4")
        assert "provider" in str(exc_info.value).lower()

    def test_empty_name_rejected(self):
        """Empty name string should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelRef(provider="openai", name="")
        assert "name" in str(exc_info.value).lower()

    def test_extra_field_rejected(self):
        """Extra fields should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelRef(provider="openai", name="gpt-4", extra="field")
        assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not allowed" in str(exc_info.value)

    def test_frozen_model(self):
        """Model should be frozen (immutable)."""
        ref = ModelRef(provider="openai", name="gpt-4")
        with pytest.raises(ValidationError):
            ref.provider = "anthropic"


class TestModelSettings:
    """Test ModelSettings model."""

    def test_valid_settings_with_values(self):
        """Valid settings with both fields set."""
        settings = ModelSettings(temperature=0.7, max_tokens=1000)
        assert settings.temperature == 0.7
        assert settings.max_tokens == 1000

    def test_valid_settings_with_none(self):
        """Valid settings with both fields as None."""
        settings = ModelSettings(temperature=None, max_tokens=None)
        assert settings.temperature is None
        assert settings.max_tokens is None

    def test_temperature_range_low(self):
        """Temperature below 0 should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelSettings(temperature=-0.1, max_tokens=None)
        assert "temperature" in str(exc_info.value).lower()

    def test_temperature_range_high(self):
        """Temperature above 2.0 should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelSettings(temperature=2.5, max_tokens=None)
        assert "temperature" in str(exc_info.value).lower()

    def test_temperature_boundary_0(self):
        """Temperature at 0.0 should be accepted."""
        settings = ModelSettings(temperature=0.0, max_tokens=None)
        assert settings.temperature == 0.0

    def test_temperature_boundary_2(self):
        """Temperature at 2.0 should be accepted."""
        settings = ModelSettings(temperature=2.0, max_tokens=None)
        assert settings.temperature == 2.0

    def test_max_tokens_positive(self):
        """max_tokens must be > 0."""
        with pytest.raises(ValidationError) as exc_info:
            ModelSettings(temperature=None, max_tokens=0)
        assert "max_tokens" in str(exc_info.value).lower()

    def test_max_tokens_negative(self):
        """Negative max_tokens should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelSettings(temperature=None, max_tokens=-1)
        assert "max_tokens" in str(exc_info.value).lower()

    def test_extra_field_rejected(self):
        """Extra fields should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ModelSettings(temperature=0.5, max_tokens=100, extra="field")
        assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not allowed" in str(exc_info.value)

    def test_frozen_model(self):
        """Model should be frozen (immutable)."""
        settings = ModelSettings(temperature=0.5, max_tokens=100)
        with pytest.raises(ValidationError):
            settings.temperature = 0.8


class TestAgentSpec:
    """Test AgentSpec model."""

    def test_valid_spec_minimum(self):
        """Valid spec with minimum required fields."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        assert spec.spec_version == 1
        assert spec.name == "test agent"
        assert spec.instructions == ""
        assert spec.tools == ()

    def test_valid_spec_full(self):
        """Valid spec with all fields."""
        spec = AgentSpec(
            spec_version=1,
            name="test agent",
            instructions="You are a helpful assistant.",
            model=ModelRef(provider="openai", name="gpt-4"),
            tools=("tool_a", "tool_b"),
            model_settings=ModelSettings(temperature=0.7, max_tokens=1000),
        )
        assert spec.spec_version == 1
        assert spec.instructions == "You are a helpful assistant."
        assert spec.tools == ("tool_a", "tool_b")

    def test_name_stripped(self):
        """Name should be stripped of whitespace."""
        spec = AgentSpec(name="  test agent  ", instructions="", model=ModelRef(provider="openai", name="gpt-4"))
        assert spec.name == "test agent"

    def test_name_too_short(self):
        """Empty name should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(name="", instructions="", model=ModelRef(provider="openai", name="gpt-4"))
        assert "name" in str(exc_info.value).lower()

    def test_name_too_long(self):
        """Name over 100 chars should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(name="a" * 101, instructions="", model=ModelRef(provider="openai", name="gpt-4"))
        assert "name" in str(exc_info.value).lower()

    def test_tool_id_format_valid(self):
        """Valid tool id format."""
        spec = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
            tools=("a", "a1", "a-b", "a_b", "a-b_c", "a" * 64),
        )
        assert len(spec.tools) == 6

    def test_tool_id_starts_with_letter(self):
        """Tool id must start with lowercase letter."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("_invalid",),
            )
        assert "tool" in str(exc_info.value).lower()

    def test_tool_id_invalid_chars(self):
        """Tool id must only contain allowed chars."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("INVALID",),
            )
        assert "tool" in str(exc_info.value).lower()

    def test_tool_id_too_long(self):
        """Tool id must be <= 64 chars."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("a" * 65,),
            )
        assert "tool" in str(exc_info.value).lower()

    def test_duplicate_tools_rejected(self):
        """Duplicate tool ids should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("tool_a", "tool_a"),
            )
        assert "duplicate" in str(exc_info.value).lower() or "tool" in str(exc_info.value).lower()

    def test_extra_field_rejected(self):
        """Extra fields should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                extra="field",
            )
        assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not allowed" in str(exc_info.value)

    def test_frozen_model(self):
        """Model should be frozen (immutable)."""
        spec = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        with pytest.raises(ValidationError):
            spec.name = "new name"


class TestCanonicalSpecHash:
    """Test canonical_spec_hash function."""

    def test_stable_hash_for_equal_specs(self):
        """Equal specs should produce equal hashes."""
        spec1 = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec2 = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        assert canonical_spec_hash(spec1) == canonical_spec_hash(spec2)

    def test_different_hash_when_instructions_change(self):
        """Hash should change when instructions change."""
        spec1 = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec2 = AgentSpec(
            name="test",
            instructions="Different instructions",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        assert canonical_spec_hash(spec1) != canonical_spec_hash(spec2)

    def test_hash_is_sha256_hex(self):
        """Hash should be a valid SHA256 hex string."""
        spec = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        hash_val = canonical_spec_hash(spec)
        assert len(hash_val) == 64
        assert re.match(r"^[0-9a-f]+$", hash_val)

    def test_json_with_sorted_keys(self):
        """Hash should be based on JSON with sorted keys."""
        spec = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        hash_val = canonical_spec_hash(spec)
        # Verify it matches manual computation with sorted keys
        expected = json.dumps(
            spec.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        import hashlib

        expected_hash = hashlib.sha256(expected.encode()).hexdigest()
        assert hash_val == expected_hash


class TestAgentVersion:
    """Test AgentVersion model."""

    def test_valid_agent_version(self):
        """Valid AgentVersion."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        version = AgentVersion(
            agent_id=uuid4(),
            tenant_id="tenant_123",
            version=1,
            spec=spec,
            spec_hash=spec_hash,
            created_at=datetime.now(timezone.utc),
        )
        assert version.agent_id
        assert version.tenant_id == "tenant_123"
        assert version.version == 1
        assert version.spec_hash == spec_hash

    def test_wrong_spec_hash_rejected(self):
        """AgentVersion with incorrect spec_hash should be rejected."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="tenant_123",
                version=1,
                spec=spec,
                spec_hash="wrong_hash",
                created_at=datetime.now(timezone.utc),
            )
        assert "spec_hash" in str(exc_info.value).lower()

    def test_empty_tenant_id_rejected(self):
        """Empty tenant_id should be rejected."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="",
                version=1,
                spec=spec,
                spec_hash=spec_hash,
                created_at=datetime.now(timezone.utc),
            )
        assert "tenant_id" in str(exc_info.value).lower()

    def test_version_must_be_positive(self):
        """version must be >= 1."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="tenant_123",
                version=0,
                spec=spec,
                spec_hash=spec_hash,
                created_at=datetime.now(timezone.utc),
            )
        assert "version" in str(exc_info.value).lower()

    def test_naive_datetime_rejected(self):
        """Naive (timezone-unaware) datetime should be rejected."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="tenant_123",
                version=1,
                spec=spec,
                spec_hash=spec_hash,
                created_at=datetime.now(),  # No timezone
            )
        assert "timezone" in str(exc_info.value).lower() or "timezone_aware" in str(exc_info.value).lower()

    def test_extra_field_rejected(self):
        """Extra fields should be rejected."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="tenant_123",
                version=1,
                spec=spec,
                spec_hash=spec_hash,
                created_at=datetime.now(timezone.utc),
                extra="field",
            )
        assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not allowed" in str(exc_info.value)


class TestRunEventType:
    """Test RunEventType enum."""

    def test_valid_event_types(self):
        """All valid event types should be accepted."""
        valid_types = [
            "run_started",
            "text_delta",
            "tool_call",
            "tool_result",
            "run_completed",
            "run_failed",
        ]
        for event_type in valid_types:
            assert event_type in RunEventType.__args__


class TestRunEvent:
    """Test RunEvent model."""

    def test_valid_run_event_minimum(self):
        """Valid RunEvent with minimum fields."""
        event = RunEvent(
            run_id=uuid4(),
            seq=0,
            type="run_started",
            ts=datetime.now(timezone.utc),
        )
        assert event.run_id
        assert event.seq == 0
        assert event.type == "run_started"
        assert event.data == {}

    def test_valid_run_event_full(self):
        """Valid RunEvent with all fields."""
        event = RunEvent(
            run_id=uuid4(),
            seq=5,
            type="tool_result",
            data={"result": "success"},
            ts=datetime.now(timezone.utc),
        )
        assert event.seq == 5
        assert event.data == {"result": "success"}

    def test_negative_seq_rejected(self):
        """Negative seq should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RunEvent(
                run_id=uuid4(),
                seq=-1,
                type="run_started",
                ts=datetime.now(timezone.utc),
            )
        assert "seq" in str(exc_info.value).lower()

    def test_extra_field_rejected(self):
        """Extra fields should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RunEvent(
                run_id=uuid4(),
                seq=0,
                type="run_started",
                ts=datetime.now(timezone.utc),
                extra="field",
            )
        assert "extra_forbidden" in str(exc_info.value) or "Extra inputs are not allowed" in str(exc_info.value)

    def test_frozen_model(self):
        """Model should be frozen (immutable)."""
        event = RunEvent(
            run_id=uuid4(),
            seq=0,
            type="run_started",
            ts=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationError):
            event.seq = 1


class TestJsonSerialization:
    """Test JSON serialization/deserialization round-trips."""

    def test_spec_round_trip_model_dump_json(self):
        """AgentSpec should round-trip through model_dump_json/model_validate_json."""
        spec = AgentSpec(
            name="test agent",
            instructions="You are a helpful assistant.",
            model=ModelRef(provider="openai", name="gpt-4"),
            tools=("tool_a", "tool_b"),
            model_settings=ModelSettings(temperature=0.7, max_tokens=1000),
        )
        json_str = spec.model_dump_json()
        restored = AgentSpec.model_validate_json(json_str)
        assert restored == spec

    def test_model_ref_round_trip(self):
        """ModelRef should round-trip through JSON."""
        ref = ModelRef(provider="openai", name="gpt-4")
        json_str = ref.model_dump_json()
        restored = ModelRef.model_validate_json(json_str)
        assert restored == ref

    def test_model_settings_round_trip(self):
        """ModelSettings should round-trip through JSON."""
        settings = ModelSettings(temperature=0.7, max_tokens=1000)
        json_str = settings.model_dump_json()
        restored = ModelSettings.model_validate_json(json_str)
        assert restored == settings


class TestDatetimeType:
    """Test datetime type validation (defect #1 regression)."""

    def test_created_at_integer_rejected(self):
        """created_at=5 should be rejected (not a datetime)."""
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        with pytest.raises(ValidationError) as exc_info:
            AgentVersion(
                agent_id=uuid4(),
                tenant_id="tenant_123",
                version=1,
                spec=spec,
                spec_hash=spec_hash,
                created_at=5,
            )
        assert "datetime" in str(exc_info.value).lower()

    def test_ts_string_rejected(self):
        """ts='not a date' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            RunEvent(
                run_id=uuid4(),
                seq=0,
                type="run_started",
                ts="not a date",
            )
        assert "isoformat" in str(exc_info.value).lower() or "datetime" in str(exc_info.value).lower()

    def test_ts_omitted_rejected(self):
        """ts omitted should be rejected (required field)."""
        with pytest.raises(ValidationError) as exc_info:
            RunEvent(
                run_id=uuid4(),
                seq=0,
                type="run_started",
            )
        assert "ts" in str(exc_info.value).lower() or "required" in str(exc_info.value).lower()


class TestUUIDType:
    """Test UUID type validation (defect #2 regression)."""

    def test_uuid_v1_agent_id_accepted(self):
        """UUIDv1 agent_id should be accepted."""
        import uuid
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        v1_uuid = uuid.uuid1()
        version = AgentVersion(
            agent_id=v1_uuid,
            tenant_id="tenant_123",
            version=1,
            spec=spec,
            spec_hash=spec_hash,
            created_at=datetime.now(timezone.utc),
        )
        assert version.agent_id == v1_uuid

    def test_uuid_v7_agent_id_accepted(self):
        """UUIDv7 agent_id should be accepted (as string since uuid7 not in stdlib)."""
        import uuid
        spec = AgentSpec(
            name="test agent",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
        )
        spec_hash = canonical_spec_hash(spec)
        # uuid7 not in stdlib, use a v7-like UUID string
        v7_uuid_str = "018f9c3a-7c97-7000-9d93-5a45a9a9a9a9"
        version = AgentVersion(
            agent_id=v7_uuid_str,
            tenant_id="tenant_123",
            version=1,
            spec=spec,
            spec_hash=spec_hash,
            created_at=datetime.now(timezone.utc),
        )
        assert str(version.agent_id) == v7_uuid_str


class TestToolIdRegex:
    """Test tool ID regex validation (defect #3 regression)."""

    def test_non_ascii_tool_id_rejected(self):
        """Tool ID with non-ASCII chars like 'tööl' should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("tööl",),
            )
        assert "tool" in str(exc_info.value).lower()

    def test_tool_id_exactly_64_chars_accepted(self):
        """Tool ID with exactly 64 chars should be accepted."""
        spec = AgentSpec(
            name="test",
            instructions="",
            model=ModelRef(provider="openai", name="gpt-4"),
            tools=("a" * 64,),
        )
        assert spec.tools == ("a" * 64,)

    def test_tool_id_65_chars_rejected(self):
        """Tool ID with 65 chars should be rejected."""
        with pytest.raises(ValidationError) as exc_info:
            AgentSpec(
                name="test",
                instructions="",
                model=ModelRef(provider="openai", name="gpt-4"),
                tools=("a" * 65,),
            )
        assert "tool" in str(exc_info.value).lower()


class TestImportsCleanup:
    """Test that unused imports are removed (defect #4 regression)."""

    def test_no_main_module(self):
        """__main__.py should not exist."""
        import os
        main_path = os.path.join(os.path.dirname(__file__), "..", "src", "eap_schemas", "__main__.py")
        assert not os.path.exists(main_path)
