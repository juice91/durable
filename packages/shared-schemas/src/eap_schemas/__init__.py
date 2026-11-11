"""Shared Pydantic schemas for the Enterprise Agent Platform."""

from datetime import datetime, timezone
from typing import Any, Literal, Tuple
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from pydantic.functional_validators import model_validator

__all__ = (
    "AgentSpec",
    "AgentVersion",
    "ModelRef",
    "ModelSettings",
    "RunEvent",
    "RunEventType",
    "canonical_spec_hash",
)


class ModelRef(BaseModel):
    """Reference to a model provider and name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    name: str

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v):
        """Validate provider is non-empty."""
        if not v:
            raise ValueError("provider cannot be empty")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate name is non-empty."""
        if not v:
            raise ValueError("name cannot be empty")
        return v


class ModelSettings(BaseModel):
    """Settings for model inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentSpec(BaseModel):
    """Specification for an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: Literal[1] = 1
    name: str
    instructions: str = ""
    model: ModelRef
    tools: Tuple[str, ...] = ()
    model_settings: ModelSettings = Field(default_factory=ModelSettings)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        """Strip whitespace from name."""
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate name after stripping."""
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 100:
            raise ValueError("name must be at most 100 characters")
        return v

    @field_validator("tools", mode="before")
    @classmethod
    def validate_tools(cls, v):
        """Validate tool IDs and check for duplicates using exact regex."""
        import re

        if not v:
            return ()

        tool_ids = list(v)
        seen = set()
        pattern = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

        for tool_id in tool_ids:
            if not tool_id:
                raise ValueError("tool id cannot be empty")
            if not pattern.fullmatch(tool_id):
                raise ValueError(f"tool id must match ^[a-z][a-z0-9_.-]{{0,63}}$ (ASCII only, max 64 chars): {tool_id}")
            if tool_id in seen:
                raise ValueError(f"duplicate tool id: {tool_id}")
            seen.add(tool_id)

        return tuple(tool_ids)


def canonical_spec_hash(spec: AgentSpec) -> str:
    """Compute SHA256 hash of AgentSpec JSON with sorted keys.

    Equal specs give equal hashes; any field change changes the hash.
    """
    import hashlib
    import json

    json_str = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(json_str.encode()).hexdigest()


class AgentVersion(BaseModel):
    """Versioned agent specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    tenant_id: str
    version: int = Field(ge=1)
    spec: AgentSpec
    spec_hash: str
    created_at: AwareDatetime

    @field_validator("agent_id", mode="before")
    @classmethod
    def validate_agent_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("tenant_id", mode="before")
    @classmethod
    def validate_tenant_id(cls, v):
        """Validate tenant_id is non-empty string."""
        if isinstance(v, str) and not v:
            raise ValueError("tenant_id cannot be empty")
        return v

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at_datetime(cls, v):
        """Ensure created_at is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("created_at must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("created_at must be a datetime object")
        return v

    @model_validator(mode="after")
    def verify_spec_hash(self):
        """Verify spec_hash matches canonical hash of spec."""
        expected_hash = canonical_spec_hash(self.spec)
        if self.spec_hash != expected_hash:
            raise ValueError(
                f"spec_hash mismatch: expected {expected_hash}, got {self.spec_hash}"
            )
        return self


RunEventType = Literal[
    "run_started",
    "text_delta",  # transient only: sent over NOTIFY, never stored
    "model_message",
    "tool_call",
    "tool_result",
    "run_completed",
    "run_failed",
]


class RunEvent(BaseModel):
    """Event in a run execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    seq: int = Field(ge=0)
    type: RunEventType
    data: dict[str, Any] = Field(default_factory=dict)
    ts: AwareDatetime

    @field_validator("run_id", mode="before")
    @classmethod
    def validate_run_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("ts", mode="before")
    @classmethod
    def validate_ts_datetime(cls, v):
        """Ensure ts is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("ts must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("ts must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("ts must be a datetime object")
        return v


# Import after definitions to avoid circular issues
from datetime import datetime, timezone
