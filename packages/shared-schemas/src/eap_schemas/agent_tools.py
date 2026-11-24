"""Agents as tools (supervisor pattern): an agent spec lists other published agents of its tenant that
the model may call like tools.

The model sees one tool per ref, `<name>(input: str) -> {output, run_id}`. A call starts a CHILD run of
the target agent (a child DBOS workflow `eap.agent_run`, as a workflow agent step does), so the child has
its own events, trace, usage, approvals and recovery. The worker side lives in
`eap_worker.agent_tools`; the publish checks (agent exists, not archived, version exists, no cycle)
in `eap_run_store.agent_checks`.

Dumps leave out every default, so a spec that lists refs with defaults has the shortest JSON and a stable
`canonical_spec_hash`; a spec without refs is unchanged (the `agent_tools` key is omitted while empty).
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import Field, field_validator, model_serializer

from eap_schemas._omit import OmitNoneModel
from eap_schemas.workflow_scope import CHILD_RUN_NAMESPACE

AGENT_TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"
_NAME_RE = re.compile(AGENT_TOOL_NAME_PATTERN)
MAX_AGENT_TOOLS_PER_AGENT = 8
DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_MAX_CALLS_PER_RUN = 5
MAX_CALLS_PER_RUN_LIMIT = 20
MAX_DESCRIPTION_CHARS = 1000
LATEST = "latest"

# Stable error codes (publish refusals and call-time tool errors).
AGENT_TOOL_CYCLE = "agent_tool_cycle"
AGENT_TOOL_DEPTH_EXCEEDED = "agent_tool_depth_exceeded"
AGENT_TOOL_MAX_CALLS = "agent_tool_max_calls"
AGENT_TOOL_TIMEOUT = "agent_tool_timeout"
AGENT_TOOL_UNKNOWN_AGENT = "agent_tool_unknown_agent"
AGENT_TOOL_AGENT_ARCHIVED = "agent_tool_agent_archived"
AGENT_TOOL_NOT_PUBLISHED = "agent_tool_not_published"
AGENT_TOOL_VERSION_NOT_FOUND = "agent_tool_version_not_found"
AGENT_TOOL_NAME_CONFLICT = "agent_tool_name_conflict"
AGENT_TOOL_FAILED = "agent_tool_failed"
AGENT_TOOL_CANCELLED = "agent_tool_cancelled"


class AgentToolRef(OmitNoneModel):
    """One published agent the model may call. `name` (None: derived from the target agent's name,
    `agent_tool_default_name`), `description` (None: the target's) and the defaults of `version`,
    `timeout_seconds` and `max_calls_per_run` are left out of every dump."""

    agent_id: str = Field(description="Id (UUID) of a published, non-archived agent of the same tenant")
    name: str | None = Field(default=None, description="Tool name the model sees (default: derived from the agent's name)")
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    version: int | Literal["latest"] = Field(
        default=LATEST, description="`latest` published version (resolved once per call) or a pinned version"
    )
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=MAX_TIMEOUT_SECONDS)
    max_calls_per_run: int = Field(default=DEFAULT_MAX_CALLS_PER_RUN, ge=1, le=MAX_CALLS_PER_RUN_LIMIT)

    @field_validator("agent_id", mode="before")
    @classmethod
    def _uuid(cls, v: Any) -> str:
        if isinstance(v, uuid.UUID):
            return str(v)
        if isinstance(v, str):
            try:
                return str(uuid.UUID(v))
            except ValueError:
                pass
        raise ValueError("agent_id must be a UUID")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is not None and not _NAME_RE.fullmatch(v):
            raise ValueError(f"name must match {AGENT_TOOL_NAME_PATTERN}")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("version", mode="before")
    @classmethod
    def _version(cls, v: Any) -> Any:
        if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
            raise ValueError("version must be `latest` or a positive integer")
        if isinstance(v, int) and v < 1:
            raise ValueError("version must be `latest` or a positive integer")
        if isinstance(v, str) and v != LATEST:
            raise ValueError("version must be `latest` or a positive integer")
        return v

    @model_serializer(mode="wrap")
    def _omit_defaults(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in ("name", "description"):
                if getattr(self, name) is None:
                    data.pop(name, None)
            if self.version == LATEST:
                data.pop("version", None)
            if self.timeout_seconds == DEFAULT_TIMEOUT_SECONDS:
                data.pop("timeout_seconds", None)
            if self.max_calls_per_run == DEFAULT_MAX_CALLS_PER_RUN:
                data.pop("max_calls_per_run", None)
        return data


def agent_tool_default_name(agent_name: str | None, agent_id: str) -> str:
    """The tool name derived from the target agent's name: lower case, every run of characters outside
    `[a-z0-9]` an underscore, a leading digit prefixed with `agent_`, cut to 41 characters. A name with
    nothing usable (or an unknown agent) gives `agent_<first 8 hex of the id>`."""
    slug = re.sub(r"[^a-z0-9]+", "_", (agent_name or "").lower()).strip("_")
    if not slug:
        return f"agent_{agent_id.replace('-', '')[:8]}"
    if not slug[0].isalpha():
        slug = f"agent_{slug}"
    slug = slug[:41].rstrip("_")
    return slug if _NAME_RE.fullmatch(slug) else f"agent_{agent_id.replace('-', '')[:8]}"


def agent_tool_child_run_id(run_id: str, tool_call_id: str) -> str:
    """The child run id (and DBOS workflow id) of one agent-tool call: uuid5 of `{run_id}:agent_tool:{call}`,
    in the namespace workflow agent steps use. Stable across replays."""
    return str(uuid.uuid5(CHILD_RUN_NAMESPACE, f"{run_id}:agent_tool:{tool_call_id}"))


def check_agent_tool_refs(refs: tuple[AgentToolRef, ...]) -> None:
    """At most MAX_AGENT_TOOLS_PER_AGENT, one ref per agent, unique explicit names (AgentSpec validator)."""
    if len(refs) > MAX_AGENT_TOOLS_PER_AGENT:
        raise ValueError(f"at most {MAX_AGENT_TOOLS_PER_AGENT} agent tools per agent")
    agents: set[str] = set()
    names: set[str] = set()
    for ref in refs:
        if ref.agent_id in agents:
            raise ValueError(f"duplicate agent tool: {ref.agent_id}")
        agents.add(ref.agent_id)
        if ref.name is not None:
            if ref.name in names:
                raise ValueError(f"duplicate agent tool name: {ref.name}")
            names.add(ref.name)


__all__ = [
    "AGENT_TOOL_AGENT_ARCHIVED",
    "AGENT_TOOL_CANCELLED",
    "AGENT_TOOL_CYCLE",
    "AGENT_TOOL_DEPTH_EXCEEDED",
    "AGENT_TOOL_FAILED",
    "AGENT_TOOL_MAX_CALLS",
    "AGENT_TOOL_NAME_CONFLICT",
    "AGENT_TOOL_NAME_PATTERN",
    "AGENT_TOOL_NOT_PUBLISHED",
    "AGENT_TOOL_TIMEOUT",
    "AGENT_TOOL_UNKNOWN_AGENT",
    "AGENT_TOOL_VERSION_NOT_FOUND",
    "DEFAULT_MAX_CALLS_PER_RUN",
    "DEFAULT_TIMEOUT_SECONDS",
    "LATEST",
    "MAX_AGENT_TOOLS_PER_AGENT",
    "MAX_CALLS_PER_RUN_LIMIT",
    "MAX_TIMEOUT_SECONDS",
    "AgentToolRef",
    "agent_tool_child_run_id",
    "agent_tool_default_name",
    "check_agent_tool_refs",
]
