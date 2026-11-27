"""Agent-bound grants (docs/iam.md, "Agent and workflow grants"): an admin authorises an agent or a workflow to use an
MCP server or a named secret. Pure data and the interface a grant-keeping policy engine adds; no web, database or
plug-in imports.

A grant is the stored tuple "agent/workflow S may use resource R". The platform asks the engine one question per run:
which servers and secrets may S use (`used_by`), and stores the answer on the run as its start-time set."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eap_iam.protocols import GROUP_POLICY, PolicyEngine
from eap_iam.registry import PluginContext, installed
from eap_iam.types import IamConfigError, ResourceRef

FEATURE_AGENT_GRANTS = "agent_grants"
USE_SUBJECT_KINDS: tuple[str, ...] = ("agent", "workflow")
USE_RESOURCE_KINDS: tuple[str, ...] = ("mcp_server", "secret")
ENFORCE_ENV = "EAP_AGENT_GRANTS_ENFORCE"
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class UseGrant:
    """`subject` (an agent or a workflow) may use `resource` (an MCP server or a secret)."""

    subject: ResourceRef
    resource: ResourceRef


@runtime_checkable
class AgentGrantEngine(PolicyEngine, Protocol):
    """A policy engine that stores agent-bound grants (features contains `agent_grants`). Raises `GrantError` for a
    malformed grant and `PolicyUnavailable` when the engine cannot answer."""

    async def grant_use(self, grant: UseGrant) -> None: ...

    async def revoke_use(self, grant: UseGrant) -> None: ...

    async def used_by(self, subject: ResourceRef, kind: str) -> list[str]:
        """The ids of `kind` (`mcp_server`, `secret`) the subject may use."""
        ...

    async def grantees(self, resource: ResourceRef) -> list[ResourceRef]:
        """The agents and workflows granted `resource`."""
        ...


def supports_grants(engine: object) -> bool:
    return FEATURE_AGENT_GRANTS in getattr(engine, "features", frozenset())


def enforcement_requested(env: Mapping[str, str] | None = None) -> bool:
    """The operator's switch (`EAP_AGENT_GRANTS_ENFORCE`). Off by default; the gates also need a grant-keeping engine."""
    value = (dict(os.environ if env is None else env)).get(ENFORCE_ENV, "")
    return value.strip().lower() in _TRUE


def enforcement_active(engine: object, env: Mapping[str, str] | None = None) -> bool:
    """Whether grants are enforced: the switch is on AND the selected engine keeps grants. With `openaccess` (or any
    engine without the feature) nothing is enforced, as before."""
    return enforcement_requested(env) and supports_grants(engine)


def load_policy(engine: object = None, env: Mapping[str, str] | None = None) -> PolicyEngine:
    """Build only the selected policy engine (for a process that is not the API, such as the worker or a CLI)."""
    env = dict(os.environ if env is None else env)
    name = env.get("EAP_IAM_POLICY", "").strip() or "openaccess"
    policy = installed(GROUP_POLICY, env)
    if name not in policy:
        raise IamConfigError(f"unknown policy engine {name!r}")
    return policy[name](PluginContext(engine=engine, env=env))
