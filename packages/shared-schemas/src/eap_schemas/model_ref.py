"""Reference to a model provider and name (own module so processor configs can use it)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator


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
        if len(v) > 128:
            raise ValueError("provider must be at most 128 characters")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate name is non-empty."""
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 128:
            raise ValueError("name must be at most 128 characters")
        return v


__all__ = ["ModelRef"]
