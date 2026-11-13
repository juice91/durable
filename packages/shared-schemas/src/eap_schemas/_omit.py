"""Base model whose dumps leave out every field that is None (hash stability for nested configs)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_serializer


class OmitNoneModel(BaseModel):
    """Frozen, extra-forbidding model that drops None-valued fields from every dump.

    Used for AgentSpec sub-configs added after specs were first stored: an optional field that is
    None never reaches the JSON or `canonical_spec_hash`, so adding a new optional field later keeps
    every stored hash, and `{}` hashes the same as explicit `None`s.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_serializer(mode="wrap")
    def _omit_none_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in type(self).model_fields:
                if getattr(self, name) is None:
                    data.pop(name, None)
        return data
