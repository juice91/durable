"""Structured output contract (agent capabilities milestone, AC.1).

`AgentSpec.output: OutputSpec | None`. A spec without `output` answers in text, exactly as before.
With `output`, every answer is a JSON object validated against `json_schema` (the "standard" subset
of eap_schemas.json_schema_subset). The worker builds Pydantic AI's `StructuredDict(json_schema,
name, description)` wrapped in `ToolOutput(..., name=OUTPUT_TOOL_NAME)`, `NativeOutput` or
`PromptedOutput` per `mode`, and passes `retries={"output": max_retries}`; the JSON Schema itself is
enforced by the worker's `after_output_validate` hook.

Every optional field is dropped from dumps while None, so `{"json_schema": ...}` hashes the same as
the same spec with explicit Nones. No I/O.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from eap_schemas._omit import OmitNoneModel
from eap_schemas.json_schema_subset import SchemaSubsetInvalid, validate_output_schema
from eap_schemas.tools import OUTPUT_TOOL_NAME  # the fixed output tool name; in RESERVED_TOOL_NAMES

OutputMode = Literal["tool", "native", "prompted"]
DEFAULT_OUTPUT_MODE: OutputMode = "tool"
DEFAULT_OUTPUT_NAME = "output"
DEFAULT_OUTPUT_MAX_RETRIES = 2
MAX_OUTPUT_RETRIES = 5
MAX_OUTPUT_DESCRIPTION_CHARS = 1000
# Model feedback on a rejected candidate: at most this many errors, joined at most this many bytes.
MAX_OUTPUT_ERRORS = 10
MAX_OUTPUT_ERROR_BYTES = 2 * 1024


class OutputSpec(OmitNoneModel):
    """The JSON object every answer of the agent must be."""

    json_schema: dict[str, Any] = Field(description="JSON Schema subset; the root is an object")
    mode: OutputMode | None = Field(default=None, description="tool (default), native or prompted")
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    description: str | None = Field(default=None, max_length=MAX_OUTPUT_DESCRIPTION_CHARS)
    max_retries: int | None = Field(default=None, ge=0, le=MAX_OUTPUT_RETRIES)

    @field_validator("json_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_output_schema(v)
        except SchemaSubsetInvalid as exc:
            raise ValueError(str(exc)) from None

    def effective_mode(self) -> OutputMode:
        return self.mode if self.mode is not None else DEFAULT_OUTPUT_MODE

    def effective_name(self) -> str:
        return self.name if self.name is not None else DEFAULT_OUTPUT_NAME

    def effective_max_retries(self) -> int:
        return self.max_retries if self.max_retries is not None else DEFAULT_OUTPUT_MAX_RETRIES


class OutputInvalid(RuntimeError):
    """The model's output still broke the output schema after the retry budget (`output_invalid`).

    Pickle-safe (raised inside DBOS steps). `errors` holds JSON paths only, never values.
    """

    code = "output_invalid"

    def __init__(self, max_retries: int, errors: tuple[str, ...] = ()) -> None:
        self.max_retries = max_retries
        self.errors = tuple(errors)
        super().__init__(max_retries, self.errors)

    def __str__(self) -> str:
        return f"the agent's output did not match its output schema after {self.max_retries} retries"


__all__ = [
    "DEFAULT_OUTPUT_MAX_RETRIES",
    "DEFAULT_OUTPUT_MODE",
    "DEFAULT_OUTPUT_NAME",
    "MAX_OUTPUT_DESCRIPTION_CHARS",
    "MAX_OUTPUT_ERROR_BYTES",
    "MAX_OUTPUT_ERRORS",
    "MAX_OUTPUT_RETRIES",
    "OUTPUT_TOOL_NAME",
    "OutputInvalid",
    "OutputMode",
    "OutputSpec",
]
